#!/usr/bin/env python3
"""Regression tests: batch_staged_launches guard release + apply atomicity.

FIX 3: _guard_go's prelaunch_guard ACQUIRES the lock on GO under the
       "batch-staged" placeholder; the emitted instruction tells the
       spawner to acquire with its OWN task_id. The guard must release
       the placeholder after GO or every emitted instruction is HELD
       (unlaunchable) for the full TTL.
FIX 4: --apply's load->plan->mark-FIRED->write must run under
       queue_io.queue_lock — two concurrent runs otherwise both pass the
       guard (same task_id re-acquire is idempotent) and double-emit.

Synthetic fixtures only: tmp dirs, never live state.
Run: python3 -m pytest test_batch_staged_launches_guards.py
"""
import json
import os
import sys
import threading

import pytest

BASE = os.path.dirname(os.path.abspath(__file__))
ENGINES = os.path.join(BASE, "..", "engines")
sys.path.insert(0, ENGINES)
import batch_staged_launches as bsl
import launch_lock
import queue_io


@pytest.fixture()
def iso(tmp_path, monkeypatch):
    lock_dir = tmp_path / "locks"
    lock_dir.mkdir()
    monkeypatch.setattr(launch_lock, "LOCK_DIR", str(lock_dir))
    ledger = tmp_path / "ledger.json"
    ledger.write_text("[]")
    monkeypatch.setattr(launch_lock, "LEDGER_PATH", str(ledger))
    monkeypatch.setattr(queue_io, "_LOCK_PATH", str(tmp_path / "queue.lock"))
    return {"tmp": tmp_path, "ledger": str(ledger)}


def _write_staged(iso, entries):
    tmp = iso["tmp"]
    staged_file = tmp / "staged-launches.json"
    staged_file.write_text(json.dumps({"entries": entries}))
    packet_dir = tmp / "packets"
    packet_dir.mkdir(exist_ok=True)
    for e in entries:
        (packet_dir / f"{e['role_id']}.json").write_text("{}")
    return staged_file, packet_dir


def _point_at_tmp(iso, monkeypatch, staged_file, packet_dir):
    monkeypatch.setattr(bsl, "STAGED_FILE", str(staged_file))
    monkeypatch.setattr(bsl, "MAXMODE_FILE", str(iso["tmp"] / "max-mode.json"))
    monkeypatch.setattr(bsl, "PACKET_DIR", str(packet_dir))


# ---------------------------------------------------------------- FIX 3

def test_guard_go_releases_batch_staged_lock(iso):
    """After a GO verdict the spawner's own task_id must be acquirable."""
    ok, note = bsl._guard_go("R-1", "Acme", "Engineer")
    assert ok and note == "GO"
    ok2, info2 = launch_lock.acquire("R-1", "spawner-task-9", owner="spawner")
    assert ok2, f"spawner acquire blocked after guard GO: {info2}"
    assert info2["status"] == "ACQUIRED"


def test_guard_refusal_still_fires(iso):
    """The guard's refusal value is kept: a lead whose lock is held by
    another task still refuses — and the foreign lock is untouched."""
    ok, _ = launch_lock.acquire("BUSY-1", "other-task", owner="test")
    assert ok
    ok2, note = bsl._guard_go("BUSY-1", "Acme", "Engineer")
    assert not ok2
    assert "STAND_DOWN" in note or "HELD" in note
    assert launch_lock.check("BUSY-1")["task_id"] == "other-task"


def test_guard_go_propagates_corrupt_lease(iso):
    """A corrupt lease under the guard must raise loudly (K20), not be
    absorbed into a quiet skip."""
    path = launch_lock._lock_path("R-CORRUPT")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write("{not valid json")
    with pytest.raises(ValueError):
        bsl._guard_go("R-CORRUPT", "Acme", "Engineer")


# ---------------------------------------------------------------- FIX 4

def test_apply_second_run_emits_nothing_new(iso, monkeypatch, capsys):
    """Two sequential --apply runs: the second emits nothing (already FIRED)."""
    staged_file, packet_dir = _write_staged(iso, [
        {"role_id": "R-1", "company": "Acme", "title": "Eng",
         "status": "STAGED"},
    ])
    _point_at_tmp(iso, monkeypatch, staged_file, packet_dir)
    calls = []

    def fake_guard(role_id, company, title):
        calls.append(role_id)
        return True, "GO"

    monkeypatch.setattr(bsl, "_guard_go", fake_guard)
    assert bsl.main(["--apply"]) == 0
    data = json.loads(staged_file.read_text())
    assert data["entries"][0]["status"] == "FIRED"
    assert data["entries"][0]["fired_by"] == "batch_staged_launches"
    assert calls == ["R-1"]

    calls.clear()
    capsys.readouterr()
    assert bsl.main(["--apply"]) == 0
    out = capsys.readouterr().out
    assert "nothing to fire" in out
    assert calls == [], "second run must not re-run the guard (already FIRED)"
    data2 = json.loads(staged_file.read_text())
    assert data2["entries"][0]["status"] == "FIRED"


def test_apply_holds_queue_lock_during_plan(iso, monkeypatch):
    """The --apply critical section holds queue_io.queue_lock: a second
    holder (other thread) times out instead of interleaving."""
    staged_file, packet_dir = _write_staged(iso, [
        {"role_id": "R-2", "company": "Acme", "title": "Eng",
         "status": "STAGED"},
    ])
    _point_at_tmp(iso, monkeypatch, staged_file, packet_dir)
    probe = {}

    def racer():
        try:
            with queue_io.queue_lock(timeout=1, owner="concurrency-probe"):
                probe["acquired"] = True
        except queue_io.QueueLockTimeout:
            probe["acquired"] = False

    def fake_guard(role_id, company, title):
        # Runs inside plan_batch, i.e. inside main's critical section.
        t = threading.Thread(target=racer)
        t.start()
        t.join()
        return True, "GO"

    monkeypatch.setattr(bsl, "_guard_go", fake_guard)
    assert bsl.main(["--apply"]) == 0
    assert probe.get("acquired") is False, \
        "queue_lock was not held across the --apply load->plan->write section"
