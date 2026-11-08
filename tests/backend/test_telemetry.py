"""B10: telemetry and the cost model (backend/telemetry.py).

Local and deterministic. Nothing here calls AWS. `StubSdk` (from the B2 tests) imitates the Bedrock SDK, so token counts below are
SIMULATED inputs: they prove our bookkeeping, not what Bedrock bills. The comparison with real recorded tokens and the real price
sources is in docs/compliance/B10.md and docs/aws-costs.md, with the commands that were actually run.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from decimal import Decimal
from typing import Any

import pytest

from backend.app import Adapters, App, Request
from backend.config import load_settings
from backend.telemetry import (
    EVENTS,
    FIELDS,
    PRICES,
    SCENARIOS,
    Assumptions,
    CostModelError,
    ProfileStorage,
    RequestUsage,
    Step,
    build_record,
    emit,
    estimate_monthly_cost,
    record_model_call,
    request_usage,
    usd,
)
from tests.backend.helpers import ROOT, fixture
from tests.backend.test_extraction import StubSdk, canonical_facts, extractor, sdk_reply

LIVE = load_settings({"APP_MODE": "live", "COMMIT_SHA": "test123"})
AWS_KEY = "AKIAABCDEFGHIJKLMNOP"
AWS_SECRET = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
SESSION_TOKEN = "FAKESESSIONTOKENPREFIXFORTESTS0123456789" + "x" * 140
SIGNED_URL = "https://reports.s3.amazonaws.com/r.json?X-Amz-Signature=abcdef0123456789&X-Amz-Credential=AKIAABCDEFGHIJKLMNOP%2F20261107"
JSON_HEADERS = {"content-type": "application/json"}


@pytest.fixture
def lines(caplog):
    caplog.set_level(logging.INFO)

    def parsed(name_prefix: str = "trademind") -> list[dict[str, Any]]:
        out = []
        for record in caplog.records:
            if record.name.startswith(name_prefix):
                try:
                    out.append(json.loads(record.getMessage()))
                except ValueError:
                    pass
        return out

    return parsed


def extract_request() -> bytes:
    return json.dumps(fixture("extraction.json")["request"]).encode()


def live_app(*replies: Any) -> tuple[App, StubSdk]:
    ex, sdk = extractor(*replies)
    return App(LIVE, Adapters(extract=ex)), sdk


def post_extract(app: App, request_id: str = "r-1", client: str = "198.51.100.1"):
    return app.handle(Request("POST", "/extract", JSON_HEADERS, extract_request(), request_id, client=client))


# ================================================================== the emitter is an allowlist
def test_known_events_and_fields_are_written_unchanged():
    record = build_record("request", {"requestId": "abc-123", "route": "/optimize", "status": 200, "durationMs": 12.34, "errorCode": None, "mode": "live"})
    assert record == {"event": "request", "requestId": "abc-123", "route": "/optimize", "status": 200, "durationMs": 12.3, "errorCode": None, "mode": "live"}


def test_unknown_events_are_not_written_under_their_own_name():
    record = build_record("user_pasted_this", {"requestId": "r"})
    assert record["event"] == "telemetry_rejected" and "requestId" not in record


def test_unknown_fields_are_dropped_and_counted():
    record = build_record("request", {"requestId": "r", "description": "We need a design deliverable.", "scenario": {"a": 1}, "authorization": "Bearer x"})
    assert set(record) == {"event", "requestId", "telemetryDropped"} and record["telemetryDropped"] == 3


@pytest.mark.parametrize(
    "value",
    [
        "We need a design deliverable and can spare 10 hours.",  # a sentence
        "DevCo Ltd",  # a business name with a space
        SIGNED_URL,  # a signed URL
        "https://example.com/x",  # any URL
        "line one\nline two",
        "a" * 201,  # too long to be a token
        "",
        ["a", "b"],
        {"k": "v"},
        b"bytes",
        object(),
    ],
)
def test_a_value_that_could_carry_content_is_replaced_even_in_an_allowed_field(value):
    record = build_record("request", {"requestId": "r", "route": value})
    assert record["route"] == "[invalid]" and record["telemetryDropped"] == 1


def test_numbers_must_be_finite_and_bounded():
    record = build_record("request", {"durationMs": float("nan"), "status": 10**15, "modelMs": float("inf")})
    assert record["durationMs"] is None and record["status"] is None and record["modelMs"] is None and record["telemetryDropped"] == 3


@pytest.mark.parametrize("secret", [AWS_KEY, AWS_SECRET, SESSION_TOKEN, "password=hunter2hunter2"])
def test_a_secret_that_looks_like_a_token_is_still_redacted_in_the_written_line(secret, caplog):
    caplog.set_level(logging.INFO)
    emit(logging.getLogger("trademind.api"), "request", requestId=secret, route="/health")
    assert secret not in caplog.text, caplog.text


def test_emit_never_raises_even_with_hostile_input():
    class Hostile:
        def __str__(self):
            raise RuntimeError("no")

    emit(logging.getLogger("trademind.api"), "request", requestId=Hostile(), route={"a": Hostile()})
    emit(logging.getLogger("trademind.api"), "no_such_event")


def test_field_and_event_lists_hold_no_content_bearing_names():
    banned = ("text", "description", "name", "body", "scenario", "prompt", "message", "url", "secret", "token", "key", "authorization")
    for field in FIELDS:
        lowered = field.lower()
        assert not any(b in lowered for b in banned if b != "token") or field in {"inputTokens", "outputTokens"}, field
    assert "request" in EVENTS and "bedrock_call" in EVENTS


# ================================================================== per-request model usage
def test_a_request_line_reports_the_tokens_the_sdk_returned(lines):
    app, sdk = live_app(sdk_reply(canonical_facts()))
    response = post_extract(app, request_id="trace-1")
    assert response.status == 200 and len(sdk.calls) == 1
    line = next(l for l in lines("trademind.api") if l.get("event") == "request" and l["requestId"] == "trace-1")
    assert line["route"] == "/extract" and line["status"] == 200 and line["mode"] == "live"
    assert (line["modelCalls"], line["modelErrors"], line["inputTokens"], line["outputTokens"]) == (1, 0, 321, 123)
    assert line["modelMs"] >= 0 and line["durationMs"] >= line["modelMs"] - 1
    call = next(l for l in lines("trademind.bedrock") if l["event"] == "bedrock_call")
    assert (call["inputTokens"], call["outputTokens"], call["outcome"]) == (321, 123, "ok"), "the per-call line and the request line agree"
    app.close()


def test_a_request_with_no_model_call_reports_zero_calls_and_zero_tokens(lines):
    from backend.confirmation import confirm_scenario

    app = App(LIVE, Adapters(confirm=confirm_scenario))
    response = app.handle(Request("POST", "/confirm", JSON_HEADERS, json.dumps({"scenario": fixture("canonical.json")}).encode(), "no-model", client="c"))
    line = next(l for l in lines("trademind.api") if l.get("requestId") == "no-model" and l["event"] == "request")
    assert response.status == 200 and (line["modelCalls"], line["inputTokens"], line["outputTokens"], line["modelMs"]) == (0, 0, 0, 0.0)
    app.close()


def test_tokens_the_sdk_did_not_supply_are_unknown_not_zero(lines):
    reply = sdk_reply(canonical_facts())
    del reply["usage"]
    app, _ = live_app(reply)
    post_extract(app, request_id="no-usage")
    line = next(l for l in lines("trademind.api") if l.get("requestId") == "no-usage" and l["event"] == "request")
    assert line["modelCalls"] == 1 and line["inputTokens"] is None and line["outputTokens"] is None, "unknown stays null"
    app.close()


def test_a_failed_model_call_is_counted_as_an_error_with_no_invented_tokens(lines):
    from tests.backend.test_extraction import client_error

    app, _ = live_app(client_error("AccessDeniedException"))
    response = post_extract(app, request_id="denied")
    line = next(l for l in lines("trademind.api") if l.get("requestId") == "denied" and l["event"] == "request")
    assert response.status == 502 and line["errorCode"] == "MODEL_ACCESS_DENIED"
    assert (line["modelCalls"], line["modelErrors"], line["inputTokens"], line["outputTokens"]) == (1, 1, 0, 0)
    call = next(l for l in lines("trademind.bedrock") if l["event"] == "bedrock_call")
    assert call["outcome"] == "error" and call["errorCode"] == "AccessDeniedException" and "arn:" not in json.dumps(call)
    app.close()


def test_a_repair_retry_sums_both_model_calls(lines):
    bad = canonical_facts()
    bad["businesses"][0]["id"] = "evilco"
    app, sdk = live_app(sdk_reply(bad), sdk_reply(canonical_facts()))
    response = post_extract(app, request_id="repair")
    line = next(l for l in lines("trademind.api") if l.get("requestId") == "repair" and l["event"] == "request")
    assert response.status == 200 and len(sdk.calls) == 2
    assert (line["modelCalls"], line["inputTokens"], line["outputTokens"]) == (2, 642, 246), "cost is per call, so both are counted"
    app.close()


def test_concurrent_requests_never_share_usage(lines):
    app_a, _ = live_app(sdk_reply(canonical_facts()))
    reply_b = sdk_reply(canonical_facts())
    reply_b["usage"] = {"inputTokens": 1000, "outputTokens": 2000}
    app_b, _ = live_app(reply_b)
    barrier = threading.Barrier(2)

    def run(app: App, rid: str) -> None:
        barrier.wait()
        post_extract(app, request_id=rid, client=rid)

    threads = [threading.Thread(target=run, args=(app_a, "conc-a")), threading.Thread(target=run, args=(app_b, "conc-b"))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    by_id = {l["requestId"]: l for l in lines("trademind.api") if l["event"] == "request"}
    assert (by_id["conc-a"]["inputTokens"], by_id["conc-a"]["outputTokens"]) == (321, 123)
    assert (by_id["conc-b"]["inputTokens"], by_id["conc-b"]["outputTokens"]) == (1000, 2000)
    app_a.close()
    app_b.close()


def test_record_model_call_outside_a_request_is_a_harmless_no_op():
    record_model_call(input_tokens=5, output_tokens=5, latency_ms=1.0, ok=True)


def test_request_usage_unit_rules():
    with request_usage() as usage:
        record_model_call(input_tokens=10, output_tokens=None, latency_ms=5.0, ok=True)
        record_model_call(input_tokens=None, output_tokens=None, latency_ms=3.0, ok=False)
    snap = usage.snapshot()
    assert snap == {"modelCalls": 2, "modelErrors": 1, "modelMs": 8.0, "inputTokens": 10, "outputTokens": None}
    assert RequestUsage().snapshot()["inputTokens"] == 0


# ================================================================== telemetry carries no content (the acceptance sweep)
def test_nothing_logged_during_a_full_live_extraction_contains_descriptions_names_credentials_or_urls(caplog):
    caplog.set_level(logging.INFO)
    data = fixture("extraction.json")["request"]
    app, _ = live_app(sdk_reply(canonical_facts()))
    app.handle(Request("POST", "/extract", {**JSON_HEADERS, "authorization": f"Bearer {SESSION_TOKEN}", "x-api-key": AWS_KEY}, json.dumps(data).encode(), "sweep", client="203.0.113.5"))
    app.handle(Request("POST", "/extract", JSON_HEADERS, b"{not json " + AWS_SECRET.encode(), "sweep-2", client="203.0.113.5"))
    text = caplog.text
    assert "request" in text and "bedrock_call" in text, "the sweep must have something to sweep"
    for description in data["descriptions"]:
        assert description["text"] not in text and description["name"] not in text
        for sentence in re.split(r"(?<=[.!?])\s+", description["text"]):
            assert len(sentence) < 20 or sentence not in text
    for secret in (AWS_KEY, AWS_SECRET, SESSION_TOKEN, "203.0.113.5"):
        assert secret not in text, f"{secret[:8]}... found in a log line"
    assert "http://" not in text and "https://" not in text and "X-Amz-Signature" not in text
    app.close()


def test_every_line_written_by_the_app_and_bedrock_loggers_uses_only_allowlisted_fields(caplog):
    caplog.set_level(logging.INFO)
    app, _ = live_app(sdk_reply(canonical_facts()))
    post_extract(app, request_id="shape")
    app.handle(Request("GET", "/nope", {}, b"", "shape-2", client="c"))
    for record in caplog.records:
        if record.name in ("trademind.api", "trademind.bedrock"):
            line = json.loads(record.getMessage())
            assert set(line) <= FIELDS, set(line) - FIELDS
            assert line["event"] in EVENTS
            for value in line.values():
                assert not isinstance(value, (list, dict))
    app.close()


# ================================================================== cost arithmetic
P = PRICES["claude-haiku-4-5-us-profile"]


def one_step(name="x", calls=1, ms=0, tin=0, tout=0, basis="assumed") -> Step:
    return Step(name, Decimal(calls), Decimal(ms), tin, tout, basis)


def bare(steps, sessions=1, **kw) -> Assumptions:
    defaults = dict(log_bytes_per_request=0, builds_per_month=0, site_storage_mb=Decimal(0), page_loads_per_session=Decimal(0))
    defaults.update(kw)
    return Assumptions("t", sessions, tuple(steps), **defaults)


def test_bedrock_tokens_are_priced_per_million():
    cost = estimate_monthly_cost(bare([one_step(tin=1_000_000, tout=1_000_000)]), P)
    assert cost.items["Bedrock input tokens"] == Decimal("1.10") and cost.items["Bedrock output tokens"] == Decimal("5.50")
    half = estimate_monthly_cost(bare([one_step(tin=500_000, tout=250_000)]), P)
    assert half.items["Bedrock input tokens"] == Decimal("0.55") and half.items["Bedrock output tokens"] == Decimal("1.375")


def test_lambda_duration_uses_memory_in_gb_times_seconds():
    # 1000 requests x 2 s x 0.5 GB = 1000 GB-s; at 0.0000166667 that is 0.0166667
    cost = estimate_monthly_cost(bare([one_step(calls=1000, ms=2000)], memory_mb=512), P)
    assert cost.items["Lambda duration"] == Decimal(1000) * Decimal("0.0000166667")
    assert cost.items["Lambda requests"] == Decimal(1000) * Decimal("0.0000002")
    assert cost.items["API Gateway HTTP API requests"] == Decimal(1000) * Decimal("0.000001")
    double = estimate_monthly_cost(bare([one_step(calls=1000, ms=2000)], memory_mb=1024), P)
    assert double.items["Lambda duration"] == cost.items["Lambda duration"] * 2


def test_cold_starts_add_billed_duration():
    base = estimate_monthly_cost(bare([one_step(calls=10, ms=100)]), P)
    cold = estimate_monthly_cost(bare([one_step(calls=10, ms=100)], cold_starts_per_month=10, cold_start_billed_ms=Decimal(600)), P)
    assert cold.items["Lambda duration"] - base.items["Lambda duration"] == Decimal(10) * Decimal("0.6") * Decimal("0.5") * P.lambda_per_gb_second


def test_logs_are_priced_on_ingested_gb_and_on_retention():
    a = bare([one_step(calls=1024)], log_bytes_per_request=1024**2, log_retention_days=30)  # 1024 x 1 MiB = exactly 1 GB
    cost = estimate_monthly_cost(a, P)
    assert cost.items["CloudWatch Logs ingestion"] == Decimal("0.50") and cost.items["CloudWatch Logs storage"] == Decimal("0.03")
    short = estimate_monthly_cost(bare([one_step(calls=1024)], log_bytes_per_request=1024**2, log_retention_days=15), P)
    assert short.items["CloudWatch Logs storage"] == Decimal("0.015")


def test_amplify_lines():
    a = bare([], sessions=1000, builds_per_month=10, build_minutes=Decimal(3), site_storage_mb=Decimal(1024), page_loads_per_session=Decimal(2), page_transfer_mb=Decimal(512))
    cost = estimate_monthly_cost(a, P)
    assert cost.items["Amplify build minutes"] == Decimal("0.30")
    assert cost.items["Amplify hosting storage"] == Decimal("0.023")
    assert cost.items["Amplify data served"] == Decimal(1000) * 2 * Decimal(512) / 1024 * Decimal("0.15") == Decimal("150")


def test_dynamodb_lines_exist_only_when_profiles_are_enabled():
    assert not any("DynamoDB" in k for k in estimate_monthly_cost(bare([one_step()]), P).items)
    profiles = ProfileStorage(puts_per_session=Decimal(1), gets_per_session=Decimal(2), item_kb=3, stored_gb=Decimal(30))
    cost = estimate_monthly_cost(bare([one_step()], sessions=1_000_000, profiles=profiles), P)
    assert cost.items["DynamoDB profile writes"] == Decimal(1_000_000) * 3 * Decimal("0.000000625")
    assert cost.items["DynamoDB profile reads"] == Decimal(1_000_000) * 2 * 1 * Decimal("0.000000125"), "3 KB rounds up to one 4 KB read unit"
    assert cost.items["DynamoDB profile storage"] == Decimal(5) * Decimal("0.25"), "the first 25 GB are free in the price list"


def test_the_total_is_the_sum_of_the_lines_and_zero_usage_costs_nothing_but_fixed_items():
    cost = estimate_monthly_cost(SCENARIOS["demo"], PRICES["nova-2-lite"])
    assert cost.total == sum(cost.items.values())
    empty = estimate_monthly_cost(bare([], sessions=0), P)
    assert empty.total == 0


def test_demo_scenario_matches_an_independent_hand_calculation():
    """A second arithmetic path written in the test, not in the module, from the same assumptions."""
    a = SCENARIOS["demo"]
    sessions = 100
    model_steps = [(1, 694, 386), (1, 600, 200), (1, 500, 150)]  # extract (measured), explain, text proposal (assumed)
    tin = sum(c * i for c, i, _ in model_steps) * sessions
    tout = sum(c * o for c, _, o in model_steps) * sessions
    bedrock = Decimal(tin) / 1_000_000 * Decimal("1.10") + Decimal(tout) / 1_000_000 * Decimal("5.50")
    requests = 7 * sessions  # health, extract, confirm, optimize, explain, simulate x2
    busy_ms = (3 + 1500 + 3 + 5 + 1500 + 1500 + 5) * sessions + 30 * 3300  # 30 cold starts at the measured 3300 ms
    lam = Decimal(busy_ms) / 1000 * Decimal("0.5") * Decimal("0.0000166667") + Decimal(requests) * Decimal("0.0000002")
    api = Decimal(requests) * Decimal("0.000001")
    cost = estimate_monthly_cost(a, P)
    assert cost.input_tokens == tin == 179_400 and cost.output_tokens == tout == 73_600
    assert cost.items["Bedrock input tokens"] + cost.items["Bedrock output tokens"] == bedrock
    assert cost.items["Lambda duration"] + cost.items["Lambda requests"] == lam
    assert cost.items["API Gateway HTTP API requests"] == api
    assert cost.api_requests == requests and cost.model_requests == 3 * sessions


def test_money_is_rounded_half_up_to_cents_only_for_display():
    assert usd(Decimal("0.005")) == Decimal("0.01") and usd(Decimal("0.0049")) == Decimal("0.00") and usd(Decimal("1.2350")) == Decimal("1.24")
    raw = estimate_monthly_cost(SCENARIOS["demo"], P)
    assert any(v != usd(v) for v in raw.items.values()), "stored values keep full precision; only the report rounds"


def test_the_estimate_refuses_volumes_beyond_the_first_pricing_tier():
    huge = bare([one_step(calls=10**9, ms=100)], sessions=1)
    with pytest.raises(CostModelError):
        estimate_monthly_cost(huge, P)
    with pytest.raises(CostModelError):
        estimate_monthly_cost(bare([one_step()], memory_mb=0), P)


def test_a_more_expensive_model_never_gives_a_cheaper_total():
    nova = estimate_monthly_cost(SCENARIOS["busy"], PRICES["nova-2-lite"]).total
    us = estimate_monthly_cost(SCENARIOS["busy"], PRICES["claude-haiku-4-5-us-profile"]).total
    global_ = estimate_monthly_cost(SCENARIOS["busy"], PRICES["claude-haiku-4-5-global-profile"]).total
    assert nova < global_ < us


def test_cost_scales_linearly_with_sessions_for_the_usage_lines():
    one = estimate_monthly_cost(SCENARIOS["demo"], P)
    ten = estimate_monthly_cost(
        Assumptions("x", 1000, SCENARIOS["demo"].steps, cold_starts_per_month=0, builds_per_month=0, site_storage_mb=Decimal(0)), P
    )
    hundred = estimate_monthly_cost(
        Assumptions("x", 100, SCENARIOS["demo"].steps, cold_starts_per_month=0, builds_per_month=0, site_storage_mb=Decimal(0)), P
    )
    assert ten.items["Bedrock input tokens"] == hundred.items["Bedrock input tokens"] * 10
    assert one.total > 0


# ================================================================== the price table and the worksheet agree
def test_price_table_records_where_it_came_from():
    for name, prices in PRICES.items():
        assert prices.retrieved == "2026-11-07" and "Price List" in prices.sources, name
        assert prices.bedrock_output_per_million_tokens > prices.bedrock_input_per_million_tokens, "output tokens cost more than input"
    assert PRICES["claude-haiku-4-5-us-profile"].bedrock_input_per_million_tokens == PRICES["claude-haiku-4-5-global-profile"].bedrock_input_per_million_tokens * Decimal("1.1")


def test_the_worksheet_documents_every_rate_the_model_uses_and_the_totals_it_computes():
    text = (ROOT / "docs" / "aws-costs.md").read_text(encoding="utf-8")
    for needle in ("2026-11-07", "Price List", "free tier", "not a bill", "us-east-1", "UNKNOWN", "measured", "assumed", "694", "386"):
        assert needle in text, f"docs/aws-costs.md should mention: {needle}"
    for price in ("1.10", "5.50", "0.0000166667", "0.20", "1.00", "0.50", "0.03", "0.01", "0.023", "0.15", "0.33", "2.75", "0.625", "0.125"):
        assert price in text, f"rate {price} is used by the model but not documented"
    for model, label in (("claude-haiku-4-5-us-profile", "Claude Haiku 4.5 (us. profile)"), ("nova-2-lite", "Amazon Nova 2 Lite")):
        for key in ("demo", "busy"):
            total = usd(estimate_monthly_cost(SCENARIOS[key], PRICES[model]).total)
            assert f"${total:,}" in text, f"the worksheet's total for {label} / {key} (${total:,}) is out of date; rerun python -m backend.telemetry"


def test_the_runbook_covers_the_failure_modes_seen_in_this_project():
    text = (ROOT / "docs" / "aws-costs.md").read_text(encoding="utf-8")
    for needle in ("MODEL_ACCESS_DENIED", "THROTTLED", "ORIGIN_NOT_ALLOWED", "Logs Insights", "inputTokens", "REPORT", "aws-marketplace"):
        assert needle in text, f"runbook should mention: {needle}"
