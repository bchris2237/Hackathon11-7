"""B6: the deployment artifacts (infra/template.yaml, scripts/deploy.ps1, scripts/smoke.py, infra/requirements-lambda.txt).

NOTHING HERE TOUCHES AWS. These tests check that the files are internally consistent and safe, that the template's environment
actually loads through the backend's own configuration code, and that scripts/smoke.py works end to end against a LOCAL server.
That local server uses a stub Bedrock client and tests/backend/reference_optimizer.py (SIMULATED), so a passing run proves the
SCRIPT and the wiring, not that a deployment works. Whether a stack deploys, Bedrock answers, CORS holds in a browser and the
hosted frontend works is verified only by a human running scripts/deploy.ps1 and scripts/smoke.py against real AWS.
"""

from __future__ import annotations

import hashlib
import http.server
import json
import re
import shutil
import subprocess
import threading
from collections import deque
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
import yaml

from backend.app import Adapters, App, lambda_handler
from backend.bedrock import BedrockClient, BedrockSettings, load_bedrock_settings
from backend.config import load_settings
from backend.confirmation import confirm_scenario
from backend.explanation import Explainer
from backend.extraction import BedrockExtractor
from backend.local import make_handler
from backend.optimization import make_optimize_adapter
from backend.simulation import Simulator
from tests.backend.helpers import ROOT
from tests.backend.reference_optimizer import reference_optimize
from tests.backend.test_extraction import canonical_facts
from tests.backend.test_optimization import GOOD_TEXT

TEMPLATE_PATH = ROOT / "infra" / "template.yaml"
DEPLOY_PS1 = ROOT / "scripts" / "deploy.ps1"
SMOKE_PY = ROOT / "scripts" / "smoke.py"
REQ_LAMBDA = ROOT / "infra" / "requirements-lambda.txt"
REQ_LOCK = ROOT / "requirements.lock"
DEPLOY_DOC = ROOT / "docs" / "deployment.md"


# ------------------------------------------------------------------ CloudFormation YAML (short-form tags)
class CfnLoader(yaml.SafeLoader):
    pass


def _tag(loader: CfnLoader, suffix: str, node: yaml.Node) -> Any:
    if isinstance(node, yaml.ScalarNode):
        value: Any = loader.construct_scalar(node)
    elif isinstance(node, yaml.SequenceNode):
        value = loader.construct_sequence(node, deep=True)
    else:
        value = loader.construct_mapping(node, deep=True)
    return {"Ref": value} if suffix == "Ref" else {f"Fn::{suffix}": value}


CfnLoader.add_multi_constructor("!", _tag)


@pytest.fixture(scope="module")
def template() -> dict[str, Any]:
    return yaml.load(TEMPLATE_PATH.read_text(encoding="utf-8"), Loader=CfnLoader)


def resources(template: dict[str, Any], kind: str) -> dict[str, Any]:
    return {name: r for name, r in template["Resources"].items() if r["Type"] == kind}


def walk(node: Any):
    yield node
    if isinstance(node, dict):
        for value in node.values():
            yield from walk(value)
    elif isinstance(node, list):
        for item in node:
            yield from walk(item)


SAMPLE = {
    "AllowedOrigin": "https://main.d1234567890abc.amplifyapp.com",
    "BedrockModelId": "us.example.model-v1:0",
    "BedrockResourceArns": "arn:aws:bedrock:us-west-2:111122223333:inference-profile/us.example.model-v1:0,arn:aws:bedrock:us-west-2::foundation-model/example.model-v1:0",
    "CommitSha": "abc123def456",
}


def resolve(value: Any, template: dict[str, Any]) -> Any:
    if isinstance(value, dict) and set(value) == {"Ref"}:
        name = value["Ref"]
        if name in SAMPLE:
            return SAMPLE[name]
        param = template["Parameters"][name]
        return str(param["Default"])
    return value


NO_VALUE = object()


def environment_for(template: dict[str, Any]) -> dict[str, str]:
    """The function's environment as CloudFormation would produce it with the parameter defaults (profiles off => key absent)."""
    fn = next(iter(resources(template, "AWS::Serverless::Function").values()))["Properties"]
    env: dict[str, str] = {}
    for key, value in fn["Environment"]["Variables"].items():
        if isinstance(value, dict) and set(value) == {"Fn::If"}:
            condition, when_true, when_false = value["Fn::If"]
            assert condition == "ProfilesEnabled", "only the optional profile settings may be conditional"
            enabled = str(template["Parameters"]["EnableProfiles"]["Default"]) == "true"
            chosen = when_true if enabled else when_false
            if chosen == {"Ref": "AWS::NoValue"}:
                continue
            value = chosen
        env[key] = str(resolve(value, template))
    return env


# ================================================================== template: structure and safety
def test_template_is_a_sam_template_for_python_312_on_x86(template):
    assert template["Transform"] == "AWS::Serverless-2016-10-31"
    assert template["Globals"]["Function"]["Runtime"] == "python3.12"
    assert template["Globals"]["Function"]["Architectures"] == ["x86_64"], "requirements-lambda.txt is resolved for x86_64 Linux"
    fn = next(iter(resources(template, "AWS::Serverless::Function").values()))
    assert fn["Properties"]["Handler"] == "backend.app.lambda_handler"
    assert callable(lambda_handler)


def test_handler_timeouts_fit_inside_the_api_gateway_limit(template):
    fn = next(iter(resources(template, "AWS::Serverless::Function").values()))["Properties"]
    adapter_budget = float(fn["Environment"]["Variables"]["ADAPTER_TIMEOUT_SECONDS"])
    assert adapter_budget < fn["Timeout"] < 30, "Lambda timeout must be above the adapter budget and below API Gateway's 30 s cutoff"
    for event in fn["Events"].values():
        assert event["Properties"]["TimeoutInMillis"] <= 29000
        assert event["Properties"]["PayloadFormatVersion"] == "2.0", "backend/app.py handles API Gateway payload format 2.0"
    assert 0.1 <= adapter_budget <= 28.0, "inside backend/config.py's allowed range"


def test_cors_is_decided_only_by_the_lambda_the_http_api_has_no_cors_configuration(template):
    api = next(iter(resources(template, "AWS::Serverless::HttpApi").values()))["Properties"]
    assert "CorsConfiguration" not in api, "two CORS layers could disagree; backend/app.py owns CORS"
    fn = next(iter(resources(template, "AWS::Serverless::Function").values()))["Properties"]
    methods = {event["Properties"]["Method"] for event in fn["Events"].values()}
    assert methods == {"ANY"}, "OPTIONS preflights must reach the Lambda, so every method is routed"
    paths = {event["Properties"]["Path"] for event in fn["Events"].values()}
    assert paths == {"/{proxy+}", "/"}


def test_origin_parameter_has_no_default_and_refuses_wildcards_http_and_paths(template):
    param = template["Parameters"]["AllowedOrigin"]
    assert "Default" not in param, "the operator must choose the origin"
    pattern = re.compile(param["AllowedPattern"])
    for good in ("https://main.d1234567890abc.amplifyapp.com", "https://a.example,https://b.example:8443"):
        assert pattern.fullmatch(good), good
    for bad in ("*", "https://*", "http://main.example", "https://main.example/", "https://main.example/path", "https://a.example, https://b.example", "", "https://", "main.example", "null"):
        assert not pattern.fullmatch(bad), bad


def test_bedrock_resource_parameter_has_no_default_and_refuses_wildcards(template):
    param = template["Parameters"]["BedrockResourceArns"]
    assert "Default" not in param
    pattern = re.compile(param["AllowedPattern"])
    profile = "arn:aws:bedrock:us-west-2:111122223333:inference-profile/us.anthropic.claude-3-haiku-20240307-v1:0"
    model = "arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-3-haiku-20240307-v1:0"
    for good in (model, profile, f"{profile},{model}"):
        assert pattern.fullmatch(good), good
    for bad in (
        "*",
        "arn:aws:bedrock:*::foundation-model/*",
        "arn:aws:bedrock:us-west-2::foundation-model/*",
        "arn:aws:bedrock:*:*:inference-profile/*",
        "arn:aws:s3:::my-bucket",
        "arn:aws:bedrock:us-west-2::model/x",
        f"{profile}, {model}",
        f"{profile},*",
        "",
    ):
        assert not pattern.fullmatch(bad), bad


def test_model_id_parameter_has_no_default_so_nothing_is_guessed(template):
    assert "Default" not in template["Parameters"]["BedrockModelId"]
    pattern = re.compile(template["Parameters"]["BedrockModelId"]["AllowedPattern"])
    assert pattern.fullmatch("us.anthropic.claude-3-haiku-20240307-v1:0") and not pattern.fullmatch("has space") and not pattern.fullmatch("")


def test_reserved_concurrency_is_part_of_the_core_stack_with_a_documented_opt_out(template):
    fn = next(iter(resources(template, "AWS::Serverless::Function").values()))["Properties"]
    assert "ReservedConcurrentExecutions" in fn
    assert template["Parameters"]["ReservedConcurrency"]["Default"] == "5", "the default keeps a ceiling"
    pattern = re.compile(template["Parameters"]["ReservedConcurrency"]["AllowedPattern"])
    for good in ("1", "5", "100", "-1"):
        assert pattern.fullmatch(good), good
    for bad in ("0", "101", "-2", "abc", "", "5.5", "05"):
        assert not pattern.fullmatch(bad), f"{bad!r} must not be accepted (0 would throttle everything)"
    assert "HasReservedConcurrency" in template["Conditions"]
    assert "-1" in json.dumps(template["Conditions"]["HasReservedConcurrency"])


def test_log_group_has_retention_and_the_function_uses_it(template):
    group_name, group = next(iter(resources(template, "AWS::Logs::LogGroup").items()))
    assert group["Properties"]["RetentionInDays"] == {"Ref": "LogRetentionDays"}
    assert 1 <= template["Parameters"]["LogRetentionDays"]["Default"] <= 90
    fn = next(iter(resources(template, "AWS::Serverless::Function").values()))["Properties"]
    assert fn["LoggingConfig"]["LogGroup"] == {"Ref": group_name}


def test_http_api_has_request_throttles(template):
    api = next(iter(resources(template, "AWS::Serverless::HttpApi").values()))["Properties"]
    settings = api["DefaultRouteSettings"]
    assert settings["ThrottlingRateLimit"] == {"Ref": "ThrottleRatePerSecond"} and settings["ThrottlingBurstLimit"] == {"Ref": "ThrottleBurst"}
    assert template["Parameters"]["ThrottleRatePerSecond"]["Default"] <= template["Parameters"]["ThrottleBurst"]["Default"]


def test_execution_role_is_least_privilege(template):
    role = next(iter(resources(template, "AWS::IAM::Role").values()))["Properties"]
    trust = role["AssumeRolePolicyDocument"]["Statement"]
    assert trust == [{"Effect": "Allow", "Principal": {"Service": "lambda.amazonaws.com"}, "Action": "sts:AssumeRole"}]
    assert "ManagedPolicyArns" not in role, "no broad managed policy (AdministratorAccess, AmazonBedrockFullAccess ...)"
    statements = [s for policy in role["Policies"] for s in policy["PolicyDocument"]["Statement"]]
    assert len(statements) == 2 and all(s["Effect"] == "Allow" for s in statements)
    actions = sorted(a for s in statements for a in (s["Action"] if isinstance(s["Action"], list) else [s["Action"]]))
    assert actions == ["bedrock:InvokeModel", "logs:CreateLogStream", "logs:PutLogEvents"]
    bedrock = next(s for s in statements if "bedrock:InvokeModel" in s["Action"])
    assert bedrock["Action"] == ["bedrock:InvokeModel"], "Converse needs only InvokeModel (AWS Converse reference)"
    assert bedrock["Resource"] == {"Fn::Split": [",", {"Ref": "BedrockResourceArns"}]}
    logs = next(s for s in statements if "logs:PutLogEvents" in s["Action"])
    assert "FunctionLogGroup.Arn" in json.dumps(logs["Resource"]), "logging is scoped to the function's own log group"


def test_no_wildcard_resources_and_no_secrets_anywhere_in_the_template(template):
    raw = TEMPLATE_PATH.read_text(encoding="utf-8")
    assert not re.search(r"Resource:\s*['\"]?\*", raw), "no Resource: '*'"
    for node in walk(template["Resources"]):
        if isinstance(node, dict) and "Resource" in node:
            value = node["Resource"]
            assert value != "*" and value != ["*"]
    for pattern in (r"AKIA[0-9A-Z]{16}", r"ASIA[0-9A-Z]{16}", r"\b\d{12}\b", r"aws_secret", r"BEGIN (RSA )?PRIVATE KEY"):
        assert not re.search(pattern, raw, re.IGNORECASE), f"template must not contain {pattern}"


def test_the_stack_is_live_mode_only_and_never_sets_reserved_or_credential_names(template):
    env = next(iter(resources(template, "AWS::Serverless::Function").values()))["Properties"]["Environment"]["Variables"]
    assert env["APP_MODE"] == "live", "a deployed stack must never run in fixture mode"
    forbidden = {"AWS_REGION", "AWS_DEFAULT_REGION", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_PROFILE"}
    assert not forbidden & set(env), "AWS_REGION is reserved in Lambda; credentials come from the execution role"
    assert set(env) == {
        "APP_MODE", "ALLOWED_ORIGIN", "BEDROCK_MODEL_ID", "COMMIT_SHA", "MAX_IN_FLIGHT",
        "ADAPTER_TIMEOUT_SECONDS", "BEDROCK_MAX_OUTPUT_TOKENS", "BEDROCK_READ_TIMEOUT_SECONDS", "BEDROCK_STRUCTURED_OUTPUT",
        "PROFILE_TABLE", "PROFILE_TTL_SECONDS", "CLIENT_RATE_PER_MINUTE", "CLIENT_BURST",
    }
    assert "PROFILE_TABLE" not in environment_for(template), "profile storage is OFF by default: no table name reaches the function"


def test_outputs_give_the_url_health_and_region_and_nothing_sensitive(template):
    outputs = template["Outputs"]
    assert {"ApiUrl", "HealthUrl", "FunctionName", "LogGroupName", "BedrockRegion"} <= set(outputs)
    assert "/health" in json.dumps(outputs["HealthUrl"]) and outputs["BedrockRegion"]["Value"] == {"Ref": "AWS::Region"}
    assert "Role" not in "".join(outputs), "no role ARN is exported"


# ================================================================== the template's environment really loads
def test_template_environment_loads_through_the_backends_own_configuration_code(template):
    fn = next(iter(resources(template, "AWS::Serverless::Function").values()))["Properties"]
    env = environment_for(template)
    env["AWS_REGION"] = "us-west-2"  # what Lambda provides
    settings = load_settings(env)
    assert settings.mode == "live" and settings.commit == "abc123def456"
    assert settings.allowed_origins == ("https://main.d1234567890abc.amplifyapp.com",)
    assert settings.adapter_timeout_seconds == 25.0 and settings.max_in_flight == 4
    bedrock = load_bedrock_settings(env)
    assert bedrock is not None and bedrock.model_id == "us.example.model-v1:0" and bedrock.region == "us-west-2"
    assert bedrock.structured_output is False and bedrock.max_output_tokens == 1500 and bedrock.read_timeout_seconds == 10.0
    assert bedrock.worst_case_call_seconds <= fn["Timeout"], "one Converse call, retries included, must fit inside the Lambda timeout"


def test_every_environment_variable_in_the_template_is_actually_read_by_the_backend(template):
    fn = next(iter(resources(template, "AWS::Serverless::Function").values()))["Properties"]
    source = "".join((ROOT / "backend" / name).read_text(encoding="utf-8") for name in ("config.py", "bedrock.py"))
    for name in fn["Environment"]["Variables"]:
        assert f'"{name}"' in source, f"{name} is set by the template but never read by backend/config.py or backend/bedrock.py"


def test_the_built_function_handles_a_health_event_with_the_templates_environment(template, monkeypatch):
    import backend.app as module

    fn = next(iter(resources(template, "AWS::Serverless::Function").values()))["Properties"]
    for key, value in environment_for(template).items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("PROFILE_TABLE", raising=False)
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    monkeypatch.setattr(module, "_APP", None)
    monkeypatch.setattr(module, "load_optimizer_function", lambda: None)
    event = {"version": "2.0", "rawPath": "/health", "headers": {}, "requestContext": {"http": {"method": "GET", "path": "/health"}, "requestId": "r", "stage": "$default"}}
    result = lambda_handler(event, None)
    body = json.loads(result["body"])
    assert result["statusCode"] == 200 and body["mode"] == "live" and body["commit"] == "abc123def456"
    monkeypatch.setattr(module, "_APP", None)


# ================================================================== packaging inputs
def parse_requirements(path: Path) -> dict[str, str]:
    return {m.group(1).lower(): m.group(2) for m in re.finditer(r"^([A-Za-z0-9_.\-]+)==([^\s\\;]+)", path.read_text(encoding="utf-8"), re.MULTILINE)}


def test_lambda_requirements_are_pinned_hashed_runtime_only_and_match_the_lock():
    lambda_pins, lock_pins = parse_requirements(REQ_LAMBDA), parse_requirements(REQ_LOCK)
    text = REQ_LAMBDA.read_text(encoding="utf-8")
    assert {"pydantic", "pydantic-core", "boto3", "botocore"} <= set(lambda_pins)
    for name, version in lambda_pins.items():
        assert lock_pins.get(name) == version, f"{name}: Lambda pins {version}, requirements.lock pins {lock_pins.get(name)}"
    for dev_only in ("pytest", "hypothesis", "jsonschema", "pyyaml", "datamodel-code-generator", "black", "isort"):
        assert dev_only not in lambda_pins
    assert text.count("--hash=sha256:") >= len(lambda_pins), "every package is hash-pinned"


def test_staging_copies_only_python_source_and_the_three_packages():
    script = DEPLOY_PS1.read_text(encoding="utf-8")
    assert 'foreach ($pkg in @("backend", "contracts", "optimizer"))' in script
    assert "-Include *.py" in script and "__pycache__" in script
    assert "requirements-lambda.txt" in script
    packages = re.search(r'foreach \(\$pkg in @\(([^)]*)\)\)', script)
    assert packages and re.findall(r'"([^"]+)"', packages.group(1)) == ["backend", "contracts", "optimizer"], "nothing else is staged"


def test_staged_modules_are_self_sufficient(tmp_path):
    """Copy exactly what deploy.ps1 stages and import the handler with ONLY that tree on the path."""
    for pkg in ("backend", "contracts", "optimizer"):
        for source in (ROOT / pkg).rglob("*.py"):
            if "__pycache__" in source.parts:
                continue
            target = tmp_path / source.relative_to(ROOT)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(source, target)
    code = (
        "import sys, importlib; sys.path.insert(0, %r); "
        "m = importlib.import_module('backend.app'); "
        "assert m.__file__.startswith(%r), m.__file__; "
        "assert callable(m.lambda_handler); print('ok')"
    ) % (str(tmp_path), str(tmp_path))
    import sys

    result = subprocess.run([sys.executable, "-c", code], cwd=tmp_path.parent, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0 and "ok" in result.stdout, result.stderr[-800:]


# ================================================================== deploy.ps1 (static: it is never run against AWS here)
def test_deploy_script_cannot_deploy_by_accident():
    script = DEPLOY_PS1.read_text(encoding="utf-8")
    assert "[switch]$Deploy" in script
    stop = script.index("if (-not $Deploy -and -not $Teardown)")
    assert stop < script.index("get-caller-identity") < script.index('"deploy"'), "default path exits before any AWS call"
    assert script.index("exit 0", stop) < script.index("get-caller-identity")
    assert "Read-Host" in script and "$typed.Trim() -ne $account" in script, "the account id must be typed to continue"
    assert "--confirm-changeset" in script, "the change set is shown before it is executed"
    assert "--guided" not in script and "--no-confirm-changeset" not in script
    assert "-Region is required" in script and "is never guessed" in script
    assert 'if ($BedrockResourceArns -match "\\*")' in script and "notmatch \"^https://\"" in script


def test_deploy_script_never_touches_credentials():
    script = DEPLOY_PS1.read_text(encoding="utf-8")
    for token in ("AWS_SECRET_ACCESS_KEY", "AWS_ACCESS_KEY_ID", "AWS_SESSION_TOKEN", "aws configure set", "SecretAccessKey", "--query \"Credentials"):
        assert token not in script
    assert "Write-Host $env:" not in script
    assert "SAM_CLI_TELEMETRY" in script


def test_deploy_script_requires_the_optimizer_unless_told_otherwise():
    script = DEPLOY_PS1.read_text(encoding="utf-8")
    assert 'Join-Path $Root "optimizer\\api.py"' in script and "AllowMissingOptimizer" in script and "WARNING: optimizer/api.py is missing" in script


@pytest.mark.skipif(shutil.which("powershell") is None and shutil.which("pwsh") is None, reason="NOT RUN: no PowerShell on this machine to parse deploy.ps1")
def test_deploy_script_parses_as_valid_powershell():
    shell = shutil.which("powershell") or shutil.which("pwsh")
    command = (
        "$e = $null; [void][System.Management.Automation.Language.Parser]::ParseFile('%s', [ref]$null, [ref]$e); "
        "if ($e.Count) { $e | ForEach-Object { $_.Message }; exit 1 }"
    ) % str(DEPLOY_PS1)
    result = subprocess.run([shell, "-NoProfile", "-Command", command], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr


# ================================================================== smoke.py against a LOCAL server (SIMULATED backends)
class StubSdk:
    """One stub for every Bedrock call: picks a reply from the system prompt. TEST DOUBLE."""

    def __init__(self) -> None:
        self.calls: deque[str] = deque()

    def converse(self, **kwargs: Any) -> dict[str, Any]:
        system = kwargs["system"][0]["text"]
        if system.startswith("You extract facts"):
            self.calls.append("extract")
            text = json.dumps(canonical_facts())
        elif system.startswith("You explain the result"):
            self.calls.append("explain")
            text = GOOD_TEXT
        elif system.startswith("You turn one sentence"):
            self.calls.append("simulate")
            fact = {"kind": "estimate", "businessId": "designco", "targetQuote": "DesignCo", "serviceTerm": "design", "amount": 10, "unit": "hours", "quote": "design time is now 10 hours"}
            text = json.dumps({"needsClarification": False, "changes": [fact]})
        else:  # pragma: no cover
            raise AssertionError("unexpected system prompt")
        return {
            "output": {"message": {"role": "assistant", "content": [{"text": text}]}},
            "stopReason": "end_turn",
            "usage": {"inputTokens": 10, "outputTokens": 10},
            "ResponseMetadata": {"RequestId": "aws-stub"},
        }


ORIGIN = "http://localhost:5173"


def full_adapters(sdk: StubSdk, settings) -> Adapters:
    client = BedrockClient(BedrockSettings("m", "r"), sdk)
    return Adapters(
        confirm=confirm_scenario,
        optimize=make_optimize_adapter(reference_optimize, settings.mode),
        extract=BedrockExtractor(client),
        explain=Explainer(reference_optimize, client),
        simulate=Simulator(reference_optimize, settings.mode, client),
    )


@pytest.fixture
def served():
    started: list[tuple[ThreadingHTTPServer, App]] = []

    def start(adapters_factory, commit: str = "test123"):
        settings = load_settings({"APP_MODE": "live", "COMMIT_SHA": commit})
        sdk = StubSdk()
        app = App(settings, adapters_factory(sdk, settings))
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
        httpd.daemon_threads = True
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        started.append((httpd, app))
        return f"http://127.0.0.1:{httpd.server_address[1]}", sdk

    yield start
    for httpd, app in started:
        httpd.shutdown()
        httpd.server_close()
        app.close()


def run_smoke(capsys, *argv: str) -> tuple[int, str]:
    import importlib.util
    import sys

    spec = importlib.util.spec_from_file_location("smoke_under_test", SMOKE_PY)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    code = module.main(list(argv))
    return code, capsys.readouterr().out


def test_smoke_passes_end_to_end_against_a_fully_wired_local_server_SIMULATED(served, site, capsys):
    base, sdk = served(full_adapters)
    url = site(f"fetch('{base}/health')")  # TEST DOUBLE for the hosted frontend
    code, out = run_smoke(capsys, "--api-url", base, "--expect-commit", "test123", "--origin", ORIGIN, "--frontend-url", url)
    assert code == 0, out
    assert "RESULT: PASS" in out and "0 failed, 0 skipped" in out
    for must in (
        "deployed commit equals the commit you deployed",
        "CORS preflight accepts the hosted origin, no credentials header",
        "extract returns a live, unconfirmed draft",
        "canonical totals match the frozen contract (1440 / 540 / 900)",
        "tampered totals are refused before the model is used",
        "text becomes a PROPOSAL only",
        "explicit apply recomputes with the deterministic engine",
        "DesignCo design = 600 gives no_exchange (not infeasible)",
        "recalculated certificate is for the CHANGED scenario",
    ):
        assert f"PASS     {must}" in out, must
    assert list(sdk.calls) == ["extract", "explain", "simulate"], "exactly one model call per model step; the tampered explain and apply made none"


def test_smoke_output_contains_ids_and_timings_but_no_request_bodies_or_descriptions(served, capsys):
    base, _ = served(full_adapters)
    _, out = run_smoke(capsys, "--api-url", base, "--expect-commit", "test123", "--origin", ORIGIN)
    from tests.backend.helpers import fixture

    for description in fixture("extraction.json")["request"]["descriptions"]:
        assert description["text"] not in out
    assert GOOD_TEXT not in out, "model output text is never printed"
    assert re.search(r"request [0-9a-f]{32}", out), "request ids are printed so logs can be correlated"
    assert "AKIA" not in out and "Authorization" not in out


def test_smoke_fails_when_the_deployed_commit_is_not_the_expected_one(served, capsys):
    base, _ = served(full_adapters, commit="somethingelse")
    code, out = run_smoke(capsys, "--api-url", base, "--expect-commit", "test123", "--origin", ORIGIN)
    assert code == 1 and "FAIL     deployed commit equals the commit you deployed" in out and "RESULT: FAIL" in out


def test_smoke_fails_loudly_when_routes_are_unavailable_it_never_treats_503_as_success(served, capsys):
    base, _ = served(lambda sdk, settings: Adapters(confirm=confirm_scenario))
    code, out = run_smoke(capsys, "--api-url", base, "--expect-commit", "test123", "--origin", ORIGIN)
    assert code == 1
    assert "FAIL     optimize returns a result" in out and "FAIL     extract returns a live, unconfirmed draft" in out


def test_skipping_the_model_steps_is_reported_as_partial_never_as_a_pass(served, capsys):
    base, sdk = served(full_adapters)
    code, out = run_smoke(capsys, "--api-url", base, "--expect-commit", "test123", "--origin", ORIGIN, "--skip-model")
    assert code == 4 and "RESULT: PARTIAL" in out and "NOT full live verification" in out
    assert "SKIPPED  extract with real Bedrock" in out and list(sdk.calls) == []
    assert "PASS     explicit apply recomputes with the deterministic engine" in out, "the non-model steps still run"


def test_missing_origin_or_commit_is_a_partial_result_not_a_pass(served, capsys):
    base, _ = served(full_adapters)
    code, out = run_smoke(capsys, "--api-url", base)
    assert code == 4 and "SKIPPED  CORS allows the hosted frontend origin" in out and "SKIPPED  deployed commit equals the commit you deployed" in out


def test_smoke_reports_a_failure_when_the_engine_does_not_produce_the_canonical_numbers(served, capsys):
    def adapters(sdk, settings):
        client = BedrockClient(BedrockSettings("m", "r"), sdk)

        def different(payload):  # an engine that charges 30 minutes of coordination the scenario does not have
            for b in payload["businesses"]:
                b["coordinationMinutes"] = 30
            return reference_optimize(payload)

        return Adapters(
            confirm=confirm_scenario,
            optimize=make_optimize_adapter(different, settings.mode),
            extract=BedrockExtractor(client),
            explain=Explainer(different, client),
            simulate=Simulator(different, settings.mode, client),
        )

    base, _ = served(adapters)
    code, out = run_smoke(capsys, "--api-url", base, "--expect-commit", "test123", "--origin", ORIGIN)
    # The backend's own arithmetic check (B4) recomputes coordination from the scenario (0), so it refuses this engine's
    # answer with a 500 instead of returning wrong numbers; the smoke test then reports the failure.
    assert code == 1
    assert "FAIL     optimize returns a result" in out
    assert "PASS     canonical totals match" not in out


def test_smoke_rejects_a_plain_http_url_to_a_remote_host():
    import importlib.util

    spec = importlib.util.spec_from_file_location("smoke_args", SMOKE_PY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with pytest.raises(SystemExit):
        module.parse_args(["--api-url", "http://example.com"])
    assert module.parse_args(["--api-url", "http://127.0.0.1:8000"]).api_url


# ------------------------------------------------------------------ the hosted-frontend checks
class Site(http.server.BaseHTTPRequestHandler):
    """A tiny static site. `bundle` is class state set per test. TEST DOUBLE for Amplify."""

    bundle = "console.log('app')"
    spa_rewrite = True

    def do_GET(self) -> None:
        if self.path == "/assets/app.js":
            self._send(200, self.bundle, "application/javascript")
        elif self.path in ("/", "/index.html"):
            self._send(200, '<html><body><div id="root"></div><script src="/assets/app.js"></script></body></html>', "text/html")
        elif self.spa_rewrite:
            self._send(200, '<html><body><div id="root"></div><script src="/assets/app.js"></script></body></html>', "text/html")
        else:
            self._send(404, "not found", "text/plain")

    def _send(self, status: int, body: str, content_type: str) -> None:
        data = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args: Any) -> None:
        pass


@pytest.fixture
def site():
    servers: list[ThreadingHTTPServer] = []

    def start(bundle: str, spa_rewrite: bool = True) -> str:
        handler = type("S", (Site,), {"bundle": bundle, "spa_rewrite": spa_rewrite})
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        servers.append(httpd)
        return f"http://127.0.0.1:{httpd.server_address[1]}"

    yield start
    for httpd in servers:
        httpd.shutdown()
        httpd.server_close()


def test_frontend_check_passes_for_a_clean_bundle_with_a_working_spa_rewrite(served, site, capsys):
    base, _ = served(full_adapters)
    url = site(f"fetch('{base}/health')")
    code, out = run_smoke(capsys, "--api-url", base, "--expect-commit", "test123", "--origin", ORIGIN, "--frontend-url", url)
    assert code == 0, out
    assert "PASS     no credential patterns in the shipped bundle" in out and "PASS     SPA rewrite: a deep link serves the app, not a 404" in out
    assert "PASS     the bundle is built for this API URL  |  yes" in out and "PASS     every JS and CSS file the page references loads (HTTP 200)" in out


def test_a_page_whose_script_is_a_404_fails_loudly_instead_of_scanning_the_error_page(served, site, capsys):
    """Found in production: a zip with backslash paths left index.html up but every asset 404. A 404 page has no credentials either."""
    base, _ = served(full_adapters)

    class Missing(Site):
        bundle = ""

        def do_GET(self):  # noqa: N802 - http.server API
            if self.path.startswith("/assets/"):
                self._send(404, "not found", "text/plain")
            else:
                self._send(200, '<html><body><div id="root"></div><script src="/assets/app.js"></script></body></html>', "text/html")

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Missing)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        code, out = run_smoke(capsys, "--api-url", base, "--expect-commit", "test123", "--origin", ORIGIN, "--frontend-url", f"http://127.0.0.1:{httpd.server_address[1]}")
    finally:
        httpd.shutdown()
        httpd.server_close()
    assert code == 1
    assert "FAIL     every JS and CSS file the page references loads (HTTP 200)" in out and "/assets/app.js -> HTTP 404" in out
    assert "FAIL     no credential patterns in the shipped bundle" in out, "nothing was actually scanned, so it must not pass"


def test_a_bundle_built_for_another_api_address_fails(served, site, capsys):
    base, _ = served(full_adapters)
    url = site("fetch('https://some-other-api.example/health')")
    code, out = run_smoke(capsys, "--api-url", base, "--expect-commit", "test123", "--origin", ORIGIN, "--frontend-url", url)
    assert code == 1 and "FAIL     the bundle is built for this API URL" in out


def test_frontend_deploy_script_zips_with_forward_slashes_and_checks_before_uploading():
    script = (ROOT / "scripts" / "deploy_frontend.ps1").read_text(encoding="utf-8")
    assert '.Replace("\\", "/")' in script, "Compress-Archive writes backslash entry names, which made every asset a 404 on Amplify"
    assert "Contains(\"\\\\\")" in script or 'Contains("\\")' in script, "the zip is re-read and refused if any entry still has a backslash"
    upload = script.index("create-deployment")
    for guard in ("credential-looking string", "does not contain the API address", "backslash entry names"):
        assert script.index(guard) < upload, f"'{guard}' must be checked before anything is uploaded"
    assert "StatusCode -ne 200" in script, "every asset is fetched from the public URL after the deployment"
    assert "zipUploadUrl" in script and "Write-Host $created" not in script, "the signed upload URL is never printed"


@pytest.mark.parametrize("leak", ["const k='AKIAABCDEFGHIJKLMNOP'", "aws_secret_access_key=abc", "X-Amz-Security-Token", "import.meta.env.VITE_AWS_KEY"])
def test_frontend_check_fails_when_the_bundle_carries_a_credential_pattern(leak, served, site, capsys):
    base, _ = served(full_adapters)
    code, out = run_smoke(capsys, "--api-url", base, "--expect-commit", "test123", "--origin", ORIGIN, "--frontend-url", site(leak))
    assert code == 1 and "FAIL     no credential patterns in the shipped bundle" in out


def test_frontend_check_fails_without_the_spa_rewrite(served, site, capsys):
    base, _ = served(full_adapters)
    code, out = run_smoke(capsys, "--api-url", base, "--expect-commit", "test123", "--origin", ORIGIN, "--frontend-url", site("x", spa_rewrite=False))
    assert code == 1 and "FAIL     SPA rewrite" in out


# ================================================================== the written instructions
def test_deployment_guide_exists_and_contains_the_exact_commands_and_human_steps():
    text = DEPLOY_DOC.read_text(encoding="utf-8")
    for needle in (
        "sam validate --lint",
        "sam build",
        "scripts\\deploy.ps1",
        "-Deploy",
        "describe-stacks",
        "scripts\\smoke.py",
        "Model access",
        "Amplify",
        "VITE_API_BASE_URL",
        "VITE_DATA_MODE",
        "rewrite",
        "sam delete",
        "AWS_REGION",
        "ALLOWED_ORIGIN",
        "inference profile",
        "Budget",
    ):
        assert needle in text, f"docs/deployment.md should mention: {needle}"
    assert not re.search(r"AKIA[0-9A-Z]{16}|\b\d{12}\b", text), "no account ids or keys in the guide"
    assert "UNVERIFIED" in text or "unverified" in text.lower()


# ================================================================== deploy.ps1 BEHAVIOUR with fake `sam` and `aws` programs (never real AWS)
POWERSHELL = shutil.which("powershell") or shutil.which("pwsh")
needs_powershell = pytest.mark.skipif(POWERSHELL is None, reason="NOT RUN: no PowerShell on this machine to run deploy.ps1")

FAKE_SAM_CMD = r"""@echo off
echo sam %*>> "%FAKE_LOG%"
if "%1"=="--version" echo SAM CLI, version 0.0.0-fake
exit /b 0
"""
FAKE_AWS_CMD = r"""@echo off
echo aws %*>> "%FAKE_LOG%"
echo {"Account": "111122223333", "UserId": "FAKE", "Arn": "arn:aws:iam::111122223333:user/fake"}
exit /b 0
"""


@pytest.fixture
def sandbox(tmp_path):
    """A temp copy of the repo layout deploy.ps1 needs, plus fake sam/aws on PATH that only append their arguments to a log."""
    repo = tmp_path / "repo"
    for pkg in ("backend", "contracts", "optimizer"):
        for source in (ROOT / pkg).rglob("*.py"):
            if "__pycache__" not in source.parts:
                target = repo / source.relative_to(ROOT)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy(source, target)
    (repo / "infra").mkdir()
    shutil.copy(TEMPLATE_PATH, repo / "infra" / "template.yaml")
    shutil.copy(REQ_LAMBDA, repo / "infra" / "requirements-lambda.txt")
    (repo / "scripts").mkdir()
    shutil.copy(DEPLOY_PS1, repo / "scripts" / "deploy.ps1")
    (repo / ".aws-sam" / "build").mkdir(parents=True)
    (repo / ".aws-sam" / "build" / "template.yaml").write_text("fake", encoding="utf-8")  # what a real `sam build` would leave

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "sam.cmd").write_text(FAKE_SAM_CMD, encoding="utf-8")
    (bin_dir / "aws.cmd").write_text(FAKE_AWS_CMD, encoding="utf-8")
    log = tmp_path / "calls.log"
    log.write_text("", encoding="utf-8")

    git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    # The real repo now contains optimizer/api.py. This sandbox models a checkout WITHOUT an optimizer; tests that need one write their own.
    (repo / "optimizer" / "api.py").unlink(missing_ok=True)
    subprocess.run(git + ["add", "-A"], cwd=repo, check=True)
    subprocess.run(git + ["commit", "-q", "-m", "x"], cwd=repo, check=True)

    def run(*arguments: str, stdin: str | None = None) -> tuple[int, str, list[str]]:
        import os

        env = dict(os.environ, PATH=f"{bin_dir};{os.environ['PATH']}", FAKE_LOG=str(log))
        for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
            env.pop(name, None)  # the fake programs never need them, and the real ones must not leak into this test
        log.write_text("", encoding="utf-8")
        result = subprocess.run(
            [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(repo / "scripts" / "deploy.ps1"), *arguments],
            cwd=repo, capture_output=True, text=True, timeout=120, env=env, input=stdin,
        )
        calls = [line.strip() for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]
        return result.returncode, result.stdout + result.stderr, calls

    run.repo = repo
    return run


DEPLOY_ARGS = (
    "-Deploy", "-Region", "us-west-2", "-AllowedOrigin", SAMPLE["AllowedOrigin"],
    "-BedrockModelId", SAMPLE["BedrockModelId"], "-BedrockResourceArns", SAMPLE["BedrockResourceArns"],
)


@needs_powershell
def test_without_the_optimizer_the_script_refuses_and_makes_no_sam_or_aws_call(sandbox):
    assert not (sandbox.repo / "optimizer" / "api.py").exists()
    code, out, calls = sandbox()
    assert code == 1 and "optimizer/api.py does not exist" in out
    assert calls == [] or all(c.startswith("sam --version") for c in calls), calls


@needs_powershell
def test_with_the_optimizer_present_the_default_run_builds_and_never_deploys_or_calls_aws(sandbox):
    (sandbox.repo / "optimizer" / "api.py").write_text("def optimize(scenario):\n    raise NotImplementedError\n", encoding="utf-8")
    code, out, calls = sandbox()
    assert code == 0 and "Nothing was deployed" in out, out
    assert any(c.startswith("sam validate --lint") for c in calls) and any(c.startswith("sam build") for c in calls)
    assert not any(c.startswith("aws ") for c in calls), "build-only must not touch AWS"
    assert not any(c.startswith("sam deploy") for c in calls)
    staged = sandbox.repo / ".aws-sam" / "stage" / "function"
    assert (staged / "backend" / "app.py").exists() and (staged / "optimizer" / "api.py").exists() and (staged / "requirements.txt").exists()
    assert not (staged / "tests").exists() and not any(p.suffix in (".json", ".yaml", ".ts") for p in staged.rglob("*") if p.is_file() and p.name != "requirements.txt")


@needs_powershell
def test_the_thin_slice_flag_allows_packaging_without_the_optimizer_with_a_warning(sandbox):
    code, out, calls = sandbox("-AllowMissingOptimizer")
    assert code == 0 and "WARNING: optimizer/api.py is missing" in out
    assert any(c.startswith("sam build") for c in calls)


@needs_powershell
def test_deploy_without_a_region_refuses_before_any_call(sandbox):
    (sandbox.repo / "optimizer" / "api.py").write_text("def optimize(s):\n    return s\n", encoding="utf-8")
    code, out, calls = sandbox("-Deploy")
    assert code == 1 and "-Region is required" in out and not any(c.startswith("aws ") or c.startswith("sam deploy") for c in calls)


@needs_powershell
def test_wrong_typed_account_cancels_without_deploying(sandbox):
    (sandbox.repo / "optimizer" / "api.py").write_text("def optimize(s):\n    return s\n", encoding="utf-8")
    code, out, calls = sandbox(*DEPLOY_ARGS, stdin="not-the-account\n")
    assert code == 2 and "Cancelled. Nothing was changed." in out
    assert any(c.startswith("aws sts get-caller-identity") for c in calls), "it read only the identity"
    assert not any(c.startswith("sam deploy") for c in calls)
    assert "111122223333" in out, "the account is shown so the human can check it"


@needs_powershell
def test_correct_typed_account_deploys_with_a_change_set_confirmation_and_exact_parameters(sandbox):
    (sandbox.repo / "optimizer" / "api.py").write_text("def optimize(s):\n    return s\n", encoding="utf-8")
    code, out, calls = sandbox(*DEPLOY_ARGS, stdin="111122223333\n")
    assert code == 0, out
    deploy = next(c for c in calls if c.startswith("sam deploy"))
    for needle in (
        "--stack-name trademind-api", "--region us-west-2", "--resolve-s3", "--capabilities CAPABILITY_IAM", "--confirm-changeset",
        f"AllowedOrigin={SAMPLE['AllowedOrigin']}", f"BedrockModelId={SAMPLE['BedrockModelId']}", "ReservedConcurrency=5", "CommitSha=",
    ):
        assert needle in deploy, f"{needle} missing from: {deploy}"
    assert "--guided" not in deploy
    assert not re.search(r"\b\w+=(\s|$)", deploy), "SAM CLI rejects an override with an empty value (found on the real CLI): " + deploy
    assert "BudgetAlertEmail" not in deploy and "MonthlyBudgetUsd=0" in deploy, "no budget unless asked for"
    assert re.search(r"CommitSha=[0-9a-f]{12}", deploy), "the real git commit is passed so /health can be compared"
    assert any(c.startswith("aws cloudformation describe-stacks") for c in calls)
    assert "AKIA" not in out and "SecretAccessKey" not in out


@needs_powershell
def test_budget_flags_are_passed_together_and_validated_before_any_deploy(sandbox):
    (sandbox.repo / "optimizer" / "api.py").write_text("def optimize(s):\n    return s\n", encoding="utf-8")
    code, out, calls = sandbox(*DEPLOY_ARGS, "-MonthlyBudgetUsd", "25", "-BudgetAlertEmail", "owner@example.com", stdin="111122223333\n")
    deploy = next(c for c in calls if c.startswith("sam deploy"))
    assert code == 0 and "MonthlyBudgetUsd=25" in deploy and "BudgetAlertEmail=owner@example.com" in deploy, out
    for bad in (("-MonthlyBudgetUsd", "25"), ("-MonthlyBudgetUsd", "abc", "-BudgetAlertEmail", "owner@example.com"), ("-BudgetAlertEmail", "not-an-email")):
        code, out, calls = sandbox(*DEPLOY_ARGS, *bad, stdin="111122223333\n")
        assert code == 1 and not any(c.startswith("sam deploy") for c in calls), (bad, out)


@needs_powershell
def test_wildcard_arns_and_non_https_origins_are_refused_before_deploying(sandbox):
    (sandbox.repo / "optimizer" / "api.py").write_text("def optimize(s):\n    return s\n", encoding="utf-8")
    wildcard = list(DEPLOY_ARGS)
    wildcard[wildcard.index("-BedrockResourceArns") + 1] = "arn:aws:bedrock:*::foundation-model/*"
    code, out, calls = sandbox(*wildcard, stdin="111122223333\n")
    assert code == 1 and "Wildcards are not allowed" in out and not any(c.startswith("sam deploy") for c in calls)
    insecure = list(DEPLOY_ARGS)
    insecure[insecure.index("-AllowedOrigin") + 1] = "http://example.com"
    code, out, calls = sandbox(*insecure, stdin="111122223333\n")
    assert code == 1 and "https origin" in out and not any(c.startswith("sam deploy") for c in calls)


@needs_powershell
def test_the_unconfirmed_concurrency_opt_out_is_reported_loudly(sandbox):
    (sandbox.repo / "optimizer" / "api.py").write_text("def optimize(s):\n    return s\n", encoding="utf-8")
    code, out, calls = sandbox(*DEPLOY_ARGS, "-ReservedConcurrency", "-1", stdin="111122223333\n")
    assert "removes the hard ceiling" in out
    assert any("ReservedConcurrency=-1" in c for c in calls if c.startswith("sam deploy"))


@needs_powershell
def test_teardown_asks_for_the_account_and_only_then_deletes(sandbox):
    code, out, calls = sandbox("-Teardown", "-Region", "us-west-2", stdin="wrong\n")
    assert code == 2 and not any(c.startswith("sam delete") for c in calls)
    code, out, calls = sandbox("-Teardown", "-Region", "us-west-2", stdin="111122223333\n")
    assert code == 0 and any(c.startswith("sam delete --stack-name trademind-api") for c in calls)


# ================================================================== smoke.py must notice a credentials header
class CredentialedCors(http.server.BaseHTTPRequestHandler):
    """TEST DOUBLE: a misconfigured API that allows credentials with the right origin."""

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        origin = self.headers.get("Origin", "")
        self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Access-Control-Allow-Methods", "OPTIONS, POST")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Credentials", "true")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:
        body = json.dumps({"status": "ok", "schemaVersion": "1", "commit": "c", "mode": "live"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_POST = do_GET

    def log_message(self, *args: Any) -> None:
        pass


def test_smoke_flags_a_cors_configuration_that_allows_credentials(capsys):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), CredentialedCors)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        code, out = run_smoke(capsys, "--api-url", f"http://127.0.0.1:{httpd.server_address[1]}", "--origin", ORIGIN, "--skip-model")
    finally:
        httpd.shutdown()
        httpd.server_close()
    assert code == 1
    assert "FAIL     CORS preflight accepts the hosted origin, no credentials header" in out
