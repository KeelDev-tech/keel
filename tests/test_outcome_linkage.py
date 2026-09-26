"""Tiered deterministic receipt/event linkage for outcome_analytics.

Covers: tier-1 role_id link wins over company ambiguity; tier-2
receipt-ref link; multi-candidate company matches go to the explicit
review queue (never silently attributed); unambiguous company matches
keep the existing latest-date<=ts rule; linkage provenance is attached
to event copies; original event dicts are never mutated.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "engines"))
import outcome_analytics as oa


def rows_two_same_company():
    return [
        {"role_id": "A", "company": "Fixture Corp", "title": "Ops Manager",
         "status": "SUBMITTED", "date_submitted": "2026-09-15T00:00:00Z",
         "resume_lane": "ops"},
        {"role_id": "B", "company": "Fixture Corp", "title": "Ops Lead",
         "status": "SUBMITTED", "date_submitted": "2026-09-16T00:00:00Z",
         "resume_lane": "ops"},
    ]


def base_event(**kw):
    e = {"company": "Fixture Corp", "company_key": "fixture",
         "ts": "2026-09-17T10:00:00Z", "outcome": "REJECTION"}
    e.update(kw)
    return e


def test_tier1_role_id_wins_over_company_ambiguity():
    rows = rows_two_same_company()
    event = base_event(role_id="B")
    linked, unlinked, held = oa.link_responses(rows, [event])
    assert list(linked) == [1]
    assert unlinked == []
    assert held == []
    assert linked[1][0]["linkage"]["tier"] == 1


def test_tier2_receipt_ref_link():
    rows = [
        {"role_id": "A", "company": "Acme", "title": "T1",
         "status": "SUBMITTED", "date_submitted": "2026-09-15T00:00:00Z",
         "resume_lane": "ops", "submission_ref": "CONF-12345"},
        {"role_id": "B", "company": "Other", "title": "T2",
         "status": "SUBMITTED", "date_submitted": "2026-09-15T00:00:00Z",
         "resume_lane": "ops"},
    ]
    # Company name does NOT match Acme; the receipt ref must carry it.
    event = base_event(company="Some Sender", company_key="somesender",
                       receipt_ref="conf-12345")
    linked, unlinked, held = oa.link_responses(rows, [event])
    assert list(linked) == [0]
    assert held == []
    assert linked[0][0]["linkage"]["tier"] == 2


def test_tier2_receipt_ref_beats_company_fallback():
    rows = rows_two_same_company()
    rows[0]["submission_ref"] = "R-9"
    event = base_event(receipt_ref="r-9")  # would be ambiguous by company
    linked, unlinked, held = oa.link_responses(rows, [event])
    assert list(linked) == [0]
    assert linked[0][0]["linkage"]["tier"] == 2
    assert held == []


def test_tier3_posting_url_link():
    rows = [
        {"role_id": "A", "company": "Acme", "title": "T1",
         "status": "SUBMITTED", "date_submitted": "2026-09-15T00:00:00Z",
         "resume_lane": "ops",
         "posting_url": "https://boards.greenhouse.io/acme/jobs/777"},
    ]
    event = base_event(company="Some Sender", company_key="somesender",
                       posting_url="https://boards.greenhouse.io/acme/jobs/777/")
    linked, unlinked, held = oa.link_responses(rows, [event])
    assert list(linked) == [0]
    assert linked[0][0]["linkage"]["tier"] == 3


def test_ambiguous_company_match_goes_to_review_queue():
    rows = rows_two_same_company()
    event = base_event()
    linked, unlinked, held = oa.link_responses(rows, [event])
    assert not linked            # not attributed to either row
    assert unlinked == []        # not unlinked either — it IS a match
    assert len(held) == 1
    prov = held[0]["linkage"]
    assert prov["tier"] == 4
    assert "rule" in prov and "basis" in prov


def test_unambiguous_company_match_keeps_latest_date_le_ts():
    rows = [
        {"role_id": "A", "company": "Acme", "title": "T1",
         "status": "SUBMITTED", "date_submitted": "2026-09-15T00:00:00Z",
         "resume_lane": "ops"},
    ]
    event = base_event(company="Acme", company_key="acme",
                       ts="2026-09-17T00:00:00Z")
    linked, unlinked, held = oa.link_responses(rows, [event])
    assert list(linked) == [0]
    assert unlinked == []
    assert held == []
    assert linked[0][0]["linkage"]["tier"] == 4
    assert "latest-date" in linked[0][0]["linkage"]["rule"]


def test_dead_rows_never_indexed_even_for_role_id():
    rows = [
        {"role_id": "A", "company": "Acme", "title": "T1",
         "status": "REJECTED", "date_submitted": "2026-09-15T00:00:00Z",
         "resume_lane": "ops"},
    ]
    event = base_event(company="Acme", company_key="acme", role_id="A")
    linked, unlinked, held = oa.link_responses(rows, [event])
    assert not linked
    assert held == []
    assert unlinked == [event]


def test_linkage_provenance_shape():
    rows = rows_two_same_company()
    event = base_event(role_id="A")
    linked, _, _ = oa.link_responses(rows, [event])
    prov = linked[0][0]["linkage"]
    assert set(prov) == {"tier", "rule", "basis"}
    assert prov["tier"] == 1
    assert isinstance(prov["rule"], str) and prov["rule"]
    assert isinstance(prov["basis"], str) and prov["basis"]


def test_original_event_dicts_unmutated():
    rows = rows_two_same_company()
    linked_event = base_event(role_id="A")
    held_event = base_event()
    snapshot = [dict(linked_event), dict(held_event)]
    oa.link_responses(rows, [linked_event, held_event])
    assert "linkage" not in linked_event
    assert "linkage" not in held_event
    assert [dict(linked_event), dict(held_event)] == snapshot


def test_toggle_false_restores_legacy_attribution():
    rows = rows_two_same_company()
    event = base_event()
    old = oa.HOLD_AMBIGUOUS_FOR_REVIEW
    oa.HOLD_AMBIGUOUS_FOR_REVIEW = False
    try:
        linked, unlinked, held = oa.link_responses(rows, [event])
        assert held == []
        # Legacy rule: latest date_submitted <= ts -> row B (idx 1).
        assert list(linked) == [1]
    finally:
        oa.HOLD_AMBIGUOUS_FOR_REVIEW = old
