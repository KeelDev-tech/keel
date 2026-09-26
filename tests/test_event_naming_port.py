#!/usr/bin/env python3
"""Regression tests — pool-health EVENT NAMING port (2026-09-21, agent-b).

Ports ONLY the event-naming delta from the recovery candidate into the live
engines:
  (a) legacy 'submitted' is recorded as 'submission_claimed'; every
      submission claim carries details.verification_status='UNVERIFIED'
      by default;
  (b) the six new EVENT_TYPES are accepted;
  (c) the SECURITY_BLOCKED authority choke point in record_outcome.py is
      still enforced (the candidate deleted it; it was deliberately NOT
      ported).

All fixtures synthetic; events land in tmp files, never real telemetry.
No queue/ledger/journal writes.
"""

import json
import os
import sys

import pytest

KEEL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(KEEL_DIR, "engines"))

import log_event  # noqa: E402
import record_outcome  # noqa: E402

NEW_EVENT_TYPES = (
    "flow_attempt", "packet_prepared", "submission_claimed",
    "verification_attempt", "source_sync", "pipeline_recovery",
)


def _isolate_events(monkeypatch, tmp_path):
    monkeypatch.setattr(log_event, "EVENTS",
                        str(tmp_path / "events.jsonl"))


def _read_events(tmp_path):
    path = tmp_path / "events.jsonl"
    if not path.exists():
        return []
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


# ---------------------------------------------------------------- (a) remap


class TestSubmittedRemap:
    def test_submitted_is_remapped_to_submission_claimed(self, monkeypatch, tmp_path):
        _isolate_events(monkeypatch, tmp_path)
        event = log_event.log("submitted", role_id="R1", company="Acme",
                              ats="greenhouse", source="t",
                              details={"confirmation": "Received"})
        assert event["event_type"] == "submission_claimed"
        rows = _read_events(tmp_path)
        assert len(rows) == 1
        assert rows[0]["event_type"] == "submission_claimed"

    def test_submission_claim_is_unverified_by_default(self, monkeypatch, tmp_path):
        _isolate_events(monkeypatch, tmp_path)
        event = log_event.log("submitted", role_id="R1", details={})
        assert event["details"]["verification_status"] == "UNVERIFIED"

    def test_direct_submission_claimed_also_defaults_unverified(self, monkeypatch, tmp_path):
        _isolate_events(monkeypatch, tmp_path)
        event = log_event.log("submission_claimed", role_id="R1", details={})
        assert event["details"]["verification_status"] == "UNVERIFIED"

    def test_caller_supplied_verification_status_is_not_clobbered(self, monkeypatch, tmp_path):
        # "by default" — a verification receipt's explicit status survives.
        _isolate_events(monkeypatch, tmp_path)
        event = log_event.log("submitted", role_id="R1",
                              details={"verification_status": "VERIFIED"})
        assert event["details"]["verification_status"] == "VERIFIED"

    def test_submitted_still_valid_input_not_rejected(self, monkeypatch, tmp_path):
        _isolate_events(monkeypatch, tmp_path)
        event = log_event.log("submitted", details={})
        assert event["event_type"] == "submission_claimed"

    def test_unknown_event_type_still_rejected_with_live_message(self, monkeypatch, tmp_path):
        _isolate_events(monkeypatch, tmp_path)
        with pytest.raises(ValueError) as exc:
            log_event.log("submitted_successfully", details={})
        assert "unknown event_type 'submitted_successfully'" in str(exc.value)


# ------------------------------------------------------------ (b) new names


class TestNewEventTypes:
    @pytest.mark.parametrize("event_type", NEW_EVENT_TYPES)
    def test_new_event_type_accepted(self, monkeypatch, tmp_path, event_type):
        _isolate_events(monkeypatch, tmp_path)
        event = log_event.log(event_type, role_id="R1", details={})
        assert event["event_type"] == event_type
        rows = _read_events(tmp_path)
        assert [r["event_type"] for r in rows] == [event_type]

    def test_all_new_types_in_vocabulary(self):
        for event_type in NEW_EVENT_TYPES:
            assert event_type in log_event.EVENT_TYPES


# --------------------------------- (c) SECURITY_BLOCKED choke still enforced


class _BoomLog:
    """log_event.log replacement that explodes if the choke lets anything through."""

    def __init__(self):
        self.calls = []

    def __call__(self, *a, **k):
        self.calls.append((a, k))
        raise AssertionError("telemetry must not be written past the choke")


@pytest.fixture
def boom_log(monkeypatch):
    stub = _BoomLog()
    monkeypatch.setattr(log_event, "log", stub)
    return stub


def _record_submitted():
    return dict(ats="greenhouse", technique="manual-review",
                outcome="submitted", note="confirmation quote",
                role_id="R1", company="Acme")


class TestSecurityBlockedChoke:
    def test_submitted_refused_when_security_authority_unavailable(
            self, monkeypatch, tmp_path, boom_log):
        # Choke must fire before anything is recorded: the ValueError is
        # raised and the telemetry writer is never reached.
        monkeypatch.setattr(record_outcome, "_SECURITY_AVAILABLE", False)
        monkeypatch.setattr(record_outcome, "_sec_authorize_submission", None)
        _isolate_events(monkeypatch, tmp_path)
        with pytest.raises(ValueError, match="security authority unavailable"):
            record_outcome.record(**_record_submitted())
        assert boom_log.calls == []
        assert _read_events(tmp_path) == []

    def test_authority_denial_propagates(self, monkeypatch, boom_log):
        # When the authority is present but DENIES, its refusal — not the
        # record — wins. Refusal raises a ValueError (fail-closed contract).
        monkeypatch.setattr(record_outcome, "_SECURITY_AVAILABLE", True)

        def deny(**kwargs):
            raise ValueError("PolicyDenied: no record_submission capability")

        monkeypatch.setattr(record_outcome, "_sec_authorize_submission", deny)
        with pytest.raises(ValueError, match="PolicyDenied"):
            record_outcome.record(**_record_submitted())
        assert boom_log.calls == []

    def test_authority_allow_records_submission_claimed(self, monkeypatch):
        # Allowed submissions flow through and emit the canonical
        # 'submission_claimed' event name (the UNVERIFIED default stamp is
        # applied inside the real log_event.log, covered in (a) above).
        monkeypatch.setattr(record_outcome, "_SECURITY_AVAILABLE", True)
        monkeypatch.setattr(record_outcome, "_sec_authorize_submission",
                            lambda **kwargs: None)
        captured = {}

        def fake_log(event_type, **kwargs):
            captured["event_type"] = event_type
            captured["details"] = kwargs.get("details", {})
            return {"event_id": "eid-1", "event_type": event_type}

        monkeypatch.setattr(log_event, "log", fake_log)
        record_outcome.record(**_record_submitted())
        assert captured["event_type"] == "submission_claimed"

    def test_blocked_outcome_does_not_need_submission_authority(
            self, monkeypatch, boom_log):
        # The choke gates SUBMISSION-class actions only; blocked outcomes
        # must not consult the security authority at all.
        monkeypatch.setattr(record_outcome, "_SECURITY_AVAILABLE", False)
        monkeypatch.setattr(record_outcome, "_sec_authorize_submission", None)
        with pytest.raises(AssertionError, match="telemetry must not be written"):
            record_outcome.record(ats="greenhouse", technique="manual-review",
                                  outcome="blocked", note="captcha wall",
                                  role_id="R1", company="Acme")
        # the AssertionError came from the boom stub, i.e. record() reached
        # the telemetry call without touching the authority
        assert len(boom_log.calls) == 1
        assert boom_log.calls[0][0][0] == "gate_blocked"
