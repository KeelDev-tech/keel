"""Lost broker receipts are unknown, never proof that dispatch was denied."""
import base64
import errno
import io
import os
import socket
import threading
import time

import pytest

from security.execution import Boundary, HostAdapter, Outcome, Reservation
from security.execution import ipc


def request():
    return {"role_id": "role-1", "attempt_id": "attempt-1", "approval_id": "approval-1",
            "action": "browser.submit", "destination": "https://employer.example/apply",
            "payload_b64": base64.b64encode(b'{"name":"Synthetic Person"}').decode(),
            "attachments": [], "expires_at": int(time.time()) + 300,
            "nonce": "nonce-1", "policy_revision": "policy-1"}


class FixtureHost(HostAdapter):
    """Synthetic host only; real authority remains the host's responsibility."""
    def __init__(self): self.states = []
    def reserve(self, envelope, now):
        self.states.append("RESERVED")
        return Reservation("reservation-1", envelope.digest, envelope.attempt_id)
    def begin_dispatch(self, reservation, envelope, now):
        self.states.append("UNKNOWN")
        return envelope.digest
    def record_outcome(self, reservation, envelope, outcome, now): self.states.append("SUBMITTED")
    def note_unknown(self, *args): self.states.append("UNKNOWN")


@pytest.mark.parametrize("fault", ["eof", "reset", "timeout", "send_broken", "invalid_json", "invalid_shape", "invalid_status"])
def test_actual_broker_dispatch_before_in_memory_receipt_loss(tmp_path, monkeypatch, fault):
    host, dispatches = FixtureHost(), []
    boundary = Boundary(host, {"browser.submit": lambda envelope:
                        (dispatches.append(envelope.digest) or Outcome("submitted", "receipt-1"))})
    server = ipc.UnixServer(str(tmp_path / "unused.sock"), boundary,
                            {os.getuid(): "worker-1"}, synthetic_test_mode=True)

    class ServerConnection:
        def __init__(self, data): self.inbound, self.outbound = io.BytesIO(data), b""
        def settimeout(self, value): pass
        def recv(self, count): return self.inbound.read(count)
        def sendall(self, data): self.outbound += data

    class ClientConnection:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def settimeout(self, value): pass
        def connect(self, path): pass
        def sendall(self, data):
            counterpart = ServerConnection(data)
            server._handle(counterpart)  # Real framing, validation, boundary, handler, receipt.
            assert host.states == ["RESERVED", "UNKNOWN", "SUBMITTED"]
            assert b'"status":"submitted"' in counterpart.outbound
            if fault == "send_broken":
                raise BrokenPipeError("synthetic send error after complete request delivery")
            payload = b"{" if fault == "invalid_json" else b"[]"
            if fault == "invalid_status":
                payload = b'{"status":"retry","code":"x","envelope_digest":"","evidence_id":""}'
            self.response = io.BytesIO(len(payload).to_bytes(4, "big") + payload)
        def recv(self, count):
            if fault == "eof": return b""
            if fault == "reset": raise ConnectionResetError("synthetic lost response")
            if fault == "timeout": raise TimeoutError("synthetic response deadline")
            return self.response.read(count)

    monkeypatch.setattr(ipc, "peer_uid", lambda connection: os.getuid())
    monkeypatch.setattr(ipc.socket, "socket", lambda *args: ClientConnection())
    outcome = ipc.UnixClient(server.path, expected_server_uid=os.getuid()).execute(request())
    assert len(dispatches) == 1
    assert outcome == {"status": "unknown", "code": "transport_outcome_unconfirmed",
                       "envelope_digest": "", "evidence_id": ""}


def test_only_complete_structured_denial_proves_no_dispatch(tmp_path, monkeypatch):
    server = ipc.UnixServer(str(tmp_path / "unused.sock"), Boundary(),
                            {os.getuid() + 1: "other-worker"}, synthetic_test_mode=True)
    dispatches = []
    monkeypatch.setattr(server.boundary, "execute", lambda envelope: dispatches.append(envelope))

    class ServerConnection:
        def settimeout(self, value): pass
        def recv(self, count): return b""
        def sendall(self, data): self.response = data
        def shutdown(self, how): pass

    class ClientConnection:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def settimeout(self, value): pass
        def connect(self, path): pass
        def sendall(self, data):
            counterpart = ServerConnection()
            server._handle(counterpart)
            self.response = io.BytesIO(counterpart.response)
        def recv(self, count): return self.response.read(count)

    monkeypatch.setattr(ipc, "peer_uid", lambda connection: os.getuid())
    monkeypatch.setattr(ipc.socket, "socket", lambda *args: ClientConnection())
    outcome = ipc.UnixClient(server.path, expected_server_uid=os.getuid()).execute(request())
    assert dispatches == []
    assert outcome == {"status": "held", "code": "peer_denied", "envelope_digest": "", "evidence_id": ""}


def open_or_skip(server):
    try:
        return server.open()
    except OSError as exc:
        if exc.errno in {errno.EPERM, errno.EACCES, errno.EAFNOSUPPORT}:
            pytest.skip("kernel AF_UNIX unavailable: " + str(exc))
        raise


@pytest.mark.parametrize("fault", ["eof", "reset", "timeout", "invalid_json", "invalid_shape"])
def test_real_dispatch_with_lost_or_invalid_receipt_stays_unknown(tmp_path, monkeypatch, fault):
    host = FixtureHost()
    dispatches = []
    boundary = Boundary(host, {"browser.submit": lambda envelope:
                        (dispatches.append(envelope.digest) or Outcome("submitted", "receipt-1"))})
    original_send = ipc.send

    def lose_response(connection, data, *, limit, deadline):
        if limit != ipc.MAX_RESPONSE:
            return original_send(connection, data, limit=limit, deadline=deadline)
        assert host.states == ["RESERVED", "UNKNOWN", "SUBMITTED"]
        if fault == "eof":
            connection.shutdown(socket.SHUT_WR)
        elif fault == "reset":
            connection.shutdown(socket.SHUT_RDWR)
            raise ConnectionResetError("synthetic lost response")
        elif fault == "timeout":
            # Delay only the receipt; the client already handed over a complete request.
            time.sleep(.2)
        else:
            payload = b"{" if fault == "invalid_json" else b"[]"
            return original_send(connection, payload, limit=limit, deadline=deadline)

    monkeypatch.setattr(ipc, "send", lose_response)
    path = str(tmp_path / "broker.sock")
    server = open_or_skip(ipc.UnixServer(path, boundary, {os.getuid(): "worker-1"},
                                        io_timeout=.1, synthetic_test_mode=True))
    thread = threading.Thread(target=server.serve_forever, kwargs={"max_connections": 1}, daemon=True)
    thread.start()
    try:
        outcome = ipc.UnixClient(path, expected_server_uid=os.getuid(), timeout=.1).execute(request())
    finally:
        thread.join(2)
        server.close()
    assert not thread.is_alive()
    assert len(dispatches) == 1
    assert host.states == ["RESERVED", "UNKNOWN", "SUBMITTED"]
    assert outcome == {"status": "unknown", "code": "transport_outcome_unconfirmed",
                       "envelope_digest": "", "evidence_id": ""}


def test_explicit_authenticated_denial_still_reports_held(tmp_path):
    path = str(tmp_path / "denied.sock")
    server = open_or_skip(ipc.UnixServer(path, Boundary(), {os.getuid() + 1: "other-worker"},
                                        io_timeout=.1, synthetic_test_mode=True))
    thread = threading.Thread(target=server.serve_forever, kwargs={"max_connections": 1}, daemon=True)
    thread.start()
    try:
        outcome = ipc.UnixClient(path, expected_server_uid=os.getuid()).execute(request())
    finally:
        thread.join(2)
        server.close()
    assert outcome["status"] == "held"
    assert outcome["code"] == "peer_denied"
