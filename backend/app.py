"""One router for every transport.

`App.handle(Request) -> Response` is the only place routing, validation, CORS, error mapping and
logging happen. Two thin translators sit on top of it:

  * `lambda_handler(event, context)`  : API Gateway HTTP API (payload format 2.0) in AWS Lambda
  * `backend/local.py`                : a standard-library HTTP server for development

Request pipeline (the order is part of the contract; each step is cheaper than the next):

  1. route match             -> 404 NOT_FOUND
  2. OPTIONS preflight       -> 204 (CORS headers only for allow-listed origins)
  3. method check            -> 405 METHOD_NOT_ALLOWED
  4. GET /health             -> 200 (needs no adapter and reads no body)
  5. transport problems      -> 400 MALFORMED_JSON
  6. payload size > 32 KiB   -> 413 PAYLOAD_TOO_LARGE (checked before any parsing)
  7. adapter present?        -> 503 NOT_IMPLEMENTED   (never a simulated answer)
  8. parse JSON              -> 400 MALFORMED_JSON
  9. validate with the generated Pydantic model -> 422 VALIDATION_ERROR
 10. call the adapter with a timeout and a concurrency cap -> 429 / 502 / 503 / 500
 11. re-validate the adapter output and check its `mode` -> 500 INTERNAL_ERROR on mismatch

Adapters are injected callables. The real optimizer, Bedrock and storage adapters arrive in later
prompts; until then those routes answer 503. Tests inject fixture-backed stubs. Nothing in this
module returns simulated success on its own.
"""

from __future__ import annotations

import base64
import binascii
import concurrent.futures
import dataclasses
import json
import logging
import re
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ValidationError

from backend.config import ConfigError, Settings, load_settings
from backend.confirmation import confirm_scenario, validate_scenario
from backend.optimization import broken_optimizer_adapter, load_optimizer_function, make_optimize_adapter
from backend.errors import (
    ApiError,
    clean_request_id,
    clean_text,
    envelope,
    from_pydantic,
    malformed_json,
    validation_error,
)
from contracts import SCHEMA_VERSION
from contracts.models import (
    CertificateRequest,
    CertificateResponse,
    ConfirmRequest,
    ConfirmResponse,
    ExplainRequest,
    ExplainResponse,
    ExtractRequest,
    ExtractResponse,
    HealthResponse,
    OptimizeRequest,
    OptimizeResponse,
    ProfilePutRequest,
    ProfileResponse,
    SimulateRequest,
    SimulateResponse,
)

log = logging.getLogger("trademind.api")
log.setLevel(logging.INFO)  # Lambda's root logger defaults to WARNING; the handler is inherited

MAX_BODY_BYTES = 32 * 1024  # frozen contract: bound request size to 32 KiB
_MAX_BASE64_CHARS = ((MAX_BODY_BYTES + 2) // 3) * 4
_PROFILE_PATH = re.compile(r"^/profiles/([A-Za-z0-9_-]{1,128})$")


# --------------------------------------------------------------------------- transport types
@dataclass(frozen=True)
class Request:
    method: str
    path: str
    headers: Mapping[str, str] = field(default_factory=dict)  # keys lower-cased
    body: bytes = b""
    request_id: str = ""
    # Content-Length the client declared, when the transport knows it (local server).
    declared_length: int | None = None
    # A problem the transport already found (bad Content-Length, chunked body, read timeout).
    transport_error: ApiError | None = None


@dataclass(frozen=True)
class Response:
    status: int
    body: dict[str, Any]
    headers: dict[str, str] = field(default_factory=dict)

    def body_bytes(self) -> bytes:
        return b"" if self.status == 204 else json.dumps(self.body).encode("utf-8")


# --------------------------------------------------------------------------- adapters
@dataclass(frozen=True)
class Adapters:
    """Injected dependencies. None means 'not implemented': the route answers 503.

    Each adapter receives the validated request model and returns the matching response model
    (or an equivalent dict). Output is validated again and its `mode` must equal the app's mode.
    """

    extract: Callable[[ExtractRequest], Any] | None = None
    confirm: Callable[[ConfirmRequest], Any] | None = None
    optimize: Callable[[OptimizeRequest], Any] | None = None
    simulate: Callable[[SimulateRequest], Any] | None = None
    explain: Callable[[ExplainRequest], Any] | None = None
    put_profile: Callable[[str, ProfilePutRequest], Any] | None = None
    get_profile: Callable[[str], Any] | None = None
    certificates: Callable[[CertificateRequest], Any] | None = None


def default_adapters(
    settings: Settings | None = None,
    env: Mapping[str, str] | None = None,
    optimizer_loader: Callable[[], Any] | None = None,
) -> Adapters:
    """Production wiring. Only REAL adapters are ever built here; fixtures live in tests.

    * /confirm is always wired (B3). It is deterministic validation with no AWS call and no mode.
    * /optimize and /simulate (B4, B5) are wired in BOTH modes when `optimizer.api.optimize` can be imported.
      The computation is real and deterministic; their response `mode` echoes the app mode. If the package is
      absent the routes stay unwired (503 NOT_IMPLEMENTED); if it exists but is broken they answer
      503 OPTIMIZER_UNAVAILABLE. A fixture or canned optimizer is NEVER built here; tests inject their own.
    * /simulate with explicit changes needs no model. Its TEXT proposals need Bedrock: in fixture mode, or in live
      mode without BEDROCK_MODEL_ID and AWS_REGION, they answer 503 NOT_CONFIGURED.
    * live mode only: /extract and /explain use Bedrock if BEDROCK_MODEL_ID and AWS_REGION are set; otherwise
      they answer 503 NOT_CONFIGURED. No model id or region is ever assumed. (A real Bedrock reply is labelled
      'live', so wiring it into a fixture-mode app would be rejected by the mode check anyway.)
    /explain additionally needs the optimizer, because it recomputes the result before explaining it.
    Other routes stay unwired until their prompts.
    """
    adapters = Adapters(confirm=confirm_scenario)
    if settings is None:
        return adapters

    from backend.simulation import Simulator

    loaded = (optimizer_loader or load_optimizer_function)()
    optimizer_ready = callable(loaded)
    broken = broken_optimizer_adapter(loaded) if isinstance(loaded, Exception) else None

    def not_configured(_request: Any) -> Any:
        raise ApiError(
            503,
            "NOT_CONFIGURED",
            "This feature is not configured: the server needs BEDROCK_MODEL_ID and AWS_REGION.",
        )

    if optimizer_ready:
        adapters = dataclasses.replace(
            adapters,
            optimize=make_optimize_adapter(loaded, settings.mode),
            simulate=Simulator(loaded, settings.mode, None, settings.adapter_timeout_seconds),
        )
    elif broken is not None:
        adapters = dataclasses.replace(adapters, optimize=broken, simulate=broken)
    if settings.mode != "live":
        return adapters

    import os

    from backend.bedrock import BedrockClient, load_bedrock_settings
    from backend.explanation import Explainer
    from backend.extraction import BedrockExtractor

    bedrock_settings = load_bedrock_settings(os.environ if env is None else env)
    if bedrock_settings is None:
        return dataclasses.replace(
            adapters,
            extract=not_configured,
            explain=not_configured if optimizer_ready else broken,
        )

    client = BedrockClient(bedrock_settings)
    return dataclasses.replace(
        adapters,
        extract=BedrockExtractor(client, time_budget_seconds=settings.adapter_timeout_seconds),
        explain=Explainer(loaded, client) if optimizer_ready else broken,
        simulate=(
            Simulator(loaded, settings.mode, client, settings.adapter_timeout_seconds)
            if optimizer_ready
            else adapters.simulate
        ),
    )

@dataclass(frozen=True)
class Endpoint:
    adapter: str  # attribute name on Adapters
    request_model: type[BaseModel] | None
    response_model: type[BaseModel]
    drop_none: tuple[str, ...] = ()  # optional fields that must be omitted, not null


ENDPOINTS: dict[tuple[str, str], Endpoint] = {
    ("POST", "/extract"): Endpoint("extract", ExtractRequest, ExtractResponse),
    ("POST", "/confirm"): Endpoint("confirm", ConfirmRequest, ConfirmResponse),
    ("POST", "/optimize"): Endpoint("optimize", OptimizeRequest, OptimizeResponse),
    ("POST", "/simulate"): Endpoint("simulate", SimulateRequest, SimulateResponse),
    ("POST", "/explain"): Endpoint("explain", ExplainRequest, ExplainResponse),
    ("POST", "/certificates"): Endpoint(
        "certificates", CertificateRequest, CertificateResponse, drop_none=("downloadUrl", "expiresAt")
    ),
    ("PUT", "/profiles/{id}"): Endpoint("put_profile", ProfilePutRequest, ProfileResponse),
    ("GET", "/profiles/{id}"): Endpoint("get_profile", None, ProfileResponse),
}
ROUTE_METHODS: dict[str, frozenset[str]] = {"/health": frozenset({"GET"})}
for _method, _template in ENDPOINTS:
    ROUTE_METHODS[_template] = ROUTE_METHODS.get(_template, frozenset()) | {_method}


def match_route(path: str) -> tuple[str | None, str | None]:
    """Return (route template, profile id). Exact match only: no trailing slash, no normalisation."""
    if path in ROUTE_METHODS:
        return path, None
    found = _PROFILE_PATH.match(path)
    if found:
        return "/profiles/{id}", found.group(1)
    return None, None


# --------------------------------------------------------------------------- JSON parsing
class _DuplicateKey(ValueError):
    pass


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise _DuplicateKey
        out[key] = value
    return out


def _reject_constant(_name: str) -> Any:
    raise ValueError("NaN and Infinity are not valid JSON")


def parse_json_body(body: bytes) -> Any:
    if not body.strip():
        raise malformed_json("Request body is empty.")
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        raise malformed_json("Request body is not valid UTF-8.") from None
    try:
        return json.loads(text, object_pairs_hook=_no_duplicates, parse_constant=_reject_constant)
    except _DuplicateKey:
        raise malformed_json("Request body contains a duplicate object key.") from None
    except RecursionError:
        raise malformed_json("Request body is nested too deeply.") from None
    except ValueError:  # JSONDecodeError, NaN/Infinity, absurdly long integers
        raise malformed_json("Request body is not valid JSON.") from None


# --------------------------------------------------------------------------- the app
class App:
    def __init__(self, settings: Settings, adapters: Adapters | None = None) -> None:
        self.settings = settings
        self.adapters = adapters if adapters is not None else Adapters()
        # Concurrency cap. A slot is held until the adapter thread really finishes, so work
        # abandoned after a timeout still counts and cannot pile up without bound.
        self._slots = threading.BoundedSemaphore(settings.max_in_flight)
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=settings.max_in_flight, thread_name_prefix="adapter"
        )

    def close(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)

    # ------------------------------------------------------------------ entry points
    def handle(self, request: Request) -> Response:
        started = time.monotonic()
        request_id = clean_request_id(request.request_id) if request.request_id else uuid.uuid4().hex
        template, profile_id = match_route(request.path)
        try:
            response = self._dispatch(request, request_id, template, profile_id)
        except ApiError as exc:
            response = Response(
                exc.status, envelope(exc.code, exc.message, request_id, exc.fields), dict(exc.headers)
            )
        except Exception as exc:  # a bug on our side: say so without leaking details
            log.error(json.dumps({"event": "unhandled", "requestId": request_id, "errorType": type(exc).__name__}))
            response = Response(500, envelope("INTERNAL_ERROR", "Internal server error.", request_id))
        response = self._with_common_headers(request, response, request_id, template)
        log.info(
            json.dumps(
                {
                    "event": "request",
                    "requestId": request_id,
                    "method": clean_text(request.method, 16),
                    "route": template or "(unmatched)",
                    "status": response.status,
                    "durationMs": round((time.monotonic() - started) * 1000, 1),
                    "errorCode": response.body.get("error", {}).get("code") if response.status >= 400 else None,
                    "mode": self.settings.mode,
                }
            )
        )
        return response

    def handle_event(self, event: Any, context: Any = None) -> dict[str, Any]:
        """API Gateway HTTP API payload format 2.0 in, the Lambda proxy response out."""
        fallback_id = getattr(context, "aws_request_id", None) or uuid.uuid4().hex
        try:
            request = self._request_from_event(event, str(fallback_id))
        except ApiError as exc:
            request_id = clean_request_id(fallback_id)
            response = Response(exc.status, envelope(exc.code, exc.message, request_id, exc.fields))
            response = self._with_common_headers(Request("", ""), response, request_id, None)
        else:
            response = self.handle(request)
        return {
            "statusCode": response.status,
            "headers": response.headers,
            "body": response.body_bytes().decode("utf-8"),
            "isBase64Encoded": False,
        }

    # ------------------------------------------------------------------ pipeline
    def _dispatch(self, request: Request, request_id: str, template: str | None, profile_id: str | None) -> Response:
        if template is None:
            raise ApiError(404, "NOT_FOUND", "No such route.")
        allowed = ROUTE_METHODS[template]

        if request.method == "OPTIONS":
            return Response(204, {})
        if request.method not in allowed:
            raise ApiError(
                405,
                "METHOD_NOT_ALLOWED",
                f"{clean_text(request.method, 16)} is not allowed here; use {', '.join(sorted(allowed))}.",
                headers={"Allow": ", ".join(sorted(allowed | {"OPTIONS"}))},
            )

        if template == "/health":
            health = HealthResponse.model_validate(
                {
                    "status": "ok",
                    "schemaVersion": SCHEMA_VERSION,
                    "commit": self.settings.commit,
                    "mode": self.settings.mode,
                }
            )
            return Response(200, health.model_dump(mode="json"))

        if request.transport_error is not None:
            raise request.transport_error
        if max(len(request.body), request.declared_length or 0) > MAX_BODY_BYTES:
            raise ApiError(413, "PAYLOAD_TOO_LARGE", f"Request body exceeds {MAX_BODY_BYTES} bytes.")

        endpoint = ENDPOINTS[(request.method, template)]
        adapter = getattr(self.adapters, endpoint.adapter)
        if adapter is None:
            raise ApiError(
                503,
                "NOT_IMPLEMENTED",
                f"{request.method} {template} is not available: its backend adapter is not implemented yet.",
            )

        args: list[Any] = [profile_id] if profile_id is not None else []
        if endpoint.request_model is not None:
            data = parse_json_body(request.body)
            try:
                model = endpoint.request_model.model_validate(data)
            except ValidationError as exc:
                raise from_pydantic(exc) from None
            self._check_request_rules(model)
            args.append(model)

        result = self._call_adapter(adapter, args)
        return Response(200, self._validated_output(endpoint, result))

    @staticmethod
    def _check_request_rules(model: BaseModel) -> None:
        """Contract rules JSON Schema states but the generated Pydantic models cannot express."""
        if isinstance(model, (ConfirmRequest, OptimizeRequest, ExplainRequest, SimulateRequest)):
            # Same checks for every route that takes a scenario, run before any adapter. Routes built
            # later (certificates, profiles) must call this too.
            validate_scenario(model.scenario)
        if isinstance(model, SimulateRequest) and model.text is not None:
            offending = [name for name, bad in (("changes", bool(model.changes)), ("apply", model.apply)) if bad]
            if offending:
                raise validation_error(
                    "Text is proposal-only: send it with changes=[] and apply=false.", ["text", *offending]
                )

    def _call_adapter(self, adapter: Callable[..., Any], args: list[Any]) -> Any:
        if not self._slots.acquire(blocking=False):
            raise ApiError(
                429, "THROTTLED", "Too many requests are in progress. Retry shortly.", headers={"Retry-After": "1"}
            )

        def work() -> Any:
            try:
                return adapter(*args)
            finally:
                self._slots.release()

        try:
            future = self._executor.submit(work)
        except BaseException:
            self._slots.release()
            raise
        done, _ = concurrent.futures.wait([future], timeout=self.settings.adapter_timeout_seconds)
        if not done:
            if future.cancel():  # never started, so its own release will never run
                self._slots.release()
            raise ApiError(503, "ADAPTER_TIMEOUT", "The backend service did not answer in time.")
        return future.result()  # ApiError subclasses pass through; anything else becomes a 500

    def _validated_output(self, endpoint: Endpoint, result: Any) -> dict[str, Any]:
        raw = result.model_dump(mode="json") if isinstance(result, BaseModel) else result
        try:
            checked = endpoint.response_model.model_validate(raw)
        except ValidationError:
            raise ApiError(500, "INTERNAL_ERROR", "Internal server error.") from None
        mode = getattr(checked, "mode", None)
        if mode is not None and mode.root != self.settings.mode:
            # A fixture adapter wired into live (or the reverse) must fail loudly, not pass as real.
            log.error(json.dumps({"event": "mode_mismatch", "appMode": self.settings.mode, "adapterMode": mode.root}))
            raise ApiError(500, "INTERNAL_ERROR", "Internal server error.")
        body = checked.model_dump(mode="json")
        for key in endpoint.drop_none:
            if body.get(key) is None:
                body.pop(key, None)
        return body

    # ------------------------------------------------------------------ headers and CORS
    def _with_common_headers(
        self, request: Request, response: Response, request_id: str, template: str | None
    ) -> Response:
        headers = dict(response.headers)
        headers.update(
            {
                "Content-Type": "application/json",
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "X-Request-Id": request_id,
                "X-TradeMind-Mode": self.settings.mode,
                "Vary": "Origin",
            }
        )
        origin = request.headers.get("origin")
        if origin is not None and origin in self.settings.allowed_origins:
            headers["Access-Control-Allow-Origin"] = origin
            headers["Access-Control-Expose-Headers"] = "X-Request-Id, X-TradeMind-Mode"
            if request.method == "OPTIONS" and template is not None:
                methods = sorted(ROUTE_METHODS[template] | {"OPTIONS"})
                headers["Access-Control-Allow-Methods"] = ", ".join(methods)
                headers["Access-Control-Allow-Headers"] = "Content-Type"
                headers["Access-Control-Max-Age"] = "600"
        return Response(response.status, response.body, headers)

    # ------------------------------------------------------------------ API Gateway v2 events
    @staticmethod
    def _request_from_event(event: Any, fallback_id: str) -> Request:
        if not isinstance(event, dict):
            raise malformed_json("Unsupported event.")
        context = event.get("requestContext")
        http = context.get("http") if isinstance(context, dict) else None
        if not isinstance(http, dict) or not isinstance(http.get("method"), str):
            raise malformed_json("Unsupported event: expected an API Gateway HTTP API (v2) request.")
        method = http["method"].upper()
        path = event.get("rawPath") or http.get("path") or ""
        if not isinstance(path, str):
            raise malformed_json("Unsupported event path.")
        stage = context.get("stage") if isinstance(context, dict) else None
        if isinstance(stage, str) and stage != "$default":
            if path == f"/{stage}":
                path = "/"
            elif path.startswith(f"/{stage}/"):
                path = path[len(stage) + 1 :]
        raw_headers = event.get("headers") if isinstance(event.get("headers"), dict) else {}
        headers = {str(k).lower(): str(v) for k, v in raw_headers.items()}

        raw = event.get("body")
        if raw is None:
            body = b""
        elif not isinstance(raw, str):
            raise malformed_json("Unsupported event body.")
        elif event.get("isBase64Encoded") is True:
            if len(raw) > _MAX_BASE64_CHARS:  # refuse before decoding
                raise ApiError(413, "PAYLOAD_TOO_LARGE", f"Request body exceeds {MAX_BODY_BYTES} bytes.")
            try:
                body = base64.b64decode(raw, validate=True)
            except (binascii.Error, ValueError):
                raise malformed_json("Request body is not valid base64.") from None
        else:
            try:
                body = raw.encode("utf-8")
            except UnicodeEncodeError:
                raise malformed_json("Request body is not valid UTF-8.") from None

        request_id = context.get("requestId") if isinstance(context, dict) else None
        return Request(method, path, headers, body, str(request_id or fallback_id))


# --------------------------------------------------------------------------- Lambda entry point
_APP: App | None = None
_APP_LOCK = threading.Lock()


def get_app() -> App:
    """Build the production app once per Lambda container (cold start)."""
    global _APP
    with _APP_LOCK:
        if _APP is None:
            settings = load_settings()
            _APP = App(settings, default_adapters(settings))
        return _APP


def lambda_handler(event: Any, context: Any = None) -> dict[str, Any]:
    """SAM handler path: backend.app.lambda_handler"""
    try:
        app = get_app()
    except ConfigError:
        request_id = clean_request_id(getattr(context, "aws_request_id", None) or uuid.uuid4().hex)
        log.error(json.dumps({"event": "misconfigured", "requestId": request_id}))
        body = envelope("MISCONFIGURED", "The service is misconfigured.", request_id)
        return {
            "statusCode": 503,
            "headers": {"Content-Type": "application/json", "Cache-Control": "no-store", "X-Request-Id": request_id},
            "body": json.dumps(body),
            "isBase64Encoded": False,
        }
    return app.handle_event(event, context)


def handle_request(
    settings: Settings,
    method: str,
    path: str,
    request_id: str,
    body: bytes = b"",
    headers: Mapping[str, str] | None = None,
    adapters: Adapters | None = None,
) -> Response:
    """Phase 0 compatible one-shot helper (kept so existing imports and tests keep working)."""
    app = App(settings, adapters)
    try:
        bare_path = path.split("?", 1)[0]
        return app.handle(
            Request(method, bare_path, {k.lower(): v for k, v in (headers or {}).items()}, body, request_id)
        )
    finally:
        app.close()
