#!/usr/bin/env python3
"""Regression test: submit_intent same-second attempt_id collision.

FIX 8: _attempt_id is second-granular by design, and record_intent's
       `store[attempt_id] = rec` blindly overwrote. Reproduced: INTENT ->
       mark_submitted (SUBMITTED, history 2) -> record_intent in the same
       second -> state back to INTENT, history destroyed, violating
       "terminal states are immutable". The collision must raise
       AttemptIdCollision and the stored record must stay intact.

Synthetic fixtures only: tmp store dir, frozen clock.
Run: python3 -m pytest test_submit_intent_collision.py
"""
import os
import sys
from datetime import datetime, timezone

import pytest

BASE = os.path.dirname(os.path.abspath(__file__))
ENGINES = os.path.join(BASE, "..", "engines")
sys.path.insert(0, ENGINES)
import submit_intent as si


@pytest.fixture()
def store_iso(tmp_path, monkeypatch):
    d = tmp_path / "intents"
    d.mkdir()
    si.set_store_dir(str(d))
    fixed = datetime(2026, 9, 19, 6, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(si, "_utcnow", lambda: fixed)
    yield d
    si.reset_store_dir()


def test_record_intent_collision_raises_and_preserves_terminal(store_iso):
    aid = si.record_intent("R-1", "Acme", "browser", "digest-1")
    si.mark_submitted(aid, "provider confirmation text")
    before = si.get(aid)
    assert before["state"] == "SUBMITTED"
    assert len(before["history"]) == 2

    with pytest.raises(si.AttemptIdCollision):
        si.record_intent("R-1", "Acme", "browser", "digest-1")

    after = si.get(aid)
    assert after["state"] == "SUBMITTED", \
        "terminal record was resurrected by the collision"
    assert len(after["history"]) == 2, "history was destroyed by the collision"
    assert after == before


def test_attempt_id_collision_is_not_runtime_error():
    """AttemptIdCollision must propagate as-is — it must NOT be caught by
    record_intent's `except RuntimeError` store-unreadable handler."""
    assert issubclass(si.AttemptIdCollision, Exception)
    assert not issubclass(si.AttemptIdCollision, RuntimeError)


def test_distinct_seconds_do_not_collide(store_iso, monkeypatch):
    """Control: different seconds mint distinct attempts (no false positive)."""
    from datetime import timedelta
    base = datetime(2026, 9, 19, 6, 0, 0, tzinfo=timezone.utc)
    now = {"t": base}
    monkeypatch.setattr(si, "_utcnow", lambda: now["t"])
    aid1 = si.record_intent("R-2", "Acme", "browser", "digest-2")
    si.mark_submitted(aid1, "confirmation text")
    # Same role+content two seconds later: open check passes (terminal),
    # attempt_id differs, mint succeeds.
    now["t"] = base + timedelta(seconds=2)
    aid2 = si.record_intent("R-2", "Acme", "browser", "digest-2")
    assert aid2 != aid1
    assert si.get(aid2)["state"] == "INTENT"
