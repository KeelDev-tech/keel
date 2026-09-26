"""Live-tree regression tests for the Keel 0.11.0 pool_health wiring (2026-09-19).

Run from ~/workspace/keel. All fixtures are synthetic; the code under test
resolves from the live tree. The real telemetry log is never touched: the
events path is monkeypatched to a tmp file per test.

Background: keel_local.supply.PoolHealthObserver emits event_type
"pool_health" through injected loggers, monitors/pulse_snapshot.py reads
pool_health events from Keel's telemetry, and the 0.11-ported
monitors/pool_health.py wires PoolHealthObserver to engines/log_event.log.
"pool_health" is therefore registered additively in log_event.EVENT_TYPES.
Canonical names: since the 2026-09-21 pool-health event-naming port
(Trent-ordered), legacy "submitted" input is recorded under the canonical
name "submission_claimed" (details.verification_status="UNVERIFIED" by
default).
"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "engines"))

import log_event
from keel_local.supply import PoolHealthObserver

NOW = datetime.now(timezone.utc)


@pytest.fixture
def events_file(tmp_path, monkeypatch):
    path = tmp_path / "events.jsonl"
    monkeypatch.setattr(log_event, "EVENTS", str(path))
    return path


def read_events(path):
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def test_pool_health_event_type_registered():
    assert "pool_health" in log_event.EVENT_TYPES


def test_canonical_submitted_naming_ported():
    # SUPERSEDED 2026-09-21 (pool-health event-naming port, Trent-ordered):
    # the 0.11-era "must not rename" rule no longer holds — legacy
    # "submitted" input is recorded under the canonical name
    # "submission_claimed". "submitted" remains valid input (remapped at
    # log time).
    assert "submitted" in log_event.EVENT_TYPES
    assert "submission_claimed" in log_event.EVENT_TYPES


def test_submitted_records_as_submission_claimed(events_file):
    event = log_event.log("submitted", role_id="R", details={})
    assert event["event_type"] == "submission_claimed"
    assert event["details"]["verification_status"] == "UNVERIFIED"
    rows = read_events(events_file)
    assert rows[0]["event_type"] == "submission_claimed"


def test_pool_health_log_appends_and_acknowledges(events_file):
    receipt = log_event.log("pool_health", source="pool-guardian",
                            details={"state": "HEALTHY", "ready": 5,
                                     "actionable": 3})
    assert isinstance(receipt, dict)
    assert receipt["event_type"] == "pool_health"
    rows = read_events(events_file)
    assert len(rows) == 1
    assert rows[0]["event_type"] == "pool_health"
    assert rows[0]["source"] == "pool-guardian"


def test_ported_observer_emit_path_works_after_debounce(events_file):
    # Mirrors the wiring in monitors/pool_health.py --emit:
    # PoolHealthObserver(log_event.log).observe(...)
    observer = PoolHealthObserver(log_event.log)
    observed_at = NOW - timedelta(seconds=120)
    first = observer.observe(5, 3, observed_at=observed_at, now=NOW)
    assert first["event_emitted"] is False  # debounced: pending
    second = observer.observe(5, 3, observed_at=observed_at,
                              now=NOW + timedelta(seconds=30))
    assert second["event_emitted"] is True
    rows = read_events(events_file)
    assert len(rows) == 1
    assert rows[0]["event_type"] == "pool_health"
    assert rows[0]["details"]["observation_kind"] == "transition"
