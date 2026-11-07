"""API errors and the single error-envelope builder.

Every error the API returns is {"error": {"code", "message", "fields", "requestId"}}.
Messages are fixed text. They never echo request bodies, user descriptions or exception
messages, because those can contain private data (Pydantic error text includes the input).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import ValidationError

from contracts.models import ErrorEnvelope

MAX_REPORTED_FIELDS = 20
_MAX_FIELD_LEN = 100
_REQUEST_ID_RE = re.compile(r"[^A-Za-z0-9._:\-]")


class ApiError(Exception):
    """An error with a status, a stable code and a safe message. Raised anywhere in request handling."""

    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        fields: Sequence[str] = (),
        headers: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.fields = list(fields)
        self.headers = dict(headers or {})


# Statuses from the frozen contract: 400 malformed JSON, 422 validation, 429 throttled,
# 502 model failure, 503 unavailable. Adapters raise these to report their own failures.
class Throttled(ApiError):
    def __init__(self, message: str = "The service is throttled. Retry later.") -> None:
        super().__init__(429, "THROTTLED", message)


class ModelFailure(ApiError):
    def __init__(self, message: str = "The language model call failed.") -> None:
        super().__init__(502, "MODEL_FAILURE", message)


class Unavailable(ApiError):
    def __init__(self, message: str = "A required service is unavailable.") -> None:
        super().__init__(503, "UNAVAILABLE", message)


def malformed_json(message: str) -> ApiError:
    return ApiError(400, "MALFORMED_JSON", message)


def validation_error(message: str, fields: Sequence[str] = ()) -> ApiError:
    return ApiError(422, "VALIDATION_ERROR", message, fields)


def clean_text(value: object, limit: int = _MAX_FIELD_LEN) -> str:
    """Printable, length-bounded text. Used for anything that originated from a client."""
    text = "".join(ch if ch.isprintable() else "?" for ch in str(value))
    return text[:limit]


def clean_request_id(value: object) -> str:
    cleaned = _REQUEST_ID_RE.sub("", str(value))[:128]
    return cleaned or "unknown"


def format_path(loc: Sequence[object]) -> str:
    """One path style everywhere: scenario.businesses[0].estimates[2].service (indexes in brackets).

    Keeps the 'fields' list consistent across /extract (missingFields), /confirm and every 422, so a UI needs
    one parser. Only the field names and indexes are used; never values.
    """
    path = ""
    for part in loc:
        if isinstance(part, int):
            path += f"[{part}]"
        else:
            path += ("." if path else "") + clean_text(part)
    return path or "(body)"


def from_pydantic(exc: ValidationError) -> ApiError:
    """Turn a Pydantic error into a 422 listing only field PATHS (never values or error text)."""
    fields: list[str] = []
    for item in exc.errors(include_url=False, include_context=False, include_input=False):
        path = format_path(item["loc"])
        if path not in fields:
            fields.append(path)
        if len(fields) >= MAX_REPORTED_FIELDS:
            break
    return validation_error("Request body failed validation. See 'fields' for the offending paths.", fields)


def envelope(code: str, message: str, request_id: str, fields: Sequence[str] = ()) -> dict[str, Any]:
    """Build the error body, validated through the generated contract model."""
    model = ErrorEnvelope.model_validate(
        {"error": {"code": code, "message": message, "fields": list(fields), "requestId": clean_request_id(request_id)}}
    )
    return model.model_dump(mode="json")
