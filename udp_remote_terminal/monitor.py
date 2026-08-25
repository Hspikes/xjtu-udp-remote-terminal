"""Read-only HTTP monitor API for the UDP remote terminal server."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
import threading
from typing import Any
from urllib.parse import parse_qs, urlparse


class MonitorHttpServer:
    """Small standard-library HTTP server exposing UDP terminal snapshots."""

    def __init__(
        self,
        terminal_server,
        host: str = "127.0.0.1",
        port: int = 9100,
        token: str | None = None,
    ) -> None:
        self.terminal_server = terminal_server
        self.host = host
        self.port = port
        self.token = token or None
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._httpd is not None:
            return

        handler = self._make_handler()
        self._httpd = ThreadingHTTPServer((self.host, self.port), handler)
        self._httpd.daemon_threads = True
        self._thread = threading.Thread(
            target=self._httpd.serve_forever,
            name="udpterm-monitor-http",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        if self._httpd is None:
            return
        self._httpd.shutdown()
        self._httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        self._httpd = None
        self._thread = None

    def _make_handler(self):
        terminal_server = self.terminal_server
        token = self.token

        class MonitorRequestHandler(BaseHTTPRequestHandler):
            server_version = "UDPTermMonitor/1.0"

            def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
                parsed = urlparse(self.path)
                path = parsed.path.rstrip("/") or "/"
                if path == "/healthz":
                    self._send_json(200, {"ok": True})
                    return
                if not path.startswith("/api"):
                    self._send_json(404, {"error": "not found"})
                    return
                if not self._authorized():
                    self._send_json(401, {"error": "unauthorized"})
                    return

                try:
                    self._route_api(path, parsed.query)
                except ValueError as exc:
                    self._send_json(400, {"error": str(exc)})
                except KeyError:
                    self._send_json(404, {"error": "session not found"})
                except PermissionError as exc:
                    self._send_json(403, {"error": str(exc)})
                except FileNotFoundError:
                    self._send_json(404, {"error": "path not found"})
                except NotADirectoryError:
                    self._send_json(400, {"error": "path is not a directory"})

            def _route_api(self, path: str, query: str) -> None:
                if path == "/api/stats":
                    self._send_json(200, terminal_server.snapshot_stats())
                    return
                if path == "/api/sessions":
                    self._send_json(200, terminal_server.snapshot_sessions())
                    return

                parts = path.strip("/").split("/")
                if len(parts) < 3 or parts[:2] != ["api", "sessions"]:
                    self._send_json(404, {"error": "not found"})
                    return
                try:
                    client_id = int(parts[2], 10)
                except ValueError as exc:
                    raise ValueError("invalid client_id") from exc

                if len(parts) == 3:
                    snapshot = terminal_server.snapshot_session(client_id)
                    if snapshot is None:
                        raise KeyError(client_id)
                    self._send_json(200, snapshot)
                    return

                if len(parts) == 4 and parts[3] == "files":
                    query_params = parse_qs(query, keep_blank_values=True)
                    requested_path = query_params.get("path", [None])[0]
                    self._send_json(
                        200,
                        terminal_server.list_session_files(client_id, requested_path),
                    )
                    return

                self._send_json(404, {"error": "not found"})

            def _authorized(self) -> bool:
                if token is None:
                    return True
                header = self.headers.get("Authorization", "")
                prefix = "Bearer "
                if not header.startswith(prefix):
                    return False
                return hmac.compare_digest(header[len(prefix) :], token)

            def _send_json(self, status: int, payload: Any) -> None:
                body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
                    "utf-8"
                )
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format: str, *args) -> None:
                return

        return MonitorRequestHandler
