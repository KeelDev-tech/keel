#!/usr/bin/env python3
"""Regression tests for the security-boundary peer-denial race repair
(2026-09-21) and the boundary's standing holds in the live tree.

Pins:
  (a) Denial code contract: an unmapped peer observes the structured
      ("held", "peer_denied") verdict -- never a bare BrokenPipeError.
  (b) Fail-closed transport equivalence: BrokenPipeError/ConnectionResetError
      inside the handshake window map to the peer_denied verdict (held);
      no evidence is fabricated and the client never retries.
  (c) Standing holds: no external egress in security.execution (AF_UNIX only),
      dispatch stays human-gated (default Boundary binds no host authority
      and no handlers -- nothing auto-executes).

Run: python3 -m pytest test_security_boundary_peer_denial.py
Stdlib + pytest only; loopback/AF_UNIX fixtures only, no external network,
no queue/ledger/telemetry/credential writes.
"""

import base64
import os
import random
import socket
import sys
import threading
import time

import pytest

BASE = os.path.dirname(os.path.abspath(__file__))
KEEL_ROOT = os.path.join(BASE, "..")
sys.path.insert(0, os.path.abspath(KEEL_ROOT))

from security.execution import ActionEnvelope, Boundary, HostAdapter, Unbound  # noqa: E402
from security.execution import ipc  # noqa: E402
from security.execution.ipc import UnixClient, UnixServer  # noqa: E402


def make_request():
    return {"role_id": "role-1", "attempt_id": "attempt-1", "approval_id": "approval-1",
            "action": "browser.submit", "destination": "https://employer.example/apply?id=1",
            "payload_b64": base64.b64encode(b'{"name":"Synthetic Person"}').decode(),
            "attachments": [{"name": "resume.pdf",
                             "content_b64": base64.b64encode(b"synthetic-pdf").decode()}],
            "expires_at": int(time.time()) + 300, "nonce": "nonce-1",
            "policy_revision": "policy-1"}


def make_envelope(actor="worker-1"):
    return ActionEnvelope.from_request(make_request(), actor=actor, now=int(time.time()))


@pytest.fixture()
def denied_server(tmp_path):
    """A real broker whose peer map excludes the current uid -> every client is denied."""
    path = tmp_path / "broker.sock"
    server = UnixServer(str(path), Boundary(), {os.getuid() + 1: "worker-1"},
                        io_timeout=2.0, synthetic_test_mode=True)
    server.open()
    thread = threading.Thread(target=server.serve_forever,
                              kwargs={"max_connections": 64}, daemon=True)
    thread.start()
    yield server, str(path)
    server.close()
    thread.join(10)


def test_denial_code_contract_is_structured_not_bare_transport_error(denied_server):
    """(a) Unmapped peers observe peer_denied, repeatedly and under timing jitter."""
    _, path = denied_server
    for _ in range(5):
        time.sleep(random.uniform(0, 0.02))
        result = UnixClient(path, expected_server_uid=os.getuid(), timeout=10).execute(make_request())
        assert result["status"] == "held"
        assert result["code"] == "peer_denied"
        assert set(result) == {"status", "code", "envelope_digest", "evidence_id"}


def test_concurrent_denials_never_hang_never_dispatch(denied_server):
    """(a) N concurrent unmapped-peer connects: every client sees peer_denied,
    none hangs, and the broker never reaches the execution boundary."""
    server, path = denied_server
    count = 8
    calls = []
    original_execute = server.boundary.execute

    def spy(envelope):
        calls.append(envelope)
        return original_execute(envelope)

    server.boundary.execute = spy
    barrier = threading.Barrier(count)
    outcomes = {}
    lock = threading.Lock()

    def one(index):
        barrier.wait(15)
        time.sleep(random.uniform(0, 0.05))
        try:
            result = UnixClient(path, expected_server_uid=os.getuid(),
                                timeout=10).execute(make_request())
            observed = (result["status"], result["code"])
        except Exception as exc:  # any exception is a contract failure
            observed = "%s: %s" % (type(exc).__name__, exc)
        with lock:
            outcomes[index] = observed

    threads = [threading.Thread(target=one, args=(i,), daemon=True) for i in range(count)]
    started = time.monotonic()
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)
    assert not any(thread.is_alive() for thread in threads), "client hang: %r" % (outcomes,)
    assert len(outcomes) == count, "client loss: %r" % (outcomes,)
    assert time.monotonic() - started < 60
    for index in range(count):
        assert outcomes[index] == ("held", "peer_denied"), \
            "client %d observed %r" % (index, outcomes[index])
    assert calls == [], "denied peers must never reach the execution boundary"


@pytest.mark.parametrize("failure", [BrokenPipeError, ConnectionResetError])
def test_transport_error_in_handshake_window_is_denial_equivalent(tmp_path, monkeypatch, failure):
    """(b) A transport error between connect and the structured response maps to
    the peer_denied verdict -- fail-closed (held), with no fabricated evidence."""
    sock_path = str(tmp_path / "dead.sock")
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(sock_path)
    listener.listen(1)

    def raiser(*args, **kwargs):
        raise failure(32, "simulated transport failure")

    if failure is BrokenPipeError:
        monkeypatch.setattr(ipc, "send", raiser)
    else:
        monkeypatch.setattr(ipc, "receive", raiser)

    result = UnixClient(sock_path, expected_server_uid=os.getuid(), timeout=5).execute(make_request())
    listener.close()
    assert result == {"status": "held", "code": "peer_denied",
                      "envelope_digest": "", "evidence_id": ""}, result
    assert result["status"] == "held"  # fail-closed: never submitted/not_submitted


def test_no_external_egress_in_execution_boundary():
    """(c) security.execution opens no external sockets: AF_UNIX only. No
    AF_INET/AF_INET6, no HTTP client, no outbound connection helpers."""
    root = os.path.join(KEEL_ROOT, "security", "execution")
    forbidden = ("AF_INET", "AF_INET6", "SOCK_DGRAM", "http.client", "urllib.request",
                 "urlopen", "create_connection", "getaddrinfo")
    offenders = []
    for name in sorted(os.listdir(root)):
        if not name.endswith(".py"):
            continue
        text = open(os.path.join(root, name), encoding="utf-8").read()
        for token in forbidden:
            if token in text:
                offenders.append("%s: %s" % (name, token))
    assert offenders == [], "external egress surface in execution boundary: %r" % (offenders,)


def test_dispatch_stays_human_gated():
    """(c) The default boundary binds no host authority and no handlers:
    a well-formed envelope is held, never executed. Wiring a real dispatch
    requires an explicit host adapter installed by the operator."""
    boundary = Boundary()
    result = boundary.execute(make_envelope())
    assert result.status == "held"
    assert result.code == "handler_unbound"
    with pytest.raises(Unbound):
        HostAdapter().reserve(make_envelope(), int(time.time()))
    with pytest.raises(Unbound):
        HostAdapter().begin_dispatch(None, make_envelope(), int(time.time()))
