"""B1: the router, API Gateway v2 events and the local HTTP adapter.

Every adapter in this file is a TEST DOUBLE. The canned optimize answer below is hand-written for
the canonical fixture so the router can be tested; it is not solver output and proves nothing about
optimization. These tests prove routing, validation, error mapping, limits, CORS and logging.
They prove nothing about AWS, Bedrock or a deployed stack.
"""

from __future__ import annotations

import base64
import dataclasses
import http.client
import json
import logging
import threading
import time

import pytest

from backend.app import MAX_BODY_BYTES, Adapters, App, Request, lambda_handler
from backend.config import Settings, load_settings
from backend.errors import ModelFailure, Throttled, Unavailable
from backend.local import make_handler
from contracts.models import (
    CertificateResponse,
    ConfirmResponse,
    OptimizeRequest,
    OptimizeResponse,
    ProfileResponse,
    SimulateRequest,
    SimulateResponse,
)
from http.server import ThreadingHTTPServer
from tests.backend.helpers import fixture, validator

ALLOWED = "http://127.0.0.1:5173"
FIXTURE_SETTINGS = load_settings({"COMMIT_SHA": "test123"})
LIVE_SETTINGS = load_settings({"APP_MODE": "live", "COMMIT_SHA": "test123"})
ENVELOPE_KEYS = {"code", "message", "fields", "requestId"}
SECRET = "SECRET-MARKER-9f3a"


# ------------------------------------------------------------------ helpers
@pytest.fixture
def canonical():
    return fixture("canonical.json")


def new_app(settings: Settings = FIXTURE_SETTINGS, adapters: Adapters | None = None) -> App:
    return App(settings, adapters)


def call(app: App, method: str, path: str, body=None, headers=None, request_id="req-1", **kw):
    raw = body if isinstance(body, bytes) else (b"" if body is None else json.dumps(body).encode())
    return app.handle(Request(method, path, {k.lower(): v for k, v in (headers or {}).items()}, raw, request_id, **kw))


def assert_envelope(response, status, code):
    assert response.status == status, response.body
    assert set(response.body) == {"error"}
    assert set(response.body["error"]) == ENVELOPE_KEYS
    assert response.body["error"]["code"] == code
    assert isinstance(response.body["error"]["fields"], list)
    assert response.body["error"]["requestId"] == response.headers["X-Request-Id"]
    assert list(validator("api.schema.json", "ErrorEnvelope").iter_errors(response.body)) == []


def canned_optimize_response(mode: str = "fixture") -> OptimizeResponse:
    """TEST DOUBLE: hand-written figures from the frozen contract's canonical example. Not solver output."""
    metrics = [
        {
            "businessId": b,
            "baselineMinutes": 480,
            "productionMinutes": 180,
            "coordinationMinutes": 0,
            "effortMinutes": 180,
            "netSavingsMinutes": 300,
        }
        for b in ("devco", "designco", "copyco")
    ]
    return OptimizeResponse.model_validate(
        {
            "mode": mode,
            "result": {
                "schemaVersion": "1",
                "status": "collaboration",
                "assignments": [
                    {"deliverableId": "d-coding", "providerId": "devco", "minutes": 180},
                    {"deliverableId": "d-design", "providerId": "designco", "minutes": 180},
                    {"deliverableId": "d-writing", "providerId": "copyco", "minutes": 180},
                ],
                "businessMetrics": metrics,
                "totals": {
                    "baselineMinutes": 1440,
                    "productionMinutes": 540,
                    "coordinationMinutes": 0,
                    "effortMinutes": 540,
                    "grossSavingsMinutes": 900,
                    "netSavingsMinutes": 900,
                },
                "reasons": [],
                "certificate": {
                    "schemaVersion": "1",
                    "scenarioDigest": "a" * 64,
                    "allocationDigest": "b" * 64,
                    "verification": "valid",
                    "assumptions": ["test double"],
                    "checks": [{"name": "test double", "passed": True}],
                },
            },
        }
    )


class Recorder:
    """Records what the router passed to an adapter."""

    def __init__(self, result):
        self.result = result
        self.calls: list[tuple] = []

    def __call__(self, *args):
        self.calls.append(args)
        return self.result(*args) if callable(self.result) else self.result


# ------------------------------------------------------------------ health
def test_health_works_with_no_adapters_in_both_modes():
    for settings, mode in ((FIXTURE_SETTINGS, "fixture"), (LIVE_SETTINGS, "live")):
        app = new_app(settings)
        response = call(app, "GET", "/health")
        assert response.status == 200
        assert response.body == {"status": "ok", "schemaVersion": "1", "commit": "test123", "mode": mode}
        assert list(validator("api.schema.json", "HealthResponse").iter_errors(response.body)) == []
        assert response.headers["X-TradeMind-Mode"] == mode
        app.close()


def test_every_response_carries_safe_headers_and_matching_request_id():
    response = call(new_app(), "GET", "/health", request_id="abc-123")
    assert response.headers["X-Request-Id"] == "abc-123"
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Content-Type"] == "application/json"


def test_request_id_is_generated_when_missing_and_sanitised_when_hostile():
    generated = call(new_app(), "GET", "/health", request_id="")
    assert len(generated.headers["X-Request-Id"]) == 32
    hostile = call(new_app(), "GET", "/nope", request_id="a b\r\nSet-Cookie: x=1" + "z" * 300)
    assert "\n" not in hostile.headers["X-Request-Id"] and " " not in hostile.headers["X-Request-Id"]
    assert len(hostile.headers["X-Request-Id"]) <= 128
    assert hostile.body["error"]["requestId"] == hostile.headers["X-Request-Id"]


# ------------------------------------------------------------------ 404 / 405 / OPTIONS
@pytest.mark.parametrize(
    "path",
    ["/nope", "/health/", "/Health", "//health", "/", "/profiles/", "/profiles/a/b", "/profiles/bad id", "/profiles/a.b"],
)
def test_unknown_routes_are_404_with_exact_envelope(path):
    assert_envelope(call(new_app(), "GET", path), 404, "NOT_FOUND")


def test_wrong_method_is_405_and_lists_allowed_methods():
    response = call(new_app(), "POST", "/health")
    assert_envelope(response, 405, "METHOD_NOT_ALLOWED")
    assert response.headers["Allow"] == "GET, OPTIONS"
    assert_envelope(call(new_app(), "GET", "/optimize"), 405, "METHOD_NOT_ALLOWED")
    assert_envelope(call(new_app(), "DELETE", "/profiles/abc"), 405, "METHOD_NOT_ALLOWED")


def test_options_preflight_for_allowed_origin():
    response = call(new_app(), "OPTIONS", "/optimize", headers={"Origin": ALLOWED})
    assert response.status == 204 and response.body_bytes() == b""
    assert response.headers["Access-Control-Allow-Origin"] == ALLOWED
    assert response.headers["Access-Control-Allow-Methods"] == "OPTIONS, POST"
    assert response.headers["Access-Control-Allow-Headers"] == "Content-Type"
    assert response.headers["Access-Control-Max-Age"] == "600"
    assert "*" not in response.headers.values()


def test_options_for_profiles_lists_get_and_put():
    response = call(new_app(), "OPTIONS", "/profiles/abc", headers={"Origin": ALLOWED})
    assert response.headers["Access-Control-Allow-Methods"] == "GET, OPTIONS, PUT"


@pytest.mark.parametrize("origin", ["https://evil.example", "http://127.0.0.1:5173.evil.example", "null", "*"])
def test_options_and_responses_give_unlisted_origins_no_cors_headers(origin):
    for method, path in (("OPTIONS", "/optimize"), ("GET", "/health")):
        response = call(new_app(), method, path, headers={"Origin": origin})
        assert not any(name.startswith("Access-Control-") for name in response.headers)


def test_options_on_unknown_route_is_404():
    assert_envelope(call(new_app(), "OPTIONS", "/nope", headers={"Origin": ALLOWED}), 404, "NOT_FOUND")


def test_cors_header_is_present_on_error_responses_too():
    response = call(new_app(), "GET", "/nope", headers={"Origin": ALLOWED})
    assert response.headers["Access-Control-Allow-Origin"] == ALLOWED
    assert "X-Request-Id" in response.headers["Access-Control-Expose-Headers"]


# ------------------------------------------------------------------ 503 not implemented (default adapters)
ROUTES_WITH_ADAPTERS = [
    ("POST", "/extract"),
    ("POST", "/confirm"),
    ("POST", "/optimize"),
    ("POST", "/simulate"),
    ("POST", "/explain"),
    ("POST", "/certificates"),
    ("PUT", "/profiles/abc123"),
    ("GET", "/profiles/abc123"),
]


@pytest.mark.parametrize("settings", [FIXTURE_SETTINGS, LIVE_SETTINGS], ids=["fixture", "live"])
@pytest.mark.parametrize(("method", "path"), ROUTES_WITH_ADAPTERS)
def test_missing_adapter_is_503_in_both_modes_never_simulated(settings, method, path, canonical):
    app = new_app(settings)  # default: no adapters
    response = call(app, method, path, {"scenario": canonical})
    assert_envelope(response, 503, "NOT_IMPLEMENTED")
    assert "result" not in response.body and "assignments" not in json.dumps(response.body)
    app.close()


def test_missing_adapter_is_reported_before_the_body_is_parsed():
    # Garbage body, but the route has no adapter: the honest answer is 503, not a parse error.
    assert_envelope(call(new_app(), "POST", "/optimize", b"not json"), 503, "NOT_IMPLEMENTED")


# ------------------------------------------------------------------ 400 malformed JSON
@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"   ",
        b"{not json",
        b'{"scenario": ',
        b"\xff\xfe\x00bad",
        b'{"scenario": NaN}',
        b'{"a": 1, "a": 2}',
        b"[" * 30000,
    ],
    ids=["empty", "blank", "syntax", "truncated", "not-utf8", "nan", "duplicate-key", "deeply-nested"],
)
def test_malformed_json_is_400(body):
    app = new_app(adapters=Adapters(optimize=Recorder(canned_optimize_response())))
    response = call(app, "POST", "/optimize", body)
    assert_envelope(response, 400, "MALFORMED_JSON")
    assert app.adapters.optimize.calls == [], "adapter must not run on malformed input"


def test_malformed_json_message_never_echoes_the_input():
    app = new_app(adapters=Adapters(optimize=Recorder(canned_optimize_response())))
    response = call(app, "POST", "/optimize", ('{"x": "%s"' % SECRET).encode())
    assert SECRET not in json.dumps(response.body)


# ------------------------------------------------------------------ 422 validation
def adapters_for_validation() -> Adapters:
    return Adapters(
        optimize=Recorder(canned_optimize_response()),
        simulate=Recorder(None),
        extract=Recorder(None),
    )


def test_body_must_be_an_object_with_the_required_fields():
    app = new_app(adapters=adapters_for_validation())
    for body in ([], "text", 5, None, {}, {"scenario": None}, {"scenario": {}}):
        response = call(app, "POST", "/optimize", json.dumps(body).encode())
        assert_envelope(response, 422, "VALIDATION_ERROR")
    assert app.adapters.optimize.calls == []


def test_unknown_fields_are_rejected_at_every_level(canonical):
    app = new_app(adapters=adapters_for_validation())
    top = {"scenario": canonical, "extra": 1}
    deep = json.loads(json.dumps(canonical))
    deep["businesses"][0]["profit"] = 5
    for body in (top, {"scenario": deep}):
        response = call(app, "POST", "/optimize", body)
        assert_envelope(response, 422, "VALIDATION_ERROR")
    assert any("extra" in f for f in call(app, "POST", "/optimize", top).body["error"]["fields"])


@pytest.mark.parametrize(
    ("mutation", "field_hint"),
    [
        (lambda s: s["businesses"][0].__setitem__("capacityMinutes", 600.5), "capacityMinutes"),
        (lambda s: s["businesses"][0].__setitem__("capacityMinutes", "600"), "capacityMinutes"),
        (lambda s: s["businesses"][0].__setitem__("capacityMinutes", True), "capacityMinutes"),
        (lambda s: s["businesses"][0].__setitem__("capacityMinutes", -1), "capacityMinutes"),
        (lambda s: s["businesses"][0].__setitem__("capacityMinutes", 1000001), "capacityMinutes"),
        (lambda s: s["businesses"][0]["estimates"][0].__setitem__("minutes", 0), "minutes"),
        (lambda s: s["deliverables"][0].__setitem__("service", "legal"), "service"),
        (lambda s: s.__setitem__("confirmed", False), "confirmed"),
        (lambda s: s.__setitem__("schemaVersion", "2"), "schemaVersion"),
        (lambda s: s["businesses"].pop(), "businesses"),
        (lambda s: s["businesses"].append(dict(s["businesses"][0], id="extraco")), "businesses"),
        (lambda s: s["businesses"][0].__setitem__("id", "DevCo"), "id"),
    ],
)
def test_invalid_scenario_is_422_with_field_paths(mutation, field_hint, canonical):
    app = new_app(adapters=adapters_for_validation())
    broken = json.loads(json.dumps(canonical))
    mutation(broken)
    response = call(app, "POST", "/optimize", {"scenario": broken})
    assert_envelope(response, 422, "VALIDATION_ERROR")
    fields = response.body["error"]["fields"]
    assert fields and all(f.startswith("scenario") for f in fields)
    assert any(field_hint in f for f in fields), fields
    assert app.adapters.optimize.calls == [], "invalid input must never reach an adapter"


def test_422_reports_paths_only_never_the_offending_values(canonical):
    app = new_app(adapters=adapters_for_validation())
    broken = json.loads(json.dumps(canonical))
    broken["businesses"][0]["name"] = 12345  # wrong type
    broken["businesses"][1]["id"] = SECRET  # fails the slug pattern
    response = call(app, "POST", "/optimize", {"scenario": broken})
    assert_envelope(response, 422, "VALIDATION_ERROR")
    assert SECRET not in json.dumps(response.body)
    assert "12345" not in json.dumps(response.body)


def test_422_field_list_is_bounded(canonical):
    app = new_app(adapters=adapters_for_validation())
    broken = {"scenario": {"schemaVersion": "1", "confirmed": True, "businesses": [{}] * 3, "deliverables": [{}] * 6}}
    response = call(app, "POST", "/optimize", broken)
    assert_envelope(response, 422, "VALIDATION_ERROR")
    assert 0 < len(response.body["error"]["fields"]) <= 20


def test_simulate_rejects_text_together_with_changes_or_apply(canonical):
    app = new_app(adapters=adapters_for_validation())
    change = {"kind": "capacity", "businessId": "devco", "minutes": 300}
    both = {"scenario": canonical, "changes": [change], "text": "devco loses half a day", "apply": False}
    applied = {"scenario": canonical, "changes": [], "text": "devco loses half a day", "apply": True}
    response = call(app, "POST", "/simulate", both)
    assert_envelope(response, 422, "VALIDATION_ERROR")
    assert response.body["error"]["fields"] == ["text", "changes"]
    response = call(app, "POST", "/simulate", applied)
    assert_envelope(response, 422, "VALIDATION_ERROR")
    assert response.body["error"]["fields"] == ["text", "apply"]
    assert app.adapters.simulate.calls == [], "the model must never see an invalid proposal request"


@pytest.mark.parametrize(
    "change",
    [
        {"kind": "delete", "businessId": "devco", "minutes": 1},
        {"path": "businesses[0].capacityMinutes", "value": 1},
        {"kind": "estimate", "businessId": "devco", "minutes": 100},
        {"kind": "capacity", "businessId": "devco", "minutes": 1.5},
        {"kind": "capacity", "businessId": "devco", "minutes": 5, "code": "print(1)"},
    ],
)
def test_simulate_only_accepts_the_tagged_change_shapes(change, canonical):
    app = new_app(adapters=adapters_for_validation())
    response = call(app, "POST", "/simulate", {"scenario": canonical, "changes": [change], "apply": True})
    assert_envelope(response, 422, "VALIDATION_ERROR")
    assert app.adapters.simulate.calls == []


# ------------------------------------------------------------------ payload limit
def test_body_over_32_kib_is_413_before_any_parsing(canonical):
    app = new_app(adapters=Adapters(optimize=Recorder(canned_optimize_response())))
    assert MAX_BODY_BYTES == 32 * 1024
    response = call(app, "POST", "/optimize", b"x" * (MAX_BODY_BYTES + 1))
    assert_envelope(response, 413, "PAYLOAD_TOO_LARGE")
    assert app.adapters.optimize.calls == []


def test_413_wins_over_missing_adapter_and_bad_json():
    assert_envelope(call(new_app(), "POST", "/optimize", b"x" * (MAX_BODY_BYTES + 1)), 413, "PAYLOAD_TOO_LARGE")


def test_body_exactly_at_the_limit_is_not_a_size_error():
    app = new_app(adapters=Adapters(optimize=Recorder(canned_optimize_response())))
    response = call(app, "POST", "/optimize", b" " * (MAX_BODY_BYTES - 2) + b"{}")
    assert response.status == 422  # parsed, then failed schema validation: the size check passed


def test_declared_length_over_the_limit_is_413_even_with_no_body():
    app = new_app(adapters=Adapters(optimize=Recorder(canned_optimize_response())))
    assert_envelope(call(app, "POST", "/optimize", b"", declared_length=MAX_BODY_BYTES + 1), 413, "PAYLOAD_TOO_LARGE")


# ------------------------------------------------------------------ adapters: success paths (test doubles)
def test_optimize_with_the_canonical_scenario_flows_validate_adapt_validate(canonical):
    recorder = Recorder(canned_optimize_response())
    app = new_app(adapters=Adapters(optimize=recorder))
    response = call(app, "POST", "/optimize", {"scenario": canonical})
    assert response.status == 200
    assert list(validator("api.schema.json", "OptimizeResponse").iter_errors(response.body)) == []
    assert response.body["mode"] == "fixture"
    assert response.body["result"]["totals"]["baselineMinutes"] == 1440
    (request_model,) = recorder.calls[0]
    assert isinstance(request_model, OptimizeRequest), "adapters receive validated models, not raw dicts"
    assert request_model.scenario.businesses[0].id.root == "devco"


def test_adapter_may_return_a_plain_dict(canonical):
    app = new_app(adapters=Adapters(optimize=Recorder(canned_optimize_response().model_dump(mode="json"))))
    assert call(app, "POST", "/optimize", {"scenario": canonical}).status == 200


def test_confirm_simulate_certificates_and_profiles_use_their_own_adapters(canonical):
    scenario = canonical
    confirm = Recorder(lambda req: ConfirmResponse.model_validate({"scenario": scenario}))
    simulate = Recorder(
        lambda req: SimulateResponse.model_validate(
            {
                "proposedChanges": [],
                "requiresConfirmation": True,
                "scenario": scenario,
                "result": None,
                "mode": "fixture",
            }
        )
    )
    certificates = Recorder(
        lambda req: CertificateResponse.model_validate({"certificate": canned_optimize_response().result.certificate})
    )
    put_profile = Recorder(lambda pid, req: ProfileResponse.model_validate({"id": pid, "scenario": scenario}))
    get_profile = Recorder(lambda pid: ProfileResponse.model_validate({"id": pid, "scenario": scenario}))
    app = new_app(
        adapters=Adapters(
            confirm=confirm, simulate=simulate, certificates=certificates, put_profile=put_profile, get_profile=get_profile
        )
    )

    assert call(app, "POST", "/confirm", {"scenario": scenario}).body == {"scenario": scenario}

    proposal = {"scenario": scenario, "changes": [], "text": "DesignCo design now takes 10 hours", "apply": False}
    response = call(app, "POST", "/simulate", proposal)
    assert response.status == 200 and response.body["requiresConfirmation"] is True and response.body["result"] is None
    (sim_request,) = simulate.calls[0]
    assert isinstance(sim_request, SimulateRequest) and sim_request.text and sim_request.apply is False

    applied = {
        "scenario": scenario,
        "changes": [{"kind": "estimate", "businessId": "designco", "service": "design", "minutes": 600}],
        "apply": True,
    }
    assert call(app, "POST", "/simulate", applied).status == 200
    assert simulate.calls[1][0].changes[0].root.minutes.root == 600

    response = call(app, "POST", "/certificates", {"scenario": scenario, "result": canned_optimize_response().result.model_dump(mode="json")})
    assert response.status == 200
    assert "downloadUrl" not in response.body and "expiresAt" not in response.body, "optional fields are omitted, not null"

    assert call(app, "PUT", "/profiles/abc_123-x", {"scenario": scenario}).body["id"] == "abc_123-x"
    assert put_profile.calls[0][0] == "abc_123-x"
    assert call(app, "GET", "/profiles/abc_123-x").body["id"] == "abc_123-x"
    assert get_profile.calls == [("abc_123-x",)]


# ------------------------------------------------------------------ adapters: failure mapping
@pytest.mark.parametrize(
    ("raised", "status", "code"),
    [
        (Throttled(), 429, "THROTTLED"),
        (ModelFailure(), 502, "MODEL_FAILURE"),
        (Unavailable(), 503, "UNAVAILABLE"),
    ],
)
def test_adapter_errors_map_to_contract_statuses(raised, status, code, canonical):
    def boom(_request):
        raise raised

    app = new_app(adapters=Adapters(optimize=boom))
    assert_envelope(call(app, "POST", "/optimize", {"scenario": canonical}), status, code)


def test_unexpected_adapter_exception_is_500_without_leaking_details(canonical, caplog):
    def boom(_request):
        raise RuntimeError(f"db password is {SECRET}")

    caplog.set_level(logging.INFO, logger="trademind.api")
    app = new_app(adapters=Adapters(optimize=boom))
    response = call(app, "POST", "/optimize", {"scenario": canonical})
    assert_envelope(response, 500, "INTERNAL_ERROR")
    assert SECRET not in json.dumps(response.body)
    assert SECRET not in caplog.text, "exception messages must not be logged either"
    assert "RuntimeError" in caplog.text, "the exception TYPE is logged for debugging"


def test_adapter_returning_the_wrong_shape_is_500_not_passed_through(canonical):
    app = new_app(adapters=Adapters(optimize=lambda _r: {"result": "nonsense", "mode": "fixture"}))
    assert_envelope(call(app, "POST", "/optimize", {"scenario": canonical}), 500, "INTERNAL_ERROR")


def test_fixture_adapter_in_live_mode_fails_loudly_instead_of_passing_as_real(canonical):
    app = new_app(LIVE_SETTINGS, Adapters(optimize=Recorder(canned_optimize_response("fixture"))))
    response = call(app, "POST", "/optimize", {"scenario": canonical})
    assert_envelope(response, 500, "INTERNAL_ERROR")
    assert "result" not in response.body


def test_live_adapter_in_fixture_mode_also_fails(canonical):
    app = new_app(FIXTURE_SETTINGS, Adapters(optimize=Recorder(canned_optimize_response("live"))))
    assert_envelope(call(app, "POST", "/optimize", {"scenario": canonical}), 500, "INTERNAL_ERROR")


def test_adapter_that_never_answers_times_out_as_503(canonical):
    release = threading.Event()
    settings = dataclasses.replace(FIXTURE_SETTINGS, adapter_timeout_seconds=0.2)
    app = new_app(settings, Adapters(optimize=lambda _r: release.wait(10)))
    started = time.monotonic()
    try:
        response = call(app, "POST", "/optimize", {"scenario": canonical})
        assert_envelope(response, 503, "ADAPTER_TIMEOUT")
        assert time.monotonic() - started < 3, "the request must return at the timeout, not wait for the adapter"
    finally:
        release.set()
        app.close()


def test_concurrency_cap_returns_429_with_retry_after_then_recovers(canonical):
    release, started = threading.Event(), threading.Event()

    def slow(_request):
        started.set()
        release.wait(10)
        return canned_optimize_response()

    app = new_app(dataclasses.replace(FIXTURE_SETTINGS, max_in_flight=1), Adapters(optimize=slow))
    first: list = []
    worker = threading.Thread(target=lambda: first.append(call(app, "POST", "/optimize", {"scenario": canonical})))
    worker.start()
    try:
        assert started.wait(5)
        busy = call(app, "POST", "/optimize", {"scenario": canonical})
        assert_envelope(busy, 429, "THROTTLED")
        assert busy.headers["Retry-After"] == "1"
        assert call(app, "GET", "/health").status == 200, "health must never be throttled"
    finally:
        release.set()
        worker.join(5)
    assert first[0].status == 200
    assert call(app, "POST", "/optimize", {"scenario": canonical}).status == 200, "slots are released after use"
    app.close()


def test_slot_is_not_leaked_when_an_adapter_raises(canonical):
    def boom(_request):
        raise ModelFailure()

    app = new_app(dataclasses.replace(FIXTURE_SETTINGS, max_in_flight=1), Adapters(optimize=boom))
    for _ in range(5):  # would hit 429 on the second call if a slot leaked
        assert call(app, "POST", "/optimize", {"scenario": canonical}).status == 502


# ------------------------------------------------------------------ safe logging
def test_logs_contain_request_metadata_but_never_bodies_or_headers(canonical, caplog):
    caplog.set_level(logging.INFO, logger="trademind.api")
    app = new_app(adapters=Adapters(optimize=Recorder(canned_optimize_response())))
    broken = json.loads(json.dumps(canonical))
    broken["businesses"][0]["name"] = SECRET
    broken["extra"] = SECRET
    call(app, "POST", "/optimize", {"scenario": broken}, headers={"Authorization": "Bearer " + SECRET}, request_id="log-1")
    call(app, "POST", "/optimize", b"{bad " + SECRET.encode(), request_id="log-2")
    assert SECRET not in caplog.text
    lines = [json.loads(r.message) for r in caplog.records if r.name == "trademind.api"]
    assert {line["requestId"] for line in lines} == {"log-1", "log-2"}
    first = lines[0]
    assert set(first) == {"event", "requestId", "method", "route", "status", "durationMs", "errorCode", "mode"}
    assert first["route"] == "/optimize" and first["status"] == 422 and first["errorCode"] == "VALIDATION_ERROR"


def test_log_route_uses_the_template_so_profile_ids_are_not_logged(caplog):
    caplog.set_level(logging.INFO, logger="trademind.api")
    call(new_app(), "GET", "/profiles/profile-id-0001")
    assert "profile-id-0001" not in caplog.text
    assert "/profiles/{id}" in caplog.text


# ------------------------------------------------------------------ API Gateway HTTP API v2 events
def event(method="GET", path="/health", body=None, headers=None, base64_body=False, stage="$default", rid="gw-req-1"):
    return {
        "version": "2.0",
        "routeKey": "$default",
        "rawPath": path,
        "rawQueryString": "",
        "headers": headers or {},
        "requestContext": {"http": {"method": method, "path": path}, "requestId": rid, "stage": stage},
        "body": body,
        "isBase64Encoded": base64_body,
    }


def test_lambda_event_health_returns_a_proxy_response():
    result = new_app().handle_event(event())
    assert set(result) == {"statusCode", "headers", "body", "isBase64Encoded"}
    assert result["statusCode"] == 200 and result["isBase64Encoded"] is False
    assert json.loads(result["body"]) == {"status": "ok", "schemaVersion": "1", "commit": "test123", "mode": "fixture"}
    assert result["headers"]["X-Request-Id"] == "gw-req-1"
    assert all(isinstance(v, str) for v in result["headers"].values())


def test_lambda_event_uses_the_gateway_request_id_in_the_error_envelope():
    result = new_app().handle_event(event(path="/nope", rid="gw-req-77"))
    assert result["statusCode"] == 404
    assert json.loads(result["body"])["error"]["requestId"] == "gw-req-77"


def test_lambda_event_strips_a_named_stage_prefix():
    assert new_app().handle_event(event(path="/prod/health", stage="prod"))["statusCode"] == 200
    assert new_app().handle_event(event(path="/health", stage="$default"))["statusCode"] == 200


def test_lambda_event_headers_are_case_insensitive_for_cors():
    result = new_app().handle_event(event(method="OPTIONS", path="/optimize", headers={"Origin": ALLOWED}))
    assert result["statusCode"] == 204 and result["body"] == ""
    assert result["headers"]["Access-Control-Allow-Origin"] == ALLOWED


def test_lambda_event_post_body_string_and_base64(canonical):
    app = new_app(adapters=Adapters(optimize=Recorder(canned_optimize_response())))
    raw = json.dumps({"scenario": canonical})
    plain = app.handle_event(event("POST", "/optimize", raw))
    encoded = app.handle_event(event("POST", "/optimize", base64.b64encode(raw.encode()).decode(), base64_body=True))
    assert plain["statusCode"] == 200 and encoded["statusCode"] == 200


def test_lambda_event_missing_body_and_bad_base64():
    app = new_app(adapters=Adapters(optimize=Recorder(canned_optimize_response())))
    assert app.handle_event(event("POST", "/optimize", None))["statusCode"] == 400
    bad = app.handle_event(event("POST", "/optimize", "!!!not-base64!!!", base64_body=True))
    assert bad["statusCode"] == 400 and json.loads(bad["body"])["error"]["code"] == "MALFORMED_JSON"


def test_lambda_event_oversized_bodies_are_413_without_decoding_everything():
    app = new_app(adapters=Adapters(optimize=Recorder(canned_optimize_response())))
    too_big = app.handle_event(event("POST", "/optimize", "x" * (MAX_BODY_BYTES + 1)))
    assert too_big["statusCode"] == 413
    huge_b64 = app.handle_event(event("POST", "/optimize", "A" * 5_000_000, base64_body=True))
    assert huge_b64["statusCode"] == 413
    decoded_over = base64.b64encode(b"x" * (MAX_BODY_BYTES + 1)).decode()
    assert app.handle_event(event("POST", "/optimize", decoded_over, base64_body=True))["statusCode"] == 413


@pytest.mark.parametrize("bad_event", [None, "string", 5, [], {}, {"requestContext": {}}, {"requestContext": {"http": {}}}])
def test_lambda_event_unsupported_events_are_400_envelopes_not_crashes(bad_event):
    result = new_app().handle_event(bad_event)
    assert result["statusCode"] == 400
    assert set(json.loads(result["body"])["error"]) == ENVELOPE_KEYS


def test_lambda_handler_builds_the_production_app_from_the_environment(monkeypatch):
    import backend.app as module

    monkeypatch.setattr(module, "_APP", None)
    monkeypatch.setenv("APP_MODE", "live")
    monkeypatch.setenv("COMMIT_SHA", "deadbeef")
    health = lambda_handler(event(), None)
    assert json.loads(health["body"])["mode"] == "live" and json.loads(health["body"])["commit"] == "deadbeef"
    # Production wiring has no adapters yet, so live routes are honest 503s.
    optimize = lambda_handler(event("POST", "/optimize", "{}"), None)
    assert optimize["statusCode"] == 503 and json.loads(optimize["body"])["error"]["code"] == "NOT_IMPLEMENTED"
    monkeypatch.setattr(module, "_APP", None)


def test_lambda_handler_reports_misconfiguration_without_crashing(monkeypatch):
    import backend.app as module

    class Ctx:
        aws_request_id = "ctx-req-1"

    monkeypatch.setattr(module, "_APP", None)
    monkeypatch.setenv("APP_MODE", "bogus")
    result = lambda_handler(event(), Ctx())
    assert result["statusCode"] == 503
    body = json.loads(result["body"])
    assert body["error"]["code"] == "MISCONFIGURED" and body["error"]["requestId"] == "ctx-req-1"
    assert "bogus" not in result["body"], "configuration values are not echoed"
    monkeypatch.setattr(module, "_APP", None)


# ------------------------------------------------------------------ the real local HTTP adapter
@pytest.fixture
def server():
    app = new_app(adapters=Adapters(optimize=Recorder(canned_optimize_response())))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield httpd.server_address[1], app
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=5)
    app.close()


def http_call(port, method, path, body=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        resp = conn.getresponse()
        raw = resp.read()
        return resp.status, dict(resp.getheaders()), (json.loads(raw) if raw else None)
    finally:
        conn.close()


def test_local_server_serves_the_same_router(server, canonical):
    port, _ = server
    status, headers, body = http_call(port, "GET", "/health")
    assert status == 200 and body["mode"] == "fixture" and headers["X-TradeMind-Mode"] == "fixture"

    payload = json.dumps({"scenario": canonical})
    status, _, body = http_call(port, "POST", "/optimize", payload, {"Content-Type": "application/json"})
    assert status == 200 and body["result"]["totals"]["grossSavingsMinutes"] == 900

    status, _, body = http_call(port, "POST", "/optimize", "{bad", {"Content-Type": "application/json"})
    assert status == 400 and body["error"]["code"] == "MALFORMED_JSON"
    status, _, body = http_call(port, "POST", "/optimize", "{}", {"Content-Type": "application/json"})
    assert status == 422
    status, headers, body = http_call(port, "GET", "/nope")
    assert status == 404 and body["error"]["requestId"] == headers["X-Request-Id"]


def test_local_server_cors_and_options(server):
    port, _ = server
    status, headers, body = http_call(port, "OPTIONS", "/optimize", headers={"Origin": ALLOWED})
    assert status == 204 and body is None and headers["Access-Control-Allow-Origin"] == ALLOWED
    status, headers, _ = http_call(port, "OPTIONS", "/optimize", headers={"Origin": "https://evil.example"})
    assert status == 204 and "Access-Control-Allow-Origin" not in headers


def test_local_server_rejects_oversized_declared_length_without_reading_the_body(server):
    port, app = server
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.putrequest("POST", "/optimize")
        conn.putheader("Content-Type", "application/json")
        conn.putheader("Content-Length", str(MAX_BODY_BYTES * 100))
        conn.endheaders()  # the body is never sent
        resp = conn.getresponse()
        body = json.loads(resp.read())
        assert resp.status == 413 and body["error"]["code"] == "PAYLOAD_TOO_LARGE"
    finally:
        conn.close()
    assert app.adapters.optimize.calls == []


def test_local_server_rejects_bad_content_length_and_chunked_bodies(server):
    port, _ = server
    for header_name, value in (("Content-Length", "abc"), ("Content-Length", "-5"), ("Transfer-Encoding", "chunked")):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            conn.putrequest("POST", "/optimize")
            conn.putheader(header_name, value)
            conn.endheaders()
            resp = conn.getresponse()
            body = json.loads(resp.read())
            assert resp.status == 400 and body["error"]["code"] == "MALFORMED_JSON", (header_name, value)
        finally:
            conn.close()
