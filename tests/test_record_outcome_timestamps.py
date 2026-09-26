"""gap-plan item (b): occurred_at vs observed_at separation in outcome telemetry.

No network, no live telemetry writes: log_event.log is monkeypatched to
capture the details dict, or emit_telemetry=False is used. record() with
outcome="submitted" would require the security authority and raise — all
tests here use outcome="blocked".
"""
import os
import sys
from datetime import datetime, timezone

import pytest

_ENGINES = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "..", "engines")
sys.path.insert(0, _ENGINES)

import record_outcome  # noqa: E402


def _captured_details(monkeypatch):
    """Monkeypatch log_event.log; return (calls list, captured details holder)."""
    calls = []

    def fake_log(event_type, **kwargs):
        calls.append((event_type, kwargs))
        return {"event_type": event_type, **kwargs}

    monkeypatch.setattr(record_outcome.log_event, "log", fake_log)
    return calls


def test_default_basis_is_unknown_and_ineligible(monkeypatch):
    calls = _captured_details(monkeypatch)
    record_outcome.record("greenhouse", "manual", "blocked",
                          "synthetic timestamp test", emit_telemetry=True)
    assert len(calls) == 1
    details = calls[0][1]["details"]
    assert details["timestamp_basis"] == "unknown"
    # occurred_at carries the validated evidence date; evidence_date stays
    # as a backward-compat alias with an identical value.
    assert details["occurred_at"] == details["evidence_date"]
    assert record_outcome.latency_eligible(details) is False


def test_explicit_basis_ats_receipt_details(monkeypatch):
    calls = _captured_details(monkeypatch)
    record_outcome.record("greenhouse", "manual", "blocked",
                          "synthetic timestamp test", date="2026-09-15",
                          timestamp_basis="ats_receipt",
                          emit_telemetry=True)
    details = calls[0][1]["details"]
    assert details["timestamp_basis"] == "ats_receipt"
    assert details["occurred_at"] == "2026-09-15"
    assert details["evidence_date"] == "2026-09-15"  # alias, identical value
    # observed_at is a recent UTC ISO timestamp.
    observed = datetime.fromisoformat(details["observed_at"])
    assert observed.tzinfo is not None
    assert abs((datetime.now(timezone.utc) - observed).total_seconds()) < 120
    # source watermark present and pinned to the module revision.
    assert details["source_watermark"] == "record_outcome/1"
    assert details["source_watermark"] == record_outcome.WRITER_REV
    assert record_outcome.latency_eligible(details) is True


def test_date_without_basis_never_infers(monkeypatch):
    # --date without an explicit basis must leave basis "unknown": the
    # basis is never inferred from the presence of a date.
    calls = _captured_details(monkeypatch)
    record_outcome.record("greenhouse", "manual", "blocked",
                          "synthetic timestamp test", date="2026-09-10",
                          emit_telemetry=True)
    details = calls[0][1]["details"]
    assert details["timestamp_basis"] == "unknown"
    assert details["occurred_at"] == "2026-09-10"
    assert record_outcome.latency_eligible(details) is False


def test_record_rejects_unrecognized_basis_value(monkeypatch):
    _captured_details(monkeypatch)
    with pytest.raises(ValueError):
        record_outcome.record("greenhouse", "manual", "blocked",
                              "synthetic timestamp test",
                              timestamp_basis="invented", emit_telemetry=True)


def test_emit_telemetry_false_emits_nothing(monkeypatch):
    calls = _captured_details(monkeypatch)
    result = record_outcome.record("greenhouse", "manual", "blocked",
                                   "synthetic timestamp test",
                                   timestamp_basis="backfill",
                                   emit_telemetry=False)
    assert result is None
    assert calls == []


def test_parse_cli_args_accepts_timestamp_basis():
    parsed = record_outcome.parse_cli_args(
        ["record_outcome.py", "greenhouse", "manual", "blocked", "note",
         "--timestamp-basis", "ats_receipt"])
    assert parsed["timestamp_basis"] == "ats_receipt"


def test_parse_cli_args_rejects_invalid_basis():
    with pytest.raises(SystemExit):
        record_outcome.parse_cli_args(
            ["record_outcome.py", "greenhouse", "manual", "blocked", "note",
             "--timestamp-basis", "invented"])


def test_parse_cli_args_rejects_basis_without_value():
    # Fail closed, consistent with existing flag-without-value handling.
    with pytest.raises(SystemExit):
        record_outcome.parse_cli_args(
            ["record_outcome.py", "greenhouse", "manual", "blocked", "note",
             "--timestamp-basis"])


def test_latency_eligible_rejects_bad_shapes():
    le = record_outcome.latency_eligible
    assert le(None) is False
    assert le("not-a-dict") is False
    # Missing basis key is equivalent to "unknown".
    assert le({"occurred_at": "2026-09-18"}) is False
    # Missing / unparseable occurred_at.
    assert le({"timestamp_basis": "ats_api"}) is False
    assert le({"timestamp_basis": "ats_api",
               "occurred_at": "2026-02-30"}) is False
    assert le({"timestamp_basis": "ats_api",
               "occurred_at": "yesterday"}) is False
    # Value outside the closed vocabulary fails closed.
    assert le({"timestamp_basis": "invented",
               "occurred_at": "2026-09-18"}) is False
    # Every non-unknown basis is eligible with a well-formed date.
    for basis in record_outcome.TIMESTAMP_BASES - {"unknown"}:
        assert le({"timestamp_basis": basis,
                   "occurred_at": "2026-09-18"}) is True


def test_timestamp_basis_vocabulary_is_closed():
    assert record_outcome.TIMESTAMP_BASIS_DEFAULT == "unknown"
    assert set(record_outcome.TIMESTAMP_BASES) == {
        "ats_receipt", "ats_api", "browser_confirmation",
        "user_statement", "backfill", "unknown",
    }
