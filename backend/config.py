"""Environment-driven settings. Names match .env.example. No secrets live here."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass

# Local dev: developers open the Vite server as either hostname. Production sets one exact origin.
DEFAULT_ALLOWED_ORIGINS = "http://127.0.0.1:5173,http://localhost:5173"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000
# API Gateway HTTP API integrations time out at 30 s, so adapters get a little less.
DEFAULT_ADAPTER_TIMEOUT_SECONDS = 25.0
MIN_ADAPTER_TIMEOUT_SECONDS = 0.1
MAX_ADAPTER_TIMEOUT_SECONDS = 28.0
DEFAULT_MAX_IN_FLIGHT = 4
MAX_MAX_IN_FLIGHT = 16
DEFAULT_PROFILE_TTL_SECONDS = 86400  # 24 hours
MIN_PROFILE_TTL_SECONDS = 300
MAX_PROFILE_TTL_SECONDS = 604800  # 7 days
_TABLE_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,255}$")
# Per-client request limit (B8). Per container: the global limits are API Gateway throttles and reserved concurrency.
DEFAULT_CLIENT_RATE_PER_MINUTE = 60
MAX_CLIENT_RATE_PER_MINUTE = 600
DEFAULT_CLIENT_BURST = 30
MAX_CLIENT_BURST = 100


class ConfigError(Exception):
    """Startup configuration is invalid."""


@dataclass(frozen=True)
class Settings:
    mode: str  # "fixture" or "live"
    commit: str
    allowed_origins: tuple[str, ...]
    host: str
    port: int
    adapter_timeout_seconds: float = DEFAULT_ADAPTER_TIMEOUT_SECONDS
    # Most adapter calls allowed at once. More get 429 with Retry-After.
    max_in_flight: int = DEFAULT_MAX_IN_FLIGHT
    # Optional profile persistence (B7). None = disabled: /profiles answers 503 PERSISTENCE_DISABLED.
    profile_table: str | None = None
    profile_ttl_seconds: int = DEFAULT_PROFILE_TTL_SECONDS
    # Requests one client address may make per minute, and how many at once (token bucket). B8.
    client_rate_per_minute: int = DEFAULT_CLIENT_RATE_PER_MINUTE
    client_burst: int = DEFAULT_CLIENT_BURST


def _bounded_int(env: Mapping[str, str], name: str, default: int, low: int, high: int) -> int:
    raw = (env.get(name) or str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer.") from exc
    if not low <= value <= high:
        raise ConfigError(f"{name} must be {low}..{high}, got {value}.")
    return value


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    env = os.environ if env is None else env

    mode = (env.get("APP_MODE") or "fixture").strip().lower()
    # 'live' is allowed to start. Routes whose real adapter is missing answer 503 instead of
    # returning anything simulated (see backend/app.py), so /health can be deployed early.
    if mode not in ("fixture", "live"):
        raise ConfigError(f"APP_MODE must be 'fixture' or 'live', got {mode!r}.")

    raw_port = (env.get("BACKEND_PORT") or str(DEFAULT_PORT)).strip()
    try:
        port = int(raw_port)
    except ValueError as exc:
        raise ConfigError(f"BACKEND_PORT must be an integer, got {raw_port!r}.") from exc
    if not 1 <= port <= 65535:
        raise ConfigError(f"BACKEND_PORT must be 1..65535, got {port}.")

    raw_timeout = (env.get("ADAPTER_TIMEOUT_SECONDS") or str(DEFAULT_ADAPTER_TIMEOUT_SECONDS)).strip()
    try:
        timeout = float(raw_timeout)
    except ValueError as exc:
        raise ConfigError(f"ADAPTER_TIMEOUT_SECONDS must be a number, got {raw_timeout!r}.") from exc
    if not MIN_ADAPTER_TIMEOUT_SECONDS <= timeout <= MAX_ADAPTER_TIMEOUT_SECONDS:
        raise ConfigError(
            f"ADAPTER_TIMEOUT_SECONDS must be {MIN_ADAPTER_TIMEOUT_SECONDS}..{MAX_ADAPTER_TIMEOUT_SECONDS}, got {timeout}."
        )

    raw_slots = (env.get("MAX_IN_FLIGHT") or str(DEFAULT_MAX_IN_FLIGHT)).strip()
    try:
        max_in_flight = int(raw_slots)
    except ValueError as exc:
        raise ConfigError(f"MAX_IN_FLIGHT must be an integer, got {raw_slots!r}.") from exc
    if not 1 <= max_in_flight <= MAX_MAX_IN_FLIGHT:
        raise ConfigError(f"MAX_IN_FLIGHT must be 1..{MAX_MAX_IN_FLIGHT}, got {max_in_flight}.")

    profile_table = (env.get("PROFILE_TABLE") or "").strip() or None
    if profile_table is not None and not _TABLE_NAME_RE.match(profile_table):
        raise ConfigError("PROFILE_TABLE has an invalid format.")
    raw_ttl = (env.get("PROFILE_TTL_SECONDS") or str(DEFAULT_PROFILE_TTL_SECONDS)).strip()
    try:
        profile_ttl = int(raw_ttl)
    except ValueError as exc:
        raise ConfigError("PROFILE_TTL_SECONDS must be an integer.") from exc
    if not MIN_PROFILE_TTL_SECONDS <= profile_ttl <= MAX_PROFILE_TTL_SECONDS:
        raise ConfigError(f"PROFILE_TTL_SECONDS must be {MIN_PROFILE_TTL_SECONDS}..{MAX_PROFILE_TTL_SECONDS}, got {profile_ttl}.")

    origins = tuple(
        o.strip().rstrip("/") for o in (env.get("ALLOWED_ORIGIN") or DEFAULT_ALLOWED_ORIGINS).split(",") if o.strip()
    )
    if not origins or "*" in origins:
        raise ConfigError("ALLOWED_ORIGIN must list exact origins (comma separated); '*' is not allowed.")
    from backend.middleware import validate_origin_setting

    for origin in origins:
        try:
            validate_origin_setting(origin, live=(mode == "live"))
        except ValueError as exc:
            raise ConfigError(f"ALLOWED_ORIGIN is invalid: {exc}.") from None

    client_rate = _bounded_int(env, "CLIENT_RATE_PER_MINUTE", DEFAULT_CLIENT_RATE_PER_MINUTE, 1, MAX_CLIENT_RATE_PER_MINUTE)
    client_burst = _bounded_int(env, "CLIENT_BURST", DEFAULT_CLIENT_BURST, 1, MAX_CLIENT_BURST)

    return Settings(
        mode=mode,
        commit=(env.get("COMMIT_SHA") or "").strip() or "unknown",
        allowed_origins=origins,
        host=(env.get("BACKEND_HOST") or DEFAULT_HOST).strip(),
        port=port,
        adapter_timeout_seconds=timeout,
        max_in_flight=max_in_flight,
        profile_table=profile_table,
        profile_ttl_seconds=profile_ttl,
        client_rate_per_minute=client_rate,
        client_burst=client_burst,
    )
