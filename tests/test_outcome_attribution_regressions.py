"""Synthetic, read-only chronology and identity attribution regressions."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engines"))
import outcome_analytics as oa


def row(**changes):
    return {"role_id": "A", "company": "Fixture", "status": "SUBMITTED",
            "date_submitted": "2026-09-16T00:00:00Z",
            "submission_ref": "REF-1234",
            "posting_url": "https://example.org/jobs/1234", **changes}


def event(**changes):
    return {"company": "Fixture", "ts": "2026-09-17T00:00:00Z", **changes}


@pytest.mark.parametrize("identity", [{}, {"role_id": "A"},
    {"receipt_ref": "REF-1234"}, {"posting_url": "https://example.org/jobs/1234"}])
@pytest.mark.parametrize("submitted", ["2026-09-18T00:00:00Z", None,
    "invalid", "2026-02-30", "2026-09-16 25:00 PDT"])
def test_missing_invalid_and_future_submission_never_links(identity, submitted):
    response = event(**identity)
    linked, unlinked, held = oa.link_responses([row(date_submitted=submitted)], [response])
    assert not linked
    assert unlinked == [response]
    assert held == []
    assert "linkage" not in response


@pytest.mark.parametrize("response_time", [None, "invalid", "2026-02-30"])
def test_unknown_response_time_cannot_establish_chronology(response_time):
    response = event(role_id="A", ts=response_time)
    linked, unlinked, held = oa.link_responses([row()], [response])
    assert not linked and unlinked == [response] and not held


@pytest.mark.parametrize("identity", [{"role_id": "B"},
    {"receipt_ref": "REF-123"}, {"posting_url": "https://example.org/jobs/123"},
    {"ats_job_id": "123"}])
def test_explicit_unmatched_identity_never_uses_company_fallback(identity):
    response = event(**identity)
    linked, unlinked, held = oa.link_responses([row()], [response])
    assert not linked and unlinked == [response] and not held


def test_future_exact_role_never_falls_back_to_earlier_company_row():
    response = event(role_id="A")
    rows = [row(date_submitted="2026-09-18T00:00:00Z"), row(role_id="B")]
    linked, unlinked, held = oa.link_responses(rows, [response])
    assert not linked and unlinked == [response] and not held


def test_same_instant_with_timezone_offsets_remains_linkable():
    response = event(role_id="A", ts="2026-09-15T17:00:00-07:00")
    linked, unlinked, held = oa.link_responses([row()], [response])
    assert list(linked) == [0] and not unlinked and not held


def test_company_candidate_set_excludes_future_application():
    rows = [row(role_id="A"), row(role_id="B", date_submitted="2026-09-18T00:00:00Z")]
    linked, unlinked, held = oa.link_responses(rows, [event()])
    assert list(linked) == [0] and not unlinked and not held
