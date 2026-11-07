"""Environment-driven settings. Names match .env.example. No secrets live here."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

# Local dev: developers open the Vite server as either hostname. Production sets one exact origin.
DEFAULT_ALLOWED_ORIGINS = "http://127.0.0.1:5173,http://localhost:5173"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000


class ConfigError(Exception):
    """Startup configuration is invalid or asks for something not implemented."""


@dataclass(frozen=True)
class Settings:
    mode: str  # "fixture" or "live"
    commit: str
    allowed_origins: tuple[str, ...]
    host: str
    port: int


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    env = os.environ if env is None else env

    mode = (env.get("APP_MODE") or "fixture").strip().lower()
    if mode == "live":
        # Production startup must reject missing real dependencies, never fall back
        # silently to simulated behavior. The real adapters do not exist yet.
        raise ConfigError(
            "APP_MODE=live is unavailable: the real extraction (Bedrock), optimizer and "
            "simulation adapters are not implemented yet (Phase 0). "
            "Use APP_MODE=fixture for local development."
        )
    if mode != "fixture":
        raise ConfigError(f"APP_MODE must be 'fixture' or 'live', got {mode!r}.")

    raw_port = (env.get("BACKEND_PORT") or str(DEFAULT_PORT)).strip()
    try:
        port = int(raw_port)
    except ValueError as exc:
        raise ConfigError(f"BACKEND_PORT must be an integer, got {raw_port!r}.") from exc
    if not 1 <= port <= 65535:
        raise ConfigError(f"BACKEND_PORT must be 1..65535, got {port}.")

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
    )
