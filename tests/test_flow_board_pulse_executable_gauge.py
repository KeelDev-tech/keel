#!/usr/bin/env python3
"""Tests for the flow_board_pulse.py executable_ready gauge post-pass (FIX-4).

Blackboard J-20260922-0821-brow-4634: the board's executable estimate counts
every staged-INTENT lead as non-executable (evaluate_readiness appends
"attempt_hold" for INTENT; inventory appends "unresolved_attempt_history"
because a clean staged INTENT is hold_required in the attempt
reconciliation), so executable_ready reads 0 while the READY pool holds
genuinely launchable staged leads. A staged INTENT is scheduling state, not
a gate failure.

The gauge (intent_scheduling_exempt_v1) exempts exactly the staged-INTENT
scheduling bundle -- attempt_hold carried by attempt_state == "INTENT", plus
unresolved_attempt_history from a clean finding-free INTENT attempt chain --
and keeps a separate intent_held count. Every other gate (duplicate, D1/
policy holds, fit, provider route, posting/packet/answers evidence,
approval/attestation, launch locks, attempt findings, stale observations) is
never weakened; DISPATCHED / SUBMITTED / UNKNOWN attempt states are never
exempted; ambiguous leads fail closed to non-executable.

All fixtures are synthetic; nothing touches live queues, telemetry, locks,
staged-launches, or snapshots. Run from ~/workspace/keel.
"""

import hashlib
import importlib.util
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

KEEL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PULSE_PATH = os.path.join(KEEL_DIR, "flow_board_pulse.py")

from keel_local.readiness import DEPENDENCIES, dependency_hash  # noqa: E402


def load_pulse():
    spec = importlib.util.spec_from_file_location(
        "flow_board_pulse_gauge_test", PULSE_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["flow_board_pulse_gauge_test"] = mod
    spec.loader.exec_module(mod)
    return mod


P = load_pulse()
NOW = datetime(2026, 9, 22, 11, 0, tzinfo=timezone.utc)


def ident(role_id):
    return hashlib.sha256(("identity:" + role_id).encode()).hexdigest()


def synth_lead(role_id, *, status="READY", attempt_state="NONE", fit_score=80,
               action_band="APPLY", holds=None, launch_lock_held=False,
               approval_valid=True, packet_present=True, answers_resolved=True,
               identity=None):
    deps = {k: "rev-%s-%s" % (role_id, k) for k in DEPENDENCIES}
    return {
        "schema_version": 1,
        "role_id": role_id,
        "identity": identity or ident(role_id),
        "fit_score": fit_score,
        "action_band": action_band,
        "holds": holds or [],
        "status": status,
        "observed_at": NOW.isoformat(),
        "posting_url": "https://example.invalid/jobs/" + role_id,
        "history_reconciled": True,
        "attempt_state": attempt_state,
        "launch_lock_held": launch_lock_held,
        "policy_pass": action_band == "APPLY",
        "posting_verified": True,
        "answers_resolved": answers_resolved,
        "approval_valid": approval_valid,
        "packet_present": packet_present,
        "route": "browser",
        "route_supported": True,
        "provider_contract_validated": False,
        "dependencies": deps,
        "packet_dependency_hash": dependency_hash(deps) if packet_present else None,
        "approval_expires_at": (NOW + timedelta(hours=20)).isoformat(),
    }


def attempt_event(event_id, attempt_id, identity, sequence, state):
    return {
        "schema_version": 1,
        "event_id": event_id,
        "attempt_id": attempt_id,
        "application_id": identity,
        # content_hash is stable per attempt (reconcile requires it).
        "content_hash": hashlib.sha256(attempt_id.encode()).hexdigest(),
        "sequence": sequence,
        "state": state,
        "observed_at": (NOW - timedelta(minutes=5)).isoformat(),
        "source_ref": "synthetic-test",
    }


def synth_snapshot(leads, attempt_events):
    n_ready = sum(1 for l in leads if l.get("status") == "READY")
    return {
        "schema_version": 1,
        "source_revision": "synthetic-test",
        "observed_at": NOW.isoformat(),
        "complete": True,
        "active": False,
        "leads": leads,
        "holds": [],
        "hold_decisions": [],
        "pool": {"schema_version": 1, "ready": n_ready,
                 "actionable": n_ready, "observed_at": NOW.isoformat()},
        "capacity": None,
        "releases": [],
        "discovery": {"budget_minutes": 60, "sources": []},
        "tray": {"schema_version": 1, "questions": [],
                 "answer_bank": {"answers": {}, "_provenance": {}}},
        "question_dependencies": [],
        "attempt_events": attempt_events,
        "attempt_history_complete": True,
        "applications": [],
    }


def clean_chain(identity):
    return ({identity: {"application_id": identity, "hold_required": True,
                        "multiple_unresolved_attempts": False}},
            {identity: [{"reported_state": "INTENT", "reasons": []}]})


def dirty_chain(identity):
    return ({identity: {"application_id": identity, "hold_required": True,
                        "multiple_unresolved_attempts": False}},
            {identity: [{"reported_state": "INTENT",
                         "reasons": ["INVALID_TRANSITION"]}]})


# --- _gauge_lead unit tests ---------------------------------------------------

def test_intent_gate_clean_counts_executable_and_intent_held():
    lead = synth_lead("r-intent", attempt_state="INTENT")
    row = {"role_id": "r-intent", "reasons": ["attempt_hold"]}
    res = P._gauge_lead(lead, row, {}, {})
    assert res["executable"] is True
    assert res["intent_held"] is True
    assert res["exempted"] == ["attempt_hold"]
    assert res["remaining"] == []
    assert res["ambiguous"] is None


def test_intent_clean_chain_exempts_unresolved_history():
    identity = ident("r-intent-events")
    lead = synth_lead("r-intent-events", attempt_state="INTENT",
                      identity=identity)
    row = {"role_id": "r-intent-events",
           "reasons": ["attempt_hold", "unresolved_attempt_history"]}
    apps, details = clean_chain(identity)
    res = P._gauge_lead(lead, row, apps, details)
    assert res["executable"] is True
    assert res["intent_held"] is True
    assert sorted(res["exempted"]) == ["attempt_hold",
                                       "unresolved_attempt_history"]


def test_intent_dirty_chain_keeps_history_hold():
    identity = ident("r-intent-dirty")
    lead = synth_lead("r-intent-dirty", attempt_state="INTENT",
                      identity=identity)
    row = {"role_id": "r-intent-dirty",
           "reasons": ["attempt_hold", "unresolved_attempt_history"]}
    apps, details = dirty_chain(identity)
    res = P._gauge_lead(lead, row, apps, details)
    assert res["executable"] is False
    assert res["intent_held"] is True
    assert res["exempted"] == ["attempt_hold"]
    assert res["remaining"] == ["unresolved_attempt_history"]
    assert res["ambiguous"] is None


@pytest.mark.parametrize("reason", [
    "fit_below_75",
    "action_band_not_apply",
    "policy_not_passed",
    "canonical_hold_present",      # D1 / policy holds
    "duplicate_identity",
    "provider_route_unverified",
    "posting_unverified",
    "packet_missing",
    "packet_dependencies_changed",
    "answers_unresolved",
    "approval_unverified",
    "approval_expired",
    "launch_lock_held_or_unknown",
    "attempt_history_incomplete",
    "observation_stale",
    "input_contract_invalid",
    "queue_not_ready",
])
def test_real_gate_failures_never_weakened(reason):
    # A genuine gate failure is never reclassified as launchable, with or
    # without a staged INTENT.
    for attempt_state in ("NONE", "INTENT"):
        lead = synth_lead("r-gate-%s-%s" % (reason, attempt_state),
                          attempt_state=attempt_state)
        reasons = [reason]
        if attempt_state == "INTENT":
            reasons = ["attempt_hold"] + reasons
        row = {"role_id": lead["role_id"], "reasons": reasons}
        res = P._gauge_lead(lead, row, {}, {})
        assert res["executable"] is False, (reason, attempt_state)
        assert reason in res["remaining"], (reason, attempt_state)


def test_ambiguous_missing_row_not_executable():
    lead = synth_lead("r-norow", attempt_state="INTENT")
    res = P._gauge_lead(lead, None, {}, {})
    assert res["executable"] is False
    assert res["ambiguous"] == "missing_snapshot_lead_or_board_row"


def test_ambiguous_unreadable_reasons_not_executable():
    lead = synth_lead("r-badreasons", attempt_state="NONE")
    res = P._gauge_lead(lead, {"role_id": "r-badreasons",
                               "reasons": "attempt_hold"}, {}, {})
    assert res["executable"] is False
    assert res["ambiguous"] == "unreadable_board_reasons"


def test_ambiguous_unknown_attempt_state_not_executable():
    lead = synth_lead("r-bogus", attempt_state="BOGUS")
    row = {"role_id": "r-bogus", "reasons": []}
    res = P._gauge_lead(lead, row, {}, {})
    assert res["executable"] is False
    assert res["ambiguous"] == "unknown_attempt_state"


def test_intent_without_attempt_hold_is_ambiguous():
    # Exporter claims a staged INTENT but the board recorded no attempt_hold:
    # contradictory evidence -> fail closed, still counted intent_held.
    lead = synth_lead("r-intent-nohold", attempt_state="INTENT")
    row = {"role_id": "r-intent-nohold", "reasons": []}
    res = P._gauge_lead(lead, row, {}, {})
    assert res["executable"] is False
    assert res["intent_held"] is True
    assert res["ambiguous"] == "intent_without_attempt_hold"


def test_dispatched_never_exempted():
    lead = synth_lead("r-dispatched", attempt_state="DISPATCHED")
    row = {"role_id": "r-dispatched",
           "reasons": ["attempt_hold", "unresolved_attempt_history"]}
    identity = lead["identity"]
    apps = {identity: {"application_id": identity, "hold_required": True,
                       "multiple_unresolved_attempts": False}}
    details = {identity: [{"reported_state": "DISPATCHED", "reasons": []}]}
    res = P._gauge_lead(lead, row, apps, details)
    assert res["executable"] is False
    assert res["intent_held"] is False
    assert res["exempted"] == []


def test_submitted_never_exempted():
    lead = synth_lead("r-submitted", attempt_state="SUBMITTED")
    row = {"role_id": "r-submitted", "reasons": ["attempt_hold"]}
    res = P._gauge_lead(lead, row, {}, {})
    assert res["executable"] is False
    assert res["intent_held"] is False


def test_none_clean_executable_not_intent_held():
    lead = synth_lead("r-none", attempt_state="NONE")
    row = {"role_id": "r-none", "reasons": []}
    res = P._gauge_lead(lead, row, {}, {})
    assert res["executable"] is True
    assert res["intent_held"] is False


# --- corrected_executable_gauge integration tests ------------------------------

def _integration_snapshot():
    id_clean = ident("intent-clean-events")
    id_dirty = ident("intent-dirty-chain")
    id_disp = ident("dispatched-live")
    dup_identity = ident("dup-shared")
    leads = [
        synth_lead("intent-clean-events", attempt_state="INTENT",
                   identity=id_clean),
        synth_lead("intent-clean-staging-only", attempt_state="INTENT"),
        synth_lead("intent-lock-held", attempt_state="INTENT",
                   launch_lock_held=True),
        synth_lead("intent-dirty-chain", attempt_state="INTENT",
                   identity=id_dirty),
        synth_lead("dispatched-live", attempt_state="DISPATCHED",
                   identity=id_disp),
        synth_lead("fit-failed", fit_score=60),
        synth_lead("evidence-failed", packet_present=False,
                   approval_valid=False),
        synth_lead("d1-hold", holds=["policy:travel-commitment"]),
        synth_lead("dup-a", identity=dup_identity),
        synth_lead("dup-b", identity=dup_identity),
        synth_lead("none-clean"),
    ]
    events = [
        attempt_event("evt-clean-0", "att-clean", id_clean, 0, "INTENT"),
        attempt_event("evt-dirty-0", "att-dirty", id_dirty, 0, "INTENT"),
        attempt_event("evt-dirty-1", "att-dirty", id_dirty, 1,
                      "CONFIRMATION_REPORTED"),  # INVALID_TRANSITION
        attempt_event("evt-disp-0", "att-disp", id_disp, 0, "INTENT"),
        attempt_event("evt-disp-1", "att-disp", id_disp, 1, "DISPATCHED"),
    ]
    return synth_snapshot(leads, events)


def test_gauge_end_to_end_on_synthetic_snapshot():
    snap = _integration_snapshot()
    with tempfile.NamedTemporaryFile("w", suffix=".json",
                                     delete=False) as f:
        json.dump(snap, f)
        path = f.name
    try:
        g = P.corrected_executable_gauge(path, NOW)
    finally:
        os.unlink(path)
    assert g["ok"] is True
    assert g["version"] == "intent_scheduling_exempt_v1"
    assert g["nominal_ready"] == 11
    # intent-clean-events, intent-clean-staging-only, none-clean
    assert g["executable_ready"] == 3
    # the four staged INTENTs (clean x2, lock-held, dirty-chain)
    assert g["intent_held"] == 4
    assert g["board_executable_ready"] == 1  # none-clean passes the raw board too
    assert g["nominal_to_actual_loss"] == 8
    assert sorted(g["executable_role_ids"]) == [
        "intent-clean-events", "intent-clean-staging-only", "none-clean"]
    assert g["ambiguous_role_ids"] == []
    # the scheduling bundle itself never appears as a blocker for the
    # exempted leads; genuine blockers are still reported
    assert g["blocked_reasons"].get("attempt_hold", 0) == 1  # dispatched-live
    assert g["blocked_reasons"]["launch_lock_held_or_unknown"] == 1
    assert g["blocked_reasons"]["fit_below_75"] == 1
    assert g["blocked_reasons"]["duplicate_identity"] == 2
    assert g["blocked_reasons"]["canonical_hold_present"] == 1


def test_gauge_degrades_on_unreadable_snapshot():
    g = P.corrected_executable_gauge("/nonexistent/fix4-gauge-test.json", NOW)
    assert g["ok"] is False
    assert g["executable_ready"] == 0
    assert g["intent_held"] == 0
    assert "error" in g


# --- FIX-5 regression: stale-now clock race (arm02-798) ----------------------
# main() froze `now` before the export subprocess; the exporter stamps
# holds at export time, so a pre-export `now` makes keel_flow/holds.py:34
# ("invalid hold dates") fail the gauge rebuild every pulse and degrade
# fail-closed to 0. The fix re-freezes now at gauge time. This test pins
# the contract: a hold stamped after a pre-export frozen now must still be
# measurable when the gauge receives a gauge-time (post-export) now, and
# a pre-export now is what reproduces the historical failure.


def _hold_opened_after_export(hold_id, role_id, opened_at, review_after):
    return {
        "schema_version": 1,
        "hold_id": hold_id,
        "role_id": role_id,
        "family": "cooldown",
        "evidence_revision": "rev-1",
        "release_condition": "cooldown elapsed",
        "occurrences": 1,
        "opened_at": opened_at,
        "review_after": review_after,
        "owner": "test",
    }


def test_gauge_measures_export_time_holds_with_gauge_time_now():
    frozen_now = NOW  # captured before the (simulated) export ran
    export_time = NOW + timedelta(seconds=8)  # exporter stamps holds here
    lead = synth_lead("r-cooldown", attempt_state="NONE",
                      holds=["cooldown:exported"])
    snap = synth_snapshot([lead], [])
    snap["observed_at"] = export_time.isoformat()
    snap["holds"] = [_hold_opened_after_export(
        "h-1", "r-cooldown", export_time.isoformat(),
        (export_time + timedelta(hours=1)).isoformat())]
    with tempfile.NamedTemporaryFile("w", suffix=".json",
                                     delete=False) as f:
        json.dump(snap, f)
        path = f.name
    try:
        # The historical defect: handing the gauge a pre-export `now`
        # raises "invalid hold dates" and degrades fail-closed to 0.
        bad = P.corrected_executable_gauge(path, frozen_now)
        assert bad["ok"] is False
        assert bad["error"] == "invalid hold dates"
        assert bad["executable_ready"] == 0
        # The FIX-5 fix: re-freeze now at gauge time (after the export).
        good = P.corrected_executable_gauge(
            path, datetime.now(timezone.utc))
    finally:
        os.unlink(path)
    assert good["ok"] is True
    # A cooldown hold is a genuine gate: the lead is not executable, but
    # the number is measured, not errored.
    assert good["executable_ready"] == 0
    assert good["nominal_ready"] == 1
