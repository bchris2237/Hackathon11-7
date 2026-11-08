"""B8: security and production hardening (backend/middleware.py and its wiring in backend/app.py).

Everything here is local and deterministic. Nothing calls AWS. What these tests do NOT prove: how API Gateway throttling,
Lambda reserved concurrency, AWS Budgets or CloudWatch behave in a real account (those are UNVERIFIED until a human runs
the checks in docs/deployment.md), nor that a browser enforces CORS the way the spec says.

The canonical scenario (fixtures/canonical.json) is the legitimate traffic in these tests: after every hostile case the same
request still gets its normal answer, so a defence that also breaks real use would fail here.
"""

from __future__ import annotations

import copy
import dataclasses
import json
import logging
import re
from typing import Any

import pytest
import yaml

from backend.app import MAX_BODY_BYTES, Adapters, App, Request
from backend.bedrock import load_bedrock_settings
from backend.config import ConfigError, load_settings
from backend.confirmation import confirm_scenario
from backend.errors import Throttled
from backend.middleware import (
    REDACTED,
    ClientRateLimiter,
    check_content_type,
    check_origin,
    find_unsafe_text,
    install_log_redaction,
    redact,
    security_headers,
    validate_origin_setting,
)
from tests.backend.helpers import ROOT, fixture
from tests.backend.test_deployment import TEMPLATE_PATH, CfnLoader

ALLOWED = "http://localhost:5173"
EVIL = "https://evil.example"
SETTINGS = load_settings({"COMMIT_SHA": "test123"})
JSON_HEADERS = {"content-type": "application/json"}

AWS_KEY = "AKIAABCDEFGHIJKLMNOP"
AWS_SESSION_KEY = "ASIAABCDEFGHIJKLMNOP"
AWS_SECRET = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"  # 40 chars, mixed case: the AWS secret key shape (AWS documentation example)
SESSION_TOKEN = "IQoJb3JpZ2luX2VjEDoaCXVzLWVhc3QtMSJHMEUCIQD" + "x" * 140
SHA256 = "a3f5c1d2e4b6a8c0d2e4f60718293a4b5c6d7e8f9a0b1c2d3e4f5a6b7c8d9e0f"


class Calls:
    """An adapter that records every call. A request that should have been stopped must leave `count` at 0."""

    def __init__(self, inner=None) -> None:
        self.count = 0
        self.inner = inner

    def __call__(self, *args: Any) -> Any:
        self.count += 1
        if self.inner is not None:
            return self.inner(*args)
        raise AssertionError("this adapter must not be reached")


@pytest.fixture
def canonical() -> dict[str, Any]:
    return fixture("canonical.json")


def make_app(settings=SETTINGS, **adapters: Any) -> App:
    return App(settings, Adapters(**adapters))


def send(app: App, method: str, path: str, body: Any = None, headers: dict[str, str] | None = None, client: str = "198.51.100.7", raw: bytes | None = None):
    payload = raw if raw is not None else (b"" if body is None else json.dumps(body).encode())
    merged = {**JSON_HEADERS, **(headers or {})} if method in ("POST", "PUT") else dict(headers or {})
    return app.handle(Request(method, path, merged, payload, "req-1", client=client))


def error_code(response) -> str | None:
    return response.body.get("error", {}).get("code")


# ================================================================== hostile origin
HOSTILE_ORIGINS = [EVIL, "null", "http://localhost:5173.evil.example", "http://localhost:5174", "https://localhost:5173", "HTTP://LOCALHOST:5173", "http://localhost:5173/", "*", ""]


@pytest.mark.parametrize("origin", HOSTILE_ORIGINS)
def test_a_state_changing_request_from_an_unlisted_origin_is_refused_before_any_work(origin, canonical):
    adapter = Calls(confirm_scenario)
    app = make_app(confirm=adapter)
    response = send(app, "POST", "/confirm", {"scenario": canonical}, {"origin": origin})
    assert response.status == 403 and error_code(response) == "ORIGIN_NOT_ALLOWED"
    assert adapter.count == 0, "the call must be stopped, not just hidden from the browser"
    assert "Access-Control-Allow-Origin" not in response.headers
    app.close()


def test_listed_and_absent_origins_work_and_the_same_request_still_succeeds_after_hostile_ones(canonical):
    app = make_app(confirm=confirm_scenario)
    assert send(app, "POST", "/confirm", {"scenario": canonical}, {"origin": EVIL}).status == 403
    ok = send(app, "POST", "/confirm", {"scenario": canonical}, {"origin": ALLOWED})
    assert ok.status == 200 and ok.body == {"scenario": canonical}, "the canonical scenario round-trips unchanged"
    assert ok.headers["Access-Control-Allow-Origin"] == ALLOWED
    assert send(app, "POST", "/confirm", {"scenario": canonical}).status == 200, "no Origin = a script or curl, not a browser"
    app.close()


def test_reads_and_preflights_from_unlisted_origins_get_no_cors_headers_but_no_error():
    app = make_app()
    health = send(app, "GET", "/health", headers={"origin": EVIL})
    assert health.status == 200 and "Access-Control-Allow-Origin" not in health.headers
    preflight = send(app, "OPTIONS", "/confirm", headers={"origin": EVIL, "access-control-request-method": "POST"})
    assert preflight.status == 204 and "Access-Control-Allow-Origin" not in preflight.headers
    app.close()


def test_origin_check_matches_exactly_and_is_case_sensitive():
    check_origin({"origin": ALLOWED}, [ALLOWED])
    check_origin({}, [ALLOWED])
    for bad in ("http://LOCALHOST:5173", ALLOWED + "/", ALLOWED + ".evil.example", " " + ALLOWED):
        with pytest.raises(Exception) as caught:
            check_origin({"origin": bad}, [ALLOWED])
        assert caught.value.status == 403


# ================================================================== content type (the no-preflight route)
@pytest.mark.parametrize(
    "declared",
    ["text/plain", "text/plain;charset=UTF-8", "application/x-www-form-urlencoded", "multipart/form-data; boundary=x", "application/jsonx", "application/json, text/plain", "text/html", "application/xml", ""],
)
def test_a_body_that_is_not_declared_json_is_refused_with_415_and_never_reaches_the_adapter(declared, canonical):
    adapter = Calls(confirm_scenario)
    app = make_app(confirm=adapter)
    response = app.handle(Request("POST", "/confirm", {"content-type": declared}, json.dumps({"scenario": canonical}).encode(), "r", client="c"))
    assert response.status == 415 and error_code(response) == "UNSUPPORTED_MEDIA_TYPE" and adapter.count == 0
    app.close()


@pytest.mark.parametrize("declared", ["application/json", "Application/JSON", "application/json; charset=utf-8", 'application/json; charset="UTF-8"'])
def test_json_content_types_are_accepted(declared):
    check_content_type({"content-type": declared})


def test_a_missing_content_type_is_tolerated_because_browsers_always_send_one():
    check_content_type({})


def test_a_charset_other_than_utf8_is_refused():
    with pytest.raises(Exception) as caught:
        check_content_type({"content-type": "application/json; charset=latin-1"})
    assert caught.value.status == 415


# ================================================================== oversized and excessive input
def test_a_body_over_32_kib_is_413_before_it_is_parsed(canonical):
    adapter = Calls(confirm_scenario)
    app = make_app(confirm=adapter)
    huge = b'{"scenario":' + b'"' + b"a" * (MAX_BODY_BYTES + 10) + b'"}'
    response = send(app, "POST", "/confirm", raw=huge)
    assert response.status == 413 and error_code(response) == "PAYLOAD_TOO_LARGE" and adapter.count == 0
    declared = app.handle(Request("POST", "/confirm", JSON_HEADERS, b"", "r", declared_length=MAX_BODY_BYTES * 100, client="c"))
    assert declared.status == 413, "a declared length over the cap is refused without reading the body"
    app.close()


def test_a_body_exactly_at_the_cap_is_not_refused_for_size(canonical):
    app = make_app(confirm=confirm_scenario)
    padding = " " * (MAX_BODY_BYTES - len(json.dumps({"scenario": canonical})))
    body = (json.dumps({"scenario": canonical}) + padding).encode()
    assert len(body) == MAX_BODY_BYTES
    assert send(app, "POST", "/confirm", raw=body).status == 200
    app.close()


def never_adapters() -> dict[str, Calls]:
    return {name: Calls() for name in ("extract", "confirm", "optimize", "simulate", "explain")}


def test_oversized_free_text_is_rejected_by_the_contract_before_any_model_call(canonical):
    adapters = never_adapters()
    app = make_app(**adapters)
    long_text = "word " * 500  # 2500 characters, under the byte cap but over the 2000-character field limit
    sim = send(app, "POST", "/simulate", {"scenario": canonical, "changes": [], "text": long_text, "apply": False})
    assert sim.status == 422 and "text" in sim.body["error"]["fields"]
    ext = send(app, "POST", "/extract", {"descriptions": [{"id": "devco", "name": "DevCo", "text": "x" * 8001}]})
    assert ext.status == 422
    name = send(app, "POST", "/extract", {"descriptions": [{"id": "devco", "name": "n" * 201, "text": "ok"}]})
    assert name.status == 422
    assert sum(a.count for a in adapters.values()) == 0
    app.close()


def test_excessive_array_lengths_are_rejected(canonical):
    adapters = never_adapters()
    app = make_app(**adapters)
    change = {"kind": "capacity", "businessId": "devco", "minutes": 100}
    assert send(app, "POST", "/simulate", {"scenario": canonical, "changes": [change] * 21, "apply": False}).status == 422
    four = [{"id": f"b{i}", "name": f"B{i}", "text": "t"} for i in range(4)]
    assert send(app, "POST", "/extract", {"descriptions": four}).status == 422
    many = copy.deepcopy(canonical)
    many["deliverables"] = many["deliverables"] * 3
    assert send(app, "POST", "/optimize", {"scenario": many}).status == 422
    four_biz = copy.deepcopy(canonical)
    four_biz["businesses"].append(copy.deepcopy(four_biz["businesses"][0]))
    four_biz["businesses"][3]["id"] = "extraco"
    assert send(app, "POST", "/optimize", {"scenario": four_biz}).status == 422
    assert sum(a.count for a in adapters.values()) == 0
    app.close()


@pytest.mark.parametrize("minutes", [1_000_001, 10**12, -1, 0, 1.5, "60", True])
def test_numeric_limits_hold(minutes, canonical):
    scenario = copy.deepcopy(canonical)
    scenario["businesses"][0]["estimates"][0]["minutes"] = minutes
    app = make_app(optimize=Calls())
    assert send(app, "POST", "/optimize", {"scenario": scenario}).status == 422
    app.close()


def test_unknown_fields_and_duplicate_keys_are_rejected(canonical):
    app = make_app(confirm=Calls())
    extra = copy.deepcopy(canonical)
    extra["isAdmin"] = True
    assert send(app, "POST", "/confirm", {"scenario": extra}).status == 422
    dup = b'{"scenario": {"schemaVersion": "1", "schemaVersion": "1"}}'
    assert send(app, "POST", "/confirm", raw=dup).status == 400
    app.close()


# ================================================================== hostile characters
@pytest.mark.parametrize(
    "bad",
    ["Dev\x00Co", "Dev\nCo", "Dev\x1b[31mCo", "Dev\u202eoC", "Dev\u2066Co", "Dev\x7fCo", "Dev\rCo", "Dev\u061cCo"],
)
def test_control_and_direction_override_characters_in_names_are_refused_with_the_path(bad, canonical):
    adapter = Calls(confirm_scenario)
    app = make_app(confirm=adapter)
    scenario = copy.deepcopy(canonical)
    scenario["businesses"][0]["name"] = bad
    response = send(app, "POST", "/confirm", {"scenario": scenario})
    assert response.status == 422 and error_code(response) == "VALIDATION_ERROR"
    assert response.body["error"]["fields"] == ["scenario.businesses[0].name"]
    assert bad not in json.dumps(response.body), "the offending value is never echoed back"
    assert adapter.count == 0
    app.close()


def test_free_text_may_contain_ordinary_line_breaks_but_not_other_controls(canonical):
    adapter = Calls(lambda request: (_ for _ in ()).throw(RuntimeError("stop")))  # reached = validation passed
    app = make_app(simulate=adapter)
    ok = {"scenario": canonical, "changes": [], "text": "DesignCo's design is now 10 hours.\nThanks,\tA", "apply": False}
    send(app, "POST", "/simulate", ok)
    assert adapter.count == 1, "newline and tab are allowed inside the free-text field"
    bad = dict(ok, text="DesignCo\x00 design is 10 hours")
    response = send(app, "POST", "/simulate", bad)
    assert response.status == 422 and response.body["error"]["fields"] == ["text"] and adapter.count == 1
    app.close()


def test_a_control_character_in_an_object_key_is_found():
    assert find_unsafe_text({"ok": 1, "bad\x00key": 2}) == ["(key)"]


def test_the_scan_is_iterative_so_deep_nesting_cannot_overflow_the_stack():
    deep: Any = "end"
    for _ in range(50_000):
        deep = [deep]
    assert find_unsafe_text(deep) == []
    deep_bad: Any = "bad\x00"
    for _ in range(50_000):
        deep_bad = {"a": deep_bad}
    assert len(find_unsafe_text(deep_bad)) == 1


def test_the_scan_reports_a_bounded_number_of_paths():
    assert len(find_unsafe_text(["\x00"] * 500)) <= 20


def test_legitimate_unicode_text_is_not_flagged():
    assert find_unsafe_text({"name": "Café Ünïcode 設計 🎨", "text": "line one\nline two"}) == []


# ================================================================== secret-like log payloads
SECRET_SAMPLES = [
    (f"key {AWS_KEY} used", AWS_KEY),
    (f"temporary {AWS_SESSION_KEY}", AWS_SESSION_KEY),
    (f"aws_secret_access_key={AWS_SECRET}", AWS_SECRET),
    (f"AWS_SECRET_ACCESS_KEY: {AWS_SECRET}", AWS_SECRET),
    ('{"secretAccessKey": "abcdEFGH1234"}', "abcdEFGH1234"),
    (f"AWS_SESSION_TOKEN={SESSION_TOKEN}", SESSION_TOKEN),
    (f"X-Amz-Security-Token: {SESSION_TOKEN}", SESSION_TOKEN),
    ("Authorization: Bearer abc123def456ghi789", "abc123def456ghi789"),
    ("password=hunter2hunter2", "hunter2hunter2"),
    ('{"api_key": "sk-test-1234567890"}', "sk-test-1234567890"),
    ("-----BEGIN RSA PRIVATE KEY-----\nMIIBOgIBAAJBAK\n-----END RSA PRIVATE KEY-----", "MIIBOgIBAAJBAK"),
    ("contact owner@company.example please", "owner@company.example"),
    (f"raw token {AWS_SECRET} in the middle", AWS_SECRET),
    ("blob " + "Ab1" * 40, "Ab1" * 40),
]


@pytest.mark.parametrize(("text", "secret"), SECRET_SAMPLES)
def test_secret_like_content_is_redacted(text, secret):
    cleaned = redact(text)
    assert secret not in cleaned, cleaned
    assert redact(cleaned) == cleaned, "redaction is idempotent"


def test_ordinary_log_content_is_left_alone():
    line = json.dumps(
        {
            "event": "request", "requestId": "0123456789abcdef0123456789abcdef", "route": "/optimize", "status": 200,
            "digest": SHA256, "commit": "c96559821873", "modelId": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
            "awsRequestId": "7f1a2b3c-4d5e-6f70-8192-a3b4c5d6e7f8", "inputTokens": 10, "outputTokens": 20, "maxTokens": 1500,
        }
    )
    assert redact(line) == line


@pytest.fixture
def log_text(caplog):
    install_log_redaction()
    caplog.set_level(logging.INFO)
    return caplog


@pytest.mark.parametrize("name", ["trademind.api", "trademind.bedrock", "botocore.credentials", "some.third.party"])
def test_every_logger_in_the_process_is_covered_including_format_arguments(name, log_text):
    logging.getLogger(name).error("failed for %s with key=%s", "devco", AWS_SECRET)
    logging.getLogger(name).info("session %s", SESSION_TOKEN)
    logging.getLogger(name).warning("direct %s", AWS_KEY)
    assert AWS_SECRET not in log_text.text and SESSION_TOKEN not in log_text.text and AWS_KEY not in log_text.text
    assert REDACTED in log_text.text and "devco" in log_text.text, "only the secret is removed, not the whole message"


def test_exception_logging_keeps_the_type_but_never_the_message_or_traceback(log_text):
    try:
        raise RuntimeError(f"boom {AWS_SECRET} Traceback password=hunter2hunter2")
    except RuntimeError:
        logging.getLogger("trademind.api").exception("handler failed")
    assert AWS_SECRET not in log_text.text and "hunter2hunter2" not in log_text.text
    assert "RuntimeError" in log_text.text and "Traceback (most recent call last)" not in log_text.text


def test_building_the_app_installs_redaction_by_itself(caplog):
    original = logging.getLogRecordFactory()
    logging.setLogRecordFactory(logging.LogRecord)  # a process where nothing has installed redaction yet
    try:
        app = make_app()
        caplog.set_level(logging.INFO)
        logging.getLogger("trademind.api").error("leak %s", AWS_KEY)
        assert AWS_KEY not in caplog.text, "production wiring must not depend on a test or a script calling install_log_redaction"
        app.close()
    finally:
        logging.setLogRecordFactory(original)


def test_installing_twice_does_not_wrap_twice():
    install_log_redaction()
    factory = logging.getLogRecordFactory()
    install_log_redaction()
    assert logging.getLogRecordFactory() is factory


def test_secrets_in_a_request_never_reach_the_logs_even_when_the_request_fails(log_text, canonical):
    app = make_app(confirm=confirm_scenario)
    scenario = copy.deepcopy(canonical)
    scenario["businesses"][0]["name"] = f"DevCo {AWS_SECRET}"
    scenario["isAdmin"] = AWS_KEY
    response = send(app, "POST", "/confirm", {"scenario": scenario}, {"authorization": f"Bearer {SESSION_TOKEN}", "x-api-key": AWS_KEY})
    assert response.status == 422
    for secret in (AWS_SECRET, AWS_KEY, SESSION_TOKEN):
        assert secret not in log_text.text and secret not in json.dumps(response.body)
    assert "request" in log_text.text, "the request was still logged, with its id and status"
    app.close()


# ================================================================== production errors carry no internals
def test_an_unexpected_exception_gives_a_fixed_500_with_no_stack_trace_or_internals(log_text, canonical):
    secret_message = f'Traceback (most recent call last):\n  File "/var/task/backend/app.py", line 1\nKeyError: {AWS_SECRET}'

    def explode(_request):
        raise RuntimeError(secret_message)

    app = make_app(confirm=explode)
    response = send(app, "POST", "/confirm", {"scenario": canonical})
    assert response.status == 500 and error_code(response) == "INTERNAL_ERROR"
    assert response.body["error"]["message"] == "Internal server error."
    wire = json.dumps(response.body) + json.dumps(response.headers)
    for leak in ("Traceback", "File \"", "/var/task", "RuntimeError", "KeyError", AWS_SECRET, ".py"):
        assert leak not in wire, leak
    assert set(response.body["error"]) == {"code", "message", "fields", "requestId"}
    assert AWS_SECRET not in log_text.text and "Traceback" not in log_text.text
    assert "RuntimeError" in log_text.text, "the type is logged so the failure can be found"
    app.close()


def test_every_error_status_has_the_same_envelope_and_no_extra_keys(canonical):
    app = make_app(confirm=confirm_scenario)
    cases = [
        send(app, "GET", "/nope"),
        send(app, "GET", "/confirm"),
        send(app, "POST", "/confirm", raw=b"{bad"),
        send(app, "POST", "/confirm", {"scenario": {}}),
        send(app, "POST", "/confirm", {"scenario": canonical}, {"origin": EVIL}),
        send(app, "POST", "/confirm", {"scenario": canonical}, {"content-type": "text/plain"}),
        send(app, "POST", "/optimize", {"scenario": canonical}),
    ]
    assert [c.status for c in cases] == [404, 405, 400, 422, 403, 415, 503]
    for response in cases:
        assert set(response.body) == {"error"} and set(response.body["error"]) == {"code", "message", "fields", "requestId"}
        assert response.body["error"]["requestId"] == response.headers["X-Request-Id"]
    app.close()


def test_error_messages_never_echo_client_values(canonical):
    app = make_app(confirm=confirm_scenario)
    marker = "ZZ-MARKER-ZZ"
    scenario = copy.deepcopy(canonical)
    scenario["businesses"][0]["id"] = marker + " bad slug!"
    response = send(app, "POST", "/confirm", {"scenario": scenario})
    assert response.status == 422 and marker not in json.dumps(response.body)
    app.close()


# ================================================================== 429 behavior
def test_a_throttled_adapter_surfaces_as_a_clean_429_with_no_aws_detail(canonical):
    def throttled(_request):
        raise Throttled("The language model is throttled. Retry shortly.")

    app = make_app(confirm=throttled)
    response = send(app, "POST", "/confirm", {"scenario": canonical})
    assert response.status == 429 and error_code(response) == "THROTTLED"
    assert "arn:" not in json.dumps(response.body) and "amazonaws" not in json.dumps(response.body)
    app.close()


def test_the_per_client_limit_gives_429_with_retry_after_and_never_touches_the_adapter(canonical):
    adapter = Calls(confirm_scenario)
    settings = dataclasses.replace(SETTINGS, client_burst=3, client_rate_per_minute=60)
    app = make_app(settings, confirm=adapter)
    now = [1000.0]
    app._limiter = ClientRateLimiter(60, 3, clock=lambda: now[0])
    for _ in range(3):
        assert send(app, "POST", "/confirm", {"scenario": canonical}).status == 200
    blocked = send(app, "POST", "/confirm", {"scenario": canonical})
    assert blocked.status == 429 and error_code(blocked) == "THROTTLED"
    assert blocked.headers["Retry-After"] == "1" and adapter.count == 3
    other = send(app, "POST", "/confirm", {"scenario": canonical}, client="203.0.113.9")
    assert other.status == 200, "one client's burst does not lock out another"
    health = send(app, "GET", "/health")
    assert health.status == 200, "health is never rate limited"
    now[0] += 2.0
    assert send(app, "POST", "/confirm", {"scenario": canonical}).status == 200, "the bucket refills"
    app.close()


def test_the_limiter_uses_the_api_gateway_source_ip_for_lambda_events(canonical):
    app = make_app(confirm=confirm_scenario)
    app._limiter = ClientRateLimiter(60, 1, clock=lambda: 0.0)

    def event(ip: str) -> dict[str, Any]:
        return {
            "version": "2.0", "rawPath": "/confirm", "headers": {"content-type": "application/json"},
            "requestContext": {"http": {"method": "POST", "path": "/confirm", "sourceIp": ip}, "requestId": "r", "stage": "$default"},
            "body": json.dumps({"scenario": canonical}), "isBase64Encoded": False,
        }

    assert app.handle_event(event("198.51.100.1"))["statusCode"] == 200
    assert app.handle_event(event("198.51.100.1"))["statusCode"] == 429
    assert app.handle_event(event("198.51.100.2"))["statusCode"] == 200
    hostile = event("198.51.100.3")
    hostile["headers"]["origin"] = EVIL
    assert app.handle_event(hostile)["statusCode"] == 403
    app.close()


def test_unit_limiter_refill_and_retry_after():
    now = [0.0]
    limiter = ClientRateLimiter(rate_per_minute=60, burst=2, clock=lambda: now[0])
    assert limiter.check("a") is None and limiter.check("a") is None
    assert limiter.check("a") == 1
    now[0] += 0.5
    assert limiter.check("a") == 1, "half a token is not enough"
    now[0] += 0.5
    assert limiter.check("a") is None
    slow = ClientRateLimiter(rate_per_minute=6, burst=1, clock=lambda: now[0])
    assert slow.check("x") is None and slow.check("x") == 10, "one token every 10 seconds"


def test_unit_limiter_idle_time_never_banks_more_than_the_burst():
    now = [0.0]
    limiter = ClientRateLimiter(rate_per_minute=60, burst=3, clock=lambda: now[0])
    assert limiter.check("a") is None
    now[0] += 1_000_000.0  # a client that was silent for days
    allowed = sum(1 for _ in range(20) if limiter.check("a") is None)
    assert allowed == 3, "idle time must not turn into an unlimited burst"


def test_unit_limiter_memory_is_bounded_and_evicts_the_least_recently_used():
    limiter = ClientRateLimiter(60, 1, clock=lambda: 0.0, max_clients=100)
    for i in range(10_000):
        limiter.check(f"client-{i}")
    assert len(limiter) <= 100, "an attacker rotating addresses cannot grow memory without limit"


@pytest.mark.parametrize("rate, burst, clients", [(0, 1, 1), (1, 0, 1), (1, 1, 0), (-1, 1, 1)])
def test_unit_limiter_rejects_nonsense_configuration(rate, burst, clients):
    with pytest.raises(ValueError):
        ClientRateLimiter(rate, burst, max_clients=clients)


def test_a_busy_container_still_answers_429_with_retry_after(canonical):
    import threading

    gate = threading.Event()
    started = threading.Event()

    def slow(_request):
        started.set()
        gate.wait(5)
        return {"scenario": canonical}

    settings = dataclasses.replace(SETTINGS, max_in_flight=1)
    app = make_app(settings, confirm=slow)
    worker = threading.Thread(target=lambda: send(app, "POST", "/confirm", {"scenario": canonical}, client="a"))
    worker.start()
    assert started.wait(5)
    try:
        busy = send(app, "POST", "/confirm", {"scenario": canonical}, client="b")
        assert busy.status == 429 and busy.headers["Retry-After"] == "1"
        assert send(app, "GET", "/health").status == 200
    finally:
        gate.set()
        worker.join(5)
        app.close()


# ================================================================== response headers
def test_security_headers_are_on_every_kind_of_response(canonical):
    app = make_app(confirm=confirm_scenario)
    responses = [
        send(app, "GET", "/health"),
        send(app, "GET", "/nope"),
        send(app, "POST", "/confirm", {"scenario": canonical}),
        send(app, "POST", "/confirm", {"scenario": canonical}, {"origin": EVIL}),
        send(app, "OPTIONS", "/confirm", headers={"origin": ALLOWED}),
    ]
    for response in responses:
        for name, value in security_headers().items():
            assert response.headers[name] == value, (response.status, name)
        assert response.headers["Cache-Control"] == "no-store" and response.headers["X-Content-Type-Options"] == "nosniff"
    assert "frame-ancestors 'none'" in security_headers()["Content-Security-Policy"]
    app.close()


# ================================================================== configuration
@pytest.mark.parametrize("origin", ["https://main.d1234567890abc.amplifyapp.com", "https://example.com:8443", "http://localhost:5173", "http://127.0.0.1:5173"])
def test_valid_origin_settings(origin):
    assert validate_origin_setting(origin, live=True) == origin


@pytest.mark.parametrize(
    "origin",
    ["https://*.example.com", "*", "https://example.com/path", "https://example.com?x=1", "https://example.com#frag", "https://user:pw@example.com",
     "ftp://example.com", "example.com", "https://", "https://exa mple.com", "https://example.com:99999", "http://example.com", "javascript:alert(1)"],
)
def test_invalid_origin_settings_are_refused_in_live_mode(origin):
    with pytest.raises(ValueError):
        validate_origin_setting(origin, live=True)


def test_plain_http_is_only_refused_for_remote_hosts_in_live_mode():
    assert validate_origin_setting("http://example.com", live=False)
    with pytest.raises(ValueError):
        validate_origin_setting("http://example.com", live=True)


def test_startup_refuses_a_bad_origin_or_limit():
    with pytest.raises(ConfigError, match="ALLOWED_ORIGIN"):
        load_settings({"APP_MODE": "live", "ALLOWED_ORIGIN": "https://ok.example,https://*.evil.example"})
    with pytest.raises(ConfigError, match="ALLOWED_ORIGIN"):
        load_settings({"APP_MODE": "live", "ALLOWED_ORIGIN": "http://not-local.example"})
    for name, value in (("CLIENT_RATE_PER_MINUTE", "0"), ("CLIENT_RATE_PER_MINUTE", "601"), ("CLIENT_BURST", "0"), ("CLIENT_BURST", "101"), ("CLIENT_BURST", "x")):
        with pytest.raises(ConfigError, match=name):
            load_settings({name: value})
    settings = load_settings({"CLIENT_RATE_PER_MINUTE": "120", "CLIENT_BURST": "20"})
    assert (settings.client_rate_per_minute, settings.client_burst) == (120, 20)


def test_model_tokens_and_retries_are_bounded():
    base = {"BEDROCK_MODEL_ID": "us.example.model-v1:0", "AWS_REGION": "us-east-1"}
    assert load_bedrock_settings(base).max_attempts == 2, "one retry at most"
    for bad in ("99", "4001", "-1", "abc"):
        with pytest.raises(ConfigError):
            load_bedrock_settings({**base, "BEDROCK_MAX_OUTPUT_TOKENS": bad})
    settings = load_bedrock_settings({**base, "BEDROCK_MAX_OUTPUT_TOKENS": "4000"})
    assert settings.max_output_tokens == 4000 and settings.worst_case_call_seconds < 29


# ================================================================== template and written guidance
@pytest.fixture(scope="module")
def template() -> dict[str, Any]:
    return yaml.load(TEMPLATE_PATH.read_text(encoding="utf-8"), Loader=CfnLoader)


def test_template_exposes_the_new_limits_with_hard_bounds(template):
    params = template["Parameters"]
    for name, default, low, high in (("ClientRatePerMinute", 60, 1, 600), ("ClientBurst", 30, 1, 100), ("ThrottleRatePerSecond", 5, 1, 50), ("MaxInFlight", 4, 1, 16)):
        assert (params[name]["Default"], params[name]["MinValue"], params[name]["MaxValue"]) == (default, low, high), name
    env = template["Resources"]["ApiFunction"]["Properties"]["Environment"]["Variables"]
    assert env["CLIENT_RATE_PER_MINUTE"] == {"Ref": "ClientRatePerMinute"} and env["CLIENT_BURST"] == {"Ref": "ClientBurst"}
    assert env["BEDROCK_MAX_OUTPUT_TOKENS"] == "1500"
    assert template["Resources"]["ApiFunction"]["Properties"]["ReservedConcurrentExecutions"]["Fn::If"][0] == "HasReservedConcurrency"


def test_budget_alert_is_optional_off_by_default_and_never_described_as_a_cap(template):
    params = template["Parameters"]
    assert params["MonthlyBudgetUsd"]["Default"] == 0 and params["BudgetAlertEmail"]["Default"] == ""
    budget = template["Resources"]["MonthlyBudget"]
    assert budget["Type"] == "AWS::Budgets::Budget" and budget["Condition"] == "BudgetEnabled"
    cond = template["Conditions"]["BudgetEnabled"]["Fn::And"]
    assert len(cond) == 2, "both an amount and an address are needed"
    notifications = budget["Properties"]["NotificationsWithSubscribers"]
    assert {n["Notification"]["NotificationType"] for n in notifications} == {"ACTUAL", "FORECASTED"}
    assert all(s["SubscriptionType"] == "EMAIL" for n in notifications for s in n["Subscribers"]), "alerts only: no actions that change resources"
    text = TEMPLATE_PATH.read_text(encoding="utf-8")
    assert "does not stop spending" in text and "SENDS ALERTS" in text
    assert "BudgetActions" not in text and "AWS::Budgets::BudgetsAction" not in text


def test_budget_email_pattern_rejects_garbage(template):
    pattern = re.compile(template["Parameters"]["BudgetAlertEmail"]["AllowedPattern"])
    for good in ("", "owner@example.com", "a.b+c@sub.example.org"):
        assert pattern.fullmatch(good), good
    for bad in ("not-an-email", "a@b", "a b@example.com", "a@example.com,b@example.com", "<x>@example.com"):
        assert not pattern.fullmatch(bad), bad


def test_template_still_has_no_wildcard_origin_default_and_no_api_gateway_cors(template):
    assert "Default" not in template["Parameters"]["AllowedOrigin"]
    api = template["Resources"]["HttpApi"]["Properties"]
    assert "CorsConfiguration" not in api
    assert api["DefaultRouteSettings"]["ThrottlingRateLimit"] == {"Ref": "ThrottleRatePerSecond"}


def test_deployment_guide_has_the_hardening_instructions_and_says_alerts_are_not_caps():
    text = (ROOT / "docs" / "deployment.md").read_text(encoding="utf-8")
    for needle in ("-MonthlyBudgetUsd", "-BudgetAlertEmail", "not a spending cap", "CLIENT_RATE_PER_MINUTE", "per container", "ORIGIN_NOT_ALLOWED", "415"):
        assert needle in text, f"docs/deployment.md should mention: {needle}"


def test_deploy_script_validates_budget_inputs_before_any_aws_call():
    script = (ROOT / "scripts" / "deploy.ps1").read_text(encoding="utf-8")
    assert "MonthlyBudgetUsd=$MonthlyBudgetUsd" in script and "BudgetAlertEmail=$BudgetAlertEmail" in script
    assert script.index("-MonthlyBudgetUsd needs -BudgetAlertEmail") < script.index('Say "== 6. sam deploy')
