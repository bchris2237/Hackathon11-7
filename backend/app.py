"""Transport-independent request handling.

`handle_request` maps (method, path) to (status, JSON body). It performs no I/O,
so the same function can later sit behind the Lambda handler and is trivial to test.

Phase 0 behavior:
  * GET /health                   -> 200 HealthResponse
  * every other contract route    -> 503 NOT_IMPLEMENTED (honest, never invented output)
  * unknown path                  -> 404 NOT_FOUND
  * known path, wrong method      -> 405 METHOD_NOT_ALLOWED
"""

from __future__ import annotations

from typing import Any, NamedTuple

from backend.config import Settings
from contracts import SCHEMA_VERSION
from contracts.models import ErrorEnvelope, HealthResponse

MAX_BODY_BYTES = 32 * 1024  # frozen contract; enforced when body handling lands (B1)

# Routes that exist in contracts/api.openapi.yaml, mapped to the methods they accept.
CONTRACT_ROUTES: dict[str, set[str]] = {
    "/health": {"GET"},
    "/extract": {"POST"},
    "/confirm": {"POST"},
    "/optimize": {"POST"},
    "/simulate": {"POST"},
    "/explain": {"POST"},
    "/certificates": {"POST"},  # optional
}
PROFILE_METHODS = {"GET", "PUT"}  # optional /profiles/{id}


class Response(NamedTuple):
    status: int
    body: dict[str, Any]


def error_response(status: int, code: str, message: str, request_id: str, fields: list[str] | None = None) -> Response:
    envelope = ErrorEnvelope.model_validate(
        {"error": {"code": code, "message": message, "fields": fields or [], "requestId": request_id}}
    )
    return Response(status, envelope.model_dump(mode="json"))


def _allowed_methods(path: str) -> set[str] | None:
    if path in CONTRACT_ROUTES:
        return CONTRACT_ROUTES[path]
    if path.startswith("/profiles/") and len(path) > len("/profiles/") and "/" not in path[len("/profiles/"):]:
        return PROFILE_METHODS
    return None


def handle_request(settings: Settings, method: str, path: str, request_id: str) -> Response:
    path = path.split("?", 1)[0]
    allowed = _allowed_methods(path)
    if allowed is None:
        return error_response(404, "NOT_FOUND", "No such route.", request_id)
    if method not in allowed:
        return error_response(
            405, "METHOD_NOT_ALLOWED", f"{method} is not allowed here; use {', '.join(sorted(allowed))}.", request_id
        )

    if path == "/health":
        health = HealthResponse.model_validate(
            {"status": "ok", "schemaVersion": SCHEMA_VERSION, "commit": settings.commit, "mode": settings.mode}
        )
        return Response(200, health.model_dump(mode="json"))

    return error_response(
        503,
        "NOT_IMPLEMENTED",
        f"{method} {path} is not implemented yet. Phase 0 provides GET /health only.",
        request_id,
    )
