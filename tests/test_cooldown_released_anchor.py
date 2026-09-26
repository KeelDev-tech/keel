#!/usr/bin/env python3
"""ARM 3 regression test (2026-09-18): kill the flow-board release-anchor
forecast artifact.

Measured production evidence (flow-board snapshots 2026-09-18 20:46Z /
21:02Z): the `cooldown-released` forecast cohort had 219 members with an
expected ~30 conversions (EWMA 38), while conv-recal-v2's export-time
cohort_join measured coverage 0.0 -- observed_any 0 of 219 over 24h.
Direct telemetry audit: 212 of the 219 members had ZERO lead_verified
events in the ENTIRE telemetry log. They joined the "released" cohort
through the `lv is None` branch ("never verified in 30d: no cooldown
applies, eligible now") -- a re-stamped anchor, not a genuinely expiring
cooldown cohort. Every pulse since chased the phantom forecast; the
guardian's actionable=0 was the correct reading.

Correct behavior pinned here: a forecast release contains ONLY leads
with a genuine last-verification timestamp whose per-lead cooldown has
expired (cooldown-released) or expires within 24h
(cooldown-releases-next-24h). Never-verified leads are unscanned supply:
excluded from the release forecast (named in the release evidence_ref),
still present as board leads with no cooldown hold.

Fixtures are synthetic in-memory objects -- no files, no network, no
production state. Per-lead cooldowns go through the REAL
verify_retry.verify_cooldown_hours policy.
"""

import importlib.util
import os
import sys
from datetime import datetime, timedelta, timezone

KEEL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


S = load_module("export_flow_snapshot_anchor_test",
                os.path.join(KEEL_DIR, "export_flow_snapshot.py"))

# NOTE: no sys.path.insert of the private job-pipeline engines dir — it is a
# private-host-only tree. This file loads everything it needs via load_module()
# by absolute path, so nothing here can bind a private checkout location.

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


def production_like_fixture():
    """Mirrors the measured 212/219 production cohort composition:
    2x PARKED-PENDING-VERIFICATION + 1x PARKED-AWAITING-MATERIALS, all
    APPLY / fit>=75 / no unresolved blockers, ZERO lead_verified rows --
    plus one genuinely-expired lead (verified 30h ago, 24h daily window)
    and one in-cooldown control (verified 2h ago)."""
    phantom = [
        entry("PHANTOM-1", "PARKED-PENDING-VERIFICATION"),
        entry("PHANTOM-2", "PARKED-PENDING-VERIFICATION"),
        entry("PHANTOM-3", "PARKED-AWAITING-MATERIALS"),
    ]
    genuine = entry("GENUINE-EXPIRED", "PARKED-PENDING-VERIFICATION")
    control = entry("CONTROL-IN-COOLDOWN", "PARKED-PENDING-VERIFICATION")
    rows = (evidence_rows(NOW)
            + [verified_row("GENUINE-EXPIRED", NOW - timedelta(hours=30)),
               verified_row("CONTROL-IN-COOLDOWN",
                            NOW - timedelta(hours=2))])
    return phantom + [genuine, control], rows


def test_anchor_cohort_excluded_from_cooldown_released():
    # The 212-style phantom cohort (no verification evidence at all) must
    # not join the cooldown-released forecast; only the genuinely expired
    # lead may. The anchor previously measured 219 / expected 30 on this
    # exact composition shape.
    standard, rows = production_like_fixture()
    leads, holds, releases, _ = S.build_cooldown_supply(
        standard, set(), rows, NOW, OBSERVED_AT)
    by_rel = {r["release_id"]: set(r["role_ids"]) for r in releases}
    # per-release: only the genuinely expired lead forecasts
    assert by_rel["cooldown-released"] == {"GENUINE-EXPIRED"}, by_rel
    assert by_rel["cooldown-releases-next-24h"] == {"CONTROL-IN-COOLDOWN"}
    for rid in ("PHANTOM-1", "PHANTOM-2", "PHANTOM-3"):
        assert all(rid not in ids for ids in by_rel.values())
    rel = next(r for r in releases if r["release_id"] == "cooldown-released")
    assert rel["count"] == 1
    assert rel["role_ids"] == ["GENUINE-EXPIRED"]


def test_anchor_cohort_excluded_from_next_24h_release():
    # Phantom members have no cooldown clock at all, so they cannot join
    # the next-24h release either.
    standard, rows = production_like_fixture()
    _, _, releases, _ = S.build_cooldown_supply(
        standard, set(), rows, NOW, OBSERVED_AT)
    next24 = next((r for r in releases
                   if r["release_id"] == "cooldown-releases-next-24h"), None)
    ids = set(next24["role_ids"]) if next24 else set()
    for rid in ("PHANTOM-1", "PHANTOM-2", "PHANTOM-3"):
        assert rid not in ids


def test_unscanned_supply_still_listed_as_board_leads():
    # Exclusion from the forecast is not deletion: the phantom leads are
    # live verify-pool supply and stay on the board's lead list (no
    # cooldown hold -- nothing was ever verified).
    standard, rows = production_like_fixture()
    leads, holds, releases, _ = S.build_cooldown_supply(
        standard, set(), rows, NOW, OBSERVED_AT)
    lead_ids = {r["role_id"] for r in leads}
    for rid in ("PHANTOM-1", "PHANTOM-2", "PHANTOM-3"):
        assert rid in lead_ids
    assert "PHANTOM-1" not in {h["role_id"] for h in holds}
    # the in-cooldown control (verified 2h ago, 24h window) keeps its hold
    # and is upcoming supply in the next-24h release -- never in the
    # already-released forecast
    assert "CONTROL-IN-COOLDOWN" in lead_ids
    assert "CONTROL-IN-COOLDOWN" in {h["role_id"] for h in holds}
    by_rel = {r["release_id"]: set(r["role_ids"]) for r in releases}
    assert "CONTROL-IN-COOLDOWN" not in by_rel.get("cooldown-released", set())


def test_exclusion_is_honestly_labeled():
    # The forecast must say what it did: the release evidence_ref names
    # the excluded unscanned count, and the definition records the anchor
    # fix (212/219, 2026-09-18).
    standard, rows = production_like_fixture()
    _, _, releases, _ = S.build_cooldown_supply(
        standard, set(), rows, NOW, OBSERVED_AT)
    rel = next(r for r in releases if r["release_id"] == "cooldown-released")
    assert "3" in rel["evidence_ref"] and "unscanned" in rel["evidence_ref"]
    assert "212/219" in rel["definition"]
