#!/usr/bin/env python3
"""Regression tests for export_flow_snapshot v1.2: measured discovery,
evidenced cooldown releases, synthetic exclusion, hold-family join.

2026-09-18 (J-20260918-1911-inte-2246): the cooldown-released definition is
now the live verification pool only -- the fixture entry() default status is
PARKED-PENDING-VERIFICATION because bare PARKED is no longer supply.
"""

import importlib.util

import importlib.util
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

KEEL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ADAPTER_PATH = os.path.join(KEEL_DIR, "export_flow_snapshot.py")


def load_adapter():
    spec = importlib.util.spec_from_file_location("export_flow_snapshot", ADAPTER_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["export_flow_snapshot"] = mod
    spec.loader.exec_module(mod)
    return mod


A = load_adapter()
NOW = datetime(2026, 9, 18, 10, 0, 0, tzinfo=timezone.utc)


def ev(event_type, ts, role_id="R-1", **kw):
    row = {"ts": ts.isoformat(), "event_type": event_type,
           "role_id": role_id, "source": kw.get("source"),
           "details": kw.get("details", {})}
    row.update({k: v for k, v in kw.items()
                if k not in ("details", "source")})
    return row


def entry(role_id, status="PARKED-PENDING-VERIFICATION", action_band="APPLY",
          fit=80, unresolved=None, company="Acme", title="Ops Manager",
          location="Remote", url="https://example.com/j/1"):
    return {"role_id": role_id, "company": company, "title": title,
            "location": location, "posting_url": url, "status": status,
            "action_band": action_band, "fit_score": fit,
            "unresolved": unresolved or []}


# --- LinkedIn measurement ----------------------------------------------------

def linkedin_rows(start, end, new=80, cards=80, summary=True):
    rows = []
    step = (end - start) / 4
    for i in range(4):
        rows.append(ev("scan_summary", start + i * step, "R-LI",
                       source="linkedin-discovery",
                       details={"query": f"q{i}", "cards": cards // 4,
                                "new": new // 4}))
    if summary:
        rows.append(ev("scan_summary", end, "R-LI",
                       source="linkedin-discovery",
                       details={"cards": cards, "new": new}))
    return rows


def test_linkedin_measured_minutes_with_both_anchors():
    start = NOW - timedelta(minutes=5)
    end = NOW - timedelta(minutes=2)
    src = A.build_linkedin_source(linkedin_rows(start, end),
                                  NOW - timedelta(days=7))
    assert src["measurement_complete"] is True
    assert src["verification_minutes"] == pytest.approx(3.0)
    assert src["measurement_started_at"] == start.isoformat()
    assert src["observed_at"] == end.isoformat()
    # wall-clock discovery is not qualification: no qualified-live invention
    assert src["qualified_unique_live"] == 0
    assert src["checks"] == 80


def test_linkedin_measured_without_summary_row():
    # per-query rows still measure the sweep; the summary only carries totals
    start = NOW - timedelta(minutes=5)
    end = NOW - timedelta(minutes=2)
    src = A.build_linkedin_source(linkedin_rows(start, end, summary=False),
                                  NOW - timedelta(days=7))
    assert src["measurement_complete"] is True
    assert src["checks"] == 80


def test_linkedin_unmeasured_when_no_sweep_in_window():
    start = NOW - timedelta(days=10)
    end = start + timedelta(minutes=3)
    src = A.build_linkedin_source(linkedin_rows(start, end),
                                  NOW - timedelta(days=7))
    assert src["measurement_complete"] is False
    assert src["verification_minutes"] == 0.0
    assert src["checks"] == 0
    assert "verification minutes not measured" in src["evidence_ref"]


def test_linkedin_empty_sweep_measures_zero_checks():
    start = NOW - timedelta(minutes=5)
    end = NOW - timedelta(minutes=2)
    src = A.build_linkedin_source(linkedin_rows(start, end, new=0, cards=0),
                                  NOW - timedelta(days=7))
    assert src["measurement_complete"] is True
    assert src["checks"] == 0
    assert src["qualified_unique_live"] == 0


def _load_allocator():
    spec = importlib.util.spec_from_file_location(
        "keel_flow.discovery",
        os.path.join(KEEL_DIR, "keel_flow", "discovery.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["keel_flow.discovery"] = mod
    spec.loader.exec_module(mod)
    return mod.allocate


def test_linkedin_retired_excluded_even_with_fresh_measurement():
    # RETIRED-POLICY 2026-09-22 (blackboard J-20260922-0840-sour-4640):
    # G-5 suspension + 2026-09-19 LinkedIn hard boundary. The allocator must
    # exclude the source as SOURCE_NOT_PERMITTED even with a fresh,
    # measured sweep — this pins the flip risk closed (a fresh cron run must
    # not silently re-admit the source).
    allocate = _load_allocator()
    start = NOW - timedelta(minutes=5)
    end = NOW - timedelta(minutes=2)
    src = A.build_linkedin_source(linkedin_rows(start, end),
                                  NOW - timedelta(days=7))
    assert src["permitted"] is False
    assert src["measurement_complete"] is True  # fresh measurement present
    assert "RETIRED-POLICY" in src["evidence_ref"]
    result = allocate([src], budget_minutes=60, now=NOW)
    assert result["state"] == "PROPOSAL_ONLY"
    assert result["excluded"] == [
        {"source_id": "linkedin-discovery-sweep",
         "reason": "SOURCE_NOT_PERMITTED"}]
    assert result["allocations"] == []


def test_linkedin_retired_excluded_when_unmeasured():
    # The unmeasured branch carries the same retired flag + exclusion.
    allocate = _load_allocator()
    start = NOW - timedelta(days=10)
    end = start + timedelta(minutes=3)
    src = A.build_linkedin_source(linkedin_rows(start, end),
                                  NOW - timedelta(days=7))
    assert src["permitted"] is False
    assert "RETIRED-POLICY" in src["evidence_ref"]
    result = allocate([src], budget_minutes=60, now=NOW)
    assert result["excluded"] == [
        {"source_id": "linkedin-discovery-sweep",
         "reason": "SOURCE_NOT_PERMITTED"}]


# --- verify conversion -------------------------------------------------------

def verify_rows(n_batches=10, scanned=10, promoted=2, when=None):
    when = when or (NOW - timedelta(days=1))
    return [ev("scan_summary", when + timedelta(seconds=i), f"R-V{i}",
               source="verify-retry",
               details={"synthetic_test": False, "batches": n_batches,
                        "scanned": scanned, "promoted_ready": promoted,
                        "marked_dead": 0})
            for i in range(3)]


def test_verify_conversion_measured_ratio():
    conv = A.verify_conversion(verify_rows(), NOW - timedelta(days=7))
    assert conv is not None
    assert conv["conversion"] == pytest.approx(6 / 30)
    assert conv["batches"] == 3  # one scan_summary row = one batch
    assert conv["scanned"] == 30 and conv["promoted"] == 6


def test_verify_conversion_excludes_synthetic_rows():
    rows = verify_rows() + [ev("scan_summary", NOW - timedelta(days=1),
                               "R-SYN", source="verify-retry",
                               details={"synthetic_test": True, "batches": 100,
                                        "scanned": 1000, "promoted_ready": 1000})]
    conv = A.verify_conversion(rows, NOW - timedelta(days=7))
    assert conv["scanned"] == 30  # the synthetic 1000-row batch excluded


def test_verify_conversion_none_when_no_evidence():
    assert A.verify_conversion([], NOW - timedelta(days=7)) is None


# --- cooldown supply grouping -------------------------------------------------

def full_evidence(now):
    """Verify batches + timing pairs so releases can be evidenced."""
    rows = []
    base = now - timedelta(days=1)
    for i in range(4):
        t = base + timedelta(minutes=10 * i)
        rows.append(ev("scan_summary", t, f"R-V{i}", source="verify-retry",
                       details={"scanned": 10, "promoted_ready": 2}))
    for j, dt in enumerate([timedelta(minutes=11), timedelta(minutes=12),
                            timedelta(minutes=21), timedelta(minutes=22)]):
        rows.append(ev("lead_verified", base + dt, f"R-B{j}",
                       source="verify-retry"))
    rows.append(ev("brief_built", base, "R-P1"))
    rows.append(ev("browser_launched", base + timedelta(seconds=120), "R-P1"))
    return rows


def cooldown_fixture(now, monkeypatch):
    monkeypatch.setattr(A, "live_verify_cooldown_hours", lambda entry: 24.0)
    std = [
        entry("R-RELEASED"),    # verified 30h ago -> released
        entry("R-SOON"),        # verified 20h ago -> releases within 24h
        entry("R-DEEP"),        # verified 2h ago -> still held, no release
        entry("R-UNVERIFIED"),  # never verified -> unscanned supply:
        # lead listed, no hold, NO release (ARM 3 anchor fix)
        entry("R-NOTAPPLY", action_band="PARKED"),
        entry("R-LOWFIT", fit=60),
        entry("R-BLOCKED", unresolved=["essay"]),
        entry("R-READY", status="READY"),
    ]
    rows = [
        ev("lead_verified", now - timedelta(hours=30), "R-RELEASED"),
        ev("lead_verified", now - timedelta(hours=20), "R-SOON"),
        ev("lead_verified", now - timedelta(hours=2), "R-DEEP"),
    ]
    rows.extend(full_evidence(now))
    return std, rows


def test_cooldown_groups(monkeypatch):
    std, rows = cooldown_fixture(NOW, monkeypatch)
    leads, holds, releases, skipped = A.build_cooldown_supply(
        std, set(), rows, NOW, NOW)
    got = {r["role_id"] for r in leads}
    assert got == {"R-RELEASED", "R-SOON", "R-DEEP", "R-UNVERIFIED"}
    by_rel = {r["release_id"]: set(r["role_ids"]) for r in releases}
    assert by_rel["cooldown-released"] == {"R-RELEASED"}
    # R-UNVERIFIED has no verification evidence: unscanned supply, not a
    # cooldown release (ARM 3 anchor fix); it stays on the lead list above.
    # R-DEEP was verified 2h ago: held now, but its 24h cooldown expires in
    # 22h -- inside the next-24h window, so it belongs to both the hold set
    # and the upcoming release group.
    assert by_rel["cooldown-releases-next-24h"] == {"R-SOON", "R-DEEP"}
    # release groups carry the measured conversion, not an invented one
    for r in releases:
        assert r["estimated_conversion"] == pytest.approx(0.2)
    # deep-hold role gets a hold but no release membership
    deep_holds = [h for h in holds if h["role_id"] == "R-DEEP"]
    assert len(deep_holds) == 1
    h = deep_holds[0]
    assert h["family"] == "cooldown"
    assert h["hold_id"] == "cooldown:R-DEEP"
    opened = datetime.fromisoformat(h["opened_at"])
    review = datetime.fromisoformat(h["review_after"])
    assert (review - opened) == timedelta(hours=24)
    # released roles with verification evidence carry a cooldown hold too
    assert {h["role_id"] for h in holds} == {"R-RELEASED", "R-SOON", "R-DEEP"}
    assert skipped == 0


def test_cooldown_release_evidence_ref_names_methodology(monkeypatch):
    std, rows = cooldown_fixture(NOW, monkeypatch)
    _, _, releases, _ = A.build_cooldown_supply(std, set(), rows, NOW, NOW)
    assert len(releases) == 2
    ref = releases[0]["conversion_evidence_ref"]
    assert "lower-bound proxy" in ref
    assert "includes packet-buffer queueing" in ref
    assert "synthetic excluded" in ref


def test_cooldown_no_releases_without_conversion_evidence(monkeypatch):
    monkeypatch.setattr(A, "live_verify_cooldown_hours", lambda entry: 24.0)
    std = [entry("R-A"), entry("R-B")]
    leads, holds, releases, skipped = A.build_cooldown_supply(
        std, set(), [], NOW, NOW)  # no telemetry at all
    assert releases == []
    # with no verification history anywhere, no cooldown applies
    assert {r["role_id"] for r in leads} == {"R-A", "R-B"}
    assert holds == []


def test_cooldown_skips_seen_and_unbuildable(monkeypatch):
    monkeypatch.setattr(A, "live_verify_cooldown_hours", lambda entry: 24.0)
    std = [entry("R-SEEN"), entry("R-NOID", company="")]
    leads, holds, releases, skipped = A.build_cooldown_supply(
        std, {"R-SEEN"}, [], NOW, NOW)
    assert leads == [] and holds == [] and skipped == 1


# --- synthetic attempt exclusion ----------------------------------------------

def flow_attempt_row(role_id, attempt_id, event_id):
    return {"ts": NOW.isoformat(), "event_type": "flow_attempt",
            "role_id": role_id, "source": "keel-flow", "event_id": event_id,
            "details": {"event_id": event_id, "attempt_id": attempt_id,
                        "application_id": "a" * 64,
                        "content_hash": "c" * 64,
                        "observed_at": NOW.isoformat(),
                        "execution_authorized": False}}


def test_synthetic_flow_attempt_excluded():
    rows = [flow_attempt_row("R-TEST", "attempt-1", "flow-test-001"),
            flow_attempt_row("R-REAL", "attempt-2", "flow-real-001")]
    events = A.attempt_events_from_rows(rows)
    assert [e["attempt_id"] for e in events] == ["attempt-2"]


# --- timing helpers -----------------------------------------------------------

def test_batch_verify_p95_none_when_unmeasurable():
    assert A.batch_verify_p95([], NOW - timedelta(days=7)) is None


def test_batch_verify_p95_measures_per_batch_means():
    base = NOW - timedelta(days=1)
    rows = []
    for i in range(4):
        rows.append(ev("scan_summary", base + timedelta(minutes=10 * i),
                       f"R-V{i}", source="verify-retry",
                       details={"scanned": 10}))
    for dt in [timedelta(minutes=11), timedelta(minutes=12),
               timedelta(minutes=21), timedelta(minutes=22)]:
        rows.append(ev("lead_verified", base + dt, "R-B", source="verify-retry"))
    p = A.batch_verify_p95(rows, NOW - timedelta(days=7))
    assert p["p95"] == pytest.approx(6.0)  # 60s span / 10 scanned
    assert p["batches"] == 2


def test_prepare_p95_pairs_only():
    rows = [ev("brief_built", NOW - timedelta(hours=2), "R-1"),
            ev("browser_launched", NOW - timedelta(hours=1), "R-1"),
            ev("brief_built", NOW - timedelta(hours=2), "R-2")]  # no launch
    p = A.prepare_p95(rows, NOW - timedelta(days=7))
    assert p["pairs"] == 1 and p["p95"] == pytest.approx(3600.0)
    assert A.prepare_p95([], NOW - timedelta(days=7)) is None


# --- hold ownership ------------------------------------------------------------

def _tray_card(key, question, leads, first_seen=None, times_seen=1):
    return {"key": key, "question": question, "leads": leads,
            "first_seen": (first_seen or NOW).isoformat(),
            "times_seen": times_seen}


def test_build_holds_owner_bound_to_card_employer():
    tray = {"cards": [_tray_card(
        "whatsapp-optin", "Please opt in to WhatsApp updates",
        [{"role_id": "R-1", "employer": "Acme"},
         {"role_id": "R-2", "employer": ""}])]}
    holds = A.build_holds(tray, {}, NOW, "input-tray@rev1")
    by_role = {h["role_id"]: h for h in holds}
    assert by_role["R-1"]["owner"] == "employer:Acme"
    # No issuer evidenced -> explicit None keeps ASSIGN_OWNER honest.
    assert by_role["R-2"]["owner"] is None
    for h in holds:
        assert h["not_before_utc"] is None  # no deadline evidenced on cards
        assert h["review_after"] is not None
        assert h["evidence_revision"] == "input-tray@rev1"


def test_build_holds_owner_never_personal_name():
    tray = {"cards": [_tray_card(
        "essay", "Write an essay in your own words",
        [{"role_id": "R-1", "employer": "Acme Corp"}])]}
    holds = A.build_holds(tray, {}, NOW, "input-tray@rev1")
    assert holds[0]["owner"] == "employer:Acme Corp"
    assert "Trent" not in holds[0]["owner"]


# --- supplied-capacity measurement ------------------------------------------------

def _ledger_row(ts, role_id, conf_text="confirmed", conf_url=None, status="SUBMITTED"):
    row = {"status": status, "date_submitted": ts, "role_id": role_id,
           "confirmation_text": conf_text}
    if conf_url:
        row["confirmation_url"] = conf_url
    return row


def _dispatch(ts):
    # Flat event shape: attempt_events_from_rows() unwraps row["details"]
    # via keel_flow.journal.events_from_log(), so state/observed_at sit at
    # the event's top level. The nested shape previously used here masked
    # the measure_supplied_capacity flat-shape bug (2026-09-22, pulse-803c).
    return {"observed_at": ts, "state": "DISPATCHED"}


def test_capacity_events_are_flat_details_dicts():
    """Contract pin: measure_supplied_capacity must read the flat shape
    that attempt_events_from_rows() produces. A nested-shape fixture would
    let a nested accessor pass while production starves."""
    t0 = NOW - timedelta(hours=2)
    rows = [_ledger_row((t0 + timedelta(minutes=30)).isoformat(), "R-1",
                        conf_url="https://x/1")]
    flat = [_dispatch(t0.isoformat()),
            _dispatch((t0 + timedelta(hours=2)).isoformat())]
    cap, ev = A.measure_supplied_capacity(rows, flat, NOW - timedelta(days=7),
                                          NOW, "rev1")
    assert cap is not None
    assert ev["dispatched_attempts_in_window"] == 2
    # The nested shape is NOT a real event: it must not measure capacity.
    nested = [{"details": {"observed_at": t0.isoformat(),
                           "state": "DISPATCHED"}}]
    cap2, ev2 = A.measure_supplied_capacity(rows, nested,
                                            NOW - timedelta(days=7),
                                            NOW, "rev1")
    assert cap2 is None
    assert "starved" in ev2["reason"]


def test_capacity_starved_interval_never_zero():
    rows = [_ledger_row((NOW - timedelta(days=1)).isoformat(), "R-1")]
    cap, ev = A.measure_supplied_capacity(rows, [], NOW - timedelta(days=7),
                                          NOW, "rev1")
    assert cap is None
    assert ev["measured_while_supplied"] is False
    assert "starved" in ev["reason"]
    assert ev["dispatched_attempts_in_window"] == 0
    assert ev["unique_completions_in_window"] == 1


def test_capacity_measured_rate_from_supplied_interval():
    t0 = NOW - timedelta(hours=2)
    rows = [_ledger_row((t0 + timedelta(minutes=30)).isoformat(), "R-1",
                        conf_url="https://x/1"),
            _ledger_row((t0 + timedelta(minutes=90)).isoformat(), "R-2",
                        conf_url="https://x/2")]
    events = [_dispatch(t0.isoformat()),
              _dispatch((t0 + timedelta(hours=2)).isoformat())]
    cap, ev = A.measure_supplied_capacity(rows, events, NOW - timedelta(days=7),
                                          NOW, "rev1")
    assert ev["measured_while_supplied"] is True
    assert cap["measured_while_supplied"] is True
    assert cap["value"] == pytest.approx(1.0)  # 2 completions / 2h
    assert cap["unit"] == "applications/hour"
    assert cap["source_revision"] == "rev1"


def test_capacity_excludes_duplicate_receipts_and_non_canonical():
    t0 = NOW - timedelta(hours=2)
    rows = [_ledger_row((t0 + timedelta(minutes=30)).isoformat(), "R-1",
                        conf_text="same receipt", conf_url="https://x/1"),
            _ledger_row((t0 + timedelta(minutes=40)).isoformat(), "R-1",
                        conf_text="same receipt", conf_url="https://x/1"),
            _ledger_row((t0 + timedelta(minutes=50)).isoformat(), "R-2",
                        conf_text=""),  # no confirmation -> not canonical
            _ledger_row((t0 + timedelta(minutes=60)).isoformat(), "R-3",
                        conf_url="https://x/3")]
    events = [_dispatch(t0.isoformat()),
              _dispatch((t0 + timedelta(hours=2)).isoformat())]
    cap, ev = A.measure_supplied_capacity(rows, events, NOW - timedelta(days=7),
                                          NOW, "rev1")
    assert ev["duplicate_receipts_excluded"] == 1
    assert ev["non_canonical_rows_excluded"] == 1
    assert ev["unique_completions_in_supplied_interval"] == 2
    assert cap["value"] == pytest.approx(1.0)  # 2 / 2h


def test_capacity_zero_completions_in_interval_is_unmeasured():
    t0 = NOW - timedelta(hours=2)
    # Completion lands before the supplied interval (date-only granularity).
    rows = [_ledger_row((NOW - timedelta(days=1)).date().isoformat(), "R-1")]
    events = [_dispatch(t0.isoformat()),
              _dispatch((t0 + timedelta(hours=2)).isoformat())]
    cap, ev = A.measure_supplied_capacity(rows, events, NOW - timedelta(days=7),
                                          NOW, "rev1")
    assert cap is None
    assert "zero measured completions" in ev["reason"]
    assert ev["unique_completions_in_window"] == 1


def test_capacity_short_span_unmeasurable():
    t0 = NOW - timedelta(hours=1)
    rows = [_ledger_row(t0.isoformat(), "R-1")]
    events = [_dispatch(t0.isoformat())]  # single instant, no span
    cap, ev = A.measure_supplied_capacity(rows, events, NOW - timedelta(days=7),
                                          NOW, "rev1")
    assert cap is None
    assert "unmeasurable" in ev["reason"]
