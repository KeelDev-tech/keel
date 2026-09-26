#!/usr/bin/env python3
"""Regression tests: launch_lock silent-defect fixes (2026-09-19).

FIX 7a: LEDGER_PATH pointed at <HOME>/ledger/application-ledger.json — a
        dead letter in this tree. It must be <HOME>/data/application-ledger.json.
FIX 7b: the acquire docstring promised "same-task re-acquire refreshes"
        but returned the existing lock unchanged. A fresh re-acquire must
        rewrite acquired_at.
FIX 7c: _read_lock swallowed JSONDecodeError -> None, so acquire treated
        a corrupt-but-present lease as stale and stole it. A present but
        unreadable lease must raise ValueError (K20) — never silently taken.

Synthetic fixtures only: tmp lock dir, never live state.
Run: python3 -m pytest test_launch_lock_silent_fixes.py
"""
import json
import os
import sys
from datetime import datetime, timedelta

import pytest

BASE = os.path.dirname(os.path.abspath(__file__))
ENGINES = os.path.join(BASE, "..", "engines")
sys.path.insert(0, ENGINES)
import launch_lock as ll


@pytest.fixture()
def iso(tmp_path, monkeypatch):
    lock_dir = tmp_path / "locks"
    lock_dir.mkdir()
    monkeypatch.setattr(ll, "LOCK_DIR", str(lock_dir))
    return tmp_path


# ---------------------------------------------------------------- FIX 7a

def test_ledger_path_points_at_data():
    assert ll.LEDGER_PATH.endswith(os.path.join("data",
                                                "application-ledger.json")), \
        f"LEDGER_PATH is a dead letter: {ll.LEDGER_PATH}"
    assert os.path.join("ledger", "application-ledger.json") \
        not in ll.LEDGER_PATH


# ---------------------------------------------------------------- FIX 7b

def test_same_task_reacquire_refreshes_lease(iso):
    ok, info1 = ll.acquire("R-1", "t1", owner="test")
    assert ok
    # Age the lease 60 minutes (still fresh: TTL is 2h).
    path = ll._lock_path("R-1")
    with open(path) as f:
        lock = json.load(f)
    aged = (ll._now() - timedelta(minutes=60)).isoformat()
    lock["acquired_at"] = aged
    with open(path, "w") as f:
        json.dump(lock, f)
    ok2, info2 = ll.acquire("R-1", "t1", owner="test")
    assert ok2 and info2["status"] == "ACQUIRED"
    assert info2["note"] == "already owner"  # note contract preserved
    new_ts = info2["lock"]["acquired_at"]
    assert datetime.fromisoformat(new_ts) > datetime.fromisoformat(aged), \
        "same-task re-acquire did not refresh acquired_at"
    on_disk = json.load(open(path))
    assert on_disk["acquired_at"] == new_ts
    assert on_disk["task_id"] == "t1"


# ---------------------------------------------------------------- FIX 7c

def _write_garbage_lease(role_id):
    path = ll._lock_path(role_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write("{not valid json")
    return path


def test_corrupt_lease_raises_valueerror(iso):
    path = _write_garbage_lease("R-CORRUPT")
    with pytest.raises(ValueError):
        ll.acquire("R-CORRUPT", "t2", owner="test")
    # The corrupt file must NOT have been stolen/replaced.
    with open(path) as f:
        assert f.read() == "{not valid json"


def test_prelaunch_guard_propagates_corrupt_lease(iso, tmp_path, monkeypatch):
    ledger = tmp_path / "ledger.json"
    ledger.write_text("[]")
    monkeypatch.setattr(ll, "LEDGER_PATH", str(ledger))
    _write_garbage_lease("R-9")
    with pytest.raises(ValueError):
        ll.prelaunch_guard("R-9", "t1", company="Acme", title="Engineer")


def test_missing_lease_still_acquires(iso):
    """No lease file at all: normal first acquire (None is only corrupt
    when the file EXISTS)."""
    ok, info = ll.acquire("R-NEW", "t1", owner="test")
    assert ok and info["status"] == "ACQUIRED"


# ------------------------------------------------- ValueError propagation

def test_staged_preflight_guard_go_propagates_corrupt_lease(
        iso, tmp_path, monkeypatch):
    """staged-launch-preflight's _guard_go must not swallow the K20
    ValueError into a quiet skip."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "staged_launch_preflight",
        os.path.join(ENGINES, "staged-launch-preflight.py"))
    slp = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(slp)
    path = ll._lock_path("R-PRE")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write("{not valid json")
    with pytest.raises(ValueError):
        slp._guard_go("R-PRE", "Acme", "Engineer")


def test_apply_loop_launch_guard_propagates_corrupt_lease(
        tmp_path, monkeypatch):
    """apply_loop._launch_guard must not absorb the K20 ValueError into a
    fail-open 'proceeding'."""
    sys.path.insert(0, ENGINES)
    import apply_loop
    monkeypatch.setattr(ll, "LOCK_DIR", str(tmp_path / "locks"))
    path = ll._lock_path("R-AL")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write("{not valid json")
    with pytest.raises(ValueError):
        apply_loop._launch_guard("R-AL", "Acme", "Engineer")
