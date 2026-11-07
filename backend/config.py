"""Environment-driven settings. Names match .env.example. No secrets live here."""

from __future__ import annotations

import os
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

    origins = tuple(
        o.strip().rstrip("/") for o in (env.get("ALLOWED_ORIGIN") or DEFAULT_ALLOWED_ORIGINS).split(",") if o.strip()
    )
    if not origins or "*" in origins:
        raise ConfigError("ALLOWED_ORIGIN must list exact origins (comma separated); '*' is not allowed.")

    return Settings(
        mode=mode,
        commit=(env.get("COMMIT_SHA") or "").strip() or "unknown",
        allowed_origins=origins,
        host=(env.get("BACKEND_HOST") or DEFAULT_HOST).strip(),
        port=port,
        adapter_timeout_seconds=timeout,
        max_in_flight=max_in_flight,
    )
