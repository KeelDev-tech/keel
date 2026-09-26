#!/usr/bin/env python3
"""Regression tests: apply_loop queue-IO contracts (silent-defect sweep 2026-09-19).

FIX 1: _save_queue_file must hold queue_io.queue_lock and write via
       queue_io.atomic_write_json — no unlocked read-modify-write, no
       fixed path+".tmp" name. A stale snapshot write once silently
       clobbered a canonical commit (IN-FLIGHT reverted to
       PARKED-NEEDS-INPUT).
FIX 2: the claim-dead path must release the buffer-time launch lock; the
       owner task_id is read BEFORE _claim_buffered deletes the packet
       file that carries it (else the lock leaks for the full 2h TTL).

Synthetic fixtures only: tmp dirs, never live queue files.
Run: python3 -m pytest test_apply_loop_queue_io_contracts.py
"""
import contextlib
import json
import os
import sys
import threading
import time
from datetime import datetime, timezone

import pytest

BASE = os.path.dirname(os.path.abspath(__file__))
ENGINES = os.path.join(BASE, "..", "engines")
sys.path.insert(0, ENGINES)
import apply_loop
import launch_lock
import queue_io


@pytest.fixture()
def qio_iso(tmp_path, monkeypatch):
    """Redirect queue_io's lock away from the production lock file."""
    monkeypatch.setattr(queue_io, "_LOCK_PATH", str(tmp_path / "queue.lock"))
    return tmp_path


# ---------------------------------------------------------------- FIX 1

def test_save_queue_file_holds_queue_lock(qio_iso, monkeypatch):
    """_save_queue_file takes queue_io.queue_lock around its write."""
    qf = qio_iso / "q.json"
    qf.write_text(json.dumps([{"role_id": "R-1", "status": "READY"}]))
    entered = []
    real_lock = queue_io.queue_lock

    @contextlib.contextmanager
    def spy(timeout=None, owner=None):
        entered.append(owner)
        with real_lock(timeout=timeout, owner=owner):
            yield

    monkeypatch.setattr(queue_io, "queue_lock", spy)
    apply_loop._save_queue_file(
        str(qf), [{"role_id": "R-1", "status": "IN-FLIGHT"}])
    assert entered, "_save_queue_file never took queue_io.queue_lock"
    assert json.loads(qf.read_text()) == [
        {"role_id": "R-1", "status": "IN-FLIGHT"}]


def test_save_queue_file_preserves_dict_shape(qio_iso):
    """The list-vs-dict envelope logic survives the canonical-path move."""
    qf = qio_iso / "q.json"
    qf.write_text(json.dumps({"entries": [{"role_id": "R-1"}], "meta": 7}))
    apply_loop._save_queue_file(str(qf), [{"role_id": "R-2"}])
    d = json.loads(qf.read_text())
    assert d["entries"] == [{"role_id": "R-2"}]
    assert d["meta"] == 7  # untouched envelope keys preserved


def test_save_queue_file_uses_atomic_write(qio_iso, monkeypatch):
    """The write goes through queue_io.atomic_write_json (no path+'.tmp')."""
    qf = qio_iso / "q.json"
    qf.write_text(json.dumps([]))
    calls = []
    real_atomic = queue_io.atomic_write_json

    def spy(path, items):
        calls.append((path, items))
        return real_atomic(path, items)

    monkeypatch.setattr(queue_io, "atomic_write_json", spy)
    apply_loop._save_queue_file(str(qf), [{"role_id": "R-1"}])
    assert calls == [(str(qf), [{"role_id": "R-1"}])]
    assert not os.path.exists(str(qf) + ".tmp"), \
        "fixed path+'.tmp' name must not be used"


def test_save_queue_file_serializes_against_canonical_writer(qio_iso):
    """Writer B holds the queue lock committing via the canonical path;
    A's _save_queue_file must block until B releases — not race it."""
    qf = qio_iso / "q.json"
    qf.write_text(json.dumps([{"role_id": "R-1", "status": "READY"}]))

    def holder():
        with queue_io.queue_lock(owner="test:holder", timeout=10):
            time.sleep(1.0)  # hold the lock while A tries to write
            queue_io.atomic_write_json(
                str(qf), [{"role_id": "R-1", "status": "IN-FLIGHT"}])

    t = threading.Thread(target=holder)
    start = time.monotonic()
    t.start()
    time.sleep(0.3)  # let B acquire the lock first
    apply_loop._save_queue_file(
        str(qf), [{"role_id": "R-1", "status": "STALE-A"}])
    elapsed = time.monotonic() - start
    t.join()
    assert elapsed >= 1.0, \
        f"A's write did not serialize behind B (elapsed {elapsed:.2f}s)"
    # A's full-file write landed atomically after B released: well-formed,
    # no torn mix of the two writers.
    assert json.loads(qf.read_text()) == [
        {"role_id": "R-1", "status": "STALE-A"}]


# ---------------------------------------------------------------- FIX 2

def test_claim_dead_releases_launch_lock(tmp_path, monkeypatch):
    """Dead claim: _claim_buffered deletes the packet; the launch lock
    acquired at buffer-build time must still be released."""
    monkeypatch.setattr(launch_lock, "LOCK_DIR", str(tmp_path / "locks"))
    buf_dir = tmp_path / "buffer"
    buf_dir.mkdir()
    packets = tmp_path / "packets"
    bpath = buf_dir / "R-1.json"
    bpath.write_text(json.dumps({"role_id": "R-1",
                                 "launch_task_id": "task-123"}))
    state_file = tmp_path / "buffer-state.json"
    monkeypatch.setattr(apply_loop, "BUFFER_STATE", str(state_file))
    monkeypatch.setattr(apply_loop, "BUFFER_DIR", str(buf_dir))
    monkeypatch.setattr(apply_loop, "PACKETS", str(packets))
    apply_loop.save_buffer_state([{
        "role_id": "R-1",
        "packet_path": str(bpath),
        "built_at": datetime.now(timezone.utc).isoformat(),
        "launch_task_id": "task-123",
    }])
    ok, _ = launch_lock.acquire("R-1", "task-123", owner="test")
    assert ok
    # Force the claim-dead path: the posting reads dead over HTTP.
    monkeypatch.setattr(apply_loop, "live_cached",
                        lambda role_id, url: False)
    monkeypatch.setattr(apply_loop.log_event, "log",
                        lambda *a, **k: None)
    entry = {"role_id": "R-1", "company": "Acme",
             "ats_url": "https://example.invalid/jobs/1"}
    assert apply_loop._claim_or_release(entry, str(bpath), "R-1") is None
    assert not os.path.exists(str(bpath)), "dead packet must be dropped"
    assert launch_lock.check("R-1") is None, \
        "claim-dead leaked the launch lock (would block the lead 2h)"
    assert not os.path.exists(launch_lock._lock_path("R-1"))


def test_buffered_launch_task_id_reads_buffer_state(tmp_path, monkeypatch):
    """The owner id comes from the buffer state, readable even after the
    packet file is gone."""
    state_file = tmp_path / "buffer-state.json"
    monkeypatch.setattr(apply_loop, "BUFFER_STATE", str(state_file))
    apply_loop.save_buffer_state([{
        "role_id": "R-7", "packet_path": "/nonexistent.json",
        "built_at": datetime.now(timezone.utc).isoformat(),
        "launch_task_id": "task-777",
    }])
    assert apply_loop._buffered_launch_task_id("R-7") == "task-777"
    assert apply_loop._buffered_launch_task_id("NOPE") == ""
