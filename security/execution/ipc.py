"""Linux AF_UNIX broker protocol with kernel peer identity and bounded framing.

One connection carries one length-prefixed JSON request and one response. The server
is sequential (one active client); backlog and absolute I/O deadlines are bounded.
Trusted handlers MUST bound their own browser/network operation time.

Peer-denial contract (2026-09-21 race repair): when the broker denies an unmapped
peer UID it flushes the structured peer_denied response and half-closes with a
bounded linger so a still-sending client observes the denial, not a transport
error. Residual transport errors (BrokenPipeError/ConnectionResetError) inside the
handshake window are denial-equivalent on the client: the broker dispatches only
for a validated mapped peer, so a transport failure between connect and the
structured response can never indicate a dispatch. Fail-closed either way.
"""
from __future__ import annotations
import os
from pathlib import Path
import socket
import stat
import struct
import threading
import time
from types import MappingProxyType
from .boundary import Boundary, ExecutionResult
from .envelope import ActionEnvelope, InvalidRequest, MAX_WIRE, canonical, identifier, strict_json

MAX_RESPONSE = 4096
# Bounded linger after a peer denial: the broker half-closes (SHUT_WR) and drains
# inbound bytes up to this long so a still-sending client's send can complete.
DENIAL_LINGER_SECONDS = 1.0


def peer_uid(connection: socket.socket) -> int:
    if not hasattr(socket, "SO_PEERCRED"):
        raise RuntimeError("linux_peer_credentials_required")
    pid, uid, gid = struct.unpack("3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
    if pid <= 0 or uid < 0 or gid < 0:
        raise RuntimeError("invalid_peer_credentials")
    return uid


def _read_exact(connection, count, deadline):
    chunks = []
    left = count
    while left:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("io_deadline")
        connection.settimeout(remaining)
        part = connection.recv(left)
        if not part:
            raise InvalidRequest("truncated_frame")
        chunks.append(part)
        left -= len(part)
    return b"".join(chunks)


def receive(connection, *, limit, deadline):
    size = struct.unpack("!I", _read_exact(connection, 4, deadline))[0]
    if not 0 < size <= limit:
        raise InvalidRequest("frame_size")
    return _read_exact(connection, size, deadline)


def send(connection, data, *, limit, deadline):
    if not 0 < len(data) <= limit:
        raise InvalidRequest("frame_size")
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("io_deadline")
    connection.settimeout(remaining)
    connection.sendall(struct.pack("!I", len(data)) + data)


def _safe_parent(path: Path):
    if not path.is_absolute() or ".." in path.parts or not path.name:
        raise ValueError("absolute_socket_path_required")
    parent = path.parent
    for component in (parent, *parent.parents):
        info = component.lstat()
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError("symlink_or_non_directory_parent")
        # A root-owned sticky /tmp is acceptable as an ancestor, never as direct
        # socket parent. All other ancestors must be non-writable by outsiders.
        sticky_root = (component != parent and info.st_uid == 0 and
                       bool(info.st_mode & stat.S_ISVTX))
        if info.st_uid not in (0, os.geteuid()):
            raise ValueError("untrusted_socket_ancestor_owner")
        if info.st_mode & 0o022 and not sticky_root:
            raise ValueError("untrusted_socket_parent")
    parent_info = parent.lstat()
    if parent_info.st_uid != os.geteuid() or parent_info.st_mode & 0o022:
        raise ValueError("untrusted_socket_parent")


def _linger_after_denial(connection, budget):
    """Orderly half-close after a peer denial.

    shutdown(SHUT_WR) delivers the already-flushed denial frame and a FIN so the
    client's in-flight send can complete and its receive gets the denial instead
    of a reset. We then drain inbound bytes until EOF or the bounded linger
    expires; a silent client cannot hold the sequential server past the budget.
    """
    end = time.monotonic() + budget
    try:
        connection.shutdown(socket.SHUT_WR)
    except OSError:
        return
    connection.settimeout(max(0.0, end - time.monotonic()))
    try:
        while time.monotonic() < end:
            if not connection.recv(65536):
                break
    except OSError:
        pass


class UnixServer:
    def __init__(self, path, boundary: Boundary, actor_by_uid: dict[int, str], *,
                 socket_gid: int | None = None, io_timeout: float = 5.0, backlog: int = 8,
                 synthetic_test_mode: bool = False):
        if not hasattr(socket, "SO_PEERCRED"):
            raise RuntimeError("linux_peer_credentials_required")
        if not 0.1 <= io_timeout <= 30 or type(backlog) is not int or not 1 <= backlog <= 32:
            raise ValueError("invalid_server_bounds")
        mapping = dict(actor_by_uid)
        if not mapping or any(type(uid) is not int or uid < 0 for uid in mapping):
            raise ValueError("invalid_peer_map")
        if not synthetic_test_mode and any(uid in (0, os.geteuid()) for uid in mapping):
            raise ValueError("distinct_nonroot_worker_uid_required")
        for actor in mapping.values():
            identifier(actor)
        if len(set(mapping.values())) != len(mapping):
            raise ValueError("actor_requires_unique_uid")
        if socket_gid is not None and (type(socket_gid) is not int or socket_gid < 0):
            raise ValueError("invalid_socket_group")
        self.path = Path(path)
        self.boundary = boundary
        self.actor_by_uid = MappingProxyType(mapping)
        self.socket_gid = socket_gid
        self.io_timeout = float(io_timeout)
        self.backlog = backlog
        self._listener = None
        self._socket_identity = None
        self._stop = threading.Event()

    def open(self):
        if self._listener is not None:
            raise RuntimeError("already_open")
        _safe_parent(self.path)
        try:
            self.path.lstat()
        except FileNotFoundError:
            pass
        else:
            raise FileExistsError("socket_path_exists")
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            listener.bind(str(self.path))
            info = self.path.lstat()
            self._socket_identity = (info.st_dev, info.st_ino)
            os.chmod(self.path, 0o600)
            if self.socket_gid is not None:
                os.chown(self.path, -1, self.socket_gid)
                os.chmod(self.path, 0o660)
            listener.listen(self.backlog)
            listener.settimeout(0.25)
            self._listener = listener
        except BaseException:
            listener.close()
            self._remove_own_socket()
            raise
        return self

    def _remove_own_socket(self):
        if self._socket_identity is None:
            return
        try:
            info = self.path.lstat()
            if stat.S_ISSOCK(info.st_mode) and (info.st_dev, info.st_ino) == self._socket_identity:
                self.path.unlink()
        except FileNotFoundError:
            pass
        self._socket_identity = None

    def close(self):
        self._stop.set()
        if self._listener is not None:
            self._listener.close()
            self._listener = None
        self._remove_own_socket()

    def __enter__(self):
        return self.open()

    def __exit__(self, *_):
        self.close()

    def serve_forever(self, *, max_connections: int | None = None):
        if self._listener is None:
            raise RuntimeError("server_not_open")
        if max_connections is not None and (type(max_connections) is not int or max_connections <= 0):
            raise ValueError("invalid_connection_bound")
        accepted = 0
        while not self._stop.is_set():
            try:
                connection, _ = self._listener.accept()
            except socket.timeout:
                continue
            except OSError:
                if self._stop.is_set():
                    break
                raise
            accepted += 1
            with connection:
                self._handle(connection)
            if max_connections is not None and accepted >= max_connections:
                break

    def _handle(self, connection):
        result = ExecutionResult("held", "invalid_request")
        peer_denied = False
        try:
            uid = peer_uid(connection)
            actor = self.actor_by_uid.get(uid)
            if actor is None:
                peer_denied = True
                result = ExecutionResult("held", "peer_denied")
            else:
                deadline = time.monotonic() + self.io_timeout
                request = strict_json(receive(connection, limit=MAX_WIRE, deadline=deadline))
                envelope = ActionEnvelope.from_request(request, actor=actor, now=int(self.boundary._clock()))
                result = self.boundary.execute(envelope)
        except (InvalidRequest, ValueError, TypeError, OSError, TimeoutError, RecursionError, OverflowError):
            pass
        except Exception:
            # No raw exception, payload, token or browser content crosses IPC.
            result = ExecutionResult("unknown", "broker_error")
        try:
            send(connection, canonical(result.to_dict()), limit=MAX_RESPONSE,
                 deadline=time.monotonic() + self.io_timeout)
        except (OSError, ValueError, TimeoutError):
            pass
        if peer_denied:
            # Never an immediate close: flush the denial, half-close, and drain
            # briefly so a still-sending client observes peer_denied.
            _linger_after_denial(connection, min(DENIAL_LINGER_SECONDS, self.io_timeout))


class UnixClient:
    def __init__(self, path, *, expected_server_uid: int, timeout: float = 10.0):
        if type(expected_server_uid) is not int or expected_server_uid < 0 or not 0.1 <= timeout <= 120:
            raise ValueError("invalid_client_config")
        self.path = str(path)
        self.expected_server_uid = expected_server_uid
        self.timeout = float(timeout)

    def execute(self, request: dict) -> dict:
        # Clients do not retry after an uncertain connection. The host owns recovery.
        data = canonical(request)
        if len(data) > MAX_WIRE:
            raise InvalidRequest("message_size")
        deadline = time.monotonic() + self.timeout
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(self.timeout)
            connection.connect(self.path)
            if peer_uid(connection) != self.expected_server_uid:
                raise PermissionError("unexpected_broker_uid")
            try:
                send(connection, data, limit=MAX_WIRE, deadline=deadline)
                result = strict_json(receive(connection, limit=MAX_RESPONSE, deadline=deadline))
            except (BrokenPipeError, ConnectionResetError):
                # Denial-window transport equivalence (fail-closed, documented):
                # the broker dispatches only for a validated mapped peer, and only
                # after a complete handshake. A transport error between connect
                # and the structured response therefore cannot indicate that a
                # dispatch happened; it is treated as the denial the broker
                # documented for this window. Status stays "held" (never a
                # dispatch), and the client never retries after this uncertain
                # window -- the host owns recovery.
                result = {"status": "held", "code": "peer_denied",
                          "envelope_digest": "", "evidence_id": ""}
        if (set(result) != {"status", "code", "envelope_digest", "evidence_id"}
                or any(not isinstance(value, str) for value in result.values())
                or result["status"] not in {"held", "unknown", "submitted", "not_submitted"}):
            raise InvalidRequest("invalid_broker_response")
        return result
