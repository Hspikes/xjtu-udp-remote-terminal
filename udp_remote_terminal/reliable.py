"""Small sliding-window reliability layer for DATA packets over UDP.

This module is intentionally transport-agnostic.  It tracks outbound payloads,
ACKs, retransmission deadlines, and inbound in-order delivery.  The client and
server decide how to serialize returned packets by using protocol.pack_packet().
"""

from __future__ import annotations

from collections import OrderedDict, deque
from dataclasses import dataclass
import time
from typing import Deque, Iterable, Optional

try:  # Allows both package imports and direct script execution.
    from .protocol import MAX_PAYLOAD, PacketType, chunk_payload
except ImportError:  # pragma: no cover - used when run as a loose script
    from protocol import MAX_PAYLOAD, PacketType, chunk_payload


@dataclass
class PendingPacket:
    """Outbound packet tracked until ACKed."""

    seq: int
    payload: bytes
    packet_type: PacketType = PacketType.DATA
    flags: int = 0
    rows: int = 0
    cols: int = 0
    sent_at: float = 0.0
    attempts: int = 0


class ReliableEndpoint:
    """Sliding-window sender plus ordered receiver.

    The same instance may be used for one UDP direction: outbound DATA uses the
    sender state; inbound DATA from the peer uses the receiver state.  ACK values
    are selective: acknowledging sequence ``N`` removes exactly packet ``N``.
    """

    def __init__(
        self,
        client_id: int,
        window_size: int = 8,
        timeout: float = 0.5,
        max_payload: int = MAX_PAYLOAD,
        max_retries: int = 20,
        receive_window: int = 256,
    ) -> None:
        if window_size <= 0:
            raise ValueError("window_size must be positive")
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        if max_payload <= 0:
            raise ValueError("max_payload must be positive")
        if max_retries <= 0:
            raise ValueError("max_retries must be positive")
        if receive_window <= 0:
            raise ValueError("receive_window must be positive")

        self.client_id = client_id
        self.window_size = window_size
        self.timeout = timeout
        self.max_payload = max_payload
        self.max_retries = max_retries
        self.receive_window = receive_window

        self._next_seq = 1
        self._send_queue: Deque[PendingPacket] = deque()
        self._unacked: "OrderedDict[int, PendingPacket]" = OrderedDict()
        self._expected_seq = 1
        self._receive_buffer: dict[int, bytes] = {}

    @property
    def next_seq(self) -> int:
        return self._next_seq

    @property
    def expected_seq(self) -> int:
        return self._expected_seq

    @property
    def pending_count(self) -> int:
        return len(self._send_queue) + len(self._unacked)

    @property
    def unacked_count(self) -> int:
        return len(self._unacked)

    def queue_data(self, payload: bytes) -> list[int]:
        """Queue bytes for reliable DATA transmission and return assigned seqs."""

        assigned: list[int] = []
        for chunk in chunk_payload(payload, self.max_payload):
            seq = self._allocate_seq()
            self._send_queue.append(PendingPacket(seq=seq, payload=chunk))
            assigned.append(seq)
        return assigned

    def queue_packet(
        self,
        payload: bytes = b"",
        packet_type: PacketType = PacketType.DATA,
        flags: int = 0,
        rows: int = 0,
        cols: int = 0,
    ) -> int:
        """Queue one reliable packet, primarily useful for tests/extensions."""

        seq = self._allocate_seq()
        self._send_queue.append(
            PendingPacket(
                seq=seq,
                payload=bytes(payload),
                packet_type=packet_type,
                flags=flags,
                rows=rows,
                cols=cols,
            )
        )
        return seq

    def _allocate_seq(self) -> int:
        seq = self._next_seq
        self._next_seq = 1 if self._next_seq >= 0xFFFFFFFF else self._next_seq + 1
        return seq

    def on_ack(self, ack: int) -> bool:
        """Mark one outbound packet as acknowledged."""

        return self._unacked.pop(ack, None) is not None

    def should_ack_data(self, seq: int) -> bool:
        """Return whether a DATA seq is valid enough to acknowledge.

        Duplicates that were already delivered should still be ACKed so the
        sender can stop retransmitting.  Packets too far beyond the receive
        buffer are not ACKed because accepting them would make the peer believe
        dropped data was safely received.
        """

        return seq > 0 and seq < self._expected_seq + self.receive_window

    def get_packets_to_send(self, now: Optional[float] = None) -> list[PendingPacket]:
        """Move queued packets into the send window and return them to transmit."""

        now = time.monotonic() if now is None else now
        ready: list[PendingPacket] = []
        while self._send_queue and len(self._unacked) < self.window_size:
            packet = self._send_queue.popleft()
            packet.sent_at = now
            packet.attempts = 1
            self._unacked[packet.seq] = packet
            ready.append(packet)
        return ready

    def get_packets_to_retransmit(self, now: Optional[float] = None) -> list[PendingPacket]:
        """Return unacked packets whose retransmission timer expired."""

        now = time.monotonic() if now is None else now
        ready: list[PendingPacket] = []
        for packet in list(self._unacked.values()):
            if now - packet.sent_at >= self.timeout:
                packet.sent_at = now
                packet.attempts += 1
                ready.append(packet)
        return ready

    def failed_packets(self) -> list[PendingPacket]:
        """Packets that exceeded the retry budget."""

        return [p for p in self._unacked.values() if p.attempts > self.max_retries]

    def on_data(self, seq: int, payload: bytes) -> list[bytes]:
        """Accept a DATA packet and return newly deliverable in-order payloads."""

        if seq < self._expected_seq:
            return []  # Already delivered duplicate.
        if seq >= self._expected_seq + self.receive_window:
            return []  # Too far ahead; likely corrupt/hostile or beyond buffer.
        if seq not in self._receive_buffer:
            self._receive_buffer[seq] = bytes(payload)

        delivered: list[bytes] = []
        while self._expected_seq in self._receive_buffer:
            delivered.append(self._receive_buffer.pop(self._expected_seq))
            self._expected_seq = 1 if self._expected_seq >= 0xFFFFFFFF else self._expected_seq + 1
        return delivered

    def outstanding_sequences(self) -> Iterable[int]:
        return tuple(self._unacked.keys())
