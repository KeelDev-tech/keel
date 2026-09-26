"""Published analytics snapshot block for outcome_analytics.

Covers: snapshot block present with all five fields; cohort_id is
deterministic for identical inputs; cutoff excludes a late-arriving
event when the report is rebuilt from original inputs (historical cut
preserved); held-for-review events are excluded from rates.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "engines"))
import outcome_analytics as oa


def submitted_rows():
    return [
        {"role_id": "A", "company": "Acme", "title": "T1",
         "status": "SUBMITTED", "date_submitted": "2026-09-15T00:00:00Z",
         "resume_lane": "ops", "transport": "browser", "ats": "greenhouse",
         "source": "company site"},
        {"role_id": "B", "company": "Acme", "title": "T2",
         "status": "SUBMITTED", "date_submitted": "2026-09-16T00:00:00Z",
         "resume_lane": "ops", "transport": "browser", "ats": "greenhouse",
         "source": "company site"},
        {"role_id": "C", "company": "Beta LLC", "title": "T3",
         "status": "SUBMITTED", "date_submitted": "2026-09-14T00:00:00Z",
         "resume_lane": "ops", "transport": "browser", "ats": "lever",
         "source": "company site"},
    ]


def event(**kw):
    e = {"company": "Acme", "company_key": "acme",
         "ts": "2026-09-17T10:00:00Z", "outcome": "AUTO_ACK",
         "ts_trustworthy": True}
    e.update(kw)
    return e


def test_snapshot_block_present_with_all_fields():
    rows = submitted_rows()
    report = oa.build_report(rows, [event()], link_rows=rows)
    snap = report["snapshot"]
    assert set(snap) == {"cutoff_utc", "cohort_id", "query_version",
                         "denominator_definition", "missingness"}
    assert snap["query_version"] == oa.QUERY_VERSION == "outcome-analytics/1"
    assert "distinct applications" in snap["denominator_definition"]
    assert set(snap["missingness"]) == {
        "rows_missing_or_unparseable_date_submitted",
        "latency_samples_excluded_untrusted_ts",
        "events_unlinked", "events_held_for_review"}


def test_cohort_id_deterministic_for_identical_inputs():
    rows = submitted_rows()
    events = [event(), event(ts="2026-09-18T00:00:00Z", outcome="REJECTION",
                             company="Beta LLC", company_key="beta")]
    r1 = oa.build_report(rows, events, link_rows=rows)
    r2 = oa.build_report(rows, events, link_rows=rows)
    assert r1["snapshot"]["cohort_id"] == r2["snapshot"]["cohort_id"]
    assert len(r1["snapshot"]["cohort_id"]) == 16
    int(r1["snapshot"]["cohort_id"], 16)  # valid hex
    # Different inputs -> different cohort.
    r3 = oa.build_report(rows, events + [event(outcome="OFFER")],
                         link_rows=rows)
    assert r3["snapshot"]["cohort_id"] != r1["snapshot"]["cohort_id"]


def test_cutoff_excludes_late_arriving_event_on_rebuild():
    rows = submitted_rows()
    events = [event()]
    r1 = oa.build_report(rows, events, link_rows=rows)
    cutoff1 = r1["snapshot"]["cutoff_utc"]
    assert cutoff1 == "2026-09-17T10:00:00+00:00"
    # A late-arriving event lands AFTER this report's data boundary.
    late = event(ts="2026-09-25T00:00:00Z", outcome="REJECTION")
    # Rebuilt from the ORIGINAL inputs, the historical cut is preserved.
    r2 = oa.build_report(rows, events, link_rows=rows)
    assert r2["snapshot"]["cutoff_utc"] == cutoff1
    assert r2["snapshot"]["cohort_id"] == r1["snapshot"]["cohort_id"]
    # A fresh report that INCLUDES the late event gets a new cut+cohort.
    r3 = oa.build_report(rows, events + [late], link_rows=rows)
    assert r3["snapshot"]["cutoff_utc"] == "2026-09-25T00:00:00+00:00"
    assert r3["snapshot"]["cohort_id"] != r1["snapshot"]["cohort_id"]


def test_held_for_review_excluded_from_rates():
    rows = submitted_rows()
    # "Acme" matches two linkable rows -> held, not attributed.
    ambiguous = event(ts="2026-09-17T12:00:00Z", outcome="REJECTION")
    report = oa.build_report(rows, [ambiguous], link_rows=rows)
    assert report["inputs"]["events_held_for_review"] == 1
    assert report["inputs"]["events_linked"] == 0
    assert report["inputs"]["events_unlinked"] == 0
    assert len(report["held_for_review"]) == 1
    lane = report["lanes"]["ops"]
    # The held REJECTION must not leak into outcome counters or rates.
    assert lane["decisive_outcomes"] == 0
    assert lane["outcomes"] == {}
    assert lane["submissions_with_linked_response"] == 0
    snap = report["snapshot"]
    assert snap["missingness"]["events_held_for_review"] == 1
    assert snap["missingness"]["events_unlinked"] == 0


def test_missingness_counts():
    rows = submitted_rows()
    rows.append({"role_id": "D", "company": "Gamma", "title": "T4",
                 "status": "SUBMITTED", "date_submitted": "not-a-date",
                 "resume_lane": "ops"})
    # One ack with an untrusted (backfill-run) timestamp: excluded from
    # latency, counted in missingness. role_id pins it to row A.
    ack = event(ts="2026-09-17T10:00:00Z", outcome="AUTO_ACK",
                ts_trustworthy=False, role_id="A")
    stray = event(ts="2026-09-17T10:00:00Z", outcome="AUTO_ACK",
                  company="Nobody", company_key="nobody")
    report = oa.build_report(rows, [ack, stray], link_rows=rows)
    miss = report["snapshot"]["missingness"]
    assert miss["rows_missing_or_unparseable_date_submitted"] == 1
    assert miss["latency_samples_excluded_untrusted_ts"] == 1
    assert miss["events_unlinked"] == 1
    assert miss["events_held_for_review"] == 0


def test_render_markdown_has_snapshot_section():
    rows = submitted_rows()
    report = oa.build_report(rows, [event()], link_rows=rows)
    md = oa.render_markdown(report)
    assert "## Snapshot (data cut)" in md
    assert report["snapshot"]["cohort_id"] in md
    assert "outcome-analytics/1" in md
    assert "review queue" in md
