#!/usr/bin/env python3
"""Regression tests for the 2026-09-18 cooldown-released definition fix
(J-20260918-1911-inte-2246), the regime-aware forecast mid
(J-20260918-1940-veri-2258), and the batch queue-move telemetry
(J-20260918-1951-feed-2265).

All fixtures are synthetic in-memory objects -- no files, no network, no
production state. The per-lead cooldown assertions go through the REAL
verify_retry.verify_cooldown_hours policy (never a duplicated copy): the
24h-daily / 72h-board-API-live / 168h-structural-repost-watch windows are
verify_retry's live decision, read per lead.

Fixture rows match the real telemetry schema: top-level ts / source /
event_type, payload in details.
"""

import importlib.util
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

try:
    import pytest
except ImportError:
    # stdlib-only CI: pytest-style tests are collected only under pytest.
    pytest = None

KEEL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Private-host-only engines tree (job-pipeline). Public checkouts skip the
# tests that need it; the host may point at its own tree via KEEL_PRIVATE_ENGINES.
ENGINES = os.path.expanduser(os.environ.get(
    "KEEL_PRIVATE_ENGINES",
    "~/workspace/job-pipeline/engines/application-executor"))


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


S = load_module("export_flow_snapshot_test",
                os.path.join(KEEL_DIR, "export_flow_snapshot.py"))
P = load_module("flow_board_pulse_mid_test",
                os.path.join(KEEL_DIR, "flow_board_pulse.py"))

# NOTE: no import-time sys.path.insert of the private ENGINES dir — that
# shadowed same-named test modules during unittest discovery and broke CI.
# log_event loads lazily by absolute path; tests that need it skip when
# the private path is absent. (ENGINES itself is overridable via
# KEEL_PRIVATE_ENGINES for hosts whose tree lives elsewhere.)
try:
    log_event = load_module(
        "log_event_test", os.path.join(ENGINES, "log_event.py"))
except Exception:
    log_event = None

NOW = datetime(2026, 9, 18, 20, 0, tzinfo=timezone.utc)
OBSERVED_AT = NOW.isoformat()

VERIFY_SR = ("PARKED-PENDING-VERIFICATION: awaiting verify-retry scan; "
             "posting URL confirmed live")


def entry(role_id, status, **kw):
    e = {"role_id": role_id, "status": status, "action_band": "APPLY",
         "fit_score": 80, "unresolved": [], "company": "TestCo",
         "ats": "greenhouse",
         "application_url": "https://boards.greenhouse.io/testco/jobs/"
                            + role_id,
         "status_reason": VERIFY_SR}
    e.update(kw)
    return e


def scan_row(ts, scanned, promoted):
    return {"event_type": "scan_summary", "source": "verify-retry",
            "ts": ts.isoformat(),
            "details": {"scanned": scanned, "promoted_ready": promoted}}


def evidence_rows(now):
    """Minimal telemetry so conv + both p95s are measurable (releases emit)."""
    b1 = now - timedelta(hours=2)
    b2 = now - timedelta(hours=1)
    rows = [scan_row(b1, 10, 3), scan_row(b2, 10, 3)]
    rows.append({"event_type": "lead_verified", "source": "verify-retry",
                 "role_id": "X", "ts": (b1 + timedelta(minutes=10)).isoformat(),
                 "details": {}})
    rows.append({"event_type": "lead_verified", "source": "verify-retry",
                 "role_id": "X", "ts": (b1 + timedelta(minutes=20)).isoformat(),
                 "details": {}})
    rows.append({"event_type": "brief_built", "role_id": "Y",
                 "ts": (now - timedelta(hours=3)).isoformat(), "details": {}})
    rows.append({"event_type": "browser_launched", "role_id": "Y",
                 "ts": (now - timedelta(hours=3)
                        + timedelta(minutes=5)).isoformat(), "details": {}})
    return rows


def verified_row(role_id, ts):
    return {"event_type": "lead_verified", "source": "verify-retry",
            "role_id": role_id, "ts": ts.isoformat(), "details": {}}


def released_role_ids(releases):
    return {rid for rel in releases for rid in rel.get("role_ids", [])}


# --- Part 1a: live pool-status join -------------------------------------------

def test_submitted_lead_excluded_from_cooldown_released():
    # A SUBMITTED lead is outside the verification pool and must never join
    # cooldown-released supply (ARM 72: 140 SUBMITTED leads were in it).
    sub = entry("SUB-1", "SUBMITTED")
    rows = evidence_rows(NOW) + [verified_row("SUB-1", NOW - timedelta(hours=30))]
    leads, holds, releases, skipped = S.build_cooldown_supply(
        [sub], set(), rows, NOW, OBSERVED_AT)
    assert [r["role_id"] for r in leads] == []
    assert [h["role_id"] for h in holds] == []
    assert "SUB-1" not in released_role_ids(releases)


def test_non_pool_statuses_excluded():
    for status in ("PARKED", "PARKED-LOW-FIT", "CLOSED-EXPIRED",
                   "CLOSED-INELIGIBLE", "READY"):
        e = entry("NP-" + status, status)
        rows = evidence_rows(NOW) + [verified_row(e["role_id"],
                                                  NOW - timedelta(hours=30))]
        leads, holds, releases, _ = S.build_cooldown_supply(
            [e], set(), rows, NOW, OBSERVED_AT)
        assert [r["role_id"] for r in leads] == [], status
        assert status not in [h["role_id"] for h in holds]
        assert e["role_id"] not in released_role_ids(releases)


# --- Part 1a: per-lead cooldown policy ----------------------------------------

def test_live_policy_windows_used_per_lead():
    # The policy is verify_retry's live decision, not a duplicated constant.
    daily = entry("D", "PARKED-PENDING-VERIFICATION")
    blocked = entry("T", "PARKED-PENDING-VERIFICATION",
                    status_reason="travel 50% required -- genuine blocker")
    assert S.live_verify_cooldown_hours(daily) == 24.0
    assert S.live_verify_cooldown_hours(blocked) == 168.0


def test_168h_window_lead_excluded_while_lapsed_24h_included():
    lapsed = entry("L24", "PARKED-PENDING-VERIFICATION")
    gated = entry("L168", "PARKED-PENDING-VERIFICATION",
                  status_reason="travel 50% required -- genuine blocker")
    rows = (evidence_rows(NOW)
            + [verified_row("L24", NOW - timedelta(hours=30)),
               verified_row("L168", NOW - timedelta(hours=30))])
    leads, holds, releases, _ = S.build_cooldown_supply(
        [lapsed, gated], set(), rows, NOW, OBSERVED_AT)
    lead_ids = [r["role_id"] for r in leads]
    released = released_role_ids(releases)

    # the 24h-daily lead lapsed 30h ago: in the released cohort
    assert "L24" in lead_ids
    assert "L24" in released
    # the structurally-blocked lead is inside its 168h window: hold
    # recorded, but excluded from supply
    assert "L168" not in lead_ids
    assert "L168" not in released
    hold = next(h for h in holds if h["role_id"] == "L168")
    assert "168" in hold["release_condition"]
    assert (datetime.fromisoformat(hold["review_after"])
            > NOW + timedelta(hours=24))
    rel = next(r for r in releases
               if r["release_id"] == "cooldown-released")
    assert "J-20260918-1911-inte-2246" in rel["definition"]
    assert "168h structural repost-watch" in rel["definition"]


def test_held_status_without_verification_excluded_from_releases():
    # ARM 3 (2026-09-18): PARKED-AWAITING-MATERIALS is in the live verify
    # pool, but a lead with NO lead_verified evidence is unscanned supply,
    # not a cooldown release -- the old behavior anchored the forecast
    # with 212/219 such phantoms (21:02Z board, cohort_join coverage 0.0).
    # It stays on the board lead list with no cooldown hold; it joins no
    # release and carries no forecast.
    held = entry("LH", "PARKED-AWAITING-MATERIALS")
    leads, holds, releases, _ = S.build_cooldown_supply(
        [held], set(), evidence_rows(NOW), NOW, OBSERVED_AT)
    assert "LH" in [r["role_id"] for r in leads]
    assert "LH" not in released_role_ids(releases)
    assert "LH" not in [h["role_id"] for h in holds]


# --- Part 1c: regime-aware forecast mid ----------------------------------------

def decay_rows():
    # 7d=0.23125 -> 24h=0.11667 -> 12h=0.05: monotonic decay, fresh anchor.
    rows = []
    for age_d in range(2, 7):
        rows.append(scan_row(NOW - timedelta(days=age_d), 100, 30))
    for age_h, scanned, promoted in ((20, 100, 25), (8, 100, 5)):
        rows.append(scan_row(NOW - timedelta(hours=age_h), scanned, promoted))
    rows.append(scan_row(NOW - timedelta(minutes=5), 100, 5))
    return rows


def recovery_rows():
    # 7d=0.1875 -> 24h=0.25 -> 12h=0.275: recovering, not decaying.
    rows = []
    for age_d in range(2, 7):
        rows.append(scan_row(NOW - timedelta(days=age_d), 100, 15))
    for age_h, scanned, promoted in ((20, 100, 20), (8, 100, 25)):
        rows.append(scan_row(NOW - timedelta(hours=age_h), scanned, promoted))
    rows.append(scan_row(NOW - timedelta(minutes=5), 100, 30))
    return rows


def test_forecast_mid_capped_in_decay_regime():
    releases = [{"release_id": "cooldown-released", "count": 600,
                 "expected": 90, "estimated_conversion": 0.23}]
    block = P.recalibrate_releases(releases, {}, decay_rows(), NOW)
    assert block["telemetry_stale"] is False
    assert P.decay_regime(block["windows"]) is True
    rel = block["releases"][0]
    rng = rel["expected_range"]
    assert rng["regime"] == "monotonic_decay"
    # mid = min(ewma, 24h) in expected counts; the range is untouched
    expected_24h = int(600 * block["windows"]["24h"]["conversion"])
    assert rng["mid"] == min(rel["expected_ewma"], expected_24h)
    assert rng["mid"] < rel["expected_ewma"]   # the +47%-optimism correction
    assert rng["lo"] <= rng["mid"] <= rng["hi"]
    assert rng["lo"] < rng["hi"]


def test_forecast_mid_raw_ewma_when_not_decaying():
    releases = [{"release_id": "cooldown-released", "count": 600,
                 "expected": 90, "estimated_conversion": 0.11}]
    block = P.recalibrate_releases(releases, {}, recovery_rows(), NOW)
    assert block["telemetry_stale"] is False
    assert P.decay_regime(block["windows"]) is False
    rel = block["releases"][0]
    rng = rel["expected_range"]
    assert rng["regime"] == "stable_or_recovering"
    assert rng["mid"] == rel["expected_ewma"]   # raw EWMA otherwise


# --- Part 2: batch queue-move telemetry ---------------------------------------

def _log_to_temp(*args, **kwargs):
    import unittest
    if log_event is None:
        raise unittest.SkipTest(
            "private log_event.py not present; skipping batch-move telemetry tests")
    with tempfile.NamedTemporaryFile("w", suffix=".jsonl",
                                      delete=False) as f:
        path = f.name
    old_events = log_event.EVENTS
    log_event.EVENTS = path
    try:
        ev = log_event.log_batch_move(*args, **kwargs)
    finally:
        log_event.EVENTS = old_events
    rows = [json.loads(line) for line in open(path) if line.strip()]
    os.unlink(path)
    return ev, rows


def test_batch_move_emits_exactly_one_canonical_row():
    role_ids = ["DEMOTE-%02d" % i for i in range(16)]
    ev, rows = _log_to_temp(role_ids, "READY", "CLOSED-INELIGIBLE",
                            "deep-sweep demote16: template-submit-blocker",
                            source="test-batch-move")
    assert len(rows) == 1                       # one row per batch, not per role
    row = rows[0]
    assert row["event_type"] == "queue_batch_move"  # top-level, canonical
    assert row["source"] == "test-batch-move"
    assert row["role_id"] == "unknown"           # batch-level, no single lead
    d = row["details"]
    assert d["count"] == 16
    assert d["role_ids"] == role_ids
    assert d["from_status"] == ["READY"]
    assert d["to_status"] == "CLOSED-INELIGIBLE"
    assert "demote16" in d["reason"]
    assert ev["event_type"] == "queue_batch_move"


def test_batch_move_empty_is_no_event():
    ev, rows = _log_to_temp([], "READY", "CLOSED-INELIGIBLE",
                            "nothing moved", source="test-batch-move")
    assert ev is None
    assert rows == []


def test_batch_move_mixed_from_statuses():
    ev, rows = _log_to_temp(["A", "B"],
                            ["PARKED-PENDING-VERIFICATION",
                             "PARKED-AWAITING-MATERIALS"],
                            "rejected-queue",
                            "verify-retry: posting confirmed dead",
                            source="test-batch-move")
    assert len(rows) == 1
    d = rows[0]["details"]
    assert d["count"] == 2
    assert d["from_status"] == ["PARKED-AWAITING-MATERIALS",
                               "PARKED-PENDING-VERIFICATION"]
    assert d["to_status"] == "rejected-queue"
