"""Regression tests for rescreen_standard.py.

Covers: selection filtering, the three canonical screen decisions, dry-run
zero-write guarantee, live apply routing (title/eligibility/form parks),
NEVER-promote-to-READY invariant, quoted-evidence requirement, lock
contention failing cleanly, pre-write backup, fail-closed on concurrent
mutation, and real-429 observation (hard stop) vs empty-fetch tolerance.
"""
import copy
import json
import os
import sys
import tempfile
import threading

import pytest

BASE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "engines")
if BASE not in sys.path:
    sys.path.insert(0, BASE)

import queue_io  # noqa: E402
import log_event  # noqa: E402


class FakeVR:
    """Stub for the heavy verify_retry module (lazy-imported by
    rescreen_standard). Mirrors the real functions' contracts."""
    texts = {}
    forms = {}

    @staticmethod
    def posting_url(e):
        return e.get("application_url") or e.get("ats_url") or ""

    @staticmethod
    def fetch_posting_text(url):
        return FakeVR.texts.get(url, "")

    @staticmethod
    def screen_promotion_form(entry, url):
        return list(FakeVR.forms.get(entry.get("role_id"), []))

    @staticmethod
    def _stamp_park(cur, ev):
        ts = (ev or {}).get("ts") or "2026-09-17T00:00:00+00:00"
        cur["status_updated"] = ts
        cur["status_updated_park_ref"] = ts
        return cur

    @staticmethod
    def _validate_mutation(e, ctx):
        return e

    @staticmethod
    def _qn_str(e):
        return str(e.get("queue_notes") or "")


#: Module-table stub for the heavy verify_retry (lazy-imported by
#: rescreen_standard at call time). Scoped per-test via fixture below —
#: a module-level sys.modules swap leaked FakeVR into every test module
#: imported afterwards (test_verify_retry_* got the stub instead of the
#: real module), which poisoned full-suite runs with hundreds of
#: order-dependent failures. monkeypatch restores the real entry.
@pytest.fixture(autouse=True)
def _stub_verify_retry(monkeypatch):
    monkeypatch.setitem(sys.modules, "verify_retry", FakeVR)

import rescreen_standard as rs  # noqa: E402


def _entry(rid, title="Revenue Operations Manager", fit=80,
           status="PARKED-PENDING-VERIFICATION",
           url="https://example.com/jobs/1", location="Remote"):
    return {"role_id": rid, "title": title, "company": "TestCo",
            "location": location, "fit_score": fit, "status": status,
            "status_reason": "pending verification",
            "application_url": url, "queue_notes": "", "unresolved": []}


@pytest.fixture()
def env(tmp_path, monkeypatch):
    std_p = str(tmp_path / "standard-queue.json")
    ni_p = str(tmp_path / "needs_input-queue.json")
    ev_p = str(tmp_path / "events.jsonl")
    entries = [
        _entry("R-CLEAN", url="https://example.com/jobs/clean"),
        _entry("R-TITLE", title="Treasury Analyst",
               url="https://example.com/jobs/title"),
        _entry("R-ELIG", url="https://example.com/jobs/elig"),
        _entry("R-FORM", url="https://example.com/jobs/form"),
        _entry("R-LOWFIT", fit=60),
        _entry("R-READY", status="READY"),
        _entry("R-NOFIT", fit=None),
    ]
    with open(std_p, "w") as f:
        json.dump(entries, f)
    with open(ni_p, "w") as f:
        json.dump([], f)
    open(ev_p, "w").close()
    monkeypatch.setattr(rs, "STD_Q", std_p)
    monkeypatch.setattr(rs, "NI_Q", ni_p)
    monkeypatch.setenv("JOB_PIPELINE_EVENTS_PATH", ev_p)
    old_lock = queue_io._LOCK_PATH
    queue_io.set_lock_path(str(tmp_path / "queue.lock"))
    FakeVR.texts = {
        "https://example.com/jobs/clean": "Great role. Remote friendly.",
        "https://example.com/jobs/title": "Great role. Remote friendly.",
        "https://example.com/jobs/elig":
            "This role requires 5 days per week in the office.",
        "https://example.com/jobs/form": "Great role. Remote friendly.",
    }
    FakeVR.forms = {
        "R-FORM": ["essay: \"Describe yourself\" needs the applicant's own words"],
    }
    yield {"std": std_p, "ni": ni_p, "events": ev_p, "tmp": str(tmp_path)}
    queue_io.set_lock_path(old_lock)
    FakeVR.texts = {}
    FakeVR.forms = {}


def _read(p):
    with open(p) as f:
        return json.load(f)


# --- selection ------------------------------------------------------------

def test_select_filters_status_fit_and_role_ids(env):
    std = _read(env["std"])
    sel = rs.select(std, 75, None)
    got = {e["role_id"] for e in sel}
    assert got == {"R-CLEAN", "R-TITLE", "R-ELIG", "R-FORM"}
    sel2 = rs.select(std, 75, {"R-CLEAN"})
    assert [e["role_id"] for e in sel2] == ["R-CLEAN"]


# --- screen decisions -----------------------------------------------------

def test_title_park_decision(env):
    std = _read(env["std"])
    plan, stats = rs.scan(std, 75, {"R-TITLE"}, pace=0)
    assert plan["R-TITLE"]["decision"] == "title_park"
    assert plan["R-TITLE"]["title_reason"] == "title-family-miss"
    assert stats["title_park"] == 1


def test_eligibility_park_decision(env):
    # The canonical posting-text gate (prescreen.posting_eligibility_screen)
    # flags state-exclusion and office-cap violations only -- degree text is
    # NOT part of its contract, so the fixture uses a 5-days-in-office
    # posting, which the gate quotes as the disqualifying evidence.
    std = _read(env["std"])
    plan, stats = rs.scan(std, 75, {"R-ELIG"}, pace=0)
    assert plan["R-ELIG"]["decision"] == "eligibility_park"
    reasons = plan["R-ELIG"]["reasons"]
    assert any("office requirement exceeds policy cap" in r for r in reasons)
    assert any("5 days/week in-office" in r for r in reasons)
    assert stats["eligibility_park"] == 1


def test_form_park_decision(env):
    std = _read(env["std"])
    plan, stats = rs.scan(std, 75, {"R-FORM"}, pace=0)
    assert plan["R-FORM"]["decision"] == "form_park"
    assert stats["form_park"] == 1


def test_clean_survivor(env):
    std = _read(env["std"])
    plan, stats = rs.scan(std, 75, {"R-CLEAN"}, pace=0)
    assert plan["R-CLEAN"]["decision"] == "clean"
    assert stats["clean"] == 1


def test_excluded_keyword_title_park(env):
    std = _read(env["std"])
    e = _entry("R-ENG", title="Solutions Architect Intern")
    plan, _ = rs.scan([e], 75, None, pace=0)
    assert plan["R-ENG"]["decision"] == "title_park"
    assert plan["R-ENG"]["title_reason"] == "excluded-keyword"


def test_empty_fetches_do_not_abort(env):
    # Empty fetches are extraction misses / dead postings, NOT proof of
    # rate limiting: the batch completes and counts them as a stat.
    FakeVR.texts = {}  # every URL-bearing fetch returns ""
    std = _read(env["std"])
    plan, stats = rs.scan(std, 75, None, pace=0)
    n_url_bearing = len([e for e in std
                         if e.get("status") == "PARKED-PENDING-VERIFICATION"
                         and (e.get("fit_score") or 0) >= 75
                         and (e.get("application_url") or e.get("ats_url"))])
    assert stats["empty_fetch"] == n_url_bearing
    assert stats["selected"] == len(plan)
    assert stats["observed_429s"] == 0


def test_observe_429s_counts_and_hard_stops():
    import urllib.request
    import urllib.error

    def fake_429(*a, **k):
        raise urllib.error.HTTPError("http://x/", 429, "Too Many Requests",
                                     {}, None)

    real = urllib.request.urlopen
    urllib.request.urlopen = fake_429
    try:
        seen = [0]
        with pytest.raises(rs._RateLimitObserved):
            with rs.observe_429s(seen, budget=1):
                urllib.request.urlopen("http://x/")
        assert seen[0] == 1
    finally:
        urllib.request.urlopen = real
    # transport restored after the context manager
    assert urllib.request.urlopen is real


def test_observe_429s_ignores_non_429_errors():
    import urllib.request
    import urllib.error

    def fake_404(*a, **k):
        raise urllib.error.HTTPError("http://x/", 404, "Not Found", {}, None)

    real = urllib.request.urlopen
    urllib.request.urlopen = fake_404
    try:
        seen = [0]
        with rs.observe_429s(seen, budget=1):
            with pytest.raises(urllib.error.HTTPError) as ei:
                urllib.request.urlopen("http://x/")
        assert ei.value.code == 404  # transparent re-raise
        assert seen[0] == 0  # non-429 errors are not counted
    finally:
        urllib.request.urlopen = real


# --- dry-run: zero writes --------------------------------------------------

def test_dry_run_makes_zero_writes(env):
    before_std = open(env["std"]).read()
    before_ni = open(env["ni"]).read()
    before_ev = open(env["events"]).read()
    std = _read(env["std"])
    plan, stats = rs.scan(std, 75, None, pace=0)
    assert stats["selected"] == 4
    assert open(env["std"]).read() == before_std
    assert open(env["ni"]).read() == before_ni
    assert open(env["events"]).read() == before_ev


# --- live apply ------------------------------------------------------------

def test_live_apply_routes_parks_and_never_promotes(env):
    std = _read(env["std"])
    plan, stats = rs.scan(std, 75, None, pace=0)
    clean_before = copy.deepcopy(
        next(e for e in std if e["role_id"] == "R-CLEAN"))
    applied = rs.apply(plan, stats)
    after = {e["role_id"]: e for e in _read(env["std"])}
    ni = {e["role_id"]: e for e in _read(env["ni"])}

    # NEVER promote to READY: no entry anywhere may carry READY from us.
    for e in list(after.values()) + list(ni.values()):
        if e["role_id"].startswith("R-") and e["role_id"] != "R-READY":
            assert e["status"] != "READY", e["role_id"]

    # Title park -> PARKED-TRIAGE-DEFERRED with quoted evidence.
    t = after["R-TITLE"]
    assert t["status"] == "PARKED-TRIAGE-DEFERRED"
    assert "title-family-miss" in t["queue_notes"]
    assert t["status_updated_park_ref"]  # P2 identity stamp present

    # Eligibility park -> PARKED with the disqualifying evidence quoted.
    g = after["R-ELIG"]
    assert g["status"] == "PARKED"
    assert "office requirement exceeds policy cap" in g["status_reason"]
    assert "5 days/week in-office" in g["status_reason"]

    # Form park -> moved to needs_input with conventional blockers.
    assert "R-FORM" not in after
    f = ni["R-FORM"]
    assert f["status"] == "PARKED-NEEDS-INPUT"
    assert f["unresolved"] == [
        "essay: \"Describe yourself\" needs the applicant's own words"]

    # Clean survivor: byte-identical, still PARKED-PENDING-VERIFICATION.
    assert after["R-CLEAN"] == clean_before

    # Untouched pools: low-fit, already-READY, no-fit entries unchanged.
    assert after["R-LOWFIT"]["status"] == "PARKED-PENDING-VERIFICATION"
    assert after["R-READY"]["status"] == "READY"
    assert after["R-NOFIT"]["status"] == "PARKED-PENDING-VERIFICATION"

    assert set(applied["title_park"]) == {"R-TITLE"}
    assert set(applied["eligibility_park"]) == {"R-ELIG"}
    assert set(applied["form_park"]) == {"R-FORM"}
    assert applied["skipped"] == []


def test_backup_created_before_writes(env):
    before = open(env["std"]).read()
    std = _read(env["std"])
    plan, stats = rs.scan(std, 75, {"R-ELIG"}, pace=0)
    applied = rs.apply(plan, stats)
    bdir = applied["backup"]
    assert os.path.isdir(bdir)
    assert open(os.path.join(bdir, "standard-queue.json")).read() == before
    assert os.path.exists(os.path.join(bdir, "needs_input-queue.json"))


def test_backup_lands_beside_queue_file_not_pipe(env, tmp_path):
    # Regression: apply() must derive the backup dir from the queue file's
    # own directory. An earlier version hardcoded PIPE/queue, so test runs
    # leaked backup dirs full of fixture data into the production queue dir.
    std = _read(env["std"])
    plan, stats = rs.scan(std, 75, {"R-ELIG"}, pace=0)
    applied = rs.apply(plan, stats)
    assert applied["backup"].startswith(str(tmp_path))


def test_fail_closed_on_concurrent_status_change(env):
    std = _read(env["std"])
    plan, stats = rs.scan(std, 75, {"R-ELIG"}, pace=0)
    # Concurrent writer moves the entry before our apply takes the lock.
    cur = _read(env["std"])
    for e in cur:
        if e["role_id"] == "R-ELIG":
            e["status"] = "READY"
    with open(env["std"], "w") as f:
        json.dump(cur, f)
    applied = rs.apply(plan, stats)
    assert applied["skipped"] == ["R-ELIG"]
    assert applied["eligibility_park"] == []
    after = {e["role_id"]: e for e in _read(env["std"])}
    assert after["R-ELIG"]["status"] == "READY"  # concurrent state wins


def test_lock_contention_fails_cleanly(env, monkeypatch):
    monkeypatch.setattr(queue_io, "_LOCK_TIMEOUT_S", 2)
    std = _read(env["std"])
    plan, stats = rs.scan(std, 75, {"R-CLEAN"}, pace=0)
    errors = []
    holder_ready = threading.Event()

    def hold():
        with queue_io.queue_lock(owner="test-holder"):
            holder_ready.set()
            threading.Event().wait(6)

    th = threading.Thread(target=hold, daemon=True)
    th.start()
    assert holder_ready.wait(5)
    try:
        with pytest.raises(queue_io.QueueLockTimeout):
            rs.apply(plan, stats)
    except Exception as e:  # noqa: BLE001 - must be the lock error, nothing else
        errors.append("wrong exception: %r" % e)
    assert not errors
    # No writes happened under contention.
    assert {e["role_id"]: e["status"] for e in _read(env["std"])} == \
        {e["role_id"]: e["status"] for e in std}


# --- 2026-09-17 fail-soft + guard invariants -------------------------------

def test_per_entry_failure_is_fail_soft_batch_continues(env, monkeypatch):
    # Regression: the 03:01 PDT --live run died between its 21st
    # gate_blocked event and the atomic write, losing the whole batch.
    # A per-entry failure must now record `errored` and let the rest
    # of the batch apply; the failed entry stays untouched.
    std = _read(env["std"])
    plan, stats = rs.scan(std, 75, None, pace=0)
    before_title = copy.deepcopy(
        next(e for e in std if e["role_id"] == "R-TITLE"))

    real_park = rs._park_event

    def flaky(rid, company, url, gate, screen, note):
        if rid == "R-TITLE":
            raise RuntimeError("telemetry exploded")
        return real_park(rid, company, url, gate, screen, note)

    monkeypatch.setattr(rs, "_park_event", flaky)
    applied = rs.apply(plan, stats)

    assert applied["errored"] == ["R-TITLE"]
    after = {e["role_id"]: e for e in _read(env["std"])}
    # Failed entry: untouched, still PARKED-PENDING-VERIFICATION.
    assert after["R-TITLE"] == before_title
    # Rest of the batch applied normally.
    assert after["R-ELIG"]["status"] == "PARKED"
    ni = {e["role_id"]: e for e in _read(env["ni"])}
    assert ni["R-FORM"]["status"] == "PARKED-NEEDS-INPUT"
    assert applied["eligibility_park"] == ["R-ELIG"]
    assert applied["form_park"] == ["R-FORM"]
    assert applied["title_park"] == []


def test_half_mutated_entry_never_lands_in_queue(env, monkeypatch):
    # The failed entry's in-memory mutation must not leak into the
    # written queue even if the failure happens AFTER mutation started.
    std = _read(env["std"])
    plan, stats = rs.scan(std, 75, None, pace=0)
    before_elig = copy.deepcopy(
        next(e for e in std if e["role_id"] == "R-ELIG"))

    real_stamp = FakeVR._stamp_park

    def boom(cur, ev):
        if cur["role_id"] == "R-ELIG":
            raise RuntimeError("stamp exploded")
        return real_stamp(cur, ev)

    monkeypatch.setattr(FakeVR, "_stamp_park",
                        staticmethod(boom))
    applied = rs.apply(plan, stats)
    assert applied["errored"] == ["R-ELIG"]
    after = {e["role_id"]: e for e in _read(env["std"])}
    assert after["R-ELIG"] == before_elig  # pristine, not half-parked


def test_only_the_selected_set_and_needs_input_untouched(env):
    # Guard invariants: ONLY-the-selected set is touched (role_ids filter),
    # and pre-existing needs_input entries are never modified.
    ni_seed = [_entry("NI-OLD", status="PARKED-NEEDS-INPUT")]
    ni_seed[0]["unresolved"] = ["genuine blocker: the applicant's own words"]
    with open(env["ni"], "w") as f:
        json.dump(ni_seed, f)

    std = _read(env["std"])
    plan, stats = rs.scan(std, 75, {"R-ELIG"}, pace=0)
    assert set(plan) == {"R-ELIG"}  # selection honored the filter
    applied = rs.apply(plan, stats)
    assert applied["eligibility_park"] == ["R-ELIG"]

    after = {e["role_id"]: e for e in _read(env["std"])}
    before = {e["role_id"]: e for e in std}
    # Everything not selected is byte-identical.
    for rid, e in before.items():
        if rid != "R-ELIG":
            assert after[rid] == e, rid
    # Pre-existing needs_input entry: byte-identical, never touched.
    ni_after = _read(env["ni"])
    assert len(ni_after) == 1
    assert ni_after[0] == ni_seed[0]


# --- compound-verdict vocabulary regression --------------------------------

def test_compound_verdict_vocabulary_pinned(env):
    """Pin the compound PARKED-PENDING-VERIFICATION verdict vocabulary
    end to end on isolated fixed fixtures.

    Selection reads ONLY the compound status -- a legacy bare-'PARKED'
    entry is never re-screened. Routing stamps the compound park
    vocabulary: title -> PARKED-TRIAGE-DEFERRED, posting-text -> PARKED,
    form -> PARKED-NEEDS-INPUT. Clean survivors stay
    PARKED-PENDING-VERIFICATION. Nothing is ever promoted to READY.
    """
    assert rs.TARGET_STATUS == "PARKED-PENDING-VERIFICATION"
    assert rs.SOURCE == "rescreen-standard"

    std = [
        _entry("V-CLEAN", url="https://example.com/jobs/clean"),
        _entry("V-TITLE", title="Treasury Analyst",
               url="https://example.com/jobs/title"),
        _entry("V-ELIG", url="https://example.com/jobs/elig"),
        _entry("V-FORM", url="https://example.com/jobs/form"),
        # Legacy bare-PARKED vocabulary: must NOT be selected for re-screen.
        _entry("V-LEGACYPARK", status="PARKED",
               url="https://example.com/jobs/clean"),
        _entry("V-READY", status="READY"),
    ]
    FakeVR.forms["V-FORM"] = [
        "essay: \"Describe yourself\" needs the applicant's own words"]
    with open(env["std"], "w") as f:
        json.dump(std, f)
    clean_before = copy.deepcopy(
        next(e for e in std if e["role_id"] == "V-CLEAN"))
    legacy_before = copy.deepcopy(
        next(e for e in std if e["role_id"] == "V-LEGACYPARK"))

    plan, stats = rs.scan(std, 75, None, pace=0)
    # Compound selection only: bare PARKED and READY are out of scope.
    assert set(plan) == {"V-CLEAN", "V-TITLE", "V-ELIG", "V-FORM"}
    assert plan["V-CLEAN"]["decision"] == "clean"
    assert plan["V-TITLE"]["decision"] == "title_park"
    assert plan["V-ELIG"]["decision"] == "eligibility_park"
    assert plan["V-FORM"]["decision"] == "form_park"

    applied = rs.apply(plan, stats)
    after = {e["role_id"]: e for e in _read(env["std"])}
    ni = {e["role_id"]: e for e in _read(env["ni"])}

    # Exact compound park vocabulary, one status per decision.
    assert after["V-CLEAN"] == clean_before
    assert after["V-CLEAN"]["status"] == "PARKED-PENDING-VERIFICATION"
    assert after["V-TITLE"]["status"] == "PARKED-TRIAGE-DEFERRED"
    assert after["V-ELIG"]["status"] == "PARKED"
    assert "office requirement exceeds policy cap" in \
        after["V-ELIG"]["status_reason"]
    assert "V-FORM" not in after
    assert ni["V-FORM"]["status"] == "PARKED-NEEDS-INPUT"

    # Legacy bare-PARKED entry: never selected, never touched.
    assert after["V-LEGACYPARK"] == legacy_before
    assert "V-LEGACYPARK" not in applied["title_park"]
    assert "V-LEGACYPARK" not in applied["eligibility_park"]
    assert "V-LEGACYPARK" not in applied["form_park"]

    # NEVER promote to READY: the only READY entry is the pre-existing one.
    ready_ids = [rid for rid, e in list(after.items()) + list(ni.items())
                 if e["status"] == "READY"]
    assert ready_ids == ["V-READY"]
