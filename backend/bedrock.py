"""Bedrock Runtime Converse client: bounded, server-side, and honest about failures.

Responsibilities (and only these):
  * build one Converse request from a system prompt and messages (no prompt logic lives here);
  * apply hard limits: connect/read timeouts, a small bounded retry count, capped output tokens;
  * map every SDK failure to a contract status (429 / 502 / 503) with a fixed, safe message;
  * return the reply text plus sanitized evidence (AWS request id, token counts, latency).

Credentials: never read, stored or logged here. boto3 resolves them through the standard chain
(env / AWS_PROFILE locally, the execution role in Lambda). The model id and region come from
BEDROCK_MODEL_ID and AWS_REGION. Nothing is assumed: if either is missing the extract route answers
503 NOT_CONFIGURED (see backend/app.py: default_adapters).

Never logged: prompts, model output, descriptions, credentials, ARNs.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

import boto3
from botocore.config import Config
from botocore.exceptions import (
    BotoCoreError,
    ClientError,
    ConnectTimeoutError,
    CredentialRetrievalError,
    EndpointConnectionError,
    NoCredentialsError,
    PartialCredentialsError,
    ReadTimeoutError,
)

from backend.config import ConfigError
from backend.errors import ApiError, Throttled, Unavailable, clean_text

log = logging.getLogger("trademind.bedrock")
log.setLevel(logging.INFO)

DEFAULT_MAX_OUTPUT_TOKENS = 1500
MIN_MAX_OUTPUT_TOKENS = 100
MAX_MAX_OUTPUT_TOKENS = 4000
DEFAULT_CONNECT_TIMEOUT_SECONDS = 3.0
DEFAULT_READ_TIMEOUT_SECONDS = 10.0
MIN_READ_TIMEOUT_SECONDS = 1.0
MAX_READ_TIMEOUT_SECONDS = 25.0
# Total attempts per Converse call, including the first. The SDK retries throttling, 5xx and
# timeouts with backoff inside this bound. 2 means one retry.
DEFAULT_MAX_ATTEMPTS = 2

_MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/\-]{0,2047}$")
_REGION_RE = re.compile(r"^[a-z]{2}(?:-[a-z]+)+-\d$")


@dataclass(frozen=True)
class BedrockSettings:
    model_id: str
    region: str
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS
    connect_timeout_seconds: float = DEFAULT_CONNECT_TIMEOUT_SECONDS
    read_timeout_seconds: float = DEFAULT_READ_TIMEOUT_SECONDS
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    # Opt-in. Bedrock structured output is model specific and its first call with a new schema can
    # take about 90 s to compile, which is longer than API Gateway's 30 s limit. Default: off.
    structured_output: bool = False

    @property
    def worst_case_call_seconds(self) -> float:
        """Upper bound for one Converse call including SDK retries (excluding small backoff)."""
        return self.max_attempts * (self.connect_timeout_seconds + self.read_timeout_seconds)


def _number(env: Mapping[str, str], name: str, default: float, low: float, high: float, integer: bool) -> float:
    raw = (env.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw) if integer else float(raw)
    except ValueError:
        raise ConfigError(f"{name} must be a number.") from None
    if not low <= value <= high:
        raise ConfigError(f"{name} must be between {low} and {high}.")
    return value


def load_bedrock_settings(env: Mapping[str, str]) -> BedrockSettings | None:
    """Return settings, or None if the model id or region is not configured (nothing is assumed)."""
    model_id = (env.get("BEDROCK_MODEL_ID") or "").strip()
    region = (env.get("AWS_REGION") or "").strip()
    if not model_id or not region:
        return None
    if not _MODEL_ID_RE.match(model_id):
        raise ConfigError("BEDROCK_MODEL_ID has an invalid format.")
    if not _REGION_RE.match(region):
        raise ConfigError("AWS_REGION has an invalid format.")
    flag = (env.get("BEDROCK_STRUCTURED_OUTPUT") or "off").strip().lower()
    if flag not in ("on", "off"):
        raise ConfigError("BEDROCK_STRUCTURED_OUTPUT must be 'on' or 'off'.")
    return BedrockSettings(
        model_id=model_id,
        region=region,
        max_output_tokens=int(
            _number(env, "BEDROCK_MAX_OUTPUT_TOKENS", DEFAULT_MAX_OUTPUT_TOKENS, MIN_MAX_OUTPUT_TOKENS, MAX_MAX_OUTPUT_TOKENS, True)
        ),
        read_timeout_seconds=_number(
            env, "BEDROCK_READ_TIMEOUT_SECONDS", DEFAULT_READ_TIMEOUT_SECONDS, MIN_READ_TIMEOUT_SECONDS, MAX_READ_TIMEOUT_SECONDS, False
        ),
        structured_output=flag == "on",
    )


class SdkClient(Protocol):
    def converse(self, **kwargs: Any) -> Mapping[str, Any]: ...


@dataclass(frozen=True)
class ModelReply:
    text: str
    stop_reason: str
    aws_request_id: str
    input_tokens: int | None
    output_tokens: int | None
    latency_ms: float


# --------------------------------------------------------------------------- error mapping
# (HTTP status, API code, fixed safe message). Messages never include the SDK's text, which can
# contain account ids, role names or model ARNs.
_CLIENT_ERRORS: dict[str, tuple[int, str, str]] = {
    "ThrottlingException": (429, "THROTTLED", "The language model is throttled. Retry shortly."),
    "ModelNotReadyException": (429, "THROTTLED", "The language model is not ready. Retry shortly."),
    "TooManyRequestsException": (429, "THROTTLED", "The language model is throttled. Retry shortly."),
    "AccessDeniedException": (
        502,
        "MODEL_ACCESS_DENIED",
        "The server is not allowed to use the configured language model. An administrator must check model access and permissions.",
    ),
    "ResourceNotFoundException": (
        502,
        "MODEL_NOT_FOUND",
        "The configured language model or region was not found. An administrator must check the configuration.",
    ),
    "ValidationException": (
        502,
        "MODEL_REQUEST_REJECTED",
        "The language model rejected the request. An administrator must check the model configuration.",
    ),
    "ModelTimeoutException": (502, "MODEL_TIMEOUT", "The language model took too long to answer."),
    "ModelErrorException": (502, "MODEL_FAILURE", "The language model failed to process the request."),
    "InternalServerException": (503, "UNAVAILABLE", "The language model service is temporarily unavailable."),
    "ServiceUnavailableException": (503, "UNAVAILABLE", "The language model service is temporarily unavailable."),
}
_CREDENTIAL_ERRORS = (NoCredentialsError, PartialCredentialsError, CredentialRetrievalError)


def aws_request_id_from(response: Mapping[str, Any] | None) -> str:
    meta = (response or {}).get("ResponseMetadata")
    rid = meta.get("RequestId") if isinstance(meta, Mapping) else None
    return clean_text(rid, 64) if rid else "unknown"


def map_client_error(exc: ClientError) -> ApiError:
    code = str(exc.response.get("Error", {}).get("Code", ""))
    status, api_code, message = _CLIENT_ERRORS.get(
        code, (502, "MODEL_FAILURE", "The language model call failed.")
    )
    if status == 429:
        return Throttled(message)
    if status == 503:
        return Unavailable(message)
    return ApiError(status, api_code, message)


def map_botocore_error(exc: BotoCoreError) -> ApiError:
    if isinstance(exc, ReadTimeoutError):
        return ApiError(502, "MODEL_TIMEOUT", "The language model took too long to answer.")
    if isinstance(exc, (ConnectTimeoutError, EndpointConnectionError)):
        return Unavailable("The language model service could not be reached.")
    if isinstance(exc, _CREDENTIAL_ERRORS):
        return Unavailable("AWS credentials are not available to the server. An administrator must check the execution role.")
    return Unavailable("The language model service is unavailable.")


# --------------------------------------------------------------------------- client
class BedrockClient:
    def __init__(self, settings: BedrockSettings, sdk_client: SdkClient | None = None) -> None:
        self.settings = settings
        if sdk_client is None:
            sdk_client = boto3.client(
                "bedrock-runtime",
                region_name=settings.region,
                config=Config(
                    connect_timeout=settings.connect_timeout_seconds,
                    read_timeout=settings.read_timeout_seconds,
                    retries={"total_max_attempts": settings.max_attempts, "mode": "standard"},
                    user_agent_extra="trademind-b2",
                ),
            )
        self._sdk = sdk_client

    def converse(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        response_schema: dict[str, Any] | None = None,
    ) -> ModelReply:
        s = self.settings
        request: dict[str, Any] = {
            "modelId": s.model_id,
            "system": [{"text": system}],
            "messages": messages,
            "inferenceConfig": {"maxTokens": s.max_output_tokens, "temperature": 0.0},
        }
        if s.structured_output and response_schema is not None:
            request["outputConfig"] = {
                "textFormat": {
                    "type": "json_schema",
                    "structure": {
                        "jsonSchema": {
                            "schema": json.dumps(response_schema),  # the API takes the schema as a string
                            "name": "trademind_extraction",
                            "description": "Facts extracted from business descriptions",
                        }
                    },
                }
            }

        started = time.monotonic()
        try:
            raw = self._sdk.converse(**request)
        except ClientError as exc:
            error = map_client_error(exc)
            self._log("error", aws_request_id_from(exc.response), started, errorCode=exc.response.get("Error", {}).get("Code"), status=error.status)
            raise error from None
        except BotoCoreError as exc:
            error = map_botocore_error(exc)
            self._log("error", "unknown", started, errorType=type(exc).__name__, status=error.status)
            raise error from None

        reply = self._parse(raw, started)
        self._log(
            "ok",
            reply.aws_request_id,
            started,
            stopReason=reply.stop_reason,
            inputTokens=reply.input_tokens,
            outputTokens=reply.output_tokens,
        )
        return reply

    # -- helpers
    def _parse(self, raw: Mapping[str, Any], started: float) -> ModelReply:
        request_id = aws_request_id_from(raw)
        stop = raw.get("stopReason")
        if stop == "max_tokens":
            raise ApiError(502, "MODEL_OUTPUT_TRUNCATED", "The language model's answer was cut off. Shorten the descriptions and retry.")
        if stop in ("guardrail_intervened", "content_filtered"):
            raise ApiError(502, "MODEL_REFUSED", "The language model declined to answer.")
        if stop not in ("end_turn", "stop_sequence"):
            raise ApiError(502, "MODEL_FAILURE", "The language model call failed.")

        message = (raw.get("output") or {}).get("message") if isinstance(raw.get("output"), Mapping) else None
        blocks = message.get("content") if isinstance(message, Mapping) else None
        text = ""
        if isinstance(blocks, list):
            text = "".join(b["text"] for b in blocks if isinstance(b, Mapping) and isinstance(b.get("text"), str))
        if not text.strip():
            raise ApiError(502, "MODEL_EMPTY_OUTPUT", "The language model returned no text.")

        usage = raw.get("usage") if isinstance(raw.get("usage"), Mapping) else {}
        return ModelReply(
            text=text,
            stop_reason=str(stop),
            aws_request_id=request_id,
            input_tokens=usage.get("inputTokens") if isinstance(usage.get("inputTokens"), int) else None,
            output_tokens=usage.get("outputTokens") if isinstance(usage.get("outputTokens"), int) else None,
            latency_ms=round((time.monotonic() - started) * 1000, 1),
        )

    def _log(self, outcome: str, aws_request_id: str, started: float, **extra: Any) -> None:
        log.info(
            json.dumps(
                {
                    "event": "bedrock_call",
                    "outcome": outcome,
                    "awsRequestId": aws_request_id,
                    "modelId": self.settings.model_id,
                    "region": self.settings.region,
                    "latencyMs": round((time.monotonic() - started) * 1000, 1),
                    **extra,
                }
            )
        )
