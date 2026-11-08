"""Local development server: `python -m backend.local`.

A thin translator from http.server to `backend.app.App.handle`, so local runs exercise the exact
routing, validation and CORS code that Lambda runs. Standard library only. Binds to 127.0.0.1 by
default. Logs go through the app's structured, body-free logger. Configuration comes from
environment variables (see .env.example); this module does not read .env files.
"""

from __future__ import annotations

import logging
import socket
import sys
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from backend.app import MAX_BODY_BYTES, App, Request, default_adapters
from backend.config import ConfigError, load_settings
from backend.errors import ApiError, malformed_json
from backend.middleware import install_log_redaction

SOCKET_TIMEOUT_SECONDS = 10.0  # bounds a client that connects and then stalls
_BODY_METHODS = frozenset({"POST", "PUT", "PATCH"})


def make_handler(app: App) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "TradeMindLocal"
        sys_version = ""
        timeout = SOCKET_TIMEOUT_SECONDS

        def _read_request(self) -> Request:
            request_id = uuid.uuid4().hex
            headers = {k.lower(): v for k, v in self.headers.items()}
            body = b""
            declared: int | None = None
            error: ApiError | None = None

            if self.command in _BODY_METHODS:
                if "chunked" in headers.get("transfer-encoding", "").lower():
                    error = malformed_json("Chunked request bodies are not supported.")
                else:
                    raw_length = headers.get("content-length")
                    if raw_length is not None:
                        try:
                            declared = int(raw_length)
                            if declared < 0:
                                raise ValueError
                        except ValueError:
                            declared = None
                            error = malformed_json("Content-Length is not a valid number.")
                    if error is None and declared and declared <= MAX_BODY_BYTES:
                        try:
                            body = self.rfile.read(declared)
                        except (TimeoutError, socket.timeout, OSError):
                            error = malformed_json("Timed out reading the request body.")
                    # Over the limit: the body is deliberately left unread. App answers 413
                    # from `declared_length`, and the connection is closed after the response.
            return Request(
                self.command, self.path.split("?", 1)[0], headers, body, request_id, declared, error,
                client=str(self.client_address[0]),
            )

        def _dispatch(self) -> None:
            response = app.handle(self._read_request())
            payload = response.body_bytes()
            self.send_response(response.status)
            for name, value in response.headers.items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Connection", "close")
            self.end_headers()
            if payload:
                self.wfile.write(payload)
            self.close_connection = True

        do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = _dispatch

        def log_message(self, format: str, *args: object) -> None:  # app.handle logs structured lines
            pass

    return Handler


def main() -> int:
    install_log_redaction()
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"Cannot start: {exc}", file=sys.stderr)
        return 2

    try:
        adapters = default_adapters(settings)
    except ConfigError as exc:
        print(f"Cannot start: {exc}", file=sys.stderr)
        return 2
    app = App(settings, adapters)
    server = ThreadingHTTPServer((settings.host, settings.port), make_handler(app))
    server.daemon_threads = True
    label = "FIXTURE (simulated, no AWS)" if settings.mode == "fixture" else "LIVE (real adapters required)"
    print(
        f"TradeMind backend listening on http://{settings.host}:{settings.port}  MODE={label}  commit={settings.commit}",
        flush=True,
    )
    print("Routes without an implemented adapter answer 503 NOT_IMPLEMENTED.", flush=True)
    print(f"Health: http://{settings.host}:{settings.port}/health   (Ctrl+C to stop)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.", flush=True)
    finally:
        server.server_close()
        app.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
