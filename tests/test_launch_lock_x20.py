#!/usr/bin/env python3
"""Tests for launch_lock v2: atomic acquire + prelaunch duplicate guard (x20).

Run: python3 -m pytest test_launch_lock_x20.py
"""
import json
import os
import sys
import tempfile
import threading

try:
    import pytest
except ImportError:
    # stdlib-only CI has no pytest installed: the module must still import
    # cleanly under `python -m unittest discover`. These pytest-style tests
    # are collected only under pytest; without it the fixture below is a
    # no-op and the test functions are not collected by unittest.
    pytest = None

BASE = os.path.dirname(os.path.abspath(__file__))
ENGINES = os.path.join(BASE, "..", "engines")
sys.path.insert(0, ENGINES)
import launch_lock as ll


def _fixture(*dargs, **dkwargs):
    """pytest.fixture when pytest is available, else a no-op decorator."""
    if pytest is not None:
        return pytest.fixture(*dargs, **dkwargs)

    def _wrap(fn):
        return fn

    return _wrap


@_fixture()
def iso(tmp_path, monkeypatch):
    """Isolate LOCK_DIR and LEDGER_PATH per test."""
    lock_dir = str(tmp_path / "locks")
    ledger = str(tmp_path / "ledger.json")
    monkeypatch.setattr(ll, "LOCK_DIR", lock_dir)
    monkeypatch.setattr(ll, "LEDGER_PATH", ledger)
    with open(ledger, "w") as f:
        json.dump([], f)
    return lock_dir, ledger


def _write_ledger(ledger, rows):
    with open(ledger, "w") as f:
        json.dump(rows, f)


def test_atomic_acquire_single_winner(iso):
    """20 threads race on one role_id: exactly one ACQUIRED, 19 HELD."""
    results = []
    barrier = threading.Barrier(20)

    def racer(i):
        barrier.wait()
        ok, info = ll.acquire("RACE-ROLE", f"task-{i}", owner="test")
        results.append((ok, info["status"]))

    threads = [threading.Thread(target=racer, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    won = [r for r in results if r[0]]
    held = [r for r in results if not r[0] and r[1] == "HELD"]
    assert len(won) == 1, f"expected exactly 1 winner, got {len(won)}"
    assert len(held) == 19, f"expected 19 HELD, got {len(held)}"


def test_reacquire_idempotent(iso):
    ok1, _ = ll.acquire("R1", "t1")
    ok2, info2 = ll.acquire("R1", "t1")
    assert ok1 and ok2
    assert info2["status"] == "ACQUIRED"


def test_stale_lock_takeover(iso):
    ok1, _ = ll.acquire("R2", "t1")
    assert ok1
    # Backdate the lock past TTL.
    import datetime
    path = ll._lock_path("R2")
    with open(path) as f:
        lock = json.load(f)
    stale = (ll._now() - datetime.timedelta(hours=ll.LOCK_TTL_H + 1)).isoformat()
    lock["acquired_at"] = stale
    with open(path, "w") as f:
        json.dump(lock, f)
    ok2, info2 = ll.acquire("R2", "t2")
    assert ok2, f"stale lock should be taken over: {info2}"
    assert info2["lock"]["task_id"] == "t2"


def test_release_owner_and_stranger(iso):
    ll.acquire("R3", "t1")
    ok, info = ll.release("R3", "t2")
    assert not ok and info["status"] == "NOT_OWNER"
    ok, info = ll.release("R3", "t1")
    assert ok and info["status"] == "RELEASED"


def test_guard_go(iso):
    lock_dir, ledger = iso
    ok, info = ll.prelaunch_guard("NEWR-1", "t1", company="Acme",
                                  title="Ops Manager", ledger_path=ledger)
    assert ok and info["verdict"] == "GO"


def test_guard_already_submitted(iso):
    lock_dir, ledger = iso
    _write_ledger(ledger, [{"role_id": "OLD-1", "company": "Acme",
                            "title": "Ops Manager", "status": "SUBMITTED"}])
    ok, info = ll.prelaunch_guard("OLD-1", "t9", company="Acme",
                                  title="Ops Manager", ledger_path=ledger)
    assert not ok and info["status"] == "ALREADY_SUBMITTED"


def test_guard_twin_submitted(iso):
    """Napa Valley Reserve pattern: same company+title, different role_id."""
    lock_dir, ledger = iso
    _write_ledger(ledger, [{"role_id": "NVR-OLD", "company": "Napa Valley Reserve",
                            "title": "Hospitality Events Manager",
                            "status": "SUBMITTED"}])
    ok, info = ll.prelaunch_guard("NVR-NEW", "t9",
                                  company="Napa Valley Reserve",
                                  title="Hospitality Events Manager!",
                                  ledger_path=ledger)
    assert not ok
    assert info["status"] == "TWIN_SUBMITTED"
    assert info["twin_role_id"] == "NVR-OLD"


def test_guard_held(iso):
    lock_dir, ledger = iso
    ll.acquire("BUSY-1", "t1")
    ok, info = ll.prelaunch_guard("BUSY-1", "t2", company="Acme",
                                  title="Other Role", ledger_path=ledger)
    assert not ok and info["status"] == "HELD"


def test_guard_distinct_role_same_company_goes(iso):
    """Same company, different title is a different job — GO."""
    lock_dir, ledger = iso
    _write_ledger(ledger, [{"role_id": "A-1", "company": "Acme",
                            "title": "Ops Manager", "status": "SUBMITTED"}])
    ok, info = ll.prelaunch_guard("A-2", "t1", company="Acme",
                                  title="Sales Rep", ledger_path=ledger)
    assert ok and info["verdict"] == "GO"
