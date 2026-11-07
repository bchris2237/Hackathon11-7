"""Phase 0 backend behavior: /health works; everything else is honestly unavailable."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from backend.app import handle_request
from backend.config import ConfigError, load_settings
from backend.local import make_handler
from contracts.models import ErrorEnvelope
from tests.backend.helpers import validator

SETTINGS = load_settings({"COMMIT_SHA": "abc1234"})


def test_health_response_matches_contract_schema():
    response = handle_request(SETTINGS, "GET", "/health", "req-1")
    assert response.status == 200
    assert list(validator("api.schema.json", "HealthResponse").iter_errors(response.body)) == []
    assert response.body == {"status": "ok", "schemaVersion": "1", "commit": "abc1234", "mode": "fixture"}


def test_commit_is_unknown_when_not_provided():
    assert load_settings({}).commit == "unknown"


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/extract"),
        ("POST", "/confirm"),
        ("POST", "/optimize"),
        ("POST", "/simulate"),
        ("POST", "/explain"),
        ("POST", "/certificates"),
        ("PUT", "/profiles/abc"),
        ("GET", "/profiles/abc"),
    ],
)
def test_unimplemented_routes_report_503_not_implemented(method, path):
    response = handle_request(SETTINGS, method, path, "req-2")
    assert response.status == 503
    assert response.body["error"]["code"] == "NOT_IMPLEMENTED"
    assert response.body["error"]["requestId"] == "req-2"
    assert list(validator("api.schema.json", "ErrorEnvelope").iter_errors(response.body)) == []
    ErrorEnvelope.model_validate(response.body)
    assert "assignments" not in response.body, "must never return invented optimizer output"


def test_unknown_route_is_404_and_wrong_method_is_405():
    assert handle_request(SETTINGS, "GET", "/nope", "r").status == 404
    assert handle_request(SETTINGS, "GET", "/profiles/", "r").status == 404
    assert handle_request(SETTINGS, "GET", "/profiles/a/b", "r").status == 404
    assert handle_request(SETTINGS, "POST", "/health", "r").status == 405
    assert handle_request(SETTINGS, "GET", "/optimize", "r").status == 405


def test_query_string_is_ignored_for_routing():
    assert handle_request(SETTINGS, "GET", "/health?x=1", "r").status == 200


def test_live_mode_is_rejected_at_startup_not_silently_faked():
    with pytest.raises(ConfigError, match="live"):
        load_settings({"APP_MODE": "live"})


@pytest.mark.parametrize(
    "env",
    [{"APP_MODE": "demo"}, {"BACKEND_PORT": "abc"}, {"BACKEND_PORT": "70000"}, {"ALLOWED_ORIGIN": "*"}],
)
def test_invalid_configuration_is_rejected(env):
    with pytest.raises(ConfigError):
        load_settings(env)


def _get(url, method="GET", data=None, headers=None):
    request = urllib.request.Request(url, method=method, data=data, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=5) as resp:
            return resp.status, dict(resp.headers), json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as err:
        return err.code, dict(err.headers), json.loads(err.read())


def test_real_http_server_serves_health_and_labels_mode():
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(SETTINGS))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        status, headers, body = _get(f"{base}/health")
        assert status == 200 and body["status"] == "ok"
        assert headers["X-TradeMind-Mode"] == "fixture"
        assert headers["X-Request-Id"]

        # CORS: an allow-listed origin is echoed back; any other origin gets no CORS header.
        for origin in ("http://127.0.0.1:5173", "http://localhost:5173"):
            _, cors_headers, _ = _get(f"{base}/health", headers={"Origin": origin})
            assert cors_headers["Access-Control-Allow-Origin"] == origin
        _, evil_headers, _ = _get(f"{base}/health", headers={"Origin": "https://evil.example"})
        assert "Access-Control-Allow-Origin" not in evil_headers
        _, no_origin_headers, _ = _get(f"{base}/health")
        assert "Access-Control-Allow-Origin" not in no_origin_headers

        status, _, body = _get(f"{base}/optimize", method="POST", data=b'{"scenario": {}}')
        assert status == 503 and body["error"]["code"] == "NOT_IMPLEMENTED"

        status, headers, _ = _get(f"{base}/optimize", method="OPTIONS", headers={"Origin": "http://localhost:5173"})
        assert status == 204 and "POST" in headers["Access-Control-Allow-Methods"]
        status, headers, _ = _get(f"{base}/optimize", method="OPTIONS", headers={"Origin": "https://evil.example"})
        assert status == 204 and "Access-Control-Allow-Origin" not in headers
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
