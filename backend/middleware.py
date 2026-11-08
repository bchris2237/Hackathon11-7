"""Security and production hardening (B8): the checks that sit around the routes.

Everything here is a small, separately testable function. `backend/app.py` calls them in a fixed order
(see its pipeline comment). Nothing in this module talks to AWS, reads a body into a log, or returns a simulated success.

What it provides
  * `redact` / `install_log_redaction`: secret-like text is replaced in EVERY log record, whatever module wrote it,
    and exception tracebacks are reduced to the exception type (messages and frames can contain secrets).
  * `check_origin`: a request that carries an Origin header which is not on the exact allow-list is refused (403).
    CORS alone only stops a browser from READING a response; this stops the call from being processed.
  * `check_content_type`: bodies must be application/json. A browser can send text/plain or a form post cross-site
    WITHOUT a preflight, so refusing those closes the "simple request" route to state-changing endpoints.
  * `find_unsafe_text`: control characters and Unicode direction overrides in any string of a request (log forging,
    terminal escapes, "trojan source" style spoofing). Plain newlines and tabs stay allowed only inside free text.
  * `ClientRateLimiter`: a per-client token bucket with bounded memory.
  * `validate_origin_setting`: ALLOWED_ORIGIN entries must be exact origins; plain http only for localhost.
  * `security_headers`: response headers for an API that never serves a page.

Honest limits
  * The rate limiter keeps its state in memory. In Lambda each container has its own, so it is a per-container
    limit; the real global limits are API Gateway throttling and Lambda reserved concurrency (infra/template.yaml).
  * Log redaction is pattern based and best effort. It catches AWS key shapes, labelled secrets, bearer tokens,
    private-key blocks, e-mail addresses and long token-like runs. It cannot recognise an arbitrary secret that
    has no label and no recognisable shape, so the code also never logs request bodies or model text.
  * An Origin header is easy to forge outside a browser. `check_origin` protects users of browsers (CSRF-style
    abuse); it is not authentication.
"""

from __future__ import annotations

import logging
import math
import re
import threading
import time
import unicodedata
from collections import OrderedDict
from collections.abc import Callable, Iterable, Mapping
from typing import Any
from urllib.parse import urlsplit

from backend.errors import ApiError, clean_text, format_path, validation_error

# --------------------------------------------------------------------------- log redaction
REDACTED = "[REDACTED]"

_AWS_KEY_ID = re.compile(r"\b(?:AKIA|ASIA|AIDA|AROA|AGPA|ANPA|ANVA|AIPA)[A-Z0-9]{16}\b")
_LABELLED = re.compile(
    r"(?i)\b(aws_secret_access_key|secret[_-]?access[_-]?key|secretaccesskey|session[_-]?token|security[_-]?token"
    r"|x-amz-security-token|authorization|api[_-]?key|apikey|client[_-]?secret|password|passwd|secret|token"
    r"|credentials?|private[_-]?key)\b"
    r"""(["']?\s*[:=]\s*["']?)"""
    r"""(?!\[REDACTED\])([^\s"',;&}\]]+)"""
)
_BEARER = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=\-]{8,}")
_PRIVATE_KEY = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)", re.DOTALL)
_EMAIL = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)+\b")
_TOKEN_RUN = re.compile(r"(?<![A-Za-z0-9/+_\-])[A-Za-z0-9/+_\-]{40,}={0,2}(?![A-Za-z0-9/+=_\-])")


def _token_run(match: re.Match[str]) -> str:
    """A long run of token characters is a secret if it is very long, or exactly 40 and mixed (the AWS secret key shape).

    Hex digests (SHA-256, git ids) are all one case plus digits, so they are left alone.
    """
    run = match.group(0)
    if len(run) >= 100:
        return REDACTED
    mixed = any(c.islower() for c in run) and any(c.isupper() for c in run) and any(c.isdigit() for c in run)
    if len(run) == 40 and mixed:
        return REDACTED
    return run


def redact(text: str) -> str:
    """Replace secret-like content. Idempotent and safe on any string."""
    text = _PRIVATE_KEY.sub(REDACTED, text)
    text = _AWS_KEY_ID.sub(REDACTED, text)
    text = _BEARER.sub(lambda m: f"{m.group(1)} {REDACTED}", text)
    text = _LABELLED.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", text)
    text = _EMAIL.sub("[EMAIL]", text)
    return _TOKEN_RUN.sub(_token_run, text)


_INSTALLED_MARK = "_trademind_redacting"


def install_log_redaction() -> None:
    """Wrap the logging record factory once. Applies to every logger and every handler, including libraries.

    A factory is used instead of a Filter because filters on a logger are not applied to records that child loggers
    propagate to it, and Lambda's own handler is not ours to configure.
    """
    current = logging.getLogRecordFactory()
    if getattr(current, _INSTALLED_MARK, False):
        return

    def factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
        record = current(*args, **kwargs)
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - a malformed format string must not break logging
            message = str(record.msg)
        if record.exc_info:
            kind = record.exc_info[0]
            message += f" [exception {kind.__name__ if kind else 'unknown'}]"
        record.msg = redact(message)
        record.args = None
        record.exc_info = None  # no tracebacks in logs: frames and messages can carry secrets
        record.exc_text = None
        record.stack_info = None
        return record

    setattr(factory, _INSTALLED_MARK, True)
    logging.setLogRecordFactory(factory)


# --------------------------------------------------------------------------- origin and content type
def check_origin(headers: Mapping[str, str], allowed: Iterable[str]) -> None:
    """403 when a browser-supplied Origin is not exactly on the allow-list. No Origin header = not a browser = allowed."""
    origin = headers.get("origin")
    if origin is None:
        return
    if origin not in set(allowed):
        raise ApiError(403, "ORIGIN_NOT_ALLOWED", "Requests from this origin are not allowed.")


_JSON_TYPE = re.compile(r"^application/json\s*(?:;\s*charset\s*=\s*\"?utf-8\"?\s*)?$", re.IGNORECASE)


def check_content_type(headers: Mapping[str, str]) -> None:
    """A body must be declared as application/json (UTF-8). A missing header is tolerated: browsers always send one."""
    declared = headers.get("content-type")
    if declared is None:
        return
    if not _JSON_TYPE.match(declared.strip()):
        raise ApiError(415, "UNSUPPORTED_MEDIA_TYPE", "Send the body as application/json.")


# --------------------------------------------------------------------------- hostile characters
_BIDI_CONTROLS = frozenset("\u061c\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069")
_FREE_TEXT_FIELDS = frozenset({"text"})
_FREE_TEXT_WHITESPACE = frozenset("\t\n\r")
MAX_REPORTED_PATHS = 20


def _unsafe(value: str, allow_whitespace_controls: bool) -> bool:
    for ch in value:
        if ch in _BIDI_CONTROLS:
            return True
        if unicodedata.category(ch) == "Cc":
            if allow_whitespace_controls and ch in _FREE_TEXT_WHITESPACE:
                continue
            return True
    return False


def find_unsafe_text(data: Any) -> list[str]:
    """Paths of strings (or keys) holding control characters or direction overrides. Iterative: depth cannot overflow the stack."""
    found: list[str] = []
    stack: list[tuple[Any, tuple[object, ...]]] = [(data, ())]
    while stack and len(found) < MAX_REPORTED_PATHS:
        value, loc = stack.pop()
        if isinstance(value, str):
            last = loc[-1] if loc else None
            if _unsafe(value, isinstance(last, str) and last in _FREE_TEXT_FIELDS):
                found.append(format_path(loc))
        elif isinstance(value, dict):
            for key, item in value.items():
                if isinstance(key, str) and _unsafe(key, False):
                    found.append(format_path((*loc, "(key)")))
                stack.append((item, (*loc, key)))
        elif isinstance(value, list):
            for index, item in enumerate(value):
                stack.append((item, (*loc, index)))
    return sorted(set(found))[:MAX_REPORTED_PATHS]


def check_text_safety(data: Any) -> None:
    paths = find_unsafe_text(data)
    if paths:
        raise validation_error("Request contains control or direction-override characters.", paths)


# --------------------------------------------------------------------------- per-client rate limit
class ClientRateLimiter:
    """Token bucket per client. `burst` tokens at most, refilled at `rate_per_minute / 60` per second.

    Memory is bounded: at most `max_clients` buckets are kept (least recently used evicted), so an attacker rotating
    addresses cannot grow it without limit. Eviction forgets history, which is the price of the bound.
    """

    def __init__(
        self,
        rate_per_minute: int,
        burst: int,
        clock: Callable[[], float] = time.monotonic,
        max_clients: int = 1024,
    ) -> None:
        if rate_per_minute < 1 or burst < 1 or max_clients < 1:
            raise ValueError("rate, burst and max_clients must be positive")
        self._rate = rate_per_minute / 60.0
        self._burst = float(burst)
        self._clock = clock
        self._max = max_clients
        self._buckets: OrderedDict[str, tuple[float, float]] = OrderedDict()
        self._lock = threading.Lock()

    def check(self, client: str) -> int | None:
        """None if allowed; otherwise the whole seconds to wait (>= 1)."""
        now = self._clock()
        with self._lock:
            tokens, last = self._buckets.pop(client, (self._burst, now))
            tokens = min(self._burst, tokens + max(0.0, now - last) * self._rate)
            if tokens >= 1.0:
                self._buckets[client] = (tokens - 1.0, now)
                allowed = None
            else:
                self._buckets[client] = (tokens, now)
                allowed = max(1, math.ceil((1.0 - tokens) / self._rate))
            while len(self._buckets) > self._max:
                self._buckets.popitem(last=False)
            return allowed

    def __len__(self) -> int:
        return len(self._buckets)


def rate_limited(retry_after: int) -> ApiError:
    return ApiError(
        429,
        "THROTTLED",
        "Too many requests from this client. Retry shortly.",
        headers={"Retry-After": str(retry_after)},
    )


# --------------------------------------------------------------------------- configuration helpers
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "[::1]"})
_HOST_RE = re.compile(r"^(?:[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?)(?:\.[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?)*$")


def validate_origin_setting(origin: str, live: bool) -> str:
    """Return the origin if it is an exact `scheme://host[:port]`; raise ValueError otherwise.

    No wildcard, path, query, fragment or credentials. In live mode plain http is accepted only for localhost.
    """
    if "*" in origin:
        raise ValueError("wildcards are not allowed in ALLOWED_ORIGIN")
    parts = urlsplit(origin)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("each ALLOWED_ORIGIN entry must look like https://host or https://host:port")
    if parts.path or parts.query or parts.fragment or parts.username or parts.password:
        raise ValueError("ALLOWED_ORIGIN entries must not contain a path, query, fragment or credentials")
    host = parts.hostname
    bracketed = f"[{host}]" if ":" in host else host
    if bracketed not in _LOCAL_HOSTS and not _HOST_RE.match(host):
        raise ValueError("ALLOWED_ORIGIN contains an invalid host name")
    try:
        parts.port  # noqa: B018 - raises ValueError for a bad port
    except ValueError:
        raise ValueError("ALLOWED_ORIGIN contains an invalid port") from None
    if live and parts.scheme != "https" and bracketed not in _LOCAL_HOSTS:
        raise ValueError("in live mode ALLOWED_ORIGIN must be https (plain http only for localhost)")
    return origin


# --------------------------------------------------------------------------- response headers
def security_headers() -> dict[str, str]:
    """Headers for a JSON API that never serves a page: nothing may be framed, sniffed, cached or leak a referrer."""
    return {
        "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
        "X-Frame-Options": "DENY",
        "Referrer-Policy": "no-referrer",
        "Strict-Transport-Security": "max-age=63072000",
        "Permissions-Policy": "geolocation=(), microphone=(), camera=()",
    }


def client_label(client: str) -> str:
    """A short non-reversible tag for logs, so abuse can be correlated without writing addresses into logs."""
    import hashlib

    return hashlib.sha256(client.encode("utf-8")).hexdigest()[:10]


__all__ = [
    "REDACTED",
    "ClientRateLimiter",
    "check_content_type",
    "check_origin",
    "check_text_safety",
    "clean_text",
    "client_label",
    "find_unsafe_text",
    "install_log_redaction",
    "rate_limited",
    "redact",
    "security_headers",
    "validate_origin_setting",
]
