"""Ownership repair regressions; synthetic queues and leases only."""
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Barrier

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "engines"))
import inflight_marker as im
import launch_lock
import queue_io
import submit_intent
import task_liveness as tl


@pytest.mark.parametrize("row,expected", [
    (None, tl.UNKNOWN),
    ({}, tl.UNKNOWN),
    ([], tl.UNKNOWN),
    ({"status": "novel-status"}, tl.UNKNOWN),
    ({"status": "running"}, tl.LIVE),
    ({"status": "needs_user", "terminal_reason": None}, tl.LIVE),
    ({"status": "parked_outcome", "outcome_status": "needs_user"}, tl.LIVE),
    ({"status": "completed", "terminal_reason": None}, tl.TERMINAL),
    ({"status": " FAILED "}, tl.TERMINAL),
    ({"status": "superseded"}, tl.TERMINAL),
    ({"status": "running", "outcome_status": "completed"}, tl.TERMINAL),
    ({"status": "running", "outcome_status": "failed"}, tl.TERMINAL),
    ({"terminal_reason": "closed by operator"}, tl.TERMINAL),
    ({"status": "running", "terminal_reason": ""}, tl.UNKNOWN),
    ({"status": "running", "terminal_reason": False}, tl.UNKNOWN),
    ({"status": "running", "outcome_status": "novel-outcome"}, tl.UNKNOWN),
])
def test_liveness_requires_positive_evidence(row, expected):
    assert tl.classify_task(row) == expected
    assert tl.is_terminal(row) == (expected == tl.TERMINAL)
    assert tl.is_live(row) == (expected == tl.LIVE)


def test_terminal_row_for_wrong_task_is_unknown():
    assert tl.classify_task({"task_id": "other", "status": "completed"},
                            task_id="requested") == tl.UNKNOWN
    assert tl.classify_task({"task_id": "requested", "id": "other",
                             "status": "completed"},
                            task_id="requested") == tl.UNKNOWN


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    lock_dir = tmp_path / "locks"
    lock_dir.mkdir()
    monkeypatch.setattr(launch_lock, "LOCK_DIR", str(lock_dir))
    monkeypatch.setattr(queue_io, "_LOCK_PATH", str(tmp_path / "queue.lock"))
    store = tmp_path / "intents"
    store.mkdir()
    submit_intent.set_store_dir(str(store))
    monkeypatch.setattr(im, "HOME", str(tmp_path))
    data = tmp_path / "data"
    data.mkdir()
    (data / "employer-blocklist.md").write_text("# Blocked employers\n")
    ledger = data / "application-ledger.json"
    ledger.write_text("[]")
    monkeypatch.setattr(launch_lock, "LEDGER_PATH", str(ledger))
    queue = tmp_path / "standard.json"
    queue.write_text(json.dumps([{
        "role_id": "role-1", "status": "READY", "company": "Fixture Company",
        "title": "Fixture Role", "fit_score": 80, "action_band": "APPLY",
        "browser_task_id": "prior-task", "queue_notes": ["prior note"],
    }]))
    im.set_queue_paths({"standard": str(queue)})
    yield queue
    im.set_queue_paths(None)
    submit_intent.reset_store_dir()


def read_role(queue):
    return json.loads(queue.read_text())[0]


@pytest.mark.parametrize("row", [
    {"task_id": "prior-task", "status": "completed"},
    {"task_id": "prior-task", "status": "superseded"},
    {"task_id": "prior-task", "status": "running",
     "outcome_status": "failed"},
    {"task_id": "prior-task", "terminal_reason": "closed"},
])
def test_terminal_stale_owner_clears_and_bookkeeping_transfer_succeeds(
        isolated, row):
    calls = []

    def provider(task_id):
        assert getattr(queue_io._state, "depth", 0) > 0
        calls.append(task_id)
        return row

    claimed = im.mark_inflight("role-1", task_state_provider=provider,
                               attempt_id="attempt-1")
    assert claimed["ok"] is True, claimed
    entry = read_role(isolated)
    assert entry["status"] == "IN-FLIGHT"
    assert entry["browser_task_id"] is None
    assert claimed["browser_task_id"] == entry["browser_task_id"]
    assert calls == ["prior-task"]
    assert entry["claim_stale_owner_repair"]["prior_browser_task_id"] == \
        "prior-task"
    assert "terminal prior browser_task_id cleared" in entry["queue_notes"]
    transferred = im.mark_inflight("role-1", task_id="new-task",
                                   phase="spawn", attempt_id="attempt-1")
    assert transferred["ok"] is True, transferred
    assert transferred["browser_task_id"] == \
        read_role(isolated)["browser_task_id"] == "new-task"
    assert launch_lock.check("role-1")["task_id"] == "new-task"


@pytest.mark.parametrize("row", [
    None,
    {},
    {"task_id": "prior-task", "status": "running"},
    {"task_id": "prior-task", "status": "needs_user"},
    {"task_id": "other-task", "status": "completed"},
    {"task_id": "prior-task", "status": "new-unrecognized-state"},
])
def test_live_or_unknown_prior_task_refuses_without_queue_write(isolated, row):
    before = isolated.read_bytes()
    result = im.mark_inflight("role-1", task_state_provider=lambda _: row)
    assert result["ok"] is False
    assert result["marker_task_id"] == "prior-task"
    assert isolated.read_bytes() == before
    assert launch_lock.check("role-1") is None


def test_no_provider_refuses_existing_task(isolated):
    before = isolated.read_bytes()
    assert im.mark_inflight("role-1")["ok"] is False
    assert isolated.read_bytes() == before
    assert launch_lock.check("role-1") is None


def test_provider_failure_refuses_existing_task(isolated):
    def unavailable(_):
        raise RuntimeError("runtime unavailable")

    result = im.mark_inflight("role-1", task_state_provider=unavailable)
    assert result["ok"] is False
    assert result["task_liveness"] == tl.UNKNOWN
    assert read_role(isolated)["browser_task_id"] == "prior-task"
    assert launch_lock.check("role-1") is None


def test_changed_owner_during_lookup_fails_cas(isolated):
    def provider(_):
        rows = json.loads(isolated.read_text())
        rows[0]["browser_task_id"] = "rival-task"
        queue_io.atomic_write_json(str(isolated), rows)
        return {"task_id": "prior-task", "status": "completed"}

    result = im.mark_inflight("role-1", task_state_provider=provider)
    assert result["ok"] is False
    assert "changed during task lookup" in result["reason"]
    assert read_role(isolated)["browser_task_id"] == "rival-task"
    assert read_role(isolated)["status"] == "READY"
    assert launch_lock.check("role-1") is None


def test_new_ledger_hold_during_runtime_lookup_refuses_fresh_claim(isolated):
    before = isolated.read_bytes()
    def provider(_):
        ledger = isolated.parent / "data/application-ledger.json"
        ledger.write_text(json.dumps([{"role_id": "role-1", "status": "UNKNOWN_OUTCOME"}]))
        return {"task_id": "prior-task", "status": "completed"}
    result = im.mark_inflight("role-1", task_state_provider=provider)
    assert result["ok"] is False
    assert result["reason_codes"] == ["ledger_hold"]
    assert isolated.read_bytes() == before
    assert launch_lock.check("role-1") is None


def test_lookup_cas_retains_unrelated_queue_update(isolated):
    def provider(_):
        rows = json.loads(isolated.read_text())
        rows.append({"role_id": "role-2", "status": "PARKED"})
        queue_io.atomic_write_json(str(isolated), rows)
        return {"task_id": "prior-task", "status": "completed"}

    result = im.mark_inflight("role-1", task_state_provider=provider)
    assert result["ok"] is True
    assert json.loads(isolated.read_text())[1] == {
        "role_id": "role-2", "status": "PARKED"}


def test_failed_repair_write_retains_old_marker_and_releases_lease(
        isolated, monkeypatch):
    before = isolated.read_bytes()

    def disk_full(*args, **kwargs):
        raise OSError(28, "full disk")

    monkeypatch.setattr(im, "_atomic_queue_write", disk_full)
    with pytest.raises(OSError):
        im.mark_inflight("role-1", task_state_provider=lambda _: {
            "task_id": "prior-task", "status": "completed"})
    assert isolated.read_bytes() == before
    assert launch_lock.check("role-1") is None


def test_concurrent_fresh_claimers_have_one_winner(isolated):
    barrier = Barrier(2)

    def claim(owner):
        barrier.wait(timeout=5)
        return im.mark_inflight("role-1", owner=owner,
                                task_state_provider=lambda _: {
                                    "task_id": "prior-task",
                                    "status": "completed"})

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, ["caller-a", "caller-b"]))
    assert sum(bool(row["ok"]) for row in results) == 1
    assert read_role(isolated)["status"] == "IN-FLIGHT"
    assert launch_lock.check("role-1")["task_id"] == "claim:role-1"


def test_exact_attempt_claim_replay_preserves_lease(isolated):
    provider = lambda _: {"task_id": "prior-task", "status": "completed"}
    assert im.mark_inflight("role-1", task_state_provider=provider,
                            attempt_id="attempt-1")["ok"] is True
    result = im.mark_inflight("role-1", attempt_id="attempt-1")
    assert result["ok"] is True
    assert result["note"] == "already marked by us (idempotent)"
    assert launch_lock.check("role-1") is not None
    rival = im.mark_inflight("role-1", attempt_id="attempt-2")
    assert rival["ok"] is False
    assert read_role(isolated)["attempt_id"] == "attempt-1"
    assert launch_lock.check("role-1") is not None


def test_existing_real_task_claim_returns_persisted_task(isolated):
    rows = [{"role_id": "role-1", "status": "IN-FLIGHT",
             "browser_task_id": "same-task", "attempt_id": "attempt-1"}]
    isolated.write_text(json.dumps(rows))
    result = im.mark_inflight("role-1", task_id="same-task")
    assert result["ok"] is True
    assert result["browser_task_id"] == \
        read_role(isolated)["browser_task_id"] == "same-task"


def test_foreign_inflight_marker_refuses_and_releases_new_lease(isolated):
    rows = [{"role_id": "role-1", "status": "IN-FLIGHT",
             "browser_task_id": "foreign-task"}]
    isolated.write_text(json.dumps(rows))
    before = isolated.read_bytes()
    result = im.mark_inflight("role-1", attempt_id="new-attempt")
    assert result["ok"] is False
    assert isolated.read_bytes() == before
    assert launch_lock.check("role-1") is None


def test_spawn_marker_cannot_bypass_ready_stale_owner_repair(isolated):
    before = isolated.read_bytes()
    result = im.mark_inflight("role-1", task_id="new-task", phase="spawn")
    assert result["ok"] is False
    assert result["marker_task_id"] == "prior-task"
    assert isolated.read_bytes() == before
    assert launch_lock.check("role-1") is None


def test_fresh_ready_without_prior_task_needs_no_provider(isolated):
    rows = [{"role_id": "role-1", "status": "READY", "company": "Fixture Company",
             "title": "Fixture Role", "fit_score": 80, "action_band": "APPLY"}]
    isolated.write_text(json.dumps(rows))
    result = im.mark_inflight("role-1")
    assert result["ok"] is True
    assert result["browser_task_id"] == \
        read_role(isolated).get("browser_task_id") is None


def test_fresh_claim_does_not_inherit_terminal_attempt_identity(isolated):
    rows = json.loads(isolated.read_text())
    rows[0]["attempt_id"] = "prior-attempt"
    isolated.write_text(json.dumps(rows))
    result = im.mark_inflight("role-1", task_state_provider=lambda _: {
        "task_id": "prior-task", "status": "completed"})
    assert result["ok"] is True
    entry = read_role(isolated)
    assert entry["attempt_id"] is None
    assert entry["claim_stale_owner_repair"]["prior_attempt_id"] == "prior-attempt"


def test_refusal_does_not_release_preexisting_placeholder(isolated):
    launch_lock.acquire("role-1", "claim:role-1", "existing")
    assert im.mark_inflight("role-1")["ok"] is False
    assert launch_lock.check("role-1")["task_id"] == "claim:role-1"


def test_lifecycle_poller_uses_shared_terminal_predicate():
    import browser_lifecycle_poll as poller

    assert poller.is_terminal({"status": "completed", "terminal_reason": None})
    assert poller.is_terminal({"status": "running", "outcome_status": "failed"})
    assert not poller.is_terminal(None)
    assert not poller.is_terminal({"status": "running"})


def test_lifecycle_seen_state_retains_terminal_evidence_without_submission(
        tmp_path, monkeypatch):
    import browser_lifecycle_poll as poller

    now = datetime.now(timezone.utc)
    ts = now.isoformat()
    queue = [{"role_id": "role-1", "title": "Operations Manager",
              "company": "Example", "status": "IN-FLIGHT",
              "browser_task_id": "task-1", "in_flight_at": ts}]
    task = {"task_id": "task-1",
            "title": "Job Application for Operations Manager at Example",
            "created_at": ts, "status": "completed", "terminal_reason": None}
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text(json.dumps([task]))
    seen = tmp_path / "seen.json"
    events = []
    monkeypatch.setattr(poller, "load_queue", lambda: queue)
    monkeypatch.setattr(poller, "launch_anchors", lambda _: {})
    monkeypatch.setattr(poller.log_event, "log", lambda event, **_: events.append(event))
    result = poller.poll(str(snapshot), dry_run=False, now=now,
                         events_path=str(tmp_path / "events.jsonl"),
                         seen_path=str(seen))
    assert result["n_tasks"] == 1
    assert json.loads(seen.read_text())["task-1"]["task_liveness"] == tl.TERMINAL
    assert events == ["browser_task_started"]
    assert "submitted" not in events
    assert queue[0]["status"] == "IN-FLIGHT"
