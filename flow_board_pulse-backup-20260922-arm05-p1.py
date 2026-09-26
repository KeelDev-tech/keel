#!/usr/bin/env python3
"""Flow-board pulse observer: export a canonical snapshot, run the read-only
board, record the compact signal summary. Invoked by the Keel octopus pulse
(step 1a); also runnable standalone.

Read-only: never touches queues, telemetry, tray, ledger, or schedules.
Snapshots/reports land under hidden_files/flow-board/; files older than
RETENTION_H are pruned. Prints one compact JSON summary line to stdout and
writes flow-board-latest.json for pulse consumers.

Exit 0 even when the board reports UNVERIFIED -- that is a signal, not an
error. Exit 2 only when the observer itself fails.

The executable_ready gauge post-pass (FIX-4, 2026-09-22) recomputes the
board's executable estimate read-only: a READY lead that passes all real
gates counts as executable even when a staged INTENT exists for it
(INTENT-held is scheduling state, not a gate failure); the INTENT-held
count is reported separately as intent_held. No queue/ledger/telemetry
writes; duplicate, D1, fit, security, evidence and attestation gates are
never weakened; ambiguous leads fail closed.
"""

import hashlib
from collections import Counter
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta

KEEL_DIR = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(KEEL_DIR, "hidden_files", "flow-board")
LATEST_PATH = os.path.join(OUT_DIR, "flow-board-latest.json")
RETENTION_H = 24
PIPELINE_DIR = os.path.expanduser("~/workspace/job-pipeline")
TELEMETRY_PATH = os.path.join(PIPELINE_DIR, "telemetry", "events.jsonl")


# --- conversion recalibration (ARM 72, conv-recal-v2) ------------------------
# The exporter's estimated_conversion is a flat 7d average that lags regime
# shifts (see hidden_files/arm-reports/arm70-cooldown-release-conversion.md).
# This read-only post-pass recomputes the conversion input as windowed ratios
# (7d/24h/12h) plus a 24h-half-life EWMA over per-wave ratios, emits each
# release's forecast as a range (not a point), and joins each release cohort
# against per-role verify telemetry for partial cohort measurability.
# v2 (2026-09-18, J-20260918-1940-veri-2258): the forecast mid is
# regime-aware -- in a monotonic 7d -> 24h -> 12h decay regime the EWMA
# lags the collapse (+47% optimistic measured), so the mid is capped at
# min(ewma, 24h); the range is unchanged. Fresh non-decay regimes keep the
# raw EWMA mid; stale input keeps the 7d-flat mid.
# v3 (2026-09-19, ARM 2 keel-octopus-pulse): fastpath-reuse exclusion --
# scan_summary rows from verify-retry carry fastpath_live_n (LP-E/LP-F,
# 2026-09-18), the count of zero-HTTP liveness-evidence reuses in the
# batch. ARM 516 (arm03-yield-forensics) showed these are ~220/day
# re-verifications of the same 5 held leads (Crusoe x4 + Cresta x1,
# promotable=0 by materials-abstain/D1) that ride the denominator with 0
# promotion possible. They are excluded from the attempted pool; the
# numerator is reduced by at most fastpath_live_n (a provable lower
# bound on fresh-supply promotions), so the ratio stays in [0,1] by
# construction. Rows predating LP-E/LP-F lack the field and degrade
# exactly to the v2 formula.
# v4 (2026-09-20, ARM 3 keel-octopus-pulse, J-20260920-2244-veri-3960):
# the v3 numerator subtraction erased REAL promotions -- the 12:05/12:14
# UTC waves (scanned=17/20, unscored=83, fastpath=10, promoted=7/10)
# clamped to (0,0): 17 real promotions vanished with their waves.
# Denominator truth, v4:
#   (a) the unscored_skipped subtraction is schema-conditional. v2 assumed
#       details.scanned is len(scan_order) which contains the unscored
#       rows, but the live producer emits unscored_skipped as a pool-level
#       skip count that can EXCEED the batch's scanned count (48 of the
#       last 60 waves: scanned=10, unscored=83-90 -- the fields are
#       disjoint counts). Subtracting a disjoint count from the batch is a
#       schema mismatch that zeroes real waves, so the subtraction applies
#       only when schema-consistent (unscored <= scanned); otherwise the
#       unscored pool is already disjoint from the batch by construction.
#   (b) the fastpath reuse cohort (promotable=0 by materials-abstain/D1/
#       low_fit, verified by zero promotions across the observed regime)
#       still leaves the denominator -- phantom supply contributes
#       nothing -- but NEVER reduces the numerator: every observed
#       promotion came from the attempted pool, so the numerator is the
#       observed promoted_ready capped at the denominator. This keeps
#       0 <= promoted <= scanned for every batch (and hence every
#       windowed/EWMA ratio in [0,1]) even if a reuse-cohort lead ever
#       genuinely promotes through the fast path; in the observed regime
#       (reuse cohort promotes at 0) it equals the naive denominator
#       exclusion exactly. Rows predating LP-E/LP-F lack the field and
#       degrade exactly to the v2 formula.
# The producer's accounting invariant (scan_order completeness) is
# untouched -- the correction lives at the consumer, where the metric is
# defined.
# It never fails the run: any error degrades to {"ok": false, ...}.
RECALIBRATION_VERSION = "conv-recal-v4"
WINDOW_DEFS_H = {"7d": 7 * 24, "24h": 24, "12h": 12}  # hours
EWMA_HALFLIFE_H = 24.0
MIN_WAVES = 5            # fewer waves in a window => low_confidence flag
STALE_INPUT_MIN = 120    # no scan batch within this many minutes of the
                         # anchor => the windowed inputs are stale. ~2x the
                         # hourly verify cadence; beyond it the EWMA is
                         # anchored on aging waves and the 7d flat is the
                         # widest honest input.
PER_ROLE_VERIFY_EVENTS = ("lead_verified", "lead_dead", "gate_blocked")
# 2026-09-18 (J-20260918-1922-veri-2248): silent-verdict telemetry —
# verify_retry.py emits one verify_attempt per scanned lead for the
# otherwise-silent verdict classes (ambiguous/no_url/preverify_skip/
# noop-live). Read-only observer: cohort_join counts it in observed_any.
PER_ROLE_VERIFY_EVENTS += ("verify_attempt",)


def _parse_ts(value):
    try:
        ts = datetime.fromisoformat(value)
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _load_rows(path):
    """Stream telemetry jsonl; keep only rows the recalibration needs."""
    rows = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                et = r.get("event_type")
                if et == "scan_summary" and r.get("source") == "verify-retry":
                    d = r.get("details") or {}
                    if d.get("synthetic_test"):
                        continue
                    rows.append(r)
                elif et in PER_ROLE_VERIFY_EVENTS:
                    rows.append(r)
    except OSError:
        pass
    return rows


def scan_batches_from_rows(rows, anchor):
    """(ts, scanned, promoted) for verify-retry scan batches at/before anchor.

    Denominator truth (2026-09-19, blackboard J-20260918-2350-veri-2389):
    details.scanned is len(scan_order), which DELIBERATELY includes
    unscored_skip rows so every windowed lead is accounted in the summary
    (2026-09-18 blind-wave fix) — but those rows receive zero HTTP and can
    never promote. Conversion must divide by the attempted pool only, so
    the unscored_skipped count (present since the blind-wave fix) is
    subtracted when schema-consistent (v4: only where unscored <=
    scanned; the live producer can emit a pool-level skip count disjoint
    from the batch, see below).

    Denominator truth, v4 (2026-09-20, ARM 3, J-20260920-2244-veri-3960):
    the live producer emits unscored_skipped as a pool-level skip count
    that can EXCEED the batch's scanned count (observed: scanned=10,
    unscored=83-90 on 48 of the last 60 waves) -- the v2 scan_order
    containment no longer holds and the fields are disjoint counts.
    Subtracting a disjoint count zeroed real waves WITH their promotions
    (12:05/12:14 UTC waves promoted 17 real leads; 17-83 < 0 clamped the
    wave to nothing). The unscored subtraction now applies only when
    schema-consistent (unscored <= scanned); otherwise unscored is
    already disjoint from the batch. The fastpath_live_n reuse cohort
    (zero-HTTP re-verifications of held leads, promotable=0) still leaves
    the denominator, but the numerator is NO LONGER reduced by
    fastpath_live_n: the reuse cohort promotes at 0, so every observed
    promotion came from the attempted pool. The numerator is the observed
    promoted_ready capped at the denominator, keeping 0 <= promoted <=
    scanned per batch (and hence every windowed/EWMA ratio in [0,1])
    even if a reuse-cohort lead ever genuinely promotes through the
    fast path. Older scan_summary rows lack fastpath_live_n and degrade
    exactly to the v2 formula. The producer's accounting invariant
    (scan_order completeness) is untouched — the correction lives at
    the consumer, where the metric is defined.
    """
    out = []
    for r in rows:
        if r.get("event_type") != "scan_summary":
            continue
        d = r.get("details") or {}
        ts = _parse_ts(r.get("ts"))
        if not ts or ts > anchor:
            continue
        scanned = d.get("scanned", 0) or 0
        unscored = d.get("unscored_skipped", 0) or 0
        # v4 (2026-09-20, J-20260920-2244-veri-3960): unscored subtraction is
        # schema-conditional. The live producer emits unscored_skipped as a
        # pool-level skip count that can exceed the batch's scanned count
        # (observed: scanned=10, unscored=83-90 on 48 of the last 60 waves);
        # those fields are disjoint counts and subtracting one from the
        # other zeroes real waves WITH their promotions (12:05/12:14 UTC:
        # 17 real promotions, 17-83 < 0 -> wave vanished). Apply the
        # subtraction only when schema-consistent (unscored <= scanned);
        # otherwise the unscored pool is already disjoint from the batch.
        attempted = scanned - unscored if unscored <= scanned else scanned
        fastpath = d.get("fastpath_live_n", 0) or 0
        denom = max(0, attempted - fastpath)
        # v4: the reuse cohort promotes at 0 (held: materials-abstain/D1/
        # low_fit), so every observed promotion came from the attempted
        # pool. The numerator is never reduced by cohort subtractions;
        # it is capped at the denominator so 0 <= promoted <= scanned
        # holds for every batch even if a reuse lead ever genuinely
        # promotes through the fast path.
        promoted = d.get("promoted_ready", 0) or 0
        promoted = max(0, min(promoted, denom))
        out.append((ts, denom, promoted))
    out.sort(key=lambda b: b[0])
    return out


def window_ratio(batches, anchor, hours):
    """(conversion, scanned, promoted, waves) over the trailing window.

    None conversion when no scans; low_confidence when waves < MIN_WAVES."""
    start = anchor - timedelta(hours=hours)
    scanned = promoted = waves = 0
    for ts, s, p in batches:
        if ts < start:
            continue
        waves += 1
        scanned += s
        promoted += p
    if not scanned:
        return {"conversion": None, "scanned": 0, "promoted": 0,
                "waves": waves, "low_confidence": True}
    return {"conversion": promoted / scanned, "scanned": scanned,
            "promoted": promoted, "waves": waves,
            "low_confidence": waves < MIN_WAVES}


def ewma_conversion(batches, half_life_h=EWMA_HALFLIFE_H):
    """Time-decayed EWMA of per-wave promoted/scanned ratios.

    Alpha per step decays by wave age: alpha = 1 - 2**(-dt/half_life), so a
    wave from one half-life ago contributes ~half of a fresh wave. None when
    no wave has any scans."""
    ewma = None
    prev_ts = None
    for ts, scanned, promoted in batches:
        if scanned <= 0:
            continue
        ratio = promoted / scanned
        if ewma is None or prev_ts is None:
            ewma = ratio
        else:
            dt_h = max((ts - prev_ts).total_seconds() / 3600.0, 0.0)
            alpha = 1.0 - 2.0 ** (-dt_h / half_life_h)
            ewma = alpha * ratio + (1.0 - alpha) * ewma
        prev_ts = ts
    return ewma


def cohort_join(role_ids, rows, anchor, window_h=24):
    """Export-time join: how much of a release cohort is observable.

    Counts cohort members with a per-role verify telemetry event in the
    trailing window. Since 2026-09-18 (J-20260918-1922-veri-2248) every
    scanned lead carries a per-role row — verify_attempt for the
    otherwise-silent verdict classes (ambiguous/no_url/preverify_skip/
    noop-live) — so coverage < 100% reflects unscanned supply, not
    verdict silence.
    """
    start = anchor - timedelta(hours=window_h)
    cohort = set(role_ids or [])
    seen = {}
    for r in rows:
        if r.get("event_type") not in PER_ROLE_VERIFY_EVENTS:
            continue
        rid = r.get("role_id")
        if not rid or rid not in cohort:
            continue
        ts = _parse_ts(r.get("ts"))
        if not ts or ts < start or ts > anchor:
            continue
        seen.setdefault(rid, set()).add(r.get("event_type"))
    counts = {et: 0 for et in PER_ROLE_VERIFY_EVENTS}
    for evts in seen.values():
        for et in evts:
            counts[et] += 1
    n = len(cohort)
    return {"cohort_size": n, "window_h": window_h,
            "observed_any": len(seen), "coverage": len(seen) / n if n else 0.0,
            "observed_verified": counts["lead_verified"],
            "observed_dead": counts["lead_dead"],
            "observed_gate_blocked": counts["gate_blocked"],
            "note": ("per-role verify telemetry covers every scanned lead "
                     "(verify_attempt rows for the silent verdict classes "
                     "since 2026-09-18); coverage<1.0 means unscanned "
                     "supply, not verdict silence")}


def decay_regime(windows):
    """True when the 7d -> 24h -> 12h conversions are present and
    monotonically decaying (conv-recal-v2, 2026-09-18,
    J-20260918-1940-veri-2258). In a decay regime the EWMA lags the
    collapse and reads optimistic (+47% measured), so the forecast mid is
    capped at min(ewma, 24h). Pure function of the window block."""
    try:
        c7 = windows["7d"]["conversion"]
        c24 = windows["24h"]["conversion"]
        c12 = windows["12h"]["conversion"]
    except (KeyError, TypeError):
        return False
    return (c7 is not None and c24 is not None and c12 is not None
            and c7 >= c24 >= c12)


def recalibrate_releases(releases, role_ids_by_release, rows, anchor):
    """Full recalibration block. Pure function of (releases, rows, anchor).

    Never raises on empty input: with no scan evidence it reports
    input_available=False and emits no conversions -- the board's legacy
    point estimates remain the only numbers."""
    block = {"version": RECALIBRATION_VERSION, "ok": True,
             "flat_7d_replaced": True,
             "anchor_ts": anchor.isoformat()}
    batches = scan_batches_from_rows(rows, anchor)
    if not batches:
        block["input_available"] = False
        block["fail_safe"] = {"degraded": True,
                              "reason": "no_scan_batches_7d",
                              "primary": "legacy board point estimate only"}
        block["releases"] = []
        return block
    block["input_available"] = True
    latest_scan = batches[-1][0]
    stale = (anchor - latest_scan) > timedelta(minutes=STALE_INPUT_MIN)
    block["telemetry_stale"] = stale
    windows = {name: window_ratio(batches, anchor, h)
               for name, h in WINDOW_DEFS_H.items()}
    block["windows"] = windows
    ewma = ewma_conversion(batches)
    block["ewma_conversion"] = ewma
    if stale:
        block["fail_safe"] = {
            "degraded": True, "reason": "windowed_inputs_stale",
            "latest_scan_ts": latest_scan.isoformat(),
            "primary": "7d flat window (widest evidence) until fresh waves"}
    else:
        block["fail_safe"] = {"degraded": False, "reason": None,
                              "primary": "ewma + windowed range"}
    convs = {}
    for name, w in windows.items():
        if w["conversion"] is not None:
            convs[name] = w["conversion"]
    if ewma is not None:
        convs["ewma"] = ewma
    out_releases = []
    for rel in releases or []:
        count = rel.get("count", 0) or 0
        expected = {name: int(count * c) for name, c in convs.items()}
        vals = sorted(expected.values())
        lo, hi = (vals[0], vals[-1]) if vals else (None, None)
        primary = expected["ewma"] if (not stale and "ewma" in expected) \
            else expected.get("7d")
        regime = "stable_or_recovering"
        if (not stale and "ewma" in expected and "24h" in expected
                and decay_regime(windows)):
            # conv-recal-v2 (J-20260918-1940-veri-2258): in a monotonic-decay
            # regime the EWMA lags the collapse; cap the mid at the fresher
            # 24h window so the forecast does not over-promise on dead waves.
            # The range is untouched -- only the mid moves.
            primary = min(expected["ewma"], expected["24h"])
            regime = "monotonic_decay"
        entry = {
            "release_id": rel.get("release_id"),
            "count": count,
            "expected_legacy": rel.get("expected"),
            "expected_ewma": expected.get("ewma"),
            "expected_range": {"lo": lo, "mid": primary, "hi": hi,
                               "regime": regime,
                               "basis": ("min/max over 7d, 24h, 12h, ewma; "
                                         "mid=min(ewma,24h) in monotonic-decay "
                                         "regimes, ewma when fresh, 7d when stale")},
            "conversions_used": {k: round(v, 6) for k, v in convs.items()},
        }
        role_ids = (role_ids_by_release or {}).get(rel.get("release_id"), [])
        if role_ids:
            entry["cohort_join"] = cohort_join(role_ids, rows, anchor)
        out_releases.append(entry)
    block["releases"] = out_releases
    # sanity: the 7d window must replicate the exporter's flat input
    legacy = [rel.get("estimated_conversion") for rel in releases or []
              if rel.get("estimated_conversion") is not None]
    w7 = windows["7d"]["conversion"]
    block["replicates_exporter_7d"] = (
        w7 is not None and bool(legacy) and
        all(abs(w7 - c) < 1e-3 for c in legacy))
    return block


def recalibration_summary(snap_path, releases, now):
    """Read the just-written snapshot + telemetry, return the block. Read-only."""
    role_ids_by_release = {}
    anchor = now
    try:
        with open(snap_path, "r", encoding="utf-8") as f:
            snap = json.load(f)
        anchor = _parse_ts(snap.get("observed_at")) or now
        for rel in snap.get("releases", []):
            rid = rel.get("release_id")
            if rid:
                role_ids_by_release[rid] = rel.get("role_ids", [])
    except Exception:
        pass
    rows = _load_rows(TELEMETRY_PATH)
    return recalibrate_releases(releases, role_ids_by_release, rows, anchor)


# --- executable_ready gauge correction (FIX-4, 2026-09-22) -------------------
# The board's executable estimate counts every staged-INTENT lead as
# non-executable: evaluate_readiness appends "attempt_hold" for any
# attempt_state in {INTENT, UNKNOWN, DISPATCHED, SUBMITTED}, and inventory
# appends "unresolved_attempt_history" because a clean staged INTENT is
# hold_required in the attempt reconciliation. A staged INTENT is scheduling
# state, not a gate failure -- the lead passed every real gate and sits in
# the staged-launch buffer waiting on the browser lane (blackboard
# J-20260922-0821-brow-4634, claimed by this fix).
#
# This read-only post-pass recomputes the gauge from the snapshot just
# written: a READY lead whose ONLY failures are the staged-INTENT scheduling
# bundle counts as executable. The bundle is exactly:
#   - "attempt_hold" carried by attempt_state == "INTENT", plus
#   - "unresolved_attempt_history" when the identity's canonical attempt
#     chain is a clean staged INTENT (hold_required, zero findings, no
#     state past INTENT, no multiple unresolved attempts).
# Everything else is never weakened: duplicate_identity,
# canonical_hold_present (D1/policy holds), fit_below_75 /
# action_band_not_apply / policy_not_passed, provider_route_unverified /
# provider_contract_unvalidated, posting_unverified, packet_missing /
# packet_dependencies_changed, answers_unresolved, approval_unverified /
# approval_expired, launch_lock_held_or_unknown, attempt_history_incomplete,
# observation_stale, input_contract_invalid, queue_not_ready, and any
# attempt finding (invalid transitions, multiple unresolved attempts,
# missing initial intent). DISPATCHED / SUBMITTED / UNKNOWN attempt states
# are genuine in-flight / submitted / unverified states and are never
# exempted. Fail-closed: a lead whose gate state is ambiguous (missing
# snapshot row, missing/unreadable board row, unknown attempt_state, or a
# staged INTENT the board did not record as attempt_hold) is NOT counted
# executable.
# Like the conversion recalibration above, this is additive and never fails
# the run: on error the gauge block degrades and the pulse continues.
GAUGE_VERSION = "intent_scheduling_exempt_v1"
# Attempt states the gauge understands. Anything else is ambiguous and fails
# closed to non-executable.
GAUGE_KNOWN_ATTEMPT_STATES = ("INTENT", "NONE", "AUTHORITATIVE_NOT_SUBMITTED",
                              "DISPATCHED", "SUBMITTED", "UNKNOWN")


def _clean_intent_attempt_chain(identity, attempt_apps, attempt_details):
    """True when the identity's canonical attempt history is a clean staged
    INTENT chain: hold_required with no findings on any attempt row, no
    state past INTENT, and no multiple unresolved attempts.

    Anything else (findings, a chain that moved past staging, detail rows
    missing while a hold exists) is ambiguous and returns False, so the
    unresolved_attempt_history hold is treated as genuine. Fail-closed."""
    app = (attempt_apps or {}).get(identity)
    if app is None:
        # No canonical attempt events for this identity: the INTENT is
        # staging-only. unresolved_attempt_history is derived from
        # attempt-application membership, so there is none to exempt and the
        # scheduling state is unambiguous.
        return True
    details = (attempt_details or {}).get(identity)
    if not details:
        return False
    if app.get("multiple_unresolved_attempts"):
        return False
    for det in details:
        if not isinstance(det, dict):
            return False
        if det.get("reasons"):
            return False
        if det.get("reported_state") != "INTENT":
            return False
    return True


def _gauge_lead(lead, row, attempt_apps, attempt_details):
    """Classify one READY lead for the corrected gauge.

    Returns {"executable": bool, "intent_held": bool, "exempted": [...],
    "remaining": [...], "ambiguous": None|str}. Fail-closed: any ambiguity
    yields executable=False."""
    out = {"executable": False, "intent_held": False, "exempted": [],
           "remaining": [], "ambiguous": None}
    if not isinstance(lead, dict) or not isinstance(row, dict):
        out["ambiguous"] = "missing_snapshot_lead_or_board_row"
        return out
    reasons = row.get("reasons")
    if (not isinstance(reasons, list)
            or any(not isinstance(r, str) or not r for r in reasons)):
        out["ambiguous"] = "unreadable_board_reasons"
        return out
    attempt = lead.get("attempt_state")
    if attempt not in GAUGE_KNOWN_ATTEMPT_STATES:
        out["ambiguous"] = "unknown_attempt_state"
        return out
    intent_held = attempt == "INTENT"
    out["intent_held"] = intent_held
    remaining = list(reasons)
    if intent_held:
        # attempt_hold is definitionally the staged-INTENT hold.
        if "attempt_hold" in remaining:
            remaining.remove("attempt_hold")
            out["exempted"].append("attempt_hold")
        else:
            # The exporter claims a staged INTENT but the board recorded no
            # attempt_hold: contradictory evidence -> ambiguous, fail closed.
            out["ambiguous"] = "intent_without_attempt_hold"
            out["remaining"] = remaining
            return out
        # unresolved_attempt_history is the same scheduling state, but only
        # when the canonical attempt chain is a clean staged INTENT.
        if "unresolved_attempt_history" in remaining:
            if _clean_intent_attempt_chain(lead.get("identity"), attempt_apps,
                                           attempt_details):
                remaining.remove("unresolved_attempt_history")
                out["exempted"].append("unresolved_attempt_history")
    out["remaining"] = remaining
    out["executable"] = not remaining
    return out


def corrected_executable_gauge(snapshot_path, now):
    """Read-only post-pass: recompute executable_ready with the staged-INTENT
    scheduling bundle exempted.

    A READY lead that passes all real gates counts as executable even when a
    staged INTENT exists for it; INTENT-held is a scheduling state, not a
    gate failure. The gauge keeps a separate intent_held count so the board
    does not lose that signal. Never raises on unreadable input: degrades to
    ok=False. Per-lead ambiguity fails closed to non-executable."""
    block = {
        "version": GAUGE_VERSION,
        "ok": True,
        "method": ("READY + attempt_state INTENT + zero non-scheduling "
                   "failures + clean INTENT attempt chain => executable"),
        "basis": ("INTENT-held is scheduling state, not a gate failure. "
                  "Duplicate, D1/policy, fit, security, evidence and "
                  "attestation gates are never weakened; ambiguous leads "
                  "fail closed to non-executable."),
    }
    try:
        if KEEL_DIR not in sys.path:
            sys.path.insert(0, KEEL_DIR)
        from keel_flow.board import build
        with open(snapshot_path, "r", encoding="utf-8") as f:
            snapshot = json.load(f)
        report = build(snapshot, now=now)
    except Exception as e:  # noqa: BLE001 - observer must not die here
        block["ok"] = False
        block["error"] = str(e)[:200]
        block["nominal_ready"] = 0
        block["executable_ready"] = 0
        block["intent_held"] = 0
        return block
    readiness = report.get("readiness") or {}
    lead_by_id = {}
    for lead in snapshot.get("leads") or []:
        if isinstance(lead, dict) and lead.get("role_id"):
            lead_by_id[lead["role_id"]] = lead
    row_by_id = {}
    for r in readiness.get("rows") or []:
        if isinstance(r, dict) and r.get("role_id"):
            row_by_id[r["role_id"]] = r
    attempt_report = report.get("attempts") or {}
    attempt_apps = {}
    for app in attempt_report.get("applications") or []:
        if isinstance(app, dict) and app.get("application_id"):
            attempt_apps[app["application_id"]] = app
    attempt_details = {}
    for det in attempt_report.get("attempts") or []:
        if isinstance(det, dict) and det.get("application_id"):
            attempt_details.setdefault(det["application_id"], []).append(det)
    nominal = executable = intent_held = 0
    executable_role_ids = []
    ambiguous_role_ids = []
    blocked = Counter()
    for role_id, lead in lead_by_id.items():
        if lead.get("status") != "READY":
            continue
        nominal += 1
        res = _gauge_lead(lead, row_by_id.get(role_id),
                          attempt_apps, attempt_details)
        if res["intent_held"]:
            intent_held += 1
        if res["ambiguous"]:
            ambiguous_role_ids.append(role_id)
            continue
        if res["executable"]:
            executable += 1
            executable_role_ids.append(role_id)
        else:
            for reason in res["remaining"]:
                blocked[reason] += 1
    block["nominal_ready"] = nominal
    block["executable_ready"] = executable
    block["intent_held"] = intent_held
    block["board_executable_ready"] = readiness.get("executable_ready")
    block["nominal_to_actual_loss"] = nominal - executable
    block["executable_role_ids"] = sorted(executable_role_ids)
    block["ambiguous_role_ids"] = sorted(ambiguous_role_ids)
    block["blocked_reasons"] = dict(blocked)
    return block


def run(cmd):
    p = subprocess.run(cmd, cwd=KEEL_DIR, capture_output=True, text=True,
                       timeout=600)
    return p


def prune(now_ts):
    cutoff = now_ts - RETENTION_H * 3600
    kept = 0
    for name in sorted(os.listdir(OUT_DIR)):
        if name == "flow-board-latest.json":
            continue
        path = os.path.join(OUT_DIR, name)
        try:
            if os.path.getmtime(path) < cutoff:
                os.remove(path)
            else:
                kept += 1
        except OSError:
            pass
    return kept


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    now = datetime.now(timezone.utc)
    stamp = now.strftime("%Y%m%d-%H%M%S")
    snap_path = os.path.join(OUT_DIR, f"snapshot-{stamp}.json")
    report_path = os.path.join(OUT_DIR, f"report-{stamp}.md")

    export = run([sys.executable, "export_flow_snapshot.py", snap_path])
    if export.returncode != 0 or not os.path.exists(snap_path):
        print(json.dumps({"ok": False, "stage": "export",
                          "stderr": export.stderr[-2000:]}))
        return 2

    with open(snap_path, "rb") as f:
        sha = hashlib.sha256(f.read()).hexdigest()
    with open(os.path.join(OUT_DIR, f"sha256-{stamp}.txt"), "w") as f:
        f.write(f"{sha}  snapshot-{stamp}.json\n")

    board = run([sys.executable, "flow_board.py", snap_path, report_path])
    if board.returncode != 0 or not os.path.exists(report_path):
        print(json.dumps({"ok": False, "stage": "board",
                          "stderr": board.stderr[-2000:]}))
        return 2

    try:
        summary = json.loads(board.stdout)
    except Exception:
        summary = {}
    # conversion recalibration (ARM 72): windowed/EWMA input + expected ranges
    # + cohort join, read-only over telemetry and the snapshot just written.
    # Additive only -- the board's own summary keys are untouched. Never fails
    # the run: on error the block degrades and the pulse continues.
    try:
        summary["conversion_recalibration"] = recalibration_summary(
            snap_path, summary.get("releases") or [], now)
    except Exception as e:  # noqa: BLE001 - observer must not die here
        summary["conversion_recalibration"] = {
            "version": RECALIBRATION_VERSION, "ok": False,
            "error": str(e)[:200]}
    # executable_ready gauge (FIX-4): the board's estimate counts every
    # staged-INTENT lead as non-executable; the gauge exempts that
    # scheduling bundle so the board reports genuinely launchable leads.
    # The raw board number is kept as executable_ready_legacy and the
    # INTENT-held count stays visible separately. Additive like the
    # recalibration above: never fails the run.
    try:
        # FIX-5 (2026-09-22, arm02-798): re-freeze `now` at gauge time. The
        # `now` above was captured before the export subprocess; the
        # exporter stamps holds at export time, so every pulse since FIX-4
        # shipped handed review_holds() a pre-export `now` and
        # keel_flow/holds.py:34 ("invalid hold dates") failed the whole
        # rebuild, degrading the gauge fail-closed to ok=false / 0. The
        # gauge re-runs board.build() as an observer of the snapshot just
        # written; evaluation time is the gauge's own now.
        summary["executable_ready_gauge"] = corrected_executable_gauge(
            snap_path, datetime.now(timezone.utc))
        gauge = summary["executable_ready_gauge"]
        if gauge.get("ok"):
            summary["executable_ready_legacy"] = summary.get("executable_ready")
            summary["executable_ready"] = gauge["executable_ready"]
            summary["intent_held"] = gauge["intent_held"]
    except Exception as e:  # noqa: BLE001 - observer must not die here
        summary["executable_ready_gauge"] = {
            "version": GAUGE_VERSION, "ok": False,
            "error": str(e)[:200]}
    summary = {"ok": True, "snapshot": os.path.basename(snap_path),
               "report": os.path.basename(report_path),
               "snapshot_sha256": sha, **summary}
    kept = prune(now.timestamp())
    summary["retained_files"] = kept
    with open(LATEST_PATH, "w") as f:
        json.dump(summary, f, indent=1)
    print(json.dumps(summary))
    return 0


if __name__ == "__main__":
    sys.exit(main())
