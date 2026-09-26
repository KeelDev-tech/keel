"""Real SQLite crash/reopen, fenced leases and immutable UNKNOWN regressions."""
from copy import deepcopy
import json
import os
import sqlite3

import pytest

from keel_agent.state import StateError
from keel_loki.recovery import RecoveryError, RecoveryJournal, demo
from keel_trust.common import digest


def ready(home):
    journal = RecoveryJournal(home, "workspace", now=100)
    journal.register("job", "a" * 64, now=101)
    journal.approve("job", "a" * 64, "b" * 64, now=102)
    lease = journal.claim("job", "worker-a", lease_seconds=10, now=103)
    return journal, lease


def unknown(home):
    journal, lease = ready(home)
    journal.start("job", "worker-a", lease["fence"], now=104)
    return RecoveryJournal(home, "workspace", now=105), lease


def evidence(snapshot, observation="CONFIRMED"):
    job = snapshot["state"]["jobs"]["job"]
    return {"schema": "keel.loki.reconciliation-evidence.v1", "workspace_id": "workspace", "job_id": "job",
            "attempt_id": job["attempt_id"], "revision_sha256": job["start_revision_sha256"],
            "attempt_fence": job["start_fence"], "observation": observation,
            "observed_at": 106, "evidence_sha256": "c" * 64}


def propose(journal, snapshot, proof, **changes):
    args = {"expected_checkpoint_sha256": snapshot["checkpoint_sha256"],
            "expected_evidence_sha256": digest(proof), "now": 107}
    args.update(changes)
    return journal.reconciliation_proposal("job", proof, **args)


def test_real_abrupt_process_exit_preserves_started_intent_as_unknown(tmp_path):
    # Fork inherits the regression network guard; no shell or executable is
    # invoked. os._exit bypasses Python cleanup after the SQLite FULL commit.
    home = tmp_path / "crashed"
    pid = os.fork()
    if pid == 0:
        try:
            journal, lease = ready(home)
            journal.start("job", "worker-a", lease["fence"], now=104)
        except BaseException:
            os._exit(99)
        os._exit(73)
    _, status = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(status) == 73
    journal = RecoveryJournal(home, "workspace", now=105)
    job = journal.snapshot()["state"]["jobs"]["job"]
    assert job["phase"] == "UNKNOWN"
    assert job["unknown_reason"] == "controller_reopened"
    assert job["owner"] is None and job["fence"] > job["start_fence"]
    with pytest.raises(RecoveryError, match="not_retryable"):
        journal.claim("job", "worker-b", now=106)


def test_recorded_completion_is_observation_and_cannot_repeat(tmp_path):
    journal, lease = ready(tmp_path / "state")
    journal.start("job", "worker-a", lease["fence"], now=104)
    result = journal.complete("job", "worker-a", lease["fence"], "c" * 64, now=105)
    assert result == {"phase": "RECORDED", "observation_authenticated": False, "execution_authorized": False}
    with pytest.raises(RecoveryError):
        journal.complete("job", "worker-a", lease["fence"], "c" * 64, now=106)
    with pytest.raises(RecoveryError):
        journal.claim("job", "worker-b", now=106)


def test_unapproved_start_is_rejected_without_state_mutation(tmp_path):
    journal = RecoveryJournal(tmp_path / "state", "workspace", now=100)
    journal.register("job", "a" * 64, now=101)
    lease = journal.claim("job", "worker-a", now=102)
    before = journal.snapshot()
    with pytest.raises(RecoveryError, match="approval"):
        journal.start("job", "worker-a", lease["fence"], now=103)
    assert journal.snapshot() == before


@pytest.mark.parametrize("operation", ["revoke", "revise", "hold"])
def test_invalidated_active_work_becomes_unknown_and_stale_completion_fails(tmp_path, operation):
    journal, lease = ready(tmp_path / "state")
    journal.start("job", "worker-a", lease["fence"], now=104)
    if operation == "revoke":
        journal.revoke("job", now=105)
    elif operation == "revise":
        journal.revise("job", "d" * 64, now=105)
    else:
        journal.set_hold("job", now=105)
    assert journal.snapshot()["state"]["jobs"]["job"]["phase"] == "UNKNOWN"
    with pytest.raises(RecoveryError):
        journal.complete("job", "worker-a", lease["fence"], "c" * 64, now=106)


def test_expired_idle_lease_is_fenced_before_takeover(tmp_path):
    journal, lease = ready(tmp_path / "state")
    fresh = journal.claim("job", "worker-b", now=114)
    assert fresh["fence"] > lease["fence"]
    with pytest.raises(RecoveryError, match="stale"):
        journal.start("job", "worker-a", lease["fence"], now=115)
    journal.start("job", "worker-b", fresh["fence"], now=115)


def test_expired_started_attempt_is_not_reclaimed(tmp_path):
    journal, lease = ready(tmp_path / "state")
    journal.start("job", "worker-a", lease["fence"], now=104)
    with pytest.raises(RecoveryError, match="not_retryable"):
        journal.claim("job", "worker-b", now=114)
    assert journal.recover_expired(now=114)["fenced_jobs"] == ["job"]
    assert journal.snapshot()["state"]["jobs"]["job"]["phase"] == "UNKNOWN"


def test_global_429_survives_reopen_and_blocks_other_jobs(tmp_path):
    home = tmp_path / "state"
    journal, lease = ready(home)
    journal.start("job", "worker-a", lease["fence"], now=104)
    journal.record_429(now=105)
    journal = RecoveryJournal(home, "workspace", now=106)
    journal.register("another", "d" * 64, now=107)
    journal.approve("another", "d" * 64, "e" * 64, now=108)
    with pytest.raises(RecoveryError, match="persistent_gate"):
        journal.claim("another", "worker-b", now=109)
    assert journal.snapshot()["state"]["jobs"]["job"]["phase"] == "UNKNOWN"


def test_new_approval_and_escalation_never_clear_hold(tmp_path):
    journal, lease = ready(tmp_path / "state")
    journal.set_hold("job", now=104)
    journal.approve("job", "a" * 64, "c" * 64, now=105)
    journal.escalate("job", now=106)
    state = journal.snapshot()["state"]["jobs"]["job"]
    assert state["held"] and state["escalated"]
    with pytest.raises(RecoveryError, match="persistent_gate"):
        journal.claim("job", "worker-b", now=107)


@pytest.mark.parametrize("observation", ["CONFIRMED", "NOT_OBSERVED", "INCONCLUSIVE"])
def test_reconciliation_is_bound_read_only_and_never_clears_unknown(tmp_path, observation):
    journal, lease = unknown(tmp_path / "state")
    before = journal.snapshot()
    result = propose(journal, before, evidence(before, observation))
    assert result["retry_authorized"] is False
    assert result["reconciliation_authorized"] is False
    assert result["journal_writes"] == result["canonical_writes"] == 0
    assert journal.snapshot() == before


@pytest.mark.parametrize("field,value", [("attempt_id", "other"), ("workspace_id", "other"),
    ("job_id", "other"), ("revision_sha256", "e" * 64), ("attempt_fence", True),
    ("observed_at", None), ("observed_at", 100), ("observed_at", 200), ("observation", "RETRY")])
def test_even_rehashed_evidence_cannot_escape_exact_attempt_scope(tmp_path, field, value):
    journal, lease = unknown(tmp_path / "state")
    before = journal.snapshot()
    proof = evidence(before)
    proof[field] = value
    with pytest.raises((RecoveryError, StateError)):
        propose(journal, before, proof)
    assert journal.snapshot() == before


def test_external_evidence_pin_and_checkpoint_are_both_required(tmp_path):
    journal, lease = unknown(tmp_path / "state")
    before = journal.snapshot()
    proof = evidence(before)
    with pytest.raises(RecoveryError, match="pin_mismatch"):
        propose(journal, before, proof, expected_evidence_sha256="f" * 64)
    journal.escalate("job", now=106)
    with pytest.raises(RecoveryError, match="checkpoint_mismatch"):
        propose(journal, before, proof)


@pytest.mark.parametrize("target", ["state", "event"])
def test_database_tampering_is_rejected_before_read_or_reopen(tmp_path, target):
    home = tmp_path / "state"
    journal, lease = unknown(home)
    with sqlite3.connect(home / "recovery.sqlite3") as db:
        if target == "state":
            raw = json.loads(db.execute("SELECT payload FROM loki_recovery_state").fetchone()[0])
            raw["jobs"]["job"]["phase"] = "IDLE"
            db.execute("UPDATE loki_recovery_state SET payload=?", (json.dumps(raw),))
        else:
            db.execute("UPDATE loki_recovery_events SET sha256=? WHERE sequence=1", ("f" * 64,))
    with pytest.raises(RecoveryError):
        journal.snapshot()
    with pytest.raises(RecoveryError):
        RecoveryJournal(home, "workspace", now=106)


def test_authenticated_clock_rejects_metadata_only_rollback(tmp_path):
    home = tmp_path / "state"
    journal, lease = ready(home)
    with sqlite3.connect(home / "recovery.sqlite3") as db:
        db.execute("UPDATE metadata SET value='0' WHERE name='last_now'")
    with pytest.raises(RecoveryError, match="clock_rollback"):
        journal.escalate("job", now=99)


def test_demo_reports_measured_restart_checks_without_network(tmp_path):
    result = demo(tmp_path / "demo")
    assert result["status"] == "PASS" and all(result["checks"].values())
    assert result["network_calls"] == result["external_actions"] == result["canonical_writes"] == 0
    assert result["human_approval_authenticated"] is False


def test_readonly_view_does_not_restart_controller_or_change_journal(tmp_path):
    home = tmp_path / "state"
    journal, lease = ready(home)
    journal.start("job", "worker-a", lease["fence"], now=104)
    before = journal.snapshot()
    reader = RecoveryJournal.open_readonly(home, "workspace")
    assert reader.snapshot() == before
    assert before["state"]["jobs"]["job"]["phase"] == "STARTED"
    with pytest.raises(RecoveryError, match="read_only_view"):
        reader.escalate("job", now=105)
    assert journal.snapshot() == before
    journal.complete("job", "worker-a", lease["fence"], "c" * 64, now=105)
    assert reader.snapshot()["state"]["jobs"]["job"]["phase"] == "RECORDED"


def test_readonly_proposal_has_no_constructor_epoch_write(tmp_path):
    home = tmp_path / "state"
    journal, lease = unknown(home)
    before = journal.snapshot()
    reader = RecoveryJournal.open_readonly(home, "workspace")
    assert propose(reader, before, evidence(before))["journal_writes"] == 0
    assert journal.snapshot() == before


def test_readonly_missing_state_cannot_create_anything(tmp_path):
    home = tmp_path / "absent"
    with pytest.raises((OSError, StateError)):
        RecoveryJournal.open_readonly(home, "workspace")
    assert not home.exists()


def test_demo_trace_comes_from_committed_journal_observations(tmp_path):
    result = demo(tmp_path / "demo")
    conformance = result["journal_conformance"]
    assert conformance["observation_source"] == "COMMITTED_SQLITE_SNAPSHOTS"
    assert conformance["trace_check"]["status"] == "CONFORMING"
    assert conformance["trace_check"]["steps_verified"] == 5
    assert set(conformance["trace_check"]["action_coverage"]) == {"approve", "claim_a", "start", "finish", "rate_429"}
    assert len(set(conformance["snapshot_checkpoints"])) == 6
    assert conformance["complete_implementation_refinement_proven"] is False
    trace = conformance["trace"]
    assert trace["steps"][3]["state"]["finish_fence"] == 1
    assert trace["steps"][4]["state"]["rate_limited"] is True
