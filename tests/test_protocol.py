import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from udp_remote_terminal.protocol import (  # noqa: E402
    HEADER_LEN,
    MAX_PAYLOAD,
    PacketType,
    ProtocolError,
    pack_packet,
    unpack_packet,
)


class ProtocolTests(unittest.TestCase):
    def test_pack_unpack_round_trip(self):
        raw = pack_packet(
            PacketType.DATA,
            client_id=1234,
            seq=7,
            ack=6,
            payload=b"pwd\n",
            flags=1,
            rows=30,
            cols=100,
        )
        packet = unpack_packet(raw)
        self.assertIsNotNone(packet)
        self.assertEqual(packet.packet_type, PacketType.DATA)
        self.assertEqual(packet.client_id, 1234)
        self.assertEqual(packet.seq, 7)
        self.assertEqual(packet.ack, 6)
        self.assertEqual(packet.flags, 1)
        self.assertEqual(packet.rows, 30)
        self.assertEqual(packet.cols, 100)
        self.assertEqual(packet.payload, b"pwd\n")

    def test_invalid_magic_is_dropped(self):
        raw = bytearray(pack_packet(PacketType.HEARTBEAT, client_id=1))
        raw[0] ^= 0xFF
        self.assertIsNone(unpack_packet(raw))

    def test_bad_checksum_is_dropped(self):
        raw = bytearray(pack_packet(PacketType.DATA, client_id=1, seq=1, payload=b"abc"))
        raw[-1] ^= 0x01
        self.assertIsNone(unpack_packet(raw))

    def test_payload_length_mismatch_is_dropped(self):
        raw = pack_packet(PacketType.DATA, client_id=1, seq=1, payload=b"abc")
        self.assertIsNone(unpack_packet(raw[:-1]))

    def test_header_too_short_is_dropped(self):
        self.assertIsNone(unpack_packet(b"x" * (HEADER_LEN - 1)))

    def test_empty_payload_round_trip(self):
        packet = unpack_packet(pack_packet(PacketType.ACK, client_id=9, ack=44))
        self.assertIsNotNone(packet)
        self.assertEqual(packet.payload, b"")
        self.assertEqual(packet.ack, 44)

    def test_auth_packet_types_round_trip(self):
        auth_payload = b'{"username":"alice","password":"123456"}'
        auth = unpack_packet(pack_packet(PacketType.AUTH, client_id=11, payload=auth_payload))
        self.assertIsNotNone(auth)
        self.assertEqual(auth.packet_type, PacketType.AUTH)
        self.assertEqual(auth.payload, auth_payload)

        auth_ok = unpack_packet(pack_packet(PacketType.AUTH_OK, client_id=11, payload=b"ok"))
        self.assertIsNotNone(auth_ok)
        self.assertEqual(auth_ok.packet_type, PacketType.AUTH_OK)

        auth_fail = unpack_packet(
            pack_packet(PacketType.AUTH_FAIL, client_id=11, payload=b"bad password")
        )
        self.assertIsNotNone(auth_fail)
        self.assertEqual(auth_fail.packet_type, PacketType.AUTH_FAIL)

    def test_max_payload_round_trip(self):
        payload = b"x" * MAX_PAYLOAD
        packet = unpack_packet(pack_packet(PacketType.DATA, client_id=2, seq=1, payload=payload))
        self.assertIsNotNone(packet)
        self.assertEqual(packet.payload, payload)

    def test_rejects_unknown_packet_type_on_pack(self):
        with self.assertRaises(ProtocolError):
            pack_packet(99, client_id=1)


if __name__ == "__main__":
    unittest.main()
