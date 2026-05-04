import json
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from udp_remote_terminal.protocol import PacketType, pack_packet, unpack_packet  # noqa: E402
from udp_remote_terminal.server import (  # noqa: E402
    AuthenticationError,
    UDPRemoteTerminalServer,
    load_user_db,
    resolve_user_home,
    verify_password,
)


ALICE_HASH = (
    "pbkdf2_sha256$200000$YWxpY2UtZGVtby1zYWx0$"
    "/LmLKx6FMQkeMlSrhRHwqbWaREzIPk7+EHLL6BJ8Uns="
)


class ServerAuthTests(unittest.TestCase):
    def write_user_db(self, directory, users):
        path = pathlib.Path(directory) / "users.json"
        path.write_text(json.dumps(users), encoding="utf-8")
        return path

    def make_in_memory_server(self, temp_dir, users):
        server = UDPRemoteTerminalServer.__new__(UDPRemoteTerminalServer)
        server.sessions = {}
        server.authenticated_clients = {}
        server.users = load_user_db(self.write_user_db(temp_dir, users))
        server.home_root = pathlib.Path(temp_dir) / "homes"
        server.log = lambda _message: None
        server.auth_responses = []
        server.created_sessions = []
        server._send_auth_response = (
            lambda client_id, addr, packet_type, message: server.auth_responses.append(
                (client_id, addr, packet_type, message)
            )
        )
        server._create_session = (
            lambda client_id, addr, username, home_dir, rows=0, cols=0: (
                server.created_sessions.append((client_id, username, home_dir, rows, cols))
            )
        )
        return server

    def test_verify_password_accepts_correct_password_only(self):
        self.assertTrue(verify_password("123456", ALICE_HASH))
        self.assertFalse(verify_password("wrong", ALICE_HASH))
        self.assertFalse(verify_password("123456", "sha256$bad"))

    def test_load_user_db_validates_required_fields(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            user_db = self.write_user_db(
                temp_dir,
                {"alice": {"password_hash": ALICE_HASH, "home": "alice"}},
            )
            users = load_user_db(user_db)
            self.assertEqual(users["alice"].username, "alice")
            self.assertEqual(users["alice"].home, "alice")

            bad_db = self.write_user_db(temp_dir, {"bob": {"home": "bob"}})
            with self.assertRaises(AuthenticationError):
                load_user_db(bad_db)

    def test_resolve_user_home_rejects_paths_outside_home_root(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            users = {"alice": {"password_hash": ALICE_HASH, "home": "alice"}}
            user = load_user_db(self.write_user_db(temp_dir, users))["alice"]
            home = resolve_user_home(pathlib.Path(temp_dir) / "homes", user)
            self.assertEqual(home.name, "alice")

            escaping_users = {"mallory": {"password_hash": ALICE_HASH, "home": "../outside"}}
            escaping_user = load_user_db(self.write_user_db(temp_dir, escaping_users))["mallory"]
            with self.assertRaises(AuthenticationError):
                resolve_user_home(pathlib.Path(temp_dir) / "homes", escaping_user)

    def test_auth_success_creates_home_and_records_client(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            server = self.make_in_memory_server(
                temp_dir,
                {"alice": {"password_hash": ALICE_HASH, "home": "alice"}},
            )
            packet = unpack_packet(
                pack_packet(
                    PacketType.AUTH,
                    client_id=321,
                    rows=40,
                    cols=120,
                    payload=b'{"username":"alice","password":"123456"}',
                )
            )
            self.assertIsNotNone(packet)
            server._handle_auth(packet, ("127.0.0.1", 9))

            self.assertEqual(server.authenticated_clients, {321: "alice"})
            self.assertEqual(server.auth_responses[0][2], PacketType.AUTH_OK)
            self.assertEqual(server.created_sessions[0][1], "alice")
            self.assertTrue(server.created_sessions[0][2].is_dir())
            self.assertEqual(server.created_sessions[0][3:], (40, 120))

    def test_auth_failure_does_not_create_session(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            server = self.make_in_memory_server(
                temp_dir,
                {"alice": {"password_hash": ALICE_HASH, "home": "alice"}},
            )
            packet = unpack_packet(
                pack_packet(
                    PacketType.AUTH,
                    client_id=321,
                    payload=b'{"username":"alice","password":"wrong"}',
                )
            )
            self.assertIsNotNone(packet)
            server._handle_auth(packet, ("127.0.0.1", 9))

            self.assertEqual(server.authenticated_clients, {})
            self.assertEqual(server.created_sessions, [])
            self.assertEqual(server.auth_responses[0][2], PacketType.AUTH_FAIL)

    def test_auth_rejects_non_object_json_payload(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            server = self.make_in_memory_server(
                temp_dir,
                {"alice": {"password_hash": ALICE_HASH, "home": "alice"}},
            )
            packet = unpack_packet(
                pack_packet(PacketType.AUTH, client_id=321, payload=b'["alice","123456"]')
            )
            self.assertIsNotNone(packet)
            server._handle_auth(packet, ("127.0.0.1", 9))

            self.assertEqual(server.authenticated_clients, {})
            self.assertEqual(server.created_sessions, [])
            self.assertEqual(server.auth_responses[0][2], PacketType.AUTH_FAIL)

    def test_unauthenticated_data_does_not_create_session(self):
        server = UDPRemoteTerminalServer.__new__(UDPRemoteTerminalServer)
        server.sessions = {}
        server.authenticated_clients = {}
        server.log = lambda _message: None
        auth_responses = []
        server._send_auth_response = (
            lambda client_id, addr, packet_type, message: auth_responses.append(
                (client_id, addr, packet_type, message)
            )
        )

        packet = unpack_packet(
            pack_packet(PacketType.DATA, client_id=123, seq=1, payload=b"pwd\n")
        )
        self.assertIsNotNone(packet)
        server._dispatch_packet(packet, ("127.0.0.1", 9))
        self.assertEqual(server.sessions, {})
        self.assertEqual(server.authenticated_clients, {})
        self.assertEqual(auth_responses[0][2], PacketType.AUTH_FAIL)


if __name__ == "__main__":
    unittest.main()
