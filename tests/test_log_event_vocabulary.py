#!/usr/bin/env python3
"""Silent-defect sweep (2026-09-19) — log_event.py vocabulary gaps.

FIX 4: producers emitted event/gate names the consumer vocabulary
rejected, so telemetry was silently dropped (the callers' `except`
swallowed the ValueError):
- "verify_cron_run"        (engines/verify_cron.py:249)
- "url_enriched"           (engines/verify_retry.py:1029)
- "url_enrich_failed"      (engines/verify_retry.py:1036,1045)
  -> added to EVENT_TYPES (spellings verified verbatim against producers).
- "yield_drift", "gate_surge" (monitors/ewma_drift.py:45-47 claims them
  "registered additively in log_event.GATE_TYPES" — they were not)
  -> added to GATE_TYPES.

All fixtures synthetic; events land in a tmp file, never real telemetry.
"""

import json
import os
import sys

KEEL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(KEEL_DIR, "engines"))

import log_event  # noqa: E402

NEW_EVENT_TYPES = ("verify_cron_run", "url_enriched", "url_enrich_failed")
NEW_GATE_TYPES = ("yield_drift", "gate_surge")


def _isolate_events(monkeypatch, tmp_path):
    monkeypatch.setattr(log_event, "EVENTS",
                        str(tmp_path / "events.jsonl"))


def _logged_types(tmp_path):
    types = []
    with open(tmp_path / "events.jsonl") as f:
        for line in f:
            types.append(json.loads(line)["event_type"])
    return types


def test_new_event_types_registered():
    for name in NEW_EVENT_TYPES:
        assert name in log_event.EVENT_TYPES, name


def test_new_gate_types_registered():
    for name in NEW_GATE_TYPES:
        assert name in log_event.GATE_TYPES, name


def test_log_accepts_new_event_types(monkeypatch, tmp_path):
    """log() must accept each new event type without raising and persist
    it — before the fix, verify_cron's run telemetry raised ValueError and
    was swallowed."""
    _isolate_events(monkeypatch, tmp_path)
    for name in NEW_EVENT_TYPES:
        log_event.log(name, role_id="", company="", source="silent-sweep-test",
                      details={"probe": True})
    got = _logged_types(tmp_path)
    for name in NEW_EVENT_TYPES:
        assert name in got


def test_new_gates_log_without_warning(monkeypatch, tmp_path, capsys):
    """gates in GATE_TYPES must not trigger the 'unknown gate' stderr
    warning — before the fix, ewma_drift's drift alerts warned on every
    emission."""
    _isolate_events(monkeypatch, tmp_path)
    for gate in NEW_GATE_TYPES:
        log_event.log("gate_encountered", role_id="LANE", company="",
                      source="silent-sweep-test", details={"gate": gate})
    err = capsys.readouterr().err
    assert "unknown gate" not in err


def test_unknown_event_type_still_rejected(monkeypatch, tmp_path):
    """Guard against over-permissive vocabulary: unknown names still raise."""
    _isolate_events(monkeypatch, tmp_path)
    try:
        log_event.log("no_such_event_type_xyz", role_id="", company="",
                      source="silent-sweep-test")
    except ValueError:
        pass
    else:
        raise AssertionError("unknown event_type was accepted")
