#!/usr/bin/env python3
"""UDP remote terminal server with per-client PTY sessions."""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import errno
import fcntl
import os
import pty
import selectors
import signal
import socket
import struct
import sys
import termios
import time
from typing import Optional

try:  # Package import when used as module; loose import when run as script.
    from .protocol import MAX_PAYLOAD, Packet, PacketType, pack_packet, unpack_packet
    from .reliable import ReliableEndpoint, PendingPacket
except ImportError:  # pragma: no cover
    from protocol import MAX_PAYLOAD, Packet, PacketType, pack_packet, unpack_packet
    from reliable import ReliableEndpoint, PendingPacket


@dataclass
class ClientSession:
    client_id: int
    addr: tuple[str, int]
    pty_fd: int
    child_pid: int
    reliable: ReliableEndpoint
    last_seen: float = field(default_factory=time.monotonic)
    rows: int = 24
    cols: int = 80
    closed: bool = False


class UDPRemoteTerminalServer:
    def __init__(
        self,
        host: str,
        port: int,
        shell: str,
        heartbeat_timeout: float = 15.0,
        retransmit_timeout: float = 0.5,
        window_size: int = 8,
        verbose: bool = True,
    ) -> None:
        self.host = host
        self.port = port
        self.shell = shell
        self.heartbeat_timeout = heartbeat_timeout
        self.verbose = verbose
        self.selector = selectors.DefaultSelector()
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((host, port))
        self.sock.setblocking(False)
        self.selector.register(self.sock, selectors.EVENT_READ, data="udp")
        self.sessions: dict[int, ClientSession] = {}
        self.retransmit_timeout = retransmit_timeout
        self.window_size = window_size
        self.running = True

    def log(self, message: str) -> None:
        if self.verbose:
            print(f"[server] {message}", file=sys.stderr, flush=True)

    def serve_forever(self) -> None:
        self.log(f"listening on {self.host}:{self.port}, shell={self.shell}")
        while self.running:
            events = self.selector.select(timeout=0.05)
            for key, _mask in events:
                if key.data == "udp":
                    self._handle_udp()
                else:
                    kind, client_id = key.data
                    if kind == "pty":
                        self._handle_pty(client_id)

            now = time.monotonic()
            self._flush_all(now)
            self._cleanup_idle(now)
            self._reap_children()

    def shutdown(self) -> None:
        self.running = False
        for client_id in list(self.sessions):
            self._close_session(client_id, notify=False)
        try:
            self.selector.unregister(self.sock)
        except Exception:
            pass
        self.sock.close()
        self.selector.close()

    def _handle_udp(self) -> None:
        while True:
            try:
                data, addr = self.sock.recvfrom(65535)
            except BlockingIOError:
                return
            except OSError as exc:
                self.log(f"UDP receive error: {exc}")
                return

            packet = unpack_packet(data)
            if packet is None:
                self.log(f"dropped invalid datagram from {addr}")
                continue
            self._dispatch_packet(packet, addr)

    def _dispatch_packet(self, packet: Packet, addr: tuple[str, int]) -> None:
        if packet.client_id == 0:
            self.log(f"dropped packet with reserved client_id 0 from {addr}")
            return

        session = self.sessions.get(packet.client_id)
        if session is None:
            if packet.packet_type == PacketType.ACK:
                return
            session = self._create_session(packet.client_id, addr, packet.rows, packet.cols)
        else:
            session.addr = addr  # Allow NAT rebinding / client restart on same id.

        session.last_seen = time.monotonic()

        if packet.packet_type == PacketType.ACK:
            session.reliable.on_ack(packet.ack)
            self._flush_session(session, time.monotonic())
        elif packet.packet_type == PacketType.DATA:
            if session.reliable.should_ack_data(packet.seq):
                self._send_ack(session, packet.seq)
            for payload in session.reliable.on_data(packet.seq, packet.payload):
                self._write_pty(session, payload)
        elif packet.packet_type == PacketType.HEARTBEAT:
            self._send_ack(session, packet.seq)
        elif packet.packet_type == PacketType.RESIZE:
            self._send_ack(session, packet.seq)
            self._resize_pty(session, packet.rows, packet.cols)
        elif packet.packet_type == PacketType.CLOSE:
            self._send_ack(session, packet.seq)
            self._close_session(packet.client_id, notify=False)

    def _create_session(
        self, client_id: int, addr: tuple[str, int], rows: int = 0, cols: int = 0
    ) -> ClientSession:
        pid, fd = pty.fork()
        if pid == 0:  # Child shell process.
            try:
                os.environ.setdefault("TERM", "xterm-256color")
                os.environ.setdefault("LANG", "C.UTF-8")
                os.execvp(self.shell, [self.shell])
            except Exception as exc:  # pragma: no cover - child exits immediately
                os.write(2, f"exec shell failed: {exc}\n".encode())
                os._exit(127)

        self._set_nonblocking(fd)
        session = ClientSession(
            client_id=client_id,
            addr=addr,
            pty_fd=fd,
            child_pid=pid,
            reliable=ReliableEndpoint(
                client_id=client_id,
                window_size=self.window_size,
                timeout=self.retransmit_timeout,
                max_payload=MAX_PAYLOAD,
            ),
            rows=rows or 24,
            cols=cols or 80,
        )
        self.sessions[client_id] = session
        self.selector.register(fd, selectors.EVENT_READ, data=("pty", client_id))
        self._resize_pty(session, session.rows, session.cols)
        self.log(f"client {client_id} connected from {addr}, shell pid={pid}")
        return session

    @staticmethod
    def _set_nonblocking(fd: int) -> None:
        flags = fcntl.fcntl(fd, fcntl.F_GETFL)
        fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

    def _handle_pty(self, client_id: int) -> None:
        session = self.sessions.get(client_id)
        if session is None:
            return
        while True:
            try:
                data = os.read(session.pty_fd, 4096)
            except BlockingIOError:
                return
            except OSError as exc:
                if exc.errno in (errno.EIO, errno.EBADF):
                    self._close_session(client_id, notify=True)
                else:
                    self.log(f"PTY read error for {client_id}: {exc}")
                    self._close_session(client_id, notify=True)
                return
            if not data:
                self._close_session(client_id, notify=True)
                return
            session.reliable.queue_data(data)
            self._flush_session(session, time.monotonic())

    def _write_pty(self, session: ClientSession, payload: bytes) -> None:
        if not payload:
            return
        try:
            os.write(session.pty_fd, payload)
        except OSError as exc:
            self.log(f"PTY write error for {session.client_id}: {exc}")
            self._close_session(session.client_id, notify=True)

    def _resize_pty(self, session: ClientSession, rows: int, cols: int) -> None:
        if rows <= 0 or cols <= 0:
            return
        session.rows = rows
        session.cols = cols
        try:
            winsize = struct.pack("HHHH", rows, cols, 0, 0)
            fcntl.ioctl(session.pty_fd, termios.TIOCSWINSZ, winsize)
        except OSError as exc:
            self.log(f"resize failed for {session.client_id}: {exc}")

    def _send_ack(self, session: ClientSession, ack: int) -> None:
        self._send_raw(
            session,
            pack_packet(PacketType.ACK, client_id=session.client_id, ack=ack),
        )

    def _send_close(self, session: ClientSession) -> None:
        self._send_raw(
            session,
            pack_packet(PacketType.CLOSE, client_id=session.client_id),
        )

    def _send_pending(self, session: ClientSession, packet: PendingPacket) -> None:
        data = pack_packet(
            packet.packet_type,
            client_id=session.client_id,
            seq=packet.seq,
            payload=packet.payload,
            flags=packet.flags,
            rows=packet.rows,
            cols=packet.cols,
        )
        self._send_raw(session, data)

    def _send_raw(self, session: ClientSession, data: bytes) -> None:
        try:
            self.sock.sendto(data, session.addr)
        except OSError as exc:
            self.log(f"send to {session.client_id}@{session.addr} failed: {exc}")

    def _flush_session(self, session: ClientSession, now: float) -> None:
        for packet in session.reliable.get_packets_to_send(now):
            self._send_pending(session, packet)
        for packet in session.reliable.get_packets_to_retransmit(now):
            self._send_pending(session, packet)
        failed = session.reliable.failed_packets()
        if failed:
            self.log(
                f"client {session.client_id} has unacked packets after "
                f"{failed[0].attempts} attempts"
            )

    def _flush_all(self, now: float) -> None:
        for session in list(self.sessions.values()):
            self._flush_session(session, now)

    def _cleanup_idle(self, now: float) -> None:
        for client_id, session in list(self.sessions.items()):
            if now - session.last_seen > self.heartbeat_timeout:
                self.log(f"client {client_id} heartbeat timeout; closing session")
                self._close_session(client_id, notify=False)

    def _close_session(self, client_id: int, notify: bool = True) -> None:
        session = self.sessions.pop(client_id, None)
        if session is None or session.closed:
            return
        session.closed = True
        if notify:
            self._send_close(session)
        try:
            self.selector.unregister(session.pty_fd)
        except Exception:
            pass
        try:
            os.close(session.pty_fd)
        except OSError:
            pass
        try:
            os.kill(session.child_pid, signal.SIGHUP)
        except ProcessLookupError:
            pass
        except OSError:
            pass
        try:
            os.waitpid(session.child_pid, os.WNOHANG)
        except ChildProcessError:
            pass
        self.log(f"client {client_id} closed")

    def _reap_children(self) -> None:
        while True:
            try:
                pid, _status = os.waitpid(-1, os.WNOHANG)
            except ChildProcessError:
                return
            except OSError:
                return
            if pid == 0:
                return
            for client_id, session in list(self.sessions.items()):
                if session.child_pid == pid:
                    self._close_session(client_id, notify=True)
                    break


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="UDP remote terminal server")
    parser.add_argument("--host", default="0.0.0.0", help="address to bind")
    parser.add_argument("--port", type=int, default=9000, help="UDP port to bind")
    parser.add_argument(
        "--shell",
        default=os.environ.get("SHELL", "/bin/bash"),
        help="shell executable for new PTY sessions",
    )
    parser.add_argument("--heartbeat-timeout", type=float, default=15.0)
    parser.add_argument("--retransmit-timeout", type=float, default=0.5)
    parser.add_argument("--window-size", type=int, default=8)
    parser.add_argument("--quiet", action="store_true", help="suppress server logs")
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    server = UDPRemoteTerminalServer(
        host=args.host,
        port=args.port,
        shell=args.shell,
        heartbeat_timeout=args.heartbeat_timeout,
        retransmit_timeout=args.retransmit_timeout,
        window_size=args.window_size,
        verbose=not args.quiet,
    )

    def stop(_signum, _frame) -> None:
        server.running = False

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    try:
        server.serve_forever()
    finally:
        server.shutdown()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
