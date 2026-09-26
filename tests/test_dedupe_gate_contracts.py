"""Regression tests for the 2026-09-19 silent-defect sweep in the keel tree.

Four verified silent defects, one contract each:

FIX 1 — ledger path unification. The submit writer (apply_loop) and the
other readers use the DATA convention
(keel_paths.DATA/application-ledger.json); dedupe_gate and staging_ingest
read a dead <HOME>/ledger/application-ledger.json path nothing writes to,
so the default-path `duplicate_of_submitted` leg never fired.

FIX 2 — SUBMITTED filter inside _check_rows. Callers that pass explicit
ledger_rows (e.g. sweep) hand over the full unfiltered ledger; a REJECTED
row then yielded ("duplicate", kind="duplicate_of_submitted"), blocking
legitimate re-discovery. Only SUBMITTED rows may block.

FIX 3 — candidate-side URL check across ALL url fields. _url_of returned
only the first non-empty URL field while rows are scanned across all of
them, so a candidate whose application_url matched a SUBMITTED row but
whose ats_url was fresh verified "fresh".

FIX 4 — caller title-normalization consistency. sweep pre-strips trailing
(Remote)/(Hybrid)/(Onsite) via N.norm_title before the gate, but the
ashby/lever and greenhouse enumerators passed raw titles — same lead,
different verdicts by path.

All fixtures are synthetic. Nothing here touches live queues, the real
ledger, or ~/workspace/job-pipeline.

Run: python3 -m pytest tests/test_dedupe_gate_contracts.py -q
"""
import inspect
import json
import os
import sys

import pytest

ENGINES = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "engines")
sys.path.insert(0, ENGINES)

import dedupe_gate  # noqa: E402
from keel_paths import DATA  # noqa: E402

EXPECTED_LEDGER = os.path.join(DATA, "application-ledger.json")

COMPANY = "Acme Corp"
TITLE = "Backend Engineer"
FRESH_URL = "https://fresh.example.com/postings/1"


def _submitted_row(**kw):
    row = {"role_id": "SYN-SUB-1", "company": COMPANY, "title": TITLE,
           "status": "SUBMITTED",
           "posting_url": "https://ledger.example.com/applied/1"}
    row.update(kw)
    return row


def _write_ledger(tmp_path, rows):
    p = tmp_path / "application-ledger.json"
    p.write_text(json.dumps({"rows": rows}))
    return str(p)


def _write_queue(tmp_path):
    p = tmp_path / "queue.json"
    p.write_text("[]")
    return str(p)


# ---------------------------------------------------------------------------
# FIX 1 — ledger path unification
# ---------------------------------------------------------------------------

def test_fix1_dedupe_gate_ledger_points_at_data():
    assert "/ledger/" not in dedupe_gate.LEDGER, dedupe_gate.LEDGER
    assert dedupe_gate.LEDGER == EXPECTED_LEDGER


def test_fix1_staging_ingest_ledger_points_at_data():
    import staging_ingest
    assert "/ledger/" not in staging_ingest.LEDGER, staging_ingest.LEDGER
    assert staging_ingest.LEDGER == EXPECTED_LEDGER
    # run_ingest builds its own path from the pipe root; it must follow the
    # DATA convention too (the old <pipe>/ledger/... path is dead).
    src = inspect.getsource(staging_ingest.run_ingest)
    assert 'os.path.join(pipe, "data", "application-ledger.json")' in src
    assert '"ledger", "application-ledger.json"' not in src
    assert '"ledger/application-ledger.json"' not in src


def test_fix1_default_path_reads_data_convention_ledger(tmp_path, monkeypatch):
    """Default path (no explicit rows) must see the DATA-convention ledger.

    Pre-fix LEDGER pointed at the dead <HOME>/ledger/... path, so the
    synthetic file below was never read and the verdict was "fresh".
    """
    monkeypatch.setattr(dedupe_gate, "LEDGER",
                        _write_ledger(tmp_path, [_submitted_row()]))
    monkeypatch.setattr(dedupe_gate, "STANDARD_QUEUE",
                        _write_queue(tmp_path))
    verdict, evidence = dedupe_gate.check_candidate(
        COMPANY, TITLE, "https://ledger.example.com/applied/1")
    assert verdict == "duplicate"
    assert evidence["kind"] == "duplicate_of_submitted"
    assert evidence["match"] == "posting_url"


# ---------------------------------------------------------------------------
# FIX 2 — SUBMITTED-only filter inside _check_rows
# ---------------------------------------------------------------------------

def test_fix2_rejected_row_explicit_path_is_fresh():
    """A REJECTED ledger row must never read as duplicate_of_submitted."""
    rejected = _submitted_row(status="REJECTED",
                              posting_url=FRESH_URL)
    verdict, evidence = dedupe_gate.check_candidate(
        COMPANY, TITLE, FRESH_URL,
        ledger_rows=[rejected], queue_entries=[])
    assert verdict == "fresh"
    assert evidence == {}


def test_fix2_submitted_row_explicit_path_still_duplicate():
    submitted = _submitted_row(posting_url=FRESH_URL)
    verdict, evidence = dedupe_gate.check_candidate(
        COMPANY, TITLE, FRESH_URL,
        ledger_rows=[submitted], queue_entries=[])
    assert verdict == "duplicate"
    assert evidence["kind"] == "duplicate_of_submitted"


def test_fix2_queue_entries_are_not_status_filtered():
    """Queue entries keep their old behavior: any status can block."""
    entry = {"role_id": "SYN-Q-1", "company": COMPANY, "title": TITLE,
             "status": "PARKED", "posting_url": FRESH_URL}
    verdict, evidence = dedupe_gate.check_candidate(
        COMPANY, TITLE, FRESH_URL,
        ledger_rows=[], queue_entries=[entry])
    assert verdict == "duplicate"
    assert evidence["kind"] == "duplicate_in_queue"


def test_fix2_mixed_rows_only_submitted_blocks():
    rows = [_submitted_row(status="REJECTED", role_id="SYN-R-9",
                           posting_url=FRESH_URL),
            _submitted_row(role_id="SYN-S-2",
                           posting_url="https://ledger.example.com/other")]
    verdict, evidence = dedupe_gate.check_candidate(
        COMPANY, TITLE, FRESH_URL,
        ledger_rows=rows, queue_entries=[])
    # Exact REJECTED URL is filtered. Similar title is advisory only.
    assert verdict == "suspect"
    assert evidence["kind"] == "possible_duplicate"
    assert evidence["match"] == "name_or_title"


# ---------------------------------------------------------------------------
# FIX 3 — candidate URL check across ALL url fields
# ---------------------------------------------------------------------------

def test_fix3_urls_of_collects_all_fields_in_order():
    e = {"ats_url": "  https://a.example/j1  ",
         "application_url": "",
         "posting_url": "https://p.example/j1",
         "confirmation_url": None,
         "url": "https://u.example/j1"}
    assert dedupe_gate._urls_of(e) == [
        "https://a.example/j1", "https://p.example/j1", "https://u.example/j1"]
    assert dedupe_gate._urls_of({}) == []


def test_fix3_secondary_url_field_match_dupes_via_filter_batch():
    """Reproduced case: ats_url fresh, application_url matches SUBMITTED."""
    row = _submitted_row(posting_url="https://jobs.lever.co/acme/applied-1")
    entry = {"role_id": "SYN-E-1", "company": COMPANY, "title": TITLE,
             "ats_url": FRESH_URL,
             "application_url": "https://jobs.lever.co/acme/applied-1"}
    fresh, dupes = dedupe_gate.filter_batch(
        [entry], ledger_rows=[row], queue_entries=[])
    assert fresh == []
    assert len(dupes) == 1
    assert dupes[0]["dedupe_evidence"]["kind"] == "duplicate_of_submitted"
    assert dupes[0]["dedupe_evidence"]["match"] == "posting_identity"


def test_fix3_check_candidate_urls_param_and_single_url_fallback():
    row = _submitted_row(posting_url="https://jobs.lever.co/acme/applied-1")
    # urls= given: second field matches -> duplicate
    verdict, _ = dedupe_gate.check_candidate(
        COMPANY, TITLE, FRESH_URL,
        ledger_rows=[row], queue_entries=[],
        urls=[FRESH_URL, "https://jobs.lever.co/acme/applied-1"])
    assert verdict == "duplicate"
    # urls=None: falls back to the single url arg (legacy shape)
    verdict, _ = dedupe_gate.check_candidate(
        COMPANY, TITLE, "https://jobs.lever.co/acme/applied-1",
        ledger_rows=[row], queue_entries=[])
    assert verdict == "duplicate"
    # urls=None: falls back to the single url arg (legacy shape). Use a
    # non-matching company/title to isolate the URL ladder.
    verdict, _ = dedupe_gate.check_candidate(
        "Zeta Widgets", "Junior Widget Analyst", FRESH_URL,
        ledger_rows=[row], queue_entries=[], urls=None)
    assert verdict == "fresh"


def test_fix3_single_url_main_shape_unaffected(tmp_path, monkeypatch):
    """check_candidate(company, title, url) keeps working for old callers."""
    monkeypatch.setattr(dedupe_gate, "LEDGER",
                        _write_ledger(tmp_path, [_submitted_row()]))
    monkeypatch.setattr(dedupe_gate, "STANDARD_QUEUE",
                        _write_queue(tmp_path))
    verdict, _ = dedupe_gate.check_candidate(
        COMPANY, TITLE, "https://ledger.example.com/applied/1")
    assert verdict == "duplicate"


# ---------------------------------------------------------------------------
# FIX 4 — enumerators normalize titles before the gate, like sweep
# ---------------------------------------------------------------------------

def _remote_suffix_ledger(tmp_path, monkeypatch):
    from ats_discovery import normalize as N  # noqa
    assert N.norm_title("Backend Engineer (Remote)") == "Backend Engineer"
    monkeypatch.setattr(dedupe_gate, "LEDGER", _write_ledger(
        tmp_path, [{"role_id": "SYN-S-3", "company": "Acme",
                    "title": "Backend Engineer", "status": "SUBMITTED",
                    "posting_url": "https://ledger.example.com/applied/9"}]))
    monkeypatch.setattr(dedupe_gate, "STANDARD_QUEUE",
                        _write_queue(tmp_path))


def test_fix4_ashby_enumerator_strips_remote_suffix_before_gate(
        tmp_path, monkeypatch):
    import ashby_lever_json_enumerate as ashby
    _remote_suffix_ledger(tmp_path, monkeypatch)

    def fake_fetch(platform, board):
        return {"jobs": [
            {"id": "j1", "title": "Backend Engineer (Remote)",
             "isListed": True, "location": "Remote",
             "jobUrl": "https://fresh.example.com/ashby/j1"},
            {"id": "j2", "title": "Backend Engineer",
             "isListed": True, "location": "Remote",
             "jobUrl": "https://fresh.example.com/ashby/j2"},
        ]}

    monkeypatch.setattr(ashby, "fetch_board_json", fake_fetch)
    out = ashby.enumerate_boards([("ashby", "acme", "Acme Corp")], live=False)
    # Normalized titles are similar, but distinct posting URLs must survive.
    assert out["per_board"]["ashby:acme"]["duplicates_skipped"] == 0
    assert len(out["entries"]) == 2


def test_fix4_greenhouse_enumerator_strips_remote_suffix_before_gate(
        tmp_path, monkeypatch):
    import greenhouse_json_enumerate as gh
    _remote_suffix_ledger(tmp_path, monkeypatch)

    def fake_fetch(board):
        return {"jobs": [
            {"id": 101, "title": "Backend Engineer (Remote)",
             "location": {"name": "Remote"},
             "absolute_url": "https://fresh.example.com/gh/101"},
            {"id": 102, "title": "Backend Engineer",
             "location": {"name": "Remote"},
             "absolute_url": "https://fresh.example.com/gh/102"},
        ]}

    monkeypatch.setattr(gh, "fetch_board_json", fake_fetch)
    out = gh.enumerate_boards(["acme"], live=False)
    assert out["per_board"]["acme"]["duplicates_skipped"] == 0
    assert len(out["entries"]) == 2


def test_fix4_role_id_still_built_from_raw_title(tmp_path, monkeypatch):
    """The fix must not touch entry construction: the in-batch emp_key and
    the role_id still derive from the RAW feed title (only the gate call
    normalizes)."""
    import ashby_lever_json_enumerate as ashby
    monkeypatch.setattr(dedupe_gate, "LEDGER",
                        _write_ledger(tmp_path, []))
    monkeypatch.setattr(dedupe_gate, "STANDARD_QUEUE",
                        _write_queue(tmp_path))

    def fake_fetch(platform, board):
        return {"jobs": [
            {"id": "j9", "title": "Backend Engineer (Remote)",
             "isListed": True, "location": "Remote",
             "jobUrl": "https://fresh.example.com/ashby/j9"},
        ]}

    monkeypatch.setattr(ashby, "fetch_board_json", fake_fetch)
    out = ashby.enumerate_boards([("ashby", "acme", "Acme Corp")], live=False)
    assert len(out["entries"]) == 1
    entry = out["entries"][0]
    # role_id embeds the first 4 raw title words -> "REMOTE" present means
    # the raw (Remote)-suffixed title still fed entry construction.
    assert "REMOTE" in entry["role_id"]
    # entry title normalization is pre-existing build_json_entry behavior,
    # untouched by this fix.
    assert entry["title"] == "Backend Engineer"
