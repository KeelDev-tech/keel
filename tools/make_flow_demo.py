#!/usr/bin/env python3
"""Synthetic, reproducible operating export. No private queue or answer data."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from keel_local.contracts import application_identity
from keel_local.readiness import dependency_hash, DEPENDENCIES
from keel_flow.attempts import intent

NOW = datetime(2026, 9, 18, 12, tzinfo=timezone.utc)


def lead(role_id, **changes):
    dependencies = {key: "synthetic-v1" for key in DEPENDENCIES}
    return {"schema_version": 1, "role_id": role_id, "observed_at": NOW.isoformat(), "status": "READY",
            "identity": application_identity("synthetic-candidate", "fixture", "example", role_id),
            "posting_url": "https://example.org/jobs/" + role_id,
            "history_reconciled": True, "attempt_state": "NONE", "launch_lock_held": False,
            "policy_pass": True, "posting_verified": True, "answers_resolved": True,
            "approval_valid": True, "packet_present": True, "route": "browser", "route_supported": True,
            "dependencies": dependencies, "packet_dependency_hash": dependency_hash(dependencies),
            "approval_expires_at": (NOW + timedelta(hours=1)).isoformat(),
            "fit_score": 84, "action_band": "APPLY", "holds": [], **changes}


def source(sid, **changes):
    return {"source_id": sid, "employer_group": sid, "evidence_ref": "synthetic-source-window-1",
            "permitted": True, "rate_limited": False, "measurement_complete": True,
            "observed_at": NOW.isoformat(), "fit_floor": 75, "checks": 100,
            "qualified_unique_live": 20, "verification_minutes": 60, **changes}


def hold(**changes):
    return {"hold_id": "hold-1", "role_id": "role-2", "family": "dedupe", "owner": "canonical-reviewer",
            "opened_at": (NOW - timedelta(days=2)).isoformat(), "review_after": (NOW - timedelta(hours=2)).isoformat(),
            "occurrences": 4, "evidence_revision": "synthetic-evidence-1",
            "release_condition": "Canonical reviewer reconciles exact posting identity against submission history", **changes}


def capacity(**changes):
    return {"value": 12, "unit": "applications/hour", "observed_at": NOW.isoformat(),
            "source_revision": "synthetic-supplied-window", "evidence_ref": "synthetic-capacity-1",
            "measured_while_supplied": True, "horizon_seconds": 3600, **changes}


def release(**changes):
    result = {"release_id": "synthetic-backlog", "evidence_ref": "synthetic-backlog-1", "count": 4,
            "stage": "VERIFICATION_ELIGIBLE", "available_at": (NOW + timedelta(minutes=10)).isoformat(),
            "p95_verify_seconds": 300, "p95_prepare_seconds": 300, "estimated_conversion": 0.5,
            "conversion_evidence_ref": "synthetic-conversion-window", **changes}
    result.setdefault("role_ids", [result["release_id"] + ":" + str(i) for i in range(result["count"])])
    return result


def question(qid, **changes):
    return {"id": qid, "employer": "Example", "policy_scope": "synthetic-role-scope", "field_type": "select",
            "required": True, "options": ["Yes", "No"], "text": "Are you willing to travel?", **changes}


def application(aid, **changes):
    return {"application_id": aid, "source_id": "source-a", "fit_score": 84,
            "submission_confirmed_by_adapter": True, "submission_ref": "synthetic-receipt-" + aid,
            "adapter_revision": "synthetic-adapter-1", "submitted_at": (NOW - timedelta(days=20)).isoformat(),
            "followup_observed_through": NOW.isoformat(), **changes}


def make_snapshot():
    event = intent("synthetic-candidate", "fixture", "example", "role-3", {"packet_revision": "synthetic-packet-1"},
                   nonce="synthetic-attempt-1", observed_at=(NOW - timedelta(minutes=10)).isoformat())
    dispatched = {**event, "event_id": "synthetic-dispatch-1", "sequence": 1, "state": "DISPATCHED",
                  "source_ref": "synthetic-dispatch-export", "observed_at": (NOW - timedelta(minutes=9)).isoformat()}
    unknown = {**dispatched, "event_id": "synthetic-unknown-1", "sequence": 2, "state": "UNKNOWN",
               "source_ref": "synthetic-timeout-export", "observed_at": (NOW - timedelta(minutes=8)).isoformat()}
    return {"schema_version": 1, "source_revision": "SYNTHETIC-DEMO-NOT-LIVE", "observed_at": NOW.isoformat(),
            "complete": True, "active": True,
            "leads": [lead("role-1"), lead("role-2"), lead("role-3"), lead("role-4", answers_resolved=False),
                      lead("role-5", answers_resolved=False)] +
                     [lead(rid, status="PARKED-PENDING-VERIFICATION", posting_verified=False, packet_present=False)
                      for rid in release()["role_ids"]],
            "holds": [hold(), hold(hold_id="hold-consent", role_id="role-5", family="consent", owner="applicant",
                                   release_condition="Applicant explicitly answers the exact consent question")],
            "hold_decisions": [], "pool": {"schema_version": 1, "ready": 5, "actionable": 0, "observed_at": NOW.isoformat()},
            "capacity": capacity(), "releases": [release()],
            "discovery": {"budget_minutes": 30, "sources": [source("source-a"), source("source-b", qualified_unique_live=5),
                                                              source("source-c", checks=0, qualified_unique_live=0, verification_minutes=0)]},
            "tray": {"schema_version": 1, "questions": [question("travel"), question("consent", employer="Stripe", text="WhatsApp consent?")]},
            "question_dependencies": [{"role_id": "role-4", "fit_score": 84, "action_band": "APPLY", "other_gates_clear": True,
                                       "open_question_ids": ["travel"], "deadline": (NOW + timedelta(hours=6)).isoformat()},
                                      {"role_id": "role-5", "fit_score": 84, "action_band": "APPLY", "other_gates_clear": True,
                                       "open_question_ids": ["travel", "consent"], "deadline": None}],
            "attempt_events": [event, dispatched, unknown], "attempt_history_complete": True,
            "applications": [application("app-1", interview_at=(NOW - timedelta(days=10)).isoformat(), interview_evidence_ref="synthetic-interview-1"),
                             application("app-2"), application("app-new", submitted_at=(NOW - timedelta(days=2)).isoformat()),
                             application("app-unconfirmed", submission_confirmed_by_adapter=False)]}


if __name__ == "__main__": print(json.dumps(make_snapshot(), indent=2))
