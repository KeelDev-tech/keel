"""Durability, replay, fencing and approval regressions for the trusted host."""
from concurrent.futures import ThreadPoolExecutor
import copy
from datetime import timedelta
import hashlib
import hmac
from pathlib import Path
import sqlite3
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from keel_agent.state import LocalState, StateError
from maintenance_workbench.keel_maint.contracts import canonical, digest
from tools.make_flow_demo import NOW, make_snapshot
from tools.make_assurance_demo import make_envelope
from tools.make_trust_demo import make_document


@pytest.fixture
def state(tmp_path):
    return LocalState(tmp_path / "private" / "state.db", "workspace-test")


def stamp(seconds=0):
    return NOW + timedelta(seconds=seconds)


def signed(state, sequence=1, revision="source-A", seconds=0, body=None):
    flow = make_snapshot()
    flow["source_revision"] = revision
    flow["observed_at"] = stamp(seconds).isoformat()
    return state.sign_snapshot(body or {"flow": flow}, revision, sequence,
                               stamp(seconds), stamp(seconds+90))


def resign(state, envelope):
    row = copy.deepcopy(envelope)
    row["body_sha256"] = digest(row["body"])
    unsigned = {k: v for k, v in row.items() if k != "signature"}
    row["signature"] = hmac.new(state.key_path.read_bytes(), canonical(unsigned), hashlib.sha256).hexdigest()
    return row


def queued(state, *, kind="READ", key="key", payload=None, material="a"*64, now=NOW):
    return state.enqueue(kind, payload or {"task": "inspect"}, idempotency_key=key, material_hash=material, now=now)


def binding(**changes):
    return {"role_id": "role-1", "material_hash": "a"*64, "action": "LOCAL_SUBMIT",
            "destination": "http://127.0.0.1:8790/apply", "account_id": "local-fixture", **changes}


def local_job(state, *, key="local-1", binds=None):
    binds = binds or binding()
    job = queued(state, kind="LOCAL_SUBMIT", key=key, payload=binds, material=binds["material_hash"])
    claimed = state.claim("worker", now=NOW, kinds=["LOCAL_SUBMIT"])
    assert claimed["job_id"] == job["job_id"]
    approval = state.approve(**binds, expires_at=stamp(300), now=NOW)
    return claimed, approval, binds


def receipt(job, outcome="CONFIRMED_LOCAL"):
    return {"outcome": outcome, "external_submission": False, "evidence_ref": "fixture-receipt:1", "evidence_sha256": "c"*64, "job_id": job["job_id"], **binding()}


def test_private_state_and_key_survive_restart(state):
    assert state.path.stat().st_mode & 0o777 == 0o600
    assert state.key_path.stat().st_mode & 0o777 == 0o600
    assert state.path.parent.stat().st_mode & 0o777 == 0o700
    original_key = state.key_path.read_bytes()
    snap = signed(state)
    state.import_snapshot(snap, now=NOW)
    restarted = LocalState(state.path, "workspace-test")
    assert restarted.key_path.read_bytes() == original_key
    assert restarted.latest_snapshot(now=NOW) == snap
    with pytest.raises(StateError, match="workspace"):
        LocalState(state.path, "different-workspace")


def test_missing_or_replaced_key_never_silently_reprovisions(state):
    original = state.key_path.read_bytes()
    state.key_path.unlink()
    with pytest.raises(StateError, match="no host key"):
        LocalState(state.path, "workspace-test")
    state.key_path.write_bytes(b"x"*32)
    state.key_path.chmod(0o600)
    with pytest.raises(StateError, match="key_id"):
        LocalState(state.path, "workspace-test")
    with pytest.raises(StateError, match="host key changed"):
        state.events()
    state.key_path.write_bytes(original)
    assert state.events() == []


def test_insecure_existing_key_or_parent_rejected(tmp_path):
    key = tmp_path / "key"
    key.write_bytes(b"x"*32)
    key.chmod(0o644)
    with pytest.raises(StateError, match="0600"):
        LocalState(tmp_path / "db", "test", key_path=key)
    directory = tmp_path / "public"
    directory.mkdir(mode=0o755)
    with pytest.raises(StateError, match="0700"):
        LocalState(directory / "db", "test")


def test_symlinks_rejected(state, tmp_path):
    alias = tmp_path / "alias"
    alias.symlink_to(state.path)
    with pytest.raises(StateError, match="symlink"):
        LocalState(alias, "workspace-test", key_path=state.key_path)


def test_import_is_readonly_to_source_and_preserves_blockers(state):
    snap = signed(state)
    before = copy.deepcopy(snap)
    result = state.import_snapshot(snap, now=NOW)
    assert snap == before
    assert result["flow_report"]["readiness"]["nominal_ready"] == 5
    assert result["flow_report"]["readiness"]["executable_ready"] == 1
    assert result["execution_authorized"] is False
    assert state.events()[0]["event_type"] == "SNAPSHOT_IMPORTED"


def test_optional_assurance_is_schema_checked_and_binds_actual_flow(state):
    flow = make_snapshot()
    assurance = make_envelope(flow)
    envelope = state.sign_snapshot({"flow": flow, "assurance": assurance}, flow["source_revision"],
                                   1, NOW, stamp(90))
    assert state.import_snapshot(envelope, now=NOW)["body"]["assurance"] == assurance
    bad = copy.deepcopy(envelope)
    bad["sequence"] = 2
    bad["previous_sha256"] = digest(envelope)
    bad["body"]["assurance"]["export_sha256"] = "b"*64
    with pytest.raises(StateError, match="canonical export binding"):
        state.import_snapshot(resign(state, bad), now=NOW)


def test_optional_trust_is_workspace_and_flow_bound(tmp_path):
    state = LocalState(tmp_path / "private" / "trust.db", "keel-demo")
    trust = make_document()
    flow = trust["flow_export"]
    envelope = state.sign_snapshot({"flow": flow, "trust": trust}, flow["source_revision"], 1, NOW, stamp(90))
    assert state.import_snapshot(envelope, now=NOW)["body"]["trust"] == trust
    bad = copy.deepcopy(envelope)
    bad["sequence"] = 2
    bad["previous_sha256"] = digest(envelope)
    bad["body"]["trust"]["workspace_id"] = "other"
    with pytest.raises(StateError, match="trust workspace"):
        state.import_snapshot(resign(state, bad), now=NOW)


def test_snapshot_replay_and_same_sequence_digest_conflict(state):
    first = signed(state)
    state.import_snapshot(first, now=NOW)
    with pytest.raises(StateError, match="replay"):
        state.import_snapshot(first, now=NOW)
    changed = copy.deepcopy(first)
    changed["body"]["flow"]["active"] = False
    with pytest.raises(StateError, match="digest conflict"):
        state.import_snapshot(resign(state, changed), now=NOW)
    assert len(state.events()) == 1


@pytest.mark.parametrize("field,value", [("workspace_id", "other"), ("key_id", "f"*64),
    ("source_revision", "fake"), ("sequence", 50), ("expires_at", "2099-01-01T00:00:00+00:00")])
def test_signed_envelope_tampering_rejected(state, field, value):
    snap = signed(state)
    snap[field] = value
    with pytest.raises(StateError):
        state.import_snapshot(snap, now=NOW)
    assert state.events() == []


@pytest.mark.parametrize("delta", [-1, 90, 100])
def test_snapshot_expired_or_future_rejected(state, delta):
    snap = signed(state)
    with pytest.raises(StateError, match="stale, future"):
        state.import_snapshot(snap, now=stamp(delta))
    assert state.events() == []


def test_latest_snapshot_rechecks_expiry_and_clock(state):
    state.import_snapshot(signed(state), now=NOW)
    with pytest.raises(StateError, match="stale, future"):
        state.latest_snapshot(now=stamp(90))
    with pytest.raises(StateError, match="clock rollback"):
        state.latest_snapshot(now=stamp(-1))


@pytest.mark.parametrize("field", ["complete", "attempt_history_complete"])
def test_incomplete_canonical_history_cannot_be_admitted(state, field):
    snap = signed(state)
    snap["body"]["flow"][field] = False
    with pytest.raises(StateError, match="complete canonical"):
        state.import_snapshot(resign(state, snap), now=NOW)


def test_snapshot_sequence_and_predecessor_history_required(state):
    with pytest.raises(StateError, match="missing sequence history"):
        state.import_snapshot(signed(state, sequence=3), now=NOW)
    first = signed(state)
    second_before_first = signed(state, sequence=2)
    state.import_snapshot(first, now=NOW)
    with pytest.raises(StateError, match="predecessor"):
        state.import_snapshot(second_before_first, now=NOW)
    state.import_snapshot(signed(state, sequence=2), now=NOW)
    assert [row["sequence"] for row in state.events()] == [1, 2]


def test_superseded_source_revision_cannot_return(state):
    state.import_snapshot(signed(state), now=NOW)
    state.import_snapshot(signed(state, sequence=2, revision="source-B"), now=NOW)
    with pytest.raises(StateError, match="source revision rollback"):
        state.import_snapshot(signed(state, sequence=3, revision="source-A"), now=NOW)


def test_concurrent_snapshot_admission_has_single_winner(state):
    snap = signed(state)
    def attempt(_):
        try:
            return state.import_snapshot(snap, now=NOW)
        except StateError:
            return None
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(attempt, range(8)))
    assert sum(row is not None for row in results) == 1
    assert len(state.events()) == 1


def test_job_enqueue_idempotent_across_restart_and_conflict_rejected(state):
    first = queued(state)
    assert queued(LocalState(state.path, state.workspace_id))["job_id"] == first["job_id"]
    with pytest.raises(StateError, match="idempotency"):
        queued(state, payload={"task": "changed"})
    with pytest.raises(StateError, match="idempotency"):
        queued(state, material="b"*64)
    assert len(state.events()) == 1


@pytest.mark.parametrize("kind", ["SHELL", "EXEC", "SUBMIT", "SEND_EMAIL", "RUN_CODE", "read"])
def test_job_kinds_are_exact_deterministic_allowlist(state, kind):
    with pytest.raises(StateError, match="allowlisted"):
        queued(state, kind=kind)


def test_concurrent_claim_has_single_lease(state):
    queued(state)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda i: state.claim(f"worker-{i}", now=NOW), range(8)))
    assert sum(row is not None for row in results) == 1
    assert len(state.events()) == 2


@pytest.mark.parametrize("kind", ["READ", "REVIEW", "PREPARE", "SIMULATE"])
def test_safe_read_jobs_reclaim_expired_lease_with_monotonic_fence(state, kind):
    queued(state, kind=kind)
    first = state.claim("worker-A", now=NOW, lease_seconds=5)
    assert state.claim("worker-B", now=stamp(4)) is None
    second = LocalState(state.path, state.workspace_id).claim("worker-B", now=stamp(5))
    assert second["token"] == first["token"] + 1
    assert second["job_id"] == first["job_id"]
    with pytest.raises(StateError, match="fenced"):
        state.complete(first["job_id"], first["token"], {}, now=stamp(5))
    with pytest.raises(StateError, match="fenced"):
        state.heartbeat(first["job_id"], first["token"], now=stamp(5))
    assert state.complete(second["job_id"], second["token"], {"ok": True}, now=stamp(5))["state"] == "COMPLETED"


def test_expired_lease_cannot_be_renewed_or_completed(state):
    queued(state)
    job = state.claim("worker", now=NOW, lease_seconds=1)
    for call in [lambda: state.heartbeat(job["job_id"], job["token"], now=stamp(1)),
                 lambda: state.fail(job["job_id"], job["token"], "late", now=stamp(1)),
                 lambda: state.complete(job["job_id"], job["token"], {}, now=stamp(1))]:
        with pytest.raises(StateError, match="expired"):
            call()


def test_job_failure_does_not_automatically_retry(state):
    queued(state)
    job = state.claim("worker", now=NOW)
    failed = state.fail(job["job_id"], job["token"], "observed failure", now=NOW)
    assert failed["state"] == "FAILED"
    assert state.claim("worker", now=stamp(100)) is None


@pytest.mark.parametrize("field,value", [("role_id", "other"), ("material_hash", "b"*64),
    ("action", "PREPARE"), ("destination", "http://127.0.0.1:8791/apply"), ("account_id", "other")])
def test_approval_scope_changes_do_not_consume_original(state, field, value):
    job, approval, binds = local_job(state)
    changed = {**binds, field: value}
    with pytest.raises(StateError):
        state.admit_local_submit(job["job_id"], job["token"], approval, changed, now=NOW)
    assert state.admit_local_submit(job["job_id"], job["token"], approval, binds, now=NOW)["state"] == "UNKNOWN"


def test_revoked_and_expired_approval_cannot_dispatch(state):
    job, approval, binds = local_job(state)
    state.revoke_approval(approval, now=NOW)
    with pytest.raises(StateError, match="revoked"):
        state.admit_local_submit(job["job_id"], job["token"], approval, binds, now=NOW)
    expires = state.approve(**binds, now=NOW, expires_at=stamp(1))
    with pytest.raises(StateError, match="expired"):
        state.admit_local_submit(job["job_id"], job["token"], expires, binds, now=stamp(1))
    assert state.get_job(job["job_id"])["state"] == "RUNNING"


def test_duplicate_role_blocks_different_material_and_destination_atomically(state):
    first, approval, binds = local_job(state)
    state.admit_local_submit(first["job_id"], first["token"], approval, binds, now=NOW)
    changed = binding(material_hash="b"*64, destination="http://127.0.0.1:8999/apply")
    other, other_approval, _ = local_job(state, key="other", binds=changed)
    with pytest.raises(StateError, match="already dispatched"):
        state.admit_local_submit(other["job_id"], other["token"], other_approval, changed, now=NOW)
    assert state.get_job(other["job_id"])["state"] == "RUNNING"
    with sqlite3.connect(state.path) as db:
        assert db.execute("SELECT consumed_at FROM approvals WHERE approval_id=?", (other_approval,)).fetchone()[0] is None


def test_concurrent_approval_consumption_only_one_local_dispatch(state):
    job, approval, binds = local_job(state)
    def attempt(_):
        try:
            return state.admit_local_submit(job["job_id"], job["token"], approval, binds, now=NOW)
        except StateError:
            return None
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(attempt, range(8)))
    assert sum(row is not None for row in results) == 1
    assert sum(e["event_type"] == "APPROVAL_CONSUMED" for e in state.events()) == 1


def test_dispatched_crash_remains_unknown_across_restart_and_lease_expiry(state):
    job, approval, binds = local_job(state)
    state.admit_local_submit(job["job_id"], job["token"], approval, binds, now=NOW)
    restarted = LocalState(state.path, state.workspace_id)
    assert restarted.claim("replacement", now=stamp(61)) is None
    assert restarted.get_job(job["job_id"])["state"] == "UNKNOWN"
    with pytest.raises(StateError):
        restarted.admit_local_submit(job["job_id"], job["token"], approval, binds, now=stamp(61))
    assert restarted.reconcile_local(job["job_id"], receipt(job), "CONFIRMED_LOCAL", now=stamp(61))["state"] == "CONFIRMED_LOCAL"


def test_crash_before_local_admission_is_reclaimable_with_fencing(state):
    job, approval, binds = local_job(state)
    restarted = LocalState(state.path, state.workspace_id)
    replacement = restarted.claim("replacement", now=stamp(61))
    assert replacement["job_id"] == job["job_id"]
    assert replacement["token"] == job["token"] + 1
    assert replacement["dispatched_at"] is None
    with pytest.raises(StateError, match="fenced"):
        restarted.admit_local_submit(job["job_id"], job["token"], approval, binds, now=stamp(61))
    assert restarted.admit_local_submit(replacement["job_id"], replacement["token"], approval, binds,
                                        now=stamp(61))["state"] == "UNKNOWN"


@pytest.mark.parametrize("field,value", [("job_id", "other-job"), ("role_id", "other-role"),
    ("material_hash", "b"*64), ("account_id", "other-account"), ("destination", "different"),
    ("action", "PREPARE"), ("external_submission", True), ("evidence_sha256", "invalid")])
def test_wrong_receipt_cannot_confirm_or_reconcile(state, field, value):
    job, approval, binds = local_job(state)
    state.admit_local_submit(job["job_id"], job["token"], approval, binds, now=NOW)
    wrong = {**receipt(job), field: value}
    with pytest.raises(StateError):
        state.complete(job["job_id"], job["token"], wrong, now=NOW)
    with pytest.raises(StateError):
        state.reconcile_local(job["job_id"], wrong, "CONFIRMED_LOCAL", now=NOW)
    assert state.get_job(job["job_id"])["state"] == "UNKNOWN"


def test_stored_snapshot_corruption_does_not_become_trusted(state):
    snap = signed(state)
    state.import_snapshot(snap, now=NOW)
    corrupt = copy.deepcopy(snap)
    corrupt["body"]["flow"]["active"] = False
    with sqlite3.connect(state.path) as db:
        db.execute("UPDATE snapshots SET envelope=? WHERE sequence=1", (canonical(corrupt).decode(),))
    with pytest.raises(StateError, match="signature"):
        state.latest_snapshot(now=NOW)


def test_local_submission_cannot_complete_without_consumed_approval(state):
    job, approval, binds = local_job(state)
    with pytest.raises(StateError, match="not admitted"):
        state.complete(job["job_id"], job["token"], receipt(job), now=NOW)
    state.admit_local_submit(job["job_id"], job["token"], approval, binds, now=NOW)
    assert state.complete(job["job_id"], job["token"], receipt(job), now=NOW)["state"] == "CONFIRMED_LOCAL"


def test_not_sent_reconciliation_allows_new_approval_but_never_reuses_old(state):
    job, approval, binds = local_job(state)
    state.admit_local_submit(job["job_id"], job["token"], approval, binds, now=NOW)
    state.reconcile_local(job["job_id"], receipt(job, "NOT_SENT"), "NOT_SENT", now=NOW)
    second, new_approval, binds = local_job(state, key="new")
    with pytest.raises(StateError, match="consumed"):
        state.admit_local_submit(second["job_id"], second["token"], approval, binds, now=NOW)
    assert state.admit_local_submit(second["job_id"], second["token"], new_approval, binds, now=NOW)["state"] == "UNKNOWN"


def test_failure_after_dispatch_does_not_release_role_or_approval(state):
    job, approval, binds = local_job(state)
    state.admit_local_submit(job["job_id"], job["token"], approval, binds, now=NOW)
    assert state.fail(job["job_id"], job["token"], "timeout", now=NOW)["state"] == "UNKNOWN"
    assert state.claim("replacement", now=stamp(500)) is None


def test_prepare_approval_one_use_and_submit_cannot_bypass_atomic_admission(state):
    binds = binding(action="PREPARE")
    approval = state.approve(**binds, now=NOW, expires_at=stamp(100))
    assert state.consume_approval(approval, binds, now=NOW)["consumed"]
    with pytest.raises(StateError, match="consumed"):
        state.consume_approval(approval, binds, now=NOW)
    with pytest.raises(StateError, match="atomic"):
        state.consume_approval(approval, binding(), now=NOW)


def test_event_outbox_stable_ids_monotonic_cursor_and_rollback_atomicity(state):
    queued(state)
    state.claim("worker", now=NOW)
    first = state.events(limit=1)
    remainder = state.events(after=first[0]["sequence"])
    assert first[0]["sequence"] < remainder[0]["sequence"]
    assert first[0]["event_id"] != remainder[0]["event_id"]
    assert first + remainder == LocalState(state.path, state.workspace_id).events()
    before = state.events()
    with pytest.raises(StateError):
        state.complete("unknown", 1, {}, now=NOW)
    assert state.events() == before


def test_host_clock_rollback_cannot_extend_approval_or_lease(state):
    queued(state, now=stamp(10))
    with pytest.raises(StateError, match="clock rollback"):
        state.claim("worker", now=NOW)
    with pytest.raises(StateError, match="clock rollback"):
        state.approve(**binding(), now=NOW, expires_at=stamp(100))


@pytest.mark.parametrize("value", [float("nan"), float("inf"), True, "2026-09-18T12:00:00"])
def test_untrusted_clock_forms_rejected(state, value):
    with pytest.raises(StateError):
        queued(state, now=value)
