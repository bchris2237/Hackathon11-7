"""Local development server: `python -m backend.local`.

Standard library only. Binds to 127.0.0.1 by default and logs only request id,
method, path and status. Configuration comes from environment variables
(see .env.example); this module does not read .env files.
"""

from __future__ import annotations

import json
import sys
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from backend.app import MAX_BODY_BYTES, Response, handle_request
from backend.config import ConfigError, Settings, load_settings


def make_handler(settings: Settings) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "TradeMindLocal"

        def _cors_origin(self) -> str | None:
            """Echo the request Origin only if it is in the allow-list; otherwise send no CORS header."""
            origin = self.headers.get("Origin")
            return origin if origin in settings.allowed_origins else None

        def _send(self, response: Response, request_id: str) -> None:
            payload = json.dumps(response.body).encode("utf-8")
            self.send_response(response.status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("X-Request-Id", request_id)
            self.send_header("X-TradeMind-Mode", settings.mode)
            if (allowed := self._cors_origin()) is not None:
                self.send_header("Access-Control-Allow-Origin", allowed)
            self.send_header("Vary", "Origin")
            self.end_headers()
            self.wfile.write(payload)
            sys.stderr.write(f"{request_id} {self.command} {self.path} -> {response.status}\n")

        def _drain_body(self) -> None:
            """Phase 0 ignores bodies, but read a bounded amount so clients never see a reset."""
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return
            if 0 < length <= MAX_BODY_BYTES:
                self.rfile.read(length)

        def _dispatch(self) -> None:
            request_id = uuid.uuid4().hex
            self._drain_body()
            self._send(handle_request(settings, self.command, self.path, request_id), request_id)

        do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = _dispatch

        def do_OPTIONS(self) -> None:  # CORS preflight
            self.send_response(204)
            if (allowed := self._cors_origin()) is not None:
                self.send_header("Access-Control-Allow-Origin", allowed)
                self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "Content-Type")
                self.send_header("Access-Control-Max-Age", "600")
            self.send_header("Vary", "Origin")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:  # silence default access log
            pass

    return Handler


def main() -> int:
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"Cannot start: {exc}", file=sys.stderr)
        return 2

    server = ThreadingHTTPServer((settings.host, settings.port), make_handler(settings))
    print(
        f"TradeMind backend listening on http://{settings.host}:{settings.port}  "
        f"MODE={settings.mode.upper()} (simulated, no AWS)  commit={settings.commit}",
        flush=True,
    )
    print(f"Health: http://{settings.host}:{settings.port}/health   (Ctrl+C to stop)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.", flush=True)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
