"""Observability (B10): structured, redacted telemetry, per-request model usage, and the monthly cost model.

Three parts, each small and separately testable:

1. `emit` writes one JSON log line per event. It is ALLOWLIST based: only event names in EVENTS and field names in FIELDS are
   written, and a string value must be a short token (letters, digits and `. _ : / { } ( ) -` only). A description, a business
   name, a signed URL (it contains `?` and `=`), a credential or a sentence therefore cannot get into a telemetry line even if a
   caller passes one by mistake: the field is dropped or the value replaced, and `telemetryDropped` says how many.
   The line is also passed through `backend.middleware.redact` (B8) as a second layer.
2. `RequestUsage` collects model calls made while one request is handled (count, tokens, model time) and the request log line
   reports the totals. Tokens the SDK did not supply are reported as null ("unknown"), never as 0.
3. `estimate_monthly_cost` turns EXPLICIT usage assumptions and a table of prices into a monthly estimate. All money is
   `Decimal`. The rates in PRICES come from AWS's own Price List files (see docs/aws-costs.md for the files, dates and the
   rates), and the estimate EXCLUDES free tiers, taxes, data transfer to the internet from Lambda/API Gateway, and anything
   not listed. It is an estimate from assumptions, not a bill.

Not here: nothing in this module calls AWS, and it never reads the request body.
"""

from __future__ import annotations

import contextvars
import json
import logging
import math
import re
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from backend.middleware import redact

# --------------------------------------------------------------------------- the emitter
EVENTS = frozenset(
    {
        "request",
        "bedrock_call",
        "client_rate_limited",
        "unhandled",
        "mode_mismatch",
        "telemetry_rejected",
    }
)

FIELDS = frozenset(
    {
        "event", "requestId", "awsRequestId", "client", "method", "route", "status", "durationMs", "errorCode", "errorType",
        "mode", "outcome", "modelId", "region", "latencyMs", "stopReason", "inputTokens", "outputTokens", "modelCalls",
        "modelMs", "modelErrors", "appMode", "adapterMode", "telemetryDropped", "reason",
    }
)

_SAFE_TOKEN = re.compile(r"^[A-Za-z0-9._:/{}()\-]{1,200}$")
_INVALID = "[invalid]"
_MAX_ABS_NUMBER = 10**12


def _clean_value(value: Any) -> tuple[Any, bool]:
    """(value to write, True if the value had to be replaced)."""
    if value is None or isinstance(value, bool):
        return value, False
    if isinstance(value, int):
        return (value, False) if abs(value) < _MAX_ABS_NUMBER else (None, True)
    if isinstance(value, float):
        return (round(value, 1), False) if math.isfinite(value) and abs(value) < _MAX_ABS_NUMBER else (None, True)
    if isinstance(value, str):
        return (value, False) if _SAFE_TOKEN.match(value) and "://" not in value else (_INVALID, True)
    return _INVALID, True  # lists, dicts, objects: never serialised into telemetry


def build_record(event: str, fields: Mapping[str, Any]) -> dict[str, Any]:
    """The dict that would be logged. Pure, so tests can inspect it."""
    if event not in EVENTS:
        return {"event": "telemetry_rejected", "reason": "unknown_event", "telemetryDropped": 1}
    record: dict[str, Any] = {"event": event}
    dropped = 0
    for name, value in fields.items():
        if name not in FIELDS or name == "event":
            dropped += 1
            continue
        cleaned, replaced = _clean_value(value)
        dropped += 1 if replaced else 0
        record[name] = cleaned
    if dropped:
        record["telemetryDropped"] = dropped
    return record


def emit(logger: logging.Logger, event: str, **fields: Any) -> dict[str, Any]:
    """Write one telemetry line. Never raises: telemetry must not be able to break a request."""
    try:
        record = build_record(event, fields)
        logger.info(redact(json.dumps(record)))
        return record
    except Exception:  # noqa: BLE001
        return {}


# --------------------------------------------------------------------------- per-request model usage
class RequestUsage:
    """Model usage during ONE request. Thread safe: adapters run on worker threads."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.calls = 0
        self.errors = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.input_unknown = False
        self.output_unknown = False
        self.model_ms = 0.0

    def add(self, *, input_tokens: int | None, output_tokens: int | None, latency_ms: float, ok: bool) -> None:
        with self._lock:
            self.calls += 1
            self.errors += 0 if ok else 1
            self.model_ms += max(0.0, latency_ms)
            # A failed call has no usage by nature; only a SUCCESSFUL call without usage makes the totals unknown.
            if input_tokens is None:
                self.input_unknown = self.input_unknown or ok
            else:
                self.input_tokens += input_tokens
            if output_tokens is None:
                self.output_unknown = self.output_unknown or ok
            else:
                self.output_tokens += output_tokens

    def snapshot(self) -> dict[str, Any]:
        """Fields for the request log line. A token total is null (unknown) if any successful call did not report it."""
        with self._lock:
            return {
                "modelCalls": self.calls,
                "modelErrors": self.errors,
                "modelMs": round(self.model_ms, 1),
                "inputTokens": None if self.input_unknown else self.input_tokens,
                "outputTokens": None if self.output_unknown else self.output_tokens,
            }


_CURRENT: contextvars.ContextVar[RequestUsage | None] = contextvars.ContextVar("trademind_request_usage", default=None)


@contextmanager
def request_usage() -> Iterator[RequestUsage]:
    usage = RequestUsage()
    token = _CURRENT.set(usage)
    try:
        yield usage
    finally:
        _CURRENT.reset(token)


def record_model_call(*, input_tokens: int | None, output_tokens: int | None, latency_ms: float, ok: bool) -> None:
    """Called by the Bedrock client. A no-op outside a request (scripts, tests that call the client directly)."""
    usage = _CURRENT.get()
    if usage is not None:
        usage.add(input_tokens=input_tokens, output_tokens=output_tokens, latency_ms=latency_ms, ok=ok)


# --------------------------------------------------------------------------- the cost model
MILLION = Decimal(1_000_000)
GB = Decimal(1024) ** 3
SECONDS_PER_MONTH = Decimal(30) * 24 * 3600


@dataclass(frozen=True)
class Prices:
    """US East (N. Virginia) list prices in USD. `retrieved` and `sources` say where the numbers came from."""

    retrieved: str
    sources: str
    bedrock_input_per_million_tokens: Decimal
    bedrock_output_per_million_tokens: Decimal
    lambda_per_gb_second: Decimal  # x86, first tier
    lambda_tier1_gb_seconds: Decimal  # the estimate is only valid below this monthly volume
    lambda_per_request: Decimal
    apigw_http_per_request: Decimal  # first 300 million requests
    apigw_tier1_requests: Decimal
    logs_ingest_per_gb: Decimal
    logs_storage_per_gb_month: Decimal
    amplify_build_per_minute: Decimal
    amplify_storage_per_gb_month: Decimal
    amplify_transfer_per_gb: Decimal
    ddb_write_per_request_unit: Decimal
    ddb_read_per_request_unit: Decimal
    ddb_storage_per_gb_month: Decimal
    ddb_free_gb: Decimal


# From AWS Price List files published at https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/<offer>/current/us-east-1/index.json
# retrieved 2026-11-07. Offer publication dates: AmazonBedrockFoundationModels 2026-10-12, AmazonBedrock 2026-11-06, AWSLambda 2026-10-30,
# AmazonApiGateway 2026-10-13, AmazonCloudWatch 2026-11-04, AWSAmplify 2026-09-11, AmazonDynamoDB 2026-11-05.
_COMMON = {
    "retrieved": "2026-11-07",
    "sources": "AWS Price List files, us-east-1 (see docs/aws-costs.md)",
    "lambda_per_gb_second": Decimal("0.0000166667"),
    "lambda_tier1_gb_seconds": Decimal(6_000_000_000),
    "lambda_per_request": Decimal("0.0000002"),
    "apigw_http_per_request": Decimal("0.000001"),
    "apigw_tier1_requests": Decimal(300_000_000),
    "logs_ingest_per_gb": Decimal("0.50"),
    "logs_storage_per_gb_month": Decimal("0.03"),
    "amplify_build_per_minute": Decimal("0.01"),
    "amplify_storage_per_gb_month": Decimal("0.023"),
    "amplify_transfer_per_gb": Decimal("0.15"),
    "ddb_write_per_request_unit": Decimal("0.000000625"),
    "ddb_read_per_request_unit": Decimal("0.000000125"),
    "ddb_storage_per_gb_month": Decimal("0.25"),
    "ddb_free_gb": Decimal(25),
}

# Per model: (input, output) USD per million tokens, on-demand, US East (N. Virginia).
PRICES: dict[str, Prices] = {
    # `us.` inference profile = regional ("geo") rate in the Price List, 10% above the global-profile rate.
    "claude-haiku-4-5-us-profile": Prices(
        bedrock_input_per_million_tokens=Decimal("1.10"), bedrock_output_per_million_tokens=Decimal("5.50"), **_COMMON
    ),
    "claude-haiku-4-5-global-profile": Prices(
        bedrock_input_per_million_tokens=Decimal("1.00"), bedrock_output_per_million_tokens=Decimal("5.00"), **_COMMON
    ),
    "nova-2-lite": Prices(
        bedrock_input_per_million_tokens=Decimal("0.33"), bedrock_output_per_million_tokens=Decimal("2.75"), **_COMMON
    ),
}


@dataclass(frozen=True)
class Step:
    """One kind of request in a user session."""

    name: str
    calls_per_session: Decimal
    billed_ms: Decimal  # Lambda billed duration for ONE such request
    input_tokens: int = 0  # model tokens for ONE such request (0 = this step makes no model call)
    output_tokens: int = 0
    basis: str = "assumed"  # "measured" (from a live trace) or "assumed"

    @property
    def is_model(self) -> bool:
        return self.input_tokens > 0 or self.output_tokens > 0


@dataclass(frozen=True)
class ProfileStorage:
    """Optional DynamoDB profiles (B7). Disabled unless a scenario carries one."""

    puts_per_session: Decimal
    gets_per_session: Decimal
    item_kb: int  # one write unit per KB, one strongly consistent read unit per 4 KB
    stored_gb: Decimal


@dataclass(frozen=True)
class Assumptions:
    name: str
    sessions_per_month: int
    steps: tuple[Step, ...]
    memory_mb: int = 512
    cold_starts_per_month: int = 0
    # Measured: two cold starts on the deployed stack billed 3338 ms and 3212 ms (Duration + Init Duration in the Lambda REPORT line).
    cold_start_billed_ms: Decimal = Decimal(3300)
    log_bytes_per_request: int = 1024
    log_retention_days: int = 14
    page_loads_per_session: Decimal = Decimal(1)
    page_transfer_mb: Decimal = Decimal("0.1")
    builds_per_month: int = 10
    build_minutes: Decimal = Decimal(3)
    site_storage_mb: Decimal = Decimal(5)
    profiles: ProfileStorage | None = None


@dataclass(frozen=True)
class CostBreakdown:
    items: dict[str, Decimal]
    total: Decimal
    model_requests: Decimal
    api_requests: Decimal
    input_tokens: Decimal
    output_tokens: Decimal


class CostModelError(ValueError):
    """The assumptions fall outside the range in which the price table is valid."""


def usd(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def estimate_monthly_cost(assumptions: Assumptions, prices: Prices) -> CostBreakdown:
    """Monthly USD estimate at list price, free tiers excluded. Pure arithmetic; every line is one product of an assumption and a rate."""
    a = assumptions
    sessions = Decimal(a.sessions_per_month)
    if a.sessions_per_month < 0 or a.memory_mb <= 0:
        raise CostModelError("sessions must be >= 0 and memory > 0")
    gb_per_request = Decimal(a.memory_mb) / 1024

    api_requests = sum((s.calls_per_session * sessions for s in a.steps), Decimal(0))
    model_requests = sum((s.calls_per_session * sessions for s in a.steps if s.is_model), Decimal(0))
    input_tokens = sum((s.calls_per_session * sessions * s.input_tokens for s in a.steps), Decimal(0))
    output_tokens = sum((s.calls_per_session * sessions * s.output_tokens for s in a.steps), Decimal(0))

    busy_seconds = sum((s.calls_per_session * sessions * s.billed_ms / 1000 for s in a.steps), Decimal(0))
    cold_seconds = Decimal(a.cold_starts_per_month) * a.cold_start_billed_ms / 1000
    gb_seconds = (busy_seconds + cold_seconds) * gb_per_request
    if gb_seconds >= prices.lambda_tier1_gb_seconds or api_requests >= prices.apigw_tier1_requests:
        raise CostModelError("volume is beyond the first pricing tier the price table covers")

    log_gb = (api_requests * a.log_bytes_per_request) / GB
    stored_log_gb = log_gb * Decimal(a.log_retention_days) / 30  # average GB held during the month

    items: dict[str, Decimal] = {
        "Bedrock input tokens": input_tokens / MILLION * prices.bedrock_input_per_million_tokens,
        "Bedrock output tokens": output_tokens / MILLION * prices.bedrock_output_per_million_tokens,
        "Lambda duration": gb_seconds * prices.lambda_per_gb_second,
        "Lambda requests": api_requests * prices.lambda_per_request,
        "API Gateway HTTP API requests": api_requests * prices.apigw_http_per_request,
        "CloudWatch Logs ingestion": log_gb * prices.logs_ingest_per_gb,
        "CloudWatch Logs storage": stored_log_gb * prices.logs_storage_per_gb_month,
        "Amplify build minutes": Decimal(a.builds_per_month) * a.build_minutes * prices.amplify_build_per_minute,
        "Amplify hosting storage": a.site_storage_mb / 1024 * prices.amplify_storage_per_gb_month,
        "Amplify data served": sessions * a.page_loads_per_session * a.page_transfer_mb / 1024 * prices.amplify_transfer_per_gb,
    }
    if a.profiles is not None:
        p = a.profiles
        writes = sessions * p.puts_per_session * p.item_kb  # 1 WRU per KB
        reads = sessions * p.gets_per_session * (Decimal(-(-p.item_kb // 4)))  # 1 strongly consistent RRU per 4 KB, rounded up
        items["DynamoDB profile writes"] = writes * prices.ddb_write_per_request_unit
        items["DynamoDB profile reads"] = reads * prices.ddb_read_per_request_unit
        items["DynamoDB profile storage"] = max(Decimal(0), p.stored_gb - prices.ddb_free_gb) * prices.ddb_storage_per_gb_month
    return CostBreakdown(items, sum(items.values(), Decimal(0)), model_requests, api_requests, input_tokens, output_tokens)


# --------------------------------------------------------------------------- the assumptions the worksheet publishes
# Tokens and durations marked "measured" come from one live trace (CloudWatch, 2026-11-07, Claude Haiku 4.5 /extract on the canonical
# descriptions: 694 input and 386 output tokens, 1457 ms billed). Everything marked "assumed" has NO measurement behind it.
def _session_steps(extract_in: int, extract_out: int, basis: str) -> tuple[Step, ...]:
    return (
        Step("GET /health", Decimal(1), Decimal(3), basis="measured"),
        Step("POST /extract", Decimal(1), Decimal(1500), extract_in, extract_out, basis=basis),
        Step("POST /confirm", Decimal(1), Decimal(3), basis="measured"),
        Step("POST /optimize", Decimal(1), Decimal(5), basis="assumed"),
        Step("POST /explain", Decimal(1), Decimal(1500), 600, 200, basis="assumed"),
        Step("POST /simulate (text proposal)", Decimal(1), Decimal(1500), 500, 150, basis="assumed"),
        Step("POST /simulate (apply)", Decimal(1), Decimal(5), basis="assumed"),
    )


SCENARIOS: dict[str, Assumptions] = {
    "demo": Assumptions(
        name="Hackathon demo: 100 sessions in a month",
        sessions_per_month=100,
        steps=_session_steps(694, 386, "measured"),
        cold_starts_per_month=30,
    ),
    "busy": Assumptions(
        name="Busy public demo: 10,000 sessions in a month",
        sessions_per_month=10_000,
        steps=_session_steps(694, 386, "measured"),
        cold_starts_per_month=600,
    ),
    # Amazon Nova 2 Lite counts tokens differently. One live /extract call on the deployed stack (2026-11-07) used 674 input and 581 output
    # tokens (application log and Bedrock CloudWatch metrics agree). The other model steps keep the same assumed counts as above.
    "demo-nova": Assumptions(
        name="Hackathon demo: 100 sessions in a month (Nova extract tokens)",
        sessions_per_month=100,
        steps=_session_steps(674, 581, "measured"),
        cold_starts_per_month=30,
    ),
    "busy-nova": Assumptions(
        name="Busy public demo: 10,000 sessions in a month (Nova extract tokens)",
        sessions_per_month=10_000,
        steps=_session_steps(674, 581, "measured"),
        cold_starts_per_month=600,
    ),
}


def main(argv: list[str] | None = None) -> int:
    """`python -m backend.telemetry [--scenario demo|busy] [--model <key in PRICES>]`: print the worksheet tables for docs/aws-costs.md."""
    import argparse

    parser = argparse.ArgumentParser(description="Print the monthly cost estimate for a published scenario.")
    parser.add_argument("--scenario", choices=sorted(SCENARIOS), default="demo")
    parser.add_argument("--model", choices=sorted(PRICES), default="claude-haiku-4-5-us-profile")
    args = parser.parse_args(argv)
    assumptions, prices = SCENARIOS[args.scenario], PRICES[args.model]
    cost = estimate_monthly_cost(assumptions, prices)
    print(f"{assumptions.name}  |  model rates: {args.model}  |  prices retrieved {prices.retrieved}")
    print(f"API requests {cost.api_requests:,}  |  model requests {cost.model_requests:,}  |  input tokens {cost.input_tokens:,}  |  output tokens {cost.output_tokens:,}")
    print("| Line | Monthly USD (list price, no free tier) |\n|---|---:|")
    for name, value in cost.items.items():
        print(f"| {name} | ${usd(value):,} |")
    print(f"| **Total** | **${usd(cost.total):,}** |")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "EVENTS", "FIELDS", "PRICES", "SCENARIOS", "Assumptions", "CostBreakdown", "CostModelError", "Prices", "ProfileStorage",
    "RequestUsage", "Step", "build_record", "emit", "estimate_monthly_cost", "record_model_call", "request_usage", "usd",
]
