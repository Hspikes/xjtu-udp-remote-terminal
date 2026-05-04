#!/usr/bin/env python3
"""UDP remote terminal server with per-client PTY sessions."""

from __future__ import annotations

import argparse
import base64
import binascii
from collections import deque
from contextlib import nullcontext
from dataclasses import dataclass, field
import errno
import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path
import pty
import selectors
import signal
import socket
import struct
import sys
import termios
import threading
import time
from typing import Optional

try:  # Package import when used as module; loose import when run as script.
    from .protocol import MAX_PAYLOAD, Packet, PacketType, pack_packet, unpack_packet
    from .reliable import ReliableEndpoint, PendingPacket
    from .monitor import MonitorHttpServer
except ImportError:  # pragma: no cover
    from protocol import MAX_PAYLOAD, Packet, PacketType, pack_packet, unpack_packet
    from reliable import ReliableEndpoint, PendingPacket
    from monitor import MonitorHttpServer


DEFAULT_USER_DB = Path(__file__).with_name("users.json")
DEFAULT_HOME_ROOT = Path(__file__).with_name("user_homes")


def shell_argv(shell: str) -> list[str]:
    """Return argv for an interactive shell with host startup files disabled."""

    shell_name = Path(shell).name
    if shell_name in {"bash", "rbash"}:
        return [shell, "--noprofile", "--norc", "-i"]
    return [shell]


def build_shell_env(
    username: str,
    home_dir: Path,
    client_id: int | None = None,
    state_dir: Path | None = None,
    state_file: Path | None = None,
    cwd_file: Path | None = None,
) -> dict[str, str]:
    """Build a demo-oriented shell environment for one authenticated user."""

    env = os.environ.copy()
    home = str(home_dir)
    existing_prompt_command = env.get("PROMPT_COMMAND", "")
    prompt_command = (
        'case "$PWD" in "$HOME"|"$HOME"/*) ;; '
        '*) printf "\\r\\n[server] returned to home: access outside ~ is disabled\\r\\n"; '
        'cd "$HOME";; esac'
    )
    if state_dir is not None and state_file is not None and cwd_file is not None:
        prompt_command = (
            "__udpterm_capture() { "
            "local exit_code=$?; local hist cmd cwd; "
            'case "$PWD" in "$HOME"|"$HOME"/*) ;; '
            '*) printf "\\r\\n[server] returned to home: access outside ~ is disabled\\r\\n"; '
            'cd "$HOME";; esac; '
            'printf "%s\\n" "$PWD" > "$UDPTERM_CWD_FILE"; '
            'hist="$(HISTTIMEFORMAT= history 1 2>/dev/null)"; '
            'if [ -z "${UDPTERM_HISTORY_READY:-}" ]; then '
            'UDPTERM_LAST_HISTORY="$hist"; UDPTERM_HISTORY_READY=1; return "$exit_code"; fi; '
            'if [ -z "$hist" ] || [ "$hist" = "${UDPTERM_LAST_HISTORY:-}" ]; then '
            'return "$exit_code"; fi; '
            'UDPTERM_LAST_HISTORY="$hist"; '
            'cmd="$(printf "%s\\n" "$hist" | sed "1s/^[[:space:]]*[0-9][0-9]*[[:space:]]*//")"; '
            'cmd="${cmd//$\'\\t\'/ }"; cmd="${cmd//$\'\\r\'/ }"; cmd="${cmd//$\'\\n\'/ }"; '
            'cwd="${PWD//$\'\\t\'/ }"; '
            'if [ -n "$cmd" ]; then '
            'printf "%(%s)T\\t%s\\t%s\\n" -1 "$cwd" "$cmd" >> "$UDPTERM_STATE_FILE"; fi; '
            'return "$exit_code"; '
            "}; __udpterm_capture"
        )
    if existing_prompt_command:
        prompt_command = f"{prompt_command}; {existing_prompt_command}"
    env.update(
        {
            "HOME": home,
            "PWD": home,
            "USER": username,
            "LOGNAME": username,
            "UDPTERM_USER": username,
            "UDPTERM_HOME": home,
            "TERM": env.get("TERM", "xterm-256color"),
            "LANG": env.get("LANG", "C.UTF-8"),
            "PS1": f"{username}:\\w\\$ ",
            "PROMPT_COMMAND": prompt_command,
        }
    )
    if client_id is not None:
        env["UDPTERM_SESSION_ID"] = str(client_id)
    if state_dir is not None:
        env["UDPTERM_STATE_DIR"] = str(state_dir)
    if state_file is not None:
        env["UDPTERM_STATE_FILE"] = str(state_file)
    if cwd_file is not None:
        env["UDPTERM_CWD_FILE"] = str(cwd_file)
    return env


@dataclass(frozen=True)
class UserRecord:
    username: str
    password_hash: str
    home: str


@dataclass
class CommandRecord:
    timestamp: float
    cwd: str
    command: str


@dataclass
class ClientSession:
    client_id: int
    addr: tuple[str, int]
    username: str
    home_dir: Path
    pty_fd: int
    child_pid: int
    reliable: ReliableEndpoint
    created_at: float = field(default_factory=time.time)
    last_seen_wall: float = field(default_factory=time.time)
    state_dir: Path = field(default_factory=Path)
    current_dir: Path | None = None
    recent_commands: deque[CommandRecord] = field(default_factory=lambda: deque(maxlen=10))
    last_seen: float = field(default_factory=time.monotonic)
    rows: int = 24
    cols: int = 80
    closed: bool = False


class AuthenticationError(ValueError):
    """Raised when the authentication configuration or request is invalid."""


def load_user_db(path: os.PathLike[str] | str) -> dict[str, UserRecord]:
    """Load and validate the JSON user database."""

    user_db_path = Path(path)
    try:
        with user_db_path.open("r", encoding="utf-8") as file_obj:
            raw_users = json.load(file_obj)
    except OSError as exc:
        raise AuthenticationError(f"cannot read user database {user_db_path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise AuthenticationError(f"invalid JSON in user database {user_db_path}: {exc}") from exc

    if not isinstance(raw_users, dict):
        raise AuthenticationError("user database must be a JSON object")

    users: dict[str, UserRecord] = {}
    for username, item in raw_users.items():
        if not isinstance(username, str) or not username:
            raise AuthenticationError("user database contains an invalid username")
        if not isinstance(item, dict):
            raise AuthenticationError(f"user {username!r} must be a JSON object")
        password_hash = item.get("password_hash")
        home = item.get("home")
        if not isinstance(password_hash, str) or not password_hash:
            raise AuthenticationError(f"user {username!r} is missing password_hash")
        if not isinstance(home, str) or not home:
            raise AuthenticationError(f"user {username!r} is missing home")
        users[username] = UserRecord(
            username=username,
            password_hash=password_hash,
            home=home,
        )
    return users


def verify_password(password: str, password_hash: str) -> bool:
    """Verify a password against pbkdf2_sha256$iterations$salt$hash."""

    try:
        algorithm, iterations_text, salt_text, digest_text = password_hash.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        iterations = int(iterations_text)
        if iterations <= 0:
            return False
        salt = base64.b64decode(salt_text.encode("ascii"), validate=True)
        expected_digest = base64.b64decode(digest_text.encode("ascii"), validate=True)
    except (ValueError, UnicodeEncodeError, binascii.Error):
        return False

    actual_digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        iterations,
    )
    return hmac.compare_digest(actual_digest, expected_digest)


def resolve_user_home(home_root: os.PathLike[str] | str, user: UserRecord) -> Path:
    """Resolve a user's home directory and reject paths outside home_root."""

    root = Path(home_root).expanduser().resolve(strict=False)
    home_dir = (root / user.home).expanduser().resolve(strict=False)
    try:
        home_dir.relative_to(root)
    except ValueError as exc:
        raise AuthenticationError(
            f"home for user {user.username!r} escapes home root"
        ) from exc
    return home_dir


class UDPRemoteTerminalServer:
    def __init__(
        self,
        host: str,
        port: int,
        shell: str,
        user_db: os.PathLike[str] | str = DEFAULT_USER_DB,
        home_root: os.PathLike[str] | str = DEFAULT_HOME_ROOT,
        heartbeat_timeout: float = 15.0,
        retransmit_timeout: float = 0.5,
        window_size: int = 8,
        verbose: bool = True,
    ) -> None:
        self.host = host
        self.port = port
        self.shell = shell
        self.users = load_user_db(user_db)
        self.home_root = Path(home_root).expanduser().resolve(strict=False)
        self.heartbeat_timeout = heartbeat_timeout
        self.verbose = verbose
        self.selector = selectors.DefaultSelector()
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((host, port))
        self.sock.setblocking(False)
        self.selector.register(self.sock, selectors.EVENT_READ, data="udp")
        self.sessions: dict[int, ClientSession] = {}
        self.authenticated_clients: dict[int, str] = {}
        self._lock = threading.RLock()
        self.started_at = time.time()
        self.retransmit_timeout = retransmit_timeout
        self.window_size = window_size
        self.running = True

    def _locked(self):
        return getattr(self, "_lock", nullcontext())

    def log(self, message: str) -> None:
        if self.verbose:
            print(f"[server] {message}", file=sys.stderr, flush=True)

    def _session_state_paths(self, client_id: int) -> tuple[Path, Path, Path]:
        state_dir = self.home_root / ".udpterm_state"
        return (
            state_dir,
            state_dir / f"session_{client_id}.tsv",
            state_dir / f"session_{client_id}.cwd",
        )

    @staticmethod
    def _tail_lines(path: Path, max_lines: int = 50) -> list[str]:
        try:
            with path.open("r", encoding="utf-8", errors="replace") as file_obj:
                return list(deque(file_obj, maxlen=max_lines))
        except OSError:
            return []

    @staticmethod
    def _relative_display_path(path: Path, home_dir: Path) -> str:
        try:
            rel_path = path.resolve(strict=False).relative_to(home_dir.resolve(strict=False))
        except ValueError:
            return "."
        text = rel_path.as_posix()
        return text if text and text != "." else "."

    def _display_path_from_text(self, value: str, home_dir: Path) -> str:
        if not value:
            return "."
        return self._relative_display_path(Path(value), home_dir)

    def _read_recent_commands(self, state_file: Path, home_dir: Path) -> list[CommandRecord]:
        records: list[CommandRecord] = []
        previous: tuple[float, str, str] | None = None
        for raw_line in self._tail_lines(state_file, max_lines=50):
            line = raw_line.rstrip("\n")
            parts = line.split("\t", 2)
            if len(parts) != 3:
                continue
            timestamp_text, cwd_text, command = parts
            command = command.strip()
            if not command:
                continue
            try:
                timestamp = float(timestamp_text)
            except ValueError:
                continue
            cwd = self._display_path_from_text(cwd_text, home_dir)
            key = (timestamp, cwd, command)
            if previous == key:
                continue
            previous = key
            records.append(CommandRecord(timestamp=timestamp, cwd=cwd, command=command))
        return records[-10:]

    def _refresh_session_monitor_state(self, session: ClientSession) -> None:
        _state_dir, state_file, cwd_file = self._session_state_paths(session.client_id)
        try:
            cwd_text = cwd_file.read_text(encoding="utf-8", errors="replace").splitlines()[0]
        except (OSError, IndexError):
            cwd_text = ""
        if cwd_text:
            current_dir = Path(cwd_text).resolve(strict=False)
            try:
                current_dir.relative_to(session.home_dir.resolve(strict=False))
            except ValueError:
                current_dir = session.home_dir
            session.current_dir = current_dir
        commands = self._read_recent_commands(state_file, session.home_dir)
        session.recent_commands.clear()
        session.recent_commands.extend(commands)

    def _session_snapshot(self, session: ClientSession, include_commands: bool = False) -> dict:
        self._refresh_session_monitor_state(session)
        now = time.time()
        current_dir = session.current_dir or session.home_dir
        snapshot = {
            "client_id": session.client_id,
            "username": session.username,
            "ip": session.addr[0],
            "udp_port": session.addr[1],
            "connected_at": session.created_at,
            "last_seen_at": session.last_seen_wall,
            "idle_seconds": max(0.0, now - session.last_seen_wall),
            "home": ".",
            "current_dir": self._relative_display_path(current_dir, session.home_dir),
            "rows": session.rows,
            "cols": session.cols,
            "pending_packets": session.reliable.pending_count,
            "unacked_packets": session.reliable.unacked_count,
        }
        if include_commands:
            snapshot["recent_commands"] = [
                {
                    "timestamp": record.timestamp,
                    "cwd": record.cwd,
                    "command": record.command,
                }
                for record in session.recent_commands
            ]
        return snapshot

    def snapshot_sessions(self, include_commands: bool = False) -> list[dict]:
        with self._locked():
            return [
                self._session_snapshot(session, include_commands=include_commands)
                for session in self.sessions.values()
                if not session.closed
            ]

    def snapshot_session(self, client_id: int, include_commands: bool = True) -> dict | None:
        with self._locked():
            session = self.sessions.get(client_id)
            if session is None or session.closed:
                return None
            return self._session_snapshot(session, include_commands=include_commands)

    def snapshot_stats(self) -> dict:
        sessions = self.snapshot_sessions(include_commands=False)
        return {
            "server": {
                "host": self.host,
                "port": self.port,
                "started_at": getattr(self, "started_at", time.time()),
            },
            "summary": {
                "active_sessions": len(sessions),
                "known_users": len(self.users),
                "connected_users": len({session["username"] for session in sessions}),
            },
        }

    def list_session_files(self, client_id: int, path: str | None = None) -> dict:
        with self._locked():
            session = self.sessions.get(client_id)
            if session is None or session.closed:
                raise KeyError(client_id)
            self._refresh_session_monitor_state(session)
            home_dir = session.home_dir.resolve(strict=False)
            if not path or path == ".":
                base = (session.current_dir or session.home_dir).resolve(strict=False)
            else:
                base = (home_dir / path).resolve(strict=False)
            try:
                base.relative_to(home_dir)
            except ValueError as exc:
                raise PermissionError("path escapes session home") from exc
            if not base.exists():
                raise FileNotFoundError(path or ".")
            if not base.is_dir():
                raise NotADirectoryError(path or ".")

            entries = []
            for item in sorted(base.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower())):
                try:
                    resolved = item.resolve(strict=False)
                    resolved.relative_to(home_dir)
                    stat_result = item.stat()
                except (OSError, ValueError):
                    continue
                if item.is_dir():
                    item_type = "directory"
                    size = 0
                else:
                    item_type = "file"
                    size = stat_result.st_size
                entries.append(
                    {
                        "name": item.name,
                        "path": self._relative_display_path(resolved, session.home_dir),
                        "type": item_type,
                        "size": size,
                        "mtime": stat_result.st_mtime,
                    }
                )
            return {
                "base_path": self._relative_display_path(base, session.home_dir),
                "entries": entries,
            }

    def serve_forever(self) -> None:
        self.log(
            f"listening on {self.host}:{self.port}, shell={self.shell}, "
            f"users={len(self.users)}, home_root={self.home_root}"
        )
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
        with self._locked():
            client_ids = list(self.sessions)
        for client_id in client_ids:
            self._close_session(client_id, notify=False)
        with self._locked():
            self.authenticated_clients.clear()
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

        with self._locked():
            session = self.sessions.get(packet.client_id)
        if session is None:
            if packet.packet_type == PacketType.ACK:
                return
            if packet.packet_type != PacketType.AUTH:
                self.log(
                    f"rejected unauthenticated {packet.packet_type.name} "
                    f"from client {packet.client_id}@{addr}"
                )
                self._send_auth_response(
                    packet.client_id,
                    addr,
                    PacketType.AUTH_FAIL,
                    "authentication required",
                )
                return
            self._handle_auth(packet, addr)
            return
        else:
            with self._locked():
                session.addr = addr  # Allow NAT rebinding / client restart on same id.
                session.last_seen = time.monotonic()
                session.last_seen_wall = time.time()

        if packet.packet_type == PacketType.ACK:
            session.reliable.on_ack(packet.ack)
            self._flush_session(session, time.monotonic())
        elif packet.packet_type == PacketType.AUTH:
            self._send_auth_response(
                packet.client_id,
                addr,
                PacketType.AUTH_OK,
                "already authenticated",
            )
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

    def _handle_auth(self, packet: Packet, addr: tuple[str, int]) -> None:
        try:
            credentials = json.loads(packet.payload.decode("utf-8"))
            if not isinstance(credentials, dict):
                raise AuthenticationError("AUTH payload must be a JSON object")
            username = credentials.get("username")
            password = credentials.get("password")
            if not isinstance(username, str) or not isinstance(password, str):
                raise AuthenticationError("AUTH payload must include username and password")
            user = self.users.get(username)
            if user is None or not verify_password(password, user.password_hash):
                raise AuthenticationError("invalid username or password")
            home_dir = resolve_user_home(self.home_root, user)
            home_dir.mkdir(parents=True, exist_ok=True)
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_auth_response(
                packet.client_id,
                addr,
                PacketType.AUTH_FAIL,
                "invalid AUTH payload",
            )
            return
        except (AuthenticationError, OSError) as exc:
            self._send_auth_response(
                packet.client_id,
                addr,
                PacketType.AUTH_FAIL,
                str(exc),
            )
            return

        with self._locked():
            self.authenticated_clients[packet.client_id] = username
        try:
            self._create_session(
                packet.client_id,
                addr,
                username=username,
                home_dir=home_dir,
                rows=packet.rows,
                cols=packet.cols,
            )
        except OSError as exc:
            with self._locked():
                self.authenticated_clients.pop(packet.client_id, None)
            self._send_auth_response(
                packet.client_id,
                addr,
                PacketType.AUTH_FAIL,
                f"failed to create session: {exc}",
            )
            return
        self._send_auth_response(packet.client_id, addr, PacketType.AUTH_OK, "ok")

    def _create_session(
        self,
        client_id: int,
        addr: tuple[str, int],
        username: str,
        home_dir: Path,
        rows: int = 0,
        cols: int = 0,
    ) -> ClientSession:
        state_dir, state_file, cwd_file = self._session_state_paths(client_id)
        state_dir.mkdir(parents=True, exist_ok=True)
        cwd_file.write_text(f"{home_dir}\n", encoding="utf-8")
        state_file.touch(exist_ok=True)

        pid, fd = pty.fork()
        if pid == 0:  # Child shell process.
            try:
                os.chdir(home_dir)
                os.execvpe(
                    self.shell,
                    shell_argv(self.shell),
                    build_shell_env(
                        username,
                        home_dir,
                        client_id=client_id,
                        state_dir=state_dir,
                        state_file=state_file,
                        cwd_file=cwd_file,
                    ),
                )
            except Exception as exc:  # pragma: no cover - child exits immediately
                os.write(2, f"exec shell failed: {exc}\n".encode())
                os._exit(127)

        self._set_nonblocking(fd)
        session = ClientSession(
            client_id=client_id,
            addr=addr,
            username=username,
            home_dir=home_dir,
            pty_fd=fd,
            child_pid=pid,
            reliable=ReliableEndpoint(
                client_id=client_id,
                window_size=self.window_size,
                timeout=self.retransmit_timeout,
                max_payload=MAX_PAYLOAD,
            ),
            state_dir=state_dir,
            current_dir=home_dir,
            rows=rows or 24,
            cols=cols or 80,
        )
        with self._locked():
            self.sessions[client_id] = session
        self.selector.register(fd, selectors.EVENT_READ, data=("pty", client_id))
        self._resize_pty(session, session.rows, session.cols)
        self.log(
            f"client {client_id} authenticated as {username!r} from {addr}, "
            f"home={home_dir}, shell pid={pid}"
        )
        return session

    @staticmethod
    def _set_nonblocking(fd: int) -> None:
        flags = fcntl.fcntl(fd, fcntl.F_GETFL)
        fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

    def _handle_pty(self, client_id: int) -> None:
        with self._locked():
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
        with self._locked():
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

    def _send_auth_response(
        self,
        client_id: int,
        addr: tuple[str, int],
        packet_type: PacketType,
        message: str,
    ) -> None:
        try:
            self.sock.sendto(
                pack_packet(packet_type, client_id=client_id, payload=message),
                addr,
            )
        except OSError as exc:
            self.log(f"send auth response to {client_id}@{addr} failed: {exc}")

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
        with self._locked():
            sessions = list(self.sessions.values())
        for session in sessions:
            self._flush_session(session, now)

    def _cleanup_idle(self, now: float) -> None:
        with self._locked():
            items = list(self.sessions.items())
        for client_id, session in items:
            if now - session.last_seen > self.heartbeat_timeout:
                self.log(f"client {client_id} heartbeat timeout; closing session")
                self._close_session(client_id, notify=False)

    def _close_session(self, client_id: int, notify: bool = True) -> None:
        with self._locked():
            self.authenticated_clients.pop(client_id, None)
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
        for path in self._session_state_paths(client_id)[1:]:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                self.log(f"failed to remove state file {path}: {exc}")
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
            with self._locked():
                items = list(self.sessions.items())
            for client_id, session in items:
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
    parser.add_argument(
        "--user-db",
        default=str(DEFAULT_USER_DB),
        help="JSON user database with PBKDF2 password hashes",
    )
    parser.add_argument(
        "--home-root",
        default=str(DEFAULT_HOME_ROOT),
        help="root directory for authenticated users' initial shell directories",
    )
    parser.add_argument("--heartbeat-timeout", type=float, default=15.0)
    parser.add_argument("--retransmit-timeout", type=float, default=0.5)
    parser.add_argument("--window-size", type=int, default=8)
    parser.add_argument("--monitor-host", default="127.0.0.1", help="monitor HTTP bind address")
    parser.add_argument("--monitor-port", type=int, default=9100, help="monitor HTTP port")
    parser.add_argument(
        "--disable-monitor",
        action="store_true",
        help="disable the read-only monitor HTTP API",
    )
    parser.add_argument(
        "--monitor-token",
        default=os.environ.get("UDPTERM_MONITOR_TOKEN"),
        help="optional bearer token for /api monitor requests",
    )
    parser.add_argument("--quiet", action="store_true", help="suppress server logs")
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    server = UDPRemoteTerminalServer(
        host=args.host,
        port=args.port,
        shell=args.shell,
        user_db=args.user_db,
        home_root=args.home_root,
        heartbeat_timeout=args.heartbeat_timeout,
        retransmit_timeout=args.retransmit_timeout,
        window_size=args.window_size,
        verbose=not args.quiet,
    )
    monitor = None
    if not args.disable_monitor:
        monitor = MonitorHttpServer(
            server,
            host=args.monitor_host,
            port=args.monitor_port,
            token=args.monitor_token,
        )
        monitor.start()
        server.log(f"monitor HTTP API listening on {args.monitor_host}:{args.monitor_port}")

    def stop(_signum, _frame) -> None:
        server.running = False

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    try:
        server.serve_forever()
    finally:
        if monitor is not None:
            monitor.stop()
        server.shutdown()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
