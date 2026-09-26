"""Submission-outcome boundary regressions (split from the audit's
test_outcome_integrity_regressions.py).

Only the submission_outcome.py tests are ported here: the record_outcome
tests in the source file are out of scope (record_outcome.py is deferred to
Trent) and the log_event tests cover a skipped telemetry-contract change.
Synthetic inputs and isolated stores only.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engines"))
import submission_outcome


@pytest.mark.parametrize("body", [[], False, 0, "", ["unexpected"], "invalid"])
def test_malformed_json_types_remain_unknown_with_receipt(body, tmp_path):
    receipt = submission_outcome.Receipt(
        "server_token", "synthetic-token", correlates_with_attempt=True,
        attempt_id="synthetic-attempt")
    outcome, reason = submission_outcome.classify_attempt(
        status=200, resp=body, receipt=receipt)
    assert outcome == submission_outcome.UNKNOWN
    assert "malformed" in reason
    submission_outcome.open_hold("synthetic-attempt", reason=reason,
                                 receipt=receipt, store_dir=str(tmp_path))
    holds = submission_outcome.list_open_holds(store_dir=str(tmp_path))
    assert [hold["attempt_id"] for hold in holds] == ["synthetic-attempt"]
    assert not (tmp_path / submission_outcome.OUTCOMES_LOG).exists()
    assert json.loads((tmp_path / submission_outcome.HOLDS_LOG).read_text())["status"] == "open"


@pytest.mark.parametrize("status", [True, False, 200.0, "200", 0, 600, float("nan")])
def test_invalid_http_status_is_unknown(status):
    outcome, _ = submission_outcome.classify_attempt(status=status, resp={})
    assert outcome == submission_outcome.UNKNOWN


@pytest.mark.parametrize("changes", [
    {"correlates_with_attempt": False}, {"correlates_with_attempt": "false"},
    {"correlates_with_attempt": 1}, {"kind": "invented"},
    {"value": ""}, {"value": "   "}, {"value": None},
    {"attempt_id": None}, {"attempt_id": ""},
])
def test_uncorrelated_receipt_never_classifies_or_records(changes, tmp_path):
    fields = dict(kind="server_token", value="synthetic-token",
                  correlates_with_attempt=True, attempt_id="synthetic-attempt")
    receipt = submission_outcome.Receipt(**{**fields, **changes})
    outcome, _ = submission_outcome.classify_attempt(status=200, resp={}, receipt=receipt)
    assert outcome == submission_outcome.UNKNOWN
    with pytest.raises(ValueError, match="exact attempt"):
        submission_outcome.record_submission(receipt, "synthetic-attempt",
                                             store_dir=str(tmp_path))
    assert list(tmp_path.iterdir()) == []


def test_receipt_for_another_attempt_is_refused(tmp_path):
    receipt = submission_outcome.Receipt(
        "server_token", "synthetic-token", correlates_with_attempt=True,
        attempt_id="other-attempt")
    with pytest.raises(ValueError, match="exact attempt"):
        submission_outcome.record_submission(receipt, "synthetic-attempt",
                                             store_dir=str(tmp_path))
    assert list(tmp_path.iterdir()) == []


def test_matching_receipt_record_remains_idempotent(tmp_path):
    receipt = submission_outcome.Receipt(
        "server_token", "synthetic-token", correlates_with_attempt=True,
        attempt_id="synthetic-attempt")
    first = submission_outcome.record_submission(
        receipt, "synthetic-attempt", store_dir=str(tmp_path))
    second = submission_outcome.record_submission(
        receipt, "synthetic-attempt", store_dir=str(tmp_path))
    assert first["duplicate"] is False
    assert second["duplicate"] is True
    rows = [json.loads(line) for line in
            (tmp_path / submission_outcome.OUTCOMES_LOG).read_text().splitlines()]
    assert len(rows) == 2
    assert sum(not row["duplicate"] for row in rows) == 1
