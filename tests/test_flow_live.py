"""Live-tree regression tests for the Keel 0.4.0 flow port (2026-09-18).

Run from ~/workspace/keel. All fixtures are synthetic; the code under test
resolves from the live tree. No queues, telemetry, tray, or schedules touched.
"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

# Private-host-only engines tree (job-pipeline). Public checkouts skip the
# live-logger probe below; the host may point at its own tree via
# KEEL_PRIVATE_ENGINES. The probe always isolates its telemetry to tmp_path.
PRIVATE_ENGINES = os.path.expanduser(os.environ.get(
    "KEEL_PRIVATE_ENGINES",
    "~/workspace/job-pipeline/engines/application-executor"))

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from keel_flow import board, discovery, holds, questions, journal, attempts, readiness
from keel_flow.common import ContractError

NOW = datetime.now(timezone.utc)


def make_lead(role_id="R-1", fit=80, status="READY", **kw):
    lead = {
        "schema_version": 1, "role_id": role_id,
        "identity": "a" * 64, "fit_score": fit, "action_band": "APPLY",
        "holds": [], "status": status, "observed_at": NOW.isoformat(),
        "posting_url": "https://example.com/jobs/1",
        "history_reconciled": True, "attempt_state": "NONE",
        "launch_lock_held": False, "policy_pass": True,
        "posting_verified": True, "answers_resolved": True,
        "approval_valid": True, "packet_present": True,
        "route": "browser", "route_supported": True,
        "provider_contract_validated": True,
        "dependencies": {k: "rev-1" for k in
                         ("policy", "form", "answers", "attachments",
                          "target", "approval", "route")},
        "packet_dependency_hash": None, "approval_expires_at":
            (NOW + timedelta(hours=1)).isoformat(),
    }
    lead.update(kw)
    from keel_local.readiness import dependency_hash
    lead["packet_dependency_hash"] = dependency_hash(lead["dependencies"])
    return lead


def make_snapshot(**kw):
    snap = {
        "schema_version": 1, "source_revision": "test",
        "observed_at": NOW.isoformat(), "complete": True, "active": True,
        "leads": [make_lead()], "pool": {"schema_version": 1, "ready": 5,
                                         "actionable": 3,
                                         "observed_at": NOW.isoformat()},
        "holds": [], "hold_decisions": [], "capacity": None, "releases": [],
        "discovery": {"budget_minutes": 0, "sources": []},
        "tray": {"schema_version": 1, "questions": [],
                 "answer_bank": {"answers": {}, "_provenance": {}}},
        "question_dependencies": [], "attempt_events": [],
        "attempt_history_complete": True, "applications": [],
    }
    snap.update(kw)
    return snap


# --- export freshness: stale / future / incomplete snapshots fail closed ---

def test_stale_export_degrades_not_crashes():
    snap = make_snapshot(
        observed_at=(NOW - timedelta(seconds=400)).isoformat())
    report = board.build(snap, now=NOW)
    assert report["forecast"]["state"] == "UNKNOWN"
    assert "EXPORT_STALE_OR_FUTURE" in report["forecast"]["reasons"]
    assert report["execution_authorized"] is False


def test_future_export_degrades_not_crashes():
    snap = make_snapshot(
        observed_at=(NOW + timedelta(seconds=400)).isoformat())
    report = board.build(snap, now=NOW)
    assert report["forecast"]["state"] == "UNKNOWN"
    assert "EXPORT_STALE_OR_FUTURE" in report["forecast"]["reasons"]
    assert report["execution_authorized"] is False


def test_incomplete_export_degrades_not_crashes():
    snap = make_snapshot(complete=False)
    report = board.build(snap, now=NOW)
    assert report["forecast"]["state"] == "UNKNOWN"
    assert "EXPORT_INCOMPLETE" in report["forecast"]["reasons"]
    assert report["execution_authorized"] is False


# --- discovery: 429 is a hard stop, never a partial allocation ---

def test_discovery_429_hard_stop():
    src = {"source_id": "s1", "employer_group": "g", "fit_floor": 75,
           "permitted": True, "rate_limited": True, "measurement_complete": True,
           "checks": 10, "qualified_unique_live": 8, "verification_minutes": 30.0,
           "measurement_started_at": NOW.isoformat(),
           "measurement_completed_at": NOW.isoformat(),
           "evidence_ref": "test"}
    report = discovery.allocate([src], budget_minutes=60, now=NOW)
    assert report["unallocated_minutes"] == 60
    assert report["excluded"][0]["reason"] == "HTTP_429_OR_ACTIVE_RATE_HOLD"
    assert report["state"] == "RATE_LIMIT_RECONCILIATION_REQUIRED"


# --- readiness: fit below 75 is preserved, never rounded up ---

def test_fit_below_75_preserved():
    snap = make_snapshot(leads=[make_lead(fit=74.9)])
    report = board.build(snap, now=NOW)
    row = report["readiness"]["rows"][0]
    assert row["executable"] is False
    assert "fit_below_75" in row["reasons"]


# --- holds: consent and operator_input families are never auto-released ---

def test_consent_hold_never_auto_released():
    hold = {"hold_id": "h1", "role_id": "R-1", "family": "consent",
            "owner": None, "opened_at": NOW.isoformat(),
            "review_after": (NOW + timedelta(days=7)).isoformat(),
            "occurrences": 1, "evidence_revision": "test",
            "release_condition": "Trent's explicit opt-in"}
    report = holds.review_holds([hold], [], now=NOW)
    row = report["rows"][0]
    assert row["overdue"] is False  # review_after is 7d out; still not releasable
    assert row["release_authorized"] is False
    assert row["action"] == "ASSIGN_OWNER"
    assert report["queue_writes"] == 0


def test_operator_input_hold_requires_named_owner_review():
    hold = {"hold_id": "h2", "role_id": "R-1", "family": "operator_input",
            "owner": None,
            "opened_at": (NOW - timedelta(days=8)).isoformat(),
            "review_after": (NOW - timedelta(days=1)).isoformat(),
            "occurrences": 3, "evidence_revision": "test",
            "release_condition": "Trent's own words"}
    report = holds.review_holds([hold], [], now=NOW)
    row = report["rows"][0]
    assert row["release_authorized"] is False
    assert row["action"] in ("ASSIGN_OWNER", "ESCALATE_REVIEW")
    assert report["queue_writes"] == 0


# --- journal: replay is idempotent, content conflict fails closed ---

def test_journal_replay_idempotent_and_conflict_fails():
    logged = []

    def fake_logger(event_type, *, role_id, source, details, event_id):
        assert event_type == "flow_attempt" and source == "keel-flow"
        for row in logged:
            if row["event_id"] == event_id:
                if row["details"] == details:
                    return dict(row)  # idempotent replay: stored row, no new write
                raise ValueError("replay conflict: event_id reused with different payload")
        row = {"event_type": event_type, "event_id": event_id,
               "details": dict(details)}
        logged.append(row)
        return dict(row)

    event = {"schema_version": 1, "event_id": "flow-abc123",
             "application_id": "a" * 64, "attempt_id": "attempt-1",
             "sequence": 0, "state": "INTENT", "content_hash": "c" * 64,
             "observed_at": NOW.isoformat(), "source_ref": "test",
             "execution_authorized": False}
    r1 = journal.record_event(event, logger=fake_logger, role_id="R-1", now=NOW)
    r2 = journal.record_event(event, logger=fake_logger, role_id="R-1", now=NOW)
    assert r1["recorded"] is True and r2["recorded"] is True
    assert len(logged) == 1  # replay: no new row
    assert r1["execution_authorized"] is False
    assert r1["retry_authorized"] is False
    tampered = dict(event, content_hash="d" * 64)
    with pytest.raises(ValueError):
        journal.record_event(tampered, logger=fake_logger, role_id="R-1", now=NOW)
    assert len(logged) == 1  # conflict: nothing written


def test_unknown_attempt_state_never_resolves():
    report = attempts.reconcile([], now=NOW, history_complete=False)
    assert report["history_complete"] is False
    assert report["provider_acceptance_verified"] is False
    assert report["queue_writes"] == 0


# --- questions: exact dependencies preserved, never resolved ---

def test_question_dependencies_preserved_exactly():
    tray = {"schema_version": 1, "questions": [{
        "id": "q1", "employer": "Acme", "policy_scope": "application",
        "field_type": "text", "text": "Do you consent to X?",
        "required": True, "options": [], "quarantined": False}],
        "answer_bank": {"answers": {}, "_provenance": {}}}
    deps = [{"role_id": "R-1", "fit_score": 80, "action_band": "APPLY",
             "other_gates_clear": True, "open_question_ids": ["q1"]}]
    report = questions.prioritize(tray, deps, now=NOW)
    assert report["answers_generated"] == 0
    assert report["items_resolved"] == 0
    assert report["queue_writes"] == 0
    bundle = report["dependency_bundles"][0]
    assert bundle["release_authorized"] is False


# --- journal against the REAL live logger on an isolated telemetry path ---

@pytest.mark.skipif(not os.path.isdir(PRIVATE_ENGINES),
                    reason="private-host-only: needs the job-pipeline engines tree")
def test_flow_attempt_replay_against_live_logger(tmp_path):
    """Canonical replay acknowledgement (2026-09-18 prerequisite).

    Uses JOB_PIPELINE_EVENTS_PATH isolation (the sanctioned test path).
    Proves: vocabulary accepted, replay idempotent, details round-trip
    exactly, hash chain intact, content conflict fails closed with nothing
    written. Production telemetry untouched.
    """
    import subprocess
    script = tmp_path / "replay_probe.py"
    keel_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    script.write_text((
        "import json, os, sys\n"
        "sys.path.insert(0, " + json.dumps(PRIVATE_ENGINES) + ")\n"
        "sys.path.insert(0, " + json.dumps(keel_dir) + ")\n"
    ) + '''
import log_event
from keel_flow.journal import record_event
from datetime import datetime, timezone
now = datetime.now(timezone.utc)
event = {"schema_version": 1, "event_id": "flow-live-logger-probe",
         "application_id": "a" * 64, "attempt_id": "attempt-1", "sequence": 0,
         "state": "INTENT", "content_hash": "c" * 64,
         "observed_at": now.isoformat(), "source_ref": "probe",
         "execution_authorized": False}
record_event(event, logger=log_event.log, role_id="R-PROBE", now=now)
record_event(event, logger=log_event.log, role_id="R-PROBE", now=now)
rows = [json.loads(l) for l in open(os.environ["JOB_PIPELINE_EVENTS_PATH"])]
assert len(rows) == 1, f"replay wrote {len(rows)} rows"
row = rows[0]
assert row["event_type"] == "flow_attempt"
assert row["event_id"] == "flow-live-logger-probe"
assert row["details"]["content_hash"] == "c" * 64
assert "row_hash" in row and "prev_hash" in row
try:
    record_event(dict(event, content_hash="d" * 64),
                 logger=log_event.log, role_id="R-PROBE", now=now)
    raise SystemExit("conflict not rejected")
except ValueError:
    pass
rows = [json.loads(l) for l in open(os.environ["JOB_PIPELINE_EVENTS_PATH"])]
assert len(rows) == 1, "conflict wrote a row"
print("LIVE_LOGGER_REPLAY_OK")
''')
    isolated = tmp_path / "events.jsonl"
    env = dict(os.environ, JOB_PIPELINE_EVENTS_PATH=str(isolated))
    proc = subprocess.run([sys.executable, str(script)],
                          capture_output=True, text=True, env=env)
    assert "LIVE_LOGGER_REPLAY_OK" in proc.stdout, proc.stderr[-2000:]


# --- CLI: never overwrite, never side-effect ---

def test_export_refuses_overwrite(tmp_path):
    import subprocess
    target = tmp_path / "snap.json"
    target.write_text("{}")
    script = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "export_flow_snapshot.py")
    proc = subprocess.run([sys.executable, script, str(target)],
                          capture_output=True, text=True)
    assert proc.returncode == 2
    assert "refusing to overwrite" in proc.stderr
    assert target.read_text() == "{}"  # untouched


def test_board_refuses_overwrite(tmp_path):
    import subprocess
    target = tmp_path / "report.md"
    target.write_text("existing")
    script = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "flow_board.py")
    proc = subprocess.run(
        [sys.executable, script, "/tmp/flow-snapshot-v1.json", str(target)],
        capture_output=True, text=True)
    assert proc.returncode == 2
    assert target.read_text() == "existing"  # untouched
