import json
import os
import pathlib
import sys
import tempfile
import threading
import unittest
from io import BytesIO

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from udp_remote_terminal.monitor import MonitorHttpServer  # noqa: E402
from udp_remote_terminal.reliable import ReliableEndpoint  # noqa: E402
from udp_remote_terminal.server import ClientSession, UDPRemoteTerminalServer  # noqa: E402


class DummySelector:
    def unregister(self, _fileobj):
        return None


class MonitorStateTests(unittest.TestCase):
    def make_server_with_session(self, temp_dir):
        root = pathlib.Path(temp_dir)
        home = root / "homes" / "alice"
        current = home / "project"
        current.mkdir(parents=True)
        (current / "a.txt").write_text("hello", encoding="utf-8")

        state_dir = root / "homes" / ".udpterm_state"
        state_dir.mkdir(parents=True)
        state_file = state_dir / "session_1001.tsv"
        cwd_file = state_dir / "session_1001.cwd"
        cwd_file.write_text(f"{current}\n", encoding="utf-8")

        fd = os.open(os.devnull, os.O_RDONLY)
        reliable = ReliableEndpoint(client_id=1001)
        reliable.queue_data(b"pending")
        reliable.get_packets_to_send(1.0)

        server = UDPRemoteTerminalServer.__new__(UDPRemoteTerminalServer)
        server.host = "0.0.0.0"
        server.port = 9000
        server.users = {"alice": object(), "bob": object()}
        server.home_root = root / "homes"
        server.started_at = 123.0
        server._lock = threading.RLock()
        server.authenticated_clients = {1001: "alice"}
        server.selector = DummySelector()
        server.log = lambda _message: None
        session = ClientSession(
            client_id=1001,
            addr=("127.0.0.1", 54321),
            username="alice",
            home_dir=home,
            pty_fd=fd,
            child_pid=999999,
            reliable=reliable,
            created_at=100.0,
            last_seen_wall=110.0,
            state_dir=state_dir,
            current_dir=home,
            rows=40,
            cols=120,
        )
        server.sessions = {1001: session}
        return server, state_file, cwd_file, current

    def test_snapshot_sessions_returns_monitor_fields(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            server, _state_file, _cwd_file, _current = self.make_server_with_session(temp_dir)
            snapshot = server.snapshot_sessions()[0]

            self.assertEqual(snapshot["client_id"], 1001)
            self.assertEqual(snapshot["username"], "alice")
            self.assertEqual(snapshot["ip"], "127.0.0.1")
            self.assertEqual(snapshot["udp_port"], 54321)
            self.assertEqual(snapshot["connected_at"], 100.0)
            self.assertEqual(snapshot["last_seen_at"], 110.0)
            self.assertEqual(snapshot["current_dir"], "project")
            self.assertEqual(snapshot["rows"], 40)
            self.assertEqual(snapshot["cols"], 120)
            self.assertEqual(snapshot["pending_packets"], 1)
            self.assertEqual(snapshot["unacked_packets"], 1)

    def test_state_file_parser_skips_bad_lines_dedupes_and_limits(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            server, state_file, _cwd_file, current = self.make_server_with_session(temp_dir)
            lines = [
                "bad line",
                f"100\t{current}\t",
                f"101\t{current}\tcmd-duplicate",
                f"101\t{current}\tcmd-duplicate",
            ]
            for index in range(12):
                lines.append(f"{200 + index}\t{current}\tcmd-{index}")
            state_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

            detail = server.snapshot_session(1001)
            commands = detail["recent_commands"]

            self.assertEqual(len(commands), 10)
            self.assertEqual(commands[0]["command"], "cmd-2")
            self.assertEqual(commands[-1]["command"], "cmd-11")
            self.assertEqual(commands[-1]["cwd"], "project")

    def test_file_listing_uses_current_dir_and_rejects_escape(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            server, _state_file, _cwd_file, _current = self.make_server_with_session(temp_dir)

            listing = server.list_session_files(1001)

            self.assertEqual(listing["base_path"], "project")
            self.assertEqual(listing["entries"][0]["name"], "a.txt")
            self.assertEqual(listing["entries"][0]["path"], "project/a.txt")
            with self.assertRaises(PermissionError):
                server.list_session_files(1001, "../")

    def test_close_session_removes_state_files(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            server, state_file, cwd_file, _current = self.make_server_with_session(temp_dir)

            server._close_session(1001, notify=False)

            self.assertFalse(state_file.exists())
            self.assertFalse(cwd_file.exists())
            self.assertEqual(server.sessions, {})


class FakeTerminalServer:
    def snapshot_stats(self):
        return {
            "server": {"host": "0.0.0.0", "port": 9000, "started_at": 1},
            "summary": {"active_sessions": 1, "known_users": 1, "connected_users": 1},
        }

    def snapshot_sessions(self):
        return [{"client_id": 1001, "username": "alice"}]

    def snapshot_session(self, client_id):
        if client_id == 1001:
            return {"client_id": 1001, "recent_commands": []}
        return None

    def list_session_files(self, client_id, path=None):
        if client_id != 1001:
            raise KeyError(client_id)
        if path == "../":
            raise PermissionError("path escapes session home")
        return {"base_path": ".", "entries": []}


class MonitorHttpApiTests(unittest.TestCase):
    class HandlerSocket:
        def __init__(self, request: bytes):
            self.input = BytesIO(request)
            self.output = BytesIO()

        def makefile(self, mode, _buffering=None):
            if "r" in mode:
                return self.input
            return self.output

        def sendall(self, data):
            self.output.write(data)

    def handle_get(self, path, token=None, server_token=None):
        headers = ["Host: localhost"]
        if token:
            headers.append(f"Authorization: Bearer {token}")
        request = (
            f"GET {path} HTTP/1.1\r\n"
            + "\r\n".join(headers)
            + "\r\nConnection: close\r\n\r\n"
        ).encode("ascii")
        sock = self.HandlerSocket(request)
        monitor = MonitorHttpServer(FakeTerminalServer(), token=server_token)
        handler = monitor._make_handler()
        handler(sock, ("127.0.0.1", 1), object())
        response = sock.output.getvalue()
        head, body = response.split(b"\r\n\r\n", 1)
        status = int(head.split(b" ", 2)[1])
        return status, json.loads(body.decode("utf-8"))

    def test_healthz_is_public_and_api_requires_token(self):
        status, payload = self.handle_get("/healthz", server_token="secret")
        self.assertEqual(status, 200)
        self.assertEqual(payload, {"ok": True})

        status, _payload = self.handle_get("/api/stats", server_token="secret")
        self.assertEqual(status, 401)

        status, stats = self.handle_get("/api/stats", token="secret", server_token="secret")
        self.assertEqual(status, 200)
        self.assertEqual(stats["summary"]["active_sessions"], 1)

    def test_api_routes_return_expected_status_codes(self):
        status, sessions = self.handle_get("/api/sessions")
        self.assertEqual(status, 200)
        self.assertEqual(sessions[0]["client_id"], 1001)

        status, _payload = self.handle_get("/api/sessions/9999")
        self.assertEqual(status, 404)

        status, _payload = self.handle_get("/api/sessions/not-an-int")
        self.assertEqual(status, 400)

        status, _payload = self.handle_get("/api/sessions/1001/files?path=../")
        self.assertEqual(status, 403)


if __name__ == "__main__":
    unittest.main()
