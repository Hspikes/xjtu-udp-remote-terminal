#!/usr/bin/env python3
"""UDP remote terminal client."""

from __future__ import annotations

import argparse
import os
import random
import selectors
import signal
import socket
import sys
import termios
import time
import tty
from typing import Optional

try:
    from .protocol import Packet, PacketType, pack_packet, unpack_packet
    from .reliable import ReliableEndpoint, PendingPacket
except ImportError:  # pragma: no cover
    from protocol import Packet, PacketType, pack_packet, unpack_packet
    from reliable import ReliableEndpoint, PendingPacket


class UDPRemoteTerminalClient:
    def __init__(
        self,
        host: str,
        port: int,
        client_id: Optional[int] = None,
        heartbeat_interval: float = 5.0,
        server_timeout: float = 20.0,
        retransmit_timeout: float = 0.5,
        window_size: int = 8,
    ) -> None:
        self.server_addr = (host, port)
        self.client_id = client_id or self._random_client_id()
        if self.client_id == 0:
            raise ValueError("client_id 0 is reserved")
        self.heartbeat_interval = heartbeat_interval
        self.server_timeout = server_timeout
        self.selector = selectors.DefaultSelector()
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.connect(self.server_addr)
        self.sock.setblocking(False)
        self.selector.register(self.sock, selectors.EVENT_READ, data="udp")
        self.stdin_fd = sys.stdin.fileno()
        self.stdout_fd = sys.stdout.fileno()
        self.old_term_attrs: Optional[list] = None
        self.reliable = ReliableEndpoint(
            client_id=self.client_id,
            window_size=window_size,
            timeout=retransmit_timeout,
        )
        self.running = True
        self.resize_pending = True
        self.last_heartbeat = 0.0
        self.last_rx = time.monotonic()

    @staticmethod
    def _random_client_id() -> int:
        return random.SystemRandom().randint(1, 0xFFFFFFFF)

    def run(self) -> int:
        if not os.isatty(self.stdin_fd):
            raise RuntimeError("client.py must be run from an interactive terminal")
        self._enter_raw_mode()
        self.selector.register(self.stdin_fd, selectors.EVENT_READ, data="stdin")
        self._install_signal_handlers()
        try:
            self._send_resize()
            self._send_heartbeat()
            while self.running:
                events = self.selector.select(timeout=0.05)
                for key, _mask in events:
                    if key.data == "stdin":
                        self._handle_stdin()
                    elif key.data == "udp":
                        self._handle_udp()

                now = time.monotonic()
                if self.resize_pending:
                    self._send_resize()
                if now - self.last_heartbeat >= self.heartbeat_interval:
                    self._send_heartbeat()
                self._flush_outbound(now)
                if now - self.last_rx > self.server_timeout:
                    self._write_status("\r\n[client] server timeout; exiting\r\n")
                    self.running = False
        finally:
            self._send_close_best_effort()
            self._restore_terminal()
            self._cleanup_selector()
            self.sock.close()
        return 0

    def _install_signal_handlers(self) -> None:
        def on_winch(_signum, _frame) -> None:
            self.resize_pending = True

        def on_stop(_signum, _frame) -> None:
            self.running = False

        signal.signal(signal.SIGWINCH, on_winch)
        signal.signal(signal.SIGTERM, on_stop)

    def _enter_raw_mode(self) -> None:
        self.old_term_attrs = termios.tcgetattr(self.stdin_fd)
        tty.setraw(self.stdin_fd)

    def _restore_terminal(self) -> None:
        if self.old_term_attrs is not None:
            termios.tcsetattr(self.stdin_fd, termios.TCSADRAIN, self.old_term_attrs)
            self.old_term_attrs = None

    def _cleanup_selector(self) -> None:
        for fileobj in (self.stdin_fd, self.sock):
            try:
                self.selector.unregister(fileobj)
            except Exception:
                pass
        self.selector.close()

    def _handle_stdin(self) -> None:
        try:
            data = os.read(self.stdin_fd, 4096)
        except BlockingIOError:
            return
        except OSError as exc:
            self._write_status(f"\r\n[client] stdin read error: {exc}\r\n")
            self.running = False
            return
        if not data:
            self.running = False
            return
        self.reliable.queue_data(data)
        self._flush_outbound(time.monotonic())

    def _handle_udp(self) -> None:
        while True:
            try:
                data = self.sock.recv(65535)
            except BlockingIOError:
                return
            except OSError as exc:
                self._write_status(f"\r\n[client] UDP receive error: {exc}\r\n")
                self.running = False
                return
            packet = unpack_packet(data)
            if packet is None or packet.client_id != self.client_id:
                continue
            self.last_rx = time.monotonic()
            self._dispatch_packet(packet)

    def _dispatch_packet(self, packet: Packet) -> None:
        if packet.packet_type == PacketType.ACK:
            self.reliable.on_ack(packet.ack)
        elif packet.packet_type == PacketType.DATA:
            if self.reliable.should_ack_data(packet.seq):
                self._send_ack(packet.seq)
            for payload in self.reliable.on_data(packet.seq, packet.payload):
                os.write(self.stdout_fd, payload)
        elif packet.packet_type == PacketType.HEARTBEAT:
            self._send_ack(packet.seq)
        elif packet.packet_type == PacketType.RESIZE:
            self._send_ack(packet.seq)
        elif packet.packet_type == PacketType.CLOSE:
            self.running = False

    def _flush_outbound(self, now: float) -> None:
        for packet in self.reliable.get_packets_to_send(now):
            self._send_pending(packet)
        for packet in self.reliable.get_packets_to_retransmit(now):
            self._send_pending(packet)
        failed = self.reliable.failed_packets()
        if failed:
            self._write_status(
                f"\r\n[client] packet seq={failed[0].seq} exceeded retry budget; "
                "server may be unreachable\r\n"
            )
            self.running = False

    def _send_pending(self, packet: PendingPacket) -> None:
        self._send_raw(
            pack_packet(
                packet.packet_type,
                client_id=self.client_id,
                seq=packet.seq,
                payload=packet.payload,
                flags=packet.flags,
                rows=packet.rows,
                cols=packet.cols,
            )
        )

    def _send_ack(self, ack: int) -> None:
        self._send_raw(pack_packet(PacketType.ACK, client_id=self.client_id, ack=ack))

    def _send_heartbeat(self) -> None:
        self._send_raw(pack_packet(PacketType.HEARTBEAT, client_id=self.client_id))
        self.last_heartbeat = time.monotonic()

    def _send_resize(self) -> None:
        rows, cols = self._terminal_size()
        self._send_raw(
            pack_packet(PacketType.RESIZE, client_id=self.client_id, rows=rows, cols=cols)
        )
        self.resize_pending = False

    def _send_close_best_effort(self) -> None:
        try:
            self._send_raw(pack_packet(PacketType.CLOSE, client_id=self.client_id))
        except Exception:
            pass

    def _send_raw(self, data: bytes) -> None:
        try:
            self.sock.send(data)
        except OSError as exc:
            self._write_status(f"\r\n[client] UDP send error: {exc}\r\n")
            self.running = False

    def _terminal_size(self) -> tuple[int, int]:
        try:
            size = os.get_terminal_size(self.stdin_fd)
            return size.lines, size.columns
        except OSError:
            return 24, 80

    def _write_status(self, message: str) -> None:
        try:
            os.write(self.stdout_fd, message.encode(errors="replace"))
        except OSError:
            pass


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="UDP remote terminal client")
    parser.add_argument("--host", required=True, help="server IP or hostname")
    parser.add_argument("--port", type=int, default=9000, help="server UDP port")
    parser.add_argument("--client-id", type=lambda value: int(value, 0), default=None)
    parser.add_argument("--heartbeat-interval", type=float, default=5.0)
    parser.add_argument("--server-timeout", type=float, default=20.0)
    parser.add_argument("--retransmit-timeout", type=float, default=0.5)
    parser.add_argument("--window-size", type=int, default=8)
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    client = UDPRemoteTerminalClient(
        host=args.host,
        port=args.port,
        client_id=args.client_id,
        heartbeat_interval=args.heartbeat_interval,
        server_timeout=args.server_timeout,
        retransmit_timeout=args.retransmit_timeout,
        window_size=args.window_size,
    )
    return client.run()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
