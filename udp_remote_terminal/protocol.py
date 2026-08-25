"""Binary application protocol for the UDP remote terminal.

The protocol uses one fixed-size header followed by an optional payload.  UDP
already preserves datagram boundaries, but the header lets the application
identify packet types, reject corrupted datagrams, acknowledge reliable data,
and carry terminal metadata such as window size.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
import struct
import zlib
from typing import Optional, Union


MAGIC = 0x5554  # ASCII-ish "UT" (UDP Terminal)
VERSION = 1

# Keep below typical Ethernet MTU after IPv4 + UDP headers to avoid IP fragments.
MAX_PAYLOAD = 1200


class PacketType(IntEnum):
    """Application-layer packet types."""

    DATA = 0x01
    ACK = 0x02
    HEARTBEAT = 0x03
    RESIZE = 0x04
    CLOSE = 0x05
    AUTH = 0x06
    AUTH_OK = 0x07
    AUTH_FAIL = 0x08


# magic, version, type, flags, header_len, client_id, seq, ack,
# payload_len, rows, cols, checksum
_HEADER_FORMAT = "!HBBBBIIIHHHI"
HEADER_LEN = struct.calcsize(_HEADER_FORMAT)
_CHECKSUM_OFFSET = HEADER_LEN - 4
_UINT32_MAX = 0xFFFFFFFF
_UINT16_MAX = 0xFFFF
_UINT8_MAX = 0xFF


@dataclass(frozen=True)
class Packet:
    """Decoded UDP terminal packet."""

    packet_type: PacketType
    client_id: int
    seq: int = 0
    ack: int = 0
    payload: bytes = b""
    flags: int = 0
    rows: int = 0
    cols: int = 0
    version: int = VERSION
    checksum: int = 0

    @property
    def payload_len(self) -> int:
        return len(self.payload)


class ProtocolError(ValueError):
    """Raised by strict callers when a packet cannot be encoded."""


def _ensure_uint(name: str, value: int, max_value: int) -> int:
    if not isinstance(value, int):
        raise ProtocolError(f"{name} must be an integer")
    if value < 0 or value > max_value:
        raise ProtocolError(f"{name} out of range: {value}")
    return value


def _coerce_packet_type(packet_type: Union[int, PacketType]) -> PacketType:
    try:
        return packet_type if isinstance(packet_type, PacketType) else PacketType(int(packet_type))
    except (ValueError, TypeError) as exc:
        raise ProtocolError(f"unknown packet type: {packet_type!r}") from exc


def _coerce_payload(payload: Union[bytes, bytearray, memoryview, str, None]) -> bytes:
    if payload is None:
        return b""
    if isinstance(payload, str):
        return payload.encode()
    if isinstance(payload, (bytes, bytearray, memoryview)):
        return bytes(payload)
    raise ProtocolError("payload must be bytes-like or str")


def _pack_header(
    packet_type: PacketType,
    client_id: int,
    seq: int,
    ack: int,
    payload_len: int,
    flags: int,
    rows: int,
    cols: int,
    checksum: int,
) -> bytes:
    return struct.pack(
        _HEADER_FORMAT,
        MAGIC,
        VERSION,
        int(packet_type),
        flags,
        HEADER_LEN,
        client_id,
        seq,
        ack,
        payload_len,
        rows,
        cols,
        checksum,
    )


def _checksum(header_with_zero_checksum: bytes, payload: bytes) -> int:
    return zlib.crc32(header_with_zero_checksum + payload) & _UINT32_MAX


def pack_packet(
    packet_type: Union[int, PacketType],
    client_id: int,
    seq: int = 0,
    ack: int = 0,
    payload: Union[bytes, bytearray, memoryview, str, None] = b"",
    flags: int = 0,
    rows: int = 0,
    cols: int = 0,
) -> bytes:
    """Serialize one packet.

    Raises:
        ProtocolError: if any field cannot be represented by the wire format.
    """

    packet_type = _coerce_packet_type(packet_type)
    payload_bytes = _coerce_payload(payload)

    client_id = _ensure_uint("client_id", client_id, _UINT32_MAX)
    seq = _ensure_uint("seq", seq, _UINT32_MAX)
    ack = _ensure_uint("ack", ack, _UINT32_MAX)
    flags = _ensure_uint("flags", flags, _UINT8_MAX)
    rows = _ensure_uint("rows", rows, _UINT16_MAX)
    cols = _ensure_uint("cols", cols, _UINT16_MAX)
    payload_len = _ensure_uint("payload_len", len(payload_bytes), _UINT16_MAX)

    zero_header = _pack_header(
        packet_type, client_id, seq, ack, payload_len, flags, rows, cols, 0
    )
    checksum = _checksum(zero_header, payload_bytes)
    return _pack_header(
        packet_type, client_id, seq, ack, payload_len, flags, rows, cols, checksum
    ) + payload_bytes


def unpack_packet(data: Union[bytes, bytearray, memoryview]) -> Optional[Packet]:
    """Decode one packet.

    Invalid packets are rejected by returning ``None``.  This is intentionally
    non-throwing for network receive loops: malformed UDP datagrams should be
    dropped instead of crashing the server or client.
    """

    if not isinstance(data, (bytes, bytearray, memoryview)):
        return None
    raw = bytes(data)
    if len(raw) < HEADER_LEN:
        return None

    header = raw[:HEADER_LEN]
    try:
        (
            magic,
            version,
            packet_type_value,
            flags,
            header_len,
            client_id,
            seq,
            ack,
            payload_len,
            rows,
            cols,
            checksum,
        ) = struct.unpack(_HEADER_FORMAT, header)
    except struct.error:
        return None

    if magic != MAGIC or version != VERSION or header_len != HEADER_LEN:
        return None

    if len(raw) != HEADER_LEN + payload_len:
        return None

    try:
        packet_type = PacketType(packet_type_value)
    except ValueError:
        return None

    payload = raw[HEADER_LEN:]
    zero_header = header[:_CHECKSUM_OFFSET] + b"\x00\x00\x00\x00"
    if _checksum(zero_header, payload) != checksum:
        return None

    return Packet(
        packet_type=packet_type,
        client_id=client_id,
        seq=seq,
        ack=ack,
        payload=payload,
        flags=flags,
        rows=rows,
        cols=cols,
        version=version,
        checksum=checksum,
    )


def chunk_payload(payload: bytes, max_payload: int = MAX_PAYLOAD) -> list[bytes]:
    """Split bytes into UDP-safe chunks."""

    if max_payload <= 0:
        raise ProtocolError("max_payload must be positive")
    payload = _coerce_payload(payload)
    if not payload:
        return [b""]
    return [payload[i : i + max_payload] for i in range(0, len(payload), max_payload)]
