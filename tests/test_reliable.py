import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from udp_remote_terminal.reliable import ReliableEndpoint  # noqa: E402


class ReliableEndpointTests(unittest.TestCase):
    def test_window_limits_initial_send(self):
        endpoint = ReliableEndpoint(client_id=1, window_size=2, timeout=0.1)
        endpoint.queue_data(b"a")
        endpoint.queue_data(b"b")
        endpoint.queue_data(b"c")

        packets = endpoint.get_packets_to_send(now=0.0)
        self.assertEqual([p.seq for p in packets], [1, 2])
        self.assertEqual(endpoint.unacked_count, 2)
        self.assertEqual(endpoint.pending_count, 3)
        self.assertEqual(endpoint.get_packets_to_send(now=0.01), [])

    def test_ack_slides_window(self):
        endpoint = ReliableEndpoint(client_id=1, window_size=2, timeout=0.1)
        for item in (b"a", b"b", b"c"):
            endpoint.queue_data(item)
        endpoint.get_packets_to_send(now=0.0)

        self.assertTrue(endpoint.on_ack(1))
        packets = endpoint.get_packets_to_send(now=0.01)
        self.assertEqual([p.seq for p in packets], [3])
        self.assertEqual(endpoint.unacked_count, 2)
        self.assertFalse(endpoint.on_ack(99))

    def test_timeout_retransmits_unacked_packets(self):
        endpoint = ReliableEndpoint(client_id=1, window_size=2, timeout=0.1)
        endpoint.queue_data(b"a")
        endpoint.queue_data(b"b")
        endpoint.get_packets_to_send(now=0.0)

        self.assertEqual(endpoint.get_packets_to_retransmit(now=0.05), [])
        retransmit = endpoint.get_packets_to_retransmit(now=0.2)
        self.assertEqual([p.seq for p in retransmit], [1, 2])
        self.assertEqual([p.attempts for p in retransmit], [2, 2])

    def test_duplicate_and_out_of_order_receive(self):
        endpoint = ReliableEndpoint(client_id=1)
        self.assertEqual(endpoint.on_data(1, b"one"), [b"one"])
        self.assertEqual(endpoint.on_data(1, b"one-again"), [])
        self.assertEqual(endpoint.on_data(3, b"three"), [])
        self.assertEqual(endpoint.on_data(2, b"two"), [b"two", b"three"])
        self.assertEqual(endpoint.on_data(3, b"three-again"), [])
        self.assertEqual(endpoint.expected_seq, 4)

    def test_ack_policy_rejects_zero_and_far_future(self):
        endpoint = ReliableEndpoint(client_id=1, receive_window=4)
        self.assertFalse(endpoint.should_ack_data(0))
        self.assertTrue(endpoint.should_ack_data(1))
        self.assertTrue(endpoint.should_ack_data(4))
        self.assertFalse(endpoint.should_ack_data(5))

    def test_payload_is_chunked(self):
        endpoint = ReliableEndpoint(client_id=1, max_payload=4)
        seqs = endpoint.queue_data(b"abcdefghi")
        self.assertEqual(seqs, [1, 2, 3])
        packets = endpoint.get_packets_to_send(now=0.0)
        self.assertEqual([p.payload for p in packets], [b"abcd", b"efgh", b"i"])

    def test_retry_budget_reports_failed_packets(self):
        endpoint = ReliableEndpoint(client_id=1, timeout=0.1, max_retries=2)
        endpoint.queue_data(b"a")
        endpoint.get_packets_to_send(now=0.0)
        endpoint.get_packets_to_retransmit(now=0.2)
        self.assertEqual(endpoint.failed_packets(), [])
        endpoint.get_packets_to_retransmit(now=0.4)
        self.assertEqual([p.seq for p in endpoint.failed_packets()], [1])


if __name__ == "__main__":
    unittest.main()
