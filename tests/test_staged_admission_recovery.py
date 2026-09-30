"""Staged plans must recheck current queue, scoped packet and ownership."""
import copy
import importlib.util
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engines"))
import batch_staged_launches as batch
import launch_lock
import queue_io
import ready_gate

spec = importlib.util.spec_from_file_location(
    "staged_preflight_recovery", Path(batch.BASE) / "staged-launch-preflight.py")
preflight = importlib.util.module_from_spec(spec)
spec.loader.exec_module(preflight)


@pytest.fixture
def staged_fixture(tmp_path, monkeypatch):
    data = tmp_path / "data"
    queues = data / "queues"
    packets = data / "launch-packets"
    buffer = packets / "buffer"
    queues.mkdir(parents=True)
    buffer.mkdir(parents=True)
    resume = tmp_path / "resume.txt"
    resume.write_text("Synthetic prepared resume")
    bank = {"answers": {"name": {"value": "Synthetic Applicant", "scope": "global",
                                 "source": "applicant supplied"}},
            "gates": {}, "banded_questions": {}}
    bank_path = data / "answer_bank.json"
    bank_path.write_text(json.dumps(bank))
    current = {"role_id": "role-1", "company": "Synthetic Employer", "title": "Operations Manager",
               "status": "READY", "action_band": "APPLY", "fit_score": 75,
               "ats_url": "https://jobs.lever.co/synthetic/exact-job-id",
               "materials": {"resume": str(resume)}}
    standard = queues / "standard-queue.json"
    standard.write_text(json.dumps({"entries": [current]}))
    (queues / "needs_input-queue.json").write_text("[]")
    (queues / "strategic-queue.json").write_text("[]")
    (queues / "rejected-queue.json").write_text("[]")
    (data / "employer-blocklist.md").write_text("# Blocked employers\n")
    packet = {**{key: current[key] for key in ("role_id", "company", "title", "ats_url")},
              "brief": "Prepared application for Synthetic Applicant",
              "upload_files": [str(resume)], "scope": "application",
              "execution_authorized": True}
    ready_gate.seal_packet(packet, current, bank, workspace=str(tmp_path))
    packet_path = buffer / "role-1.json"
    packet_path.write_text(json.dumps(packet))
    staged = {"role_id": "role-1", "company": current["company"], "title": current["title"],
              "status": "STAGED", "packet_path": str(packet_path)}
    staged_path = data / ".state" / "staged-launches.json"
    staged_path.parent.mkdir()
    staged_path.write_text(json.dumps({"entries": [staged]}))
    ledger = data / "application-ledger.json"
    ledger.write_text("[]")
    locks = tmp_path / "leases"
    locks.mkdir()
    monkeypatch.setattr(launch_lock, "LOCK_DIR", str(locks))
    monkeypatch.setattr(launch_lock, "LEDGER_PATH", str(ledger))
    monkeypatch.setattr(launch_lock, "_telemetry_submitted_match", lambda *_: None)
    monkeypatch.setattr(queue_io, "_LOCK_PATH", str(tmp_path / "queue.lock"))
    for module in (batch, preflight):
        monkeypatch.setattr(module, "HOME", str(tmp_path))
        monkeypatch.setattr(module, "DATA", str(data))
        monkeypatch.setattr(module, "PACKET_DIR", str(packets))
        monkeypatch.setattr(module, "STAGED_FILE", str(staged_path))
        monkeypatch.setattr(module, "MAXMODE_FILE", str(data / ".state" / "max-mode.json"))
    monkeypatch.setattr(preflight, "BACKUP_DIR", str(data / ".state" / "staged-backups"))
    monkeypatch.setattr(preflight, "_budget_ok", lambda _: (True, "budget available"))
    monkeypatch.setattr(preflight, "_screen_packet", lambda *_: {"verdict": "CLEAN", "reasons": []})
    return {"workspace": tmp_path, "data": data, "current": current, "staged": staged,
            "standard": standard, "packet": packet, "packet_path": packet_path,
            "bank": bank, "bank_path": bank_path, "resume": resume,
            "staged_path": staged_path, "ledger": ledger, "locks": locks}


def outcome(fixture):
    planned, skipped = batch.plan_batch([fixture["staged"]], 4)
    verdict, reasons = preflight.check_entry(fixture["staged"], datetime.now(timezone.utc))
    return planned, skipped, verdict, reasons


def rewrite_current(fixture, changes=None, *, entries=None):
    rows = entries if entries is not None else [{**fixture["current"], **(changes or {})}]
    fixture["standard"].write_text(json.dumps({"entries": rows}))


def test_current_scoped_buffer_packet_is_plannable_without_leases(staged_fixture):
    planned, skipped, verdict, reasons = outcome(staged_fixture)
    assert [item["role_id"] for item in planned] == ["role-1"]
    assert planned[0]["packet"] == str(staged_fixture["packet_path"])
    assert skipped == []
    assert verdict == "FIRE", reasons
    assert list(staged_fixture["locks"].iterdir()) == []


@pytest.mark.parametrize("changes", [
    {"status": "SUBMITTED"}, {"status": "IN-FLIGHT"},
    {"status": "PARKED-NEEDS-INPUT"}, {"fit_score": 74},
    {"d1_office_exclusion": True}, {"human_hold": True},
    {"unresolved": ["Applicant response required"]}, {"action_band": "PARKED"},
])
def test_staged_copy_cannot_override_current_queue_gate(staged_fixture, changes):
    rewrite_current(staged_fixture, changes)
    planned, skipped, verdict, _ = outcome(staged_fixture)
    assert planned == []
    assert skipped
    assert verdict == "STALE-ADMISSION"


@pytest.mark.parametrize("case", ["missing", "same_queue_duplicate", "cross_queue_duplicate", "rejected_duplicate", "malformed"])
def test_one_current_queue_home_is_required(staged_fixture, case):
    if case == "missing":
        rewrite_current(staged_fixture, entries=[])
    elif case == "same_queue_duplicate":
        rewrite_current(staged_fixture, entries=[staged_fixture["current"], staged_fixture["current"]])
    elif case in ("cross_queue_duplicate", "rejected_duplicate"):
        home = "needs_input" if case == "cross_queue_duplicate" else "rejected"
        (staged_fixture["data"] / "queues" / (home + "-queue.json")).write_text(
            json.dumps([staged_fixture["current"]]))
    else:
        staged_fixture["standard"].write_text('{"entries": [], "items": []}')
    planned, skipped, verdict, _ = outcome(staged_fixture)
    assert planned == [] and skipped
    assert verdict == "STALE-ADMISSION"


@pytest.mark.parametrize("home", ["needs_input", "rejected"])
def test_ready_label_in_non_admission_home_cannot_launch(staged_fixture, home):
    rewrite_current(staged_fixture, entries=[])
    (staged_fixture["data"] / "queues" / (home+"-queue.json")).write_text(
        json.dumps([staged_fixture["current"]]))
    planned, skipped, verdict, reasons = outcome(staged_fixture)
    assert planned == [] and skipped
    assert verdict == "STALE-ADMISSION"
    assert "not an admission queue" in "; ".join(reasons)


@pytest.mark.parametrize("home", ["standard", "needs_input", "strategic", "rejected"])
def test_missing_canonical_queue_refuses_uniqueness_claim(staged_fixture, home):
    (staged_fixture["data"] / "queues" / (home+"-queue.json")).unlink()
    planned, skipped, verdict, _ = outcome(staged_fixture)
    assert planned == [] and skipped
    assert verdict == "STALE-ADMISSION"


@pytest.mark.parametrize("case", ["no_manifest", "changed_answer", "changed_material", "changed_context",
                                      "tampered_packet", "expired_manifest", "review_only"])
def test_manifest_and_authority_changes_block_staged_launch(staged_fixture, case):
    packet = copy.deepcopy(staged_fixture["packet"])
    if case == "changed_answer":
        bank = copy.deepcopy(staged_fixture["bank"])
        bank["answers"]["name"]["scope"] = "employer:Other Employer"
        staged_fixture["bank_path"].write_text(json.dumps(bank))
    elif case == "changed_material":
        staged_fixture["resume"].write_text("Changed resume bytes")
    elif case == "changed_context":
        rewrite_current(staged_fixture, {"company": "Changed Employer"})
    elif case == "no_manifest":
        packet.pop("ready_manifest")
    elif case == "tampered_packet":
        packet["brief"] += " extra text"
    elif case == "expired_manifest":
        ready_gate.seal_packet(packet, staged_fixture["current"], staged_fixture["bank"],
                               now=datetime.now(timezone.utc)-timedelta(hours=13),
                               workspace=str(staged_fixture["workspace"]))
    else:
        packet["scope"] = "preparation_only"
        packet["execution_authorized"] = False
        ready_gate.seal_packet(packet, staged_fixture["current"], staged_fixture["bank"],
                               workspace=str(staged_fixture["workspace"]))
    staged_fixture["packet_path"].write_text(json.dumps(packet))
    planned, skipped, verdict, _ = outcome(staged_fixture)
    assert planned == [] and skipped
    assert verdict == "STALE-ADMISSION"


def test_current_manifest_overrules_old_filesystem_mtime(staged_fixture):
    old = (datetime.now(timezone.utc)-timedelta(days=3)).timestamp()
    os.utime(staged_fixture["packet_path"], (old, old))
    assert outcome(staged_fixture)[2] == "FIRE"


def test_packet_path_escape_is_refused(staged_fixture):
    staged_fixture["staged"]["packet_path"] = str(staged_fixture["workspace"] / "resume.txt")
    planned, skipped, verdict, _ = outcome(staged_fixture)
    assert planned == [] and skipped
    assert verdict == "STALE-ADMISSION"


def test_revive_annotation_requires_explicit_reverification(staged_fixture):
    staged_fixture["staged"].update(fireable=False, reverify_required=True)
    assert outcome(staged_fixture)[0] == []
    assert outcome(staged_fixture)[2] == "STALE-ADMISSION"


def test_live_lease_blocks_plans_without_releasing_owner(staged_fixture):
    launch_lock.acquire("role-1", "live-owner", "synthetic")
    before = launch_lock._read_lock("role-1")
    planned, skipped, verdict, _ = outcome(staged_fixture)
    assert planned == [] and skipped
    assert verdict == "DEFERRED-GUARD"
    assert launch_lock._read_lock("role-1") == before


def test_deferred_live_owner_is_rechecked_after_release(staged_fixture):
    launch_lock.acquire("role-1", "live-owner", "synthetic")
    staged_fixture["staged"]["status"] = "DEFERRED-GUARD"
    assert outcome(staged_fixture)[2] == "DEFERRED-GUARD"
    launch_lock.release("role-1", "live-owner")
    assert outcome(staged_fixture)[2] == "FIRE"


def test_budget_deferral_remains_recheckable(staged_fixture, monkeypatch):
    staged_fixture["staged"]["status"] = "DEFERRED-BUDGET"
    monkeypatch.setattr(preflight, "_budget_ok", lambda _: (False, "provider cooldown"))
    assert outcome(staged_fixture)[2] == "DEFERRED-BUDGET"
    monkeypatch.setattr(preflight, "_budget_ok", lambda _: (True, "budget available"))
    assert outcome(staged_fixture)[2] == "FIRE"


@pytest.mark.parametrize("status", ["DEFERRED-GUARD", "DEFERRED-BUDGET"])
def test_full_preflight_rearms_operational_deferral_only(staged_fixture, status):
    staged = {**staged_fixture["staged"], "status": status}
    staged_fixture["staged_path"].write_text(json.dumps({"entries": [staged]}))
    before_queue = staged_fixture["standard"].read_bytes()
    before_packet = staged_fixture["packet_path"].read_bytes()
    assert batch.plan_batch([staged], 4)[0] == []
    assert preflight.main(["--apply"]) == 0
    updated = json.loads(staged_fixture["staged_path"].read_text())["entries"][0]
    assert updated["status"] == "STAGED"
    assert batch.plan_batch([updated], 4)[0]
    assert staged_fixture["standard"].read_bytes() == before_queue
    assert staged_fixture["packet_path"].read_bytes() == before_packet


def test_confirmed_submission_blocks_without_new_lease(staged_fixture):
    staged_fixture["ledger"].write_text(json.dumps([{
        "role_id": "role-1", "status": "SUBMITTED",
        "company": "Synthetic Employer", "title": "Operations Manager"}]))
    planned, skipped, verdict, _ = outcome(staged_fixture)
    assert planned == [] and skipped
    assert verdict == "STALE-ADMISSION"
    assert list(staged_fixture["locks"].iterdir()) == []


@pytest.mark.parametrize("status", ["SUBMITTED", "UNKNOWN_OUTCOME", "SUBMISSION_CLAIMED", "IN-FLIGHT",
                                    "APPLYING", "REJECTED", "DEAD", "CANCELLED"])
@pytest.mark.parametrize("join", ["role", "exact_posting"])
def test_active_or_uncertain_ledger_role_and_posting_hold(staged_fixture, status, join):
    row = {"role_id": "role-1" if join == "role" else "other-role", "status": status,
           "company": "Synthetic Employer", "title": "Operations Manager"}
    if join == "exact_posting":
        row["application_url"] = staged_fixture["current"]["ats_url"] + "/apply?source=fixture"
    staged_fixture["ledger"].write_text(json.dumps([row]))
    planned, skipped, verdict, reasons = outcome(staged_fixture)
    assert planned == [] and skipped
    assert verdict == "STALE-ADMISSION"
    assert status in "; ".join(reasons)
    assert list(staged_fixture["locks"].iterdir()) == []
    if join == "role":
        assert batch._guard_go("role-1", "Synthetic Employer", "Operations Manager")[0] is False


def test_other_role_and_exact_posting_do_not_share_active_ledger_hold(staged_fixture):
    staged_fixture["ledger"].write_text(json.dumps([{
        "role_id": "different-role", "status": "UNKNOWN_OUTCOME", "company": "Other Employer",
        "title": "Different Role", "ats_url": "https://jobs.lever.co/synthetic/different-posting"}]))
    planned, skipped, verdict, _ = outcome(staged_fixture)
    assert planned and not skipped
    assert verdict == "FIRE"


def test_unknown_outcome_twin_is_held_by_shared_identity_policy(staged_fixture):
    staged_fixture["ledger"].write_text(json.dumps([{
        "role_id": "different-role", "status": "UNKNOWN_OUTCOME", "company": "Synthetic Employer",
        "title": "Operations Manager"}]))
    planned, skipped, verdict, _ = outcome(staged_fixture)
    assert planned == [] and skipped
    assert verdict == "STALE-ADMISSION"


def test_missing_execution_authorization_cannot_fire(staged_fixture):
    packet = copy.deepcopy(staged_fixture["packet"])
    packet.pop("execution_authorized")
    ready_gate.seal_packet(packet, staged_fixture["current"], staged_fixture["bank"],
                           workspace=str(staged_fixture["workspace"]))
    staged_fixture["packet_path"].write_text(json.dumps(packet))
    planned, skipped, verdict, _ = outcome(staged_fixture)
    assert planned == [] and skipped
    assert verdict == "STALE-ADMISSION"


@pytest.mark.parametrize("case", ["missing", "corrupt"])
def test_unreadable_canonical_ledger_cannot_authorize_launch(staged_fixture, case):
    if case == "missing":
        staged_fixture["ledger"].unlink()
    else:
        staged_fixture["ledger"].write_text("{malformed")
    planned, skipped, verdict, _ = outcome(staged_fixture)
    assert planned == [] and skipped
    assert verdict == "STALE-ADMISSION"


@pytest.mark.parametrize("format", ["plain", "legacy_bold"])
def test_current_employer_blocklist_is_mandatory(staged_fixture, format):
    content = "# Blocked employers\nSynthetic Employer\n" if format == "plain" else \
        "# Policy\n## Blocked employers\n- **Synthetic Employer** — user hold\n"
    (staged_fixture["data"] / "employer-blocklist.md").write_text(content)
    planned, skipped, verdict, reasons = outcome(staged_fixture)
    assert planned == [] and skipped
    assert verdict == "STALE-ADMISSION"
    assert "blocklist" in "; ".join(reasons).lower()


def test_unknown_prescreen_never_becomes_fire(staged_fixture, monkeypatch):
    monkeypatch.setattr(preflight, "_screen_packet", lambda *_: {"verdict": "UNKNOWN"})
    verdict, _ = preflight.check_entry(staged_fixture["staged"], datetime.now(timezone.utc))
    assert verdict == "STALE-PRESCREEN"


def test_pending_queue_recovery_refuses_without_repair(staged_fixture):
    journal_dir = Path(queue_io.get_lock_path()+".transactions")
    journal_dir.mkdir()
    journal = journal_dir / "pending.json"
    journal.write_text('{"state": "PREPARED"}')
    before = staged_fixture["standard"].read_bytes()
    planned, skipped, verdict, _ = outcome(staged_fixture)
    assert planned == [] and skipped
    assert verdict == "STALE-ADMISSION"
    assert staged_fixture["standard"].read_bytes() == before
    assert journal.exists()


def test_duplicate_staged_rows_never_emit_two_instructions(staged_fixture):
    planned, skipped = batch.plan_batch([staged_fixture["staged"], staged_fixture["staged"]], 4)
    assert planned == []
    assert len(skipped) == 2


@pytest.mark.parametrize("container", ["entries", "staged", "list"])
def test_canonical_and_legacy_staged_envelopes_are_readable(staged_fixture, container, capsys):
    entries = [staged_fixture["staged"]]
    staged_fixture["staged_path"].write_text(json.dumps(entries if container == "list" else {container: entries}))
    before = staged_fixture["staged_path"].read_bytes()
    assert batch.main([]) == 0
    assert preflight.main([]) == 0
    output = capsys.readouterr().out
    assert '"verdict": "FIRE"' in output
    assert staged_fixture["staged_path"].read_bytes() == before
    assert list(staged_fixture["locks"].iterdir()) == []


def test_conflicting_staged_envelopes_fail_closed(staged_fixture):
    staged_fixture["staged_path"].write_text(json.dumps({"entries": [], "staged": [staged_fixture["staged"]]}))
    with pytest.raises(ValueError):
        batch.main([])
    with pytest.raises(ValueError):
        preflight.main([])
