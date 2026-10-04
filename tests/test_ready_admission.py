"""Synthetic READY admission regressions; no live queues or HTTP."""
import copy
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engines"))
import apply_loop
import packet_contract
import ready_gate
import stage_ready_launches as stage
import queue_io


NOW = datetime.now(timezone.utc)


@pytest.fixture(autouse=True)
def synthetic_workspace(tmp_path, monkeypatch):
    (tmp_path / "resume.txt").write_text("Synthetic applicant resume")
    data = tmp_path / "data"
    (data / "queues").mkdir(parents=True)
    for name in ("standard", "strategic", "needs_input", "rejected"):
        (data / "queues" / (name+"-queue.json")).write_text("[]")
    (data / "application-ledger.json").write_text("[]")
    (data / "employer-blocklist.md").write_text("# Blocked employers\n")
    for module in (ready_gate, apply_loop, stage):
        monkeypatch.setattr(module, "HOME", str(tmp_path))
    for module in (apply_loop, stage):
        monkeypatch.setattr(module, "DATA", str(data))
    monkeypatch.setattr(apply_loop, "QUEUE", str(data / "queues/standard-queue.json"))
    monkeypatch.setattr(apply_loop, "SQUEUE", str(data / "queues/strategic-queue.json"))
    monkeypatch.setattr(apply_loop, "LEDGER", str(data / "application-ledger.json"))
    monkeypatch.setattr(apply_loop, "BLOCKLIST", str(data / "employer-blocklist.md"))
    monkeypatch.setattr(queue_io, "_LOCK_PATH", str(data / "queue.lock"))


def lead(**changes):
    return {"role_id": "fixture-role", "company": "Fixture", "title": "Role",
            "status": "READY", "action_band": "APPLY", "fit_score": 75,
            "ats_url": "https://jobs.lever.co/fixture/exact-job-id",
            "materials": {"resume": "resume.txt"}, **changes}


def bank():
    return {"answers": {
        "name": {"value": "Fixture Applicant", "scope": "global", "source": "applicant words"},
        "current_company_name": {"value": "Private Employer", "scope": "employer:Other"},
        "role_date": {"value": "2026-01-01", "scope": "role_id fixture-role only"},
        "old_fact": "unresolved legacy value",
    }, "gates": {}, "banded_questions": {}}


def packet(entry, answers=None):
    answer_bank = bank() if answers is None else answers
    result = {"role_id": entry["role_id"], "company": entry["company"],
        "title": entry["title"], "ats_url": entry["ats_url"],
        "brief": apply_loop.build_generic_brief(entry, None, answer_bank),
        "form_intel": {"ats": "fixture", "source_url": entry["ats_url"],
                       "questions": [], "rendered_option_fetch_needed": [],
                       "extraction_complete": True},
        "form_intel_complete": True,
        "posting_text": "Synthetic posting evidence for ready-gate tests.",
        "posting_text_complete": True,
        "posting_text_url": entry["ats_url"], "posting_text_source": "synthetic test fixture",
        "upload_files": [str(Path(ready_gate.HOME) / "resume.txt")],
        "scope": "application", "execution_authorized": True}
    return ready_gate.seal_packet(result, entry, answer_bank)


@pytest.mark.parametrize("score", [74, None, True, float("nan"), float("inf"), "75"])
def test_below_or_unknown_fit_cannot_be_admitted(score):
    assert not ready_gate.entry_admission(lead(fit_score=score))["allowed"]


def test_fit75_still_respects_durable_office_exclusion():
    result = ready_gate.entry_admission(lead(d1_office_exclusion=True,
        queue_notes="READY, verified live; hybrid likely three days"))
    assert not result["allowed"]
    assert "explicit_hold:d1_office_exclusion" in result["reason_codes"]


@pytest.mark.parametrize("field", ready_gate.HOLD_FIELDS)
def test_explicit_holds_and_applicant_only_flags_are_preserved(field):
    entry = lead(**{field: ["held"]})
    before = copy.deepcopy(entry)
    assert not ready_gate.entry_admission(entry)["allowed"]
    assert entry == before


@pytest.mark.parametrize("field", ready_gate.QUESTION_FIELDS)
def test_genuine_unanswered_questions_are_not_cleared(field):
    entry = lead(**{field: ["Can you commit to relocation?"]})
    before = copy.deepcopy(entry)
    assert not ready_gate.entry_admission(entry)["allowed"]
    assert entry == before


def test_historical_needs_input_note_is_not_an_unanswered_question():
    assert ready_gate.entry_admission(lead(queue_notes="needs_input was historical", unresolved=[]))["allowed"]


def test_unchanged_eligible_case_is_allowed_without_invented_policy_facts():
    assert ready_gate.entry_admission(lead())["allowed"]
    assert ready_gate.entry_admission(lead(action_band="STRATEGIC-VERIFIED"), "strategic")["allowed"]


def test_present_unknown_posting_evidence_fails_closed():
    assert not ready_gate.entry_admission(lead(posting_verification={"verdict": "live"}))["allowed"]


def test_latest_failed_attempt_blocks_preserved_live_observation():
    entry = lead(posting_verification={"verdict": "live", "identity": ["lever", "fixture", "exact-job-id"],
        "observed_at": NOW.isoformat()}, verification_attempt={"signal": "NONE",
        "identity": ["lever", "fixture", "exact-job-id"], "observed_at": NOW.isoformat()})
    assert not ready_gate.entry_admission(entry, now=NOW)["allowed"]


def test_scoped_values_never_enter_another_employers_brief():
    brief = apply_loop.build_generic_brief(lead(), None, bank())
    assert "Private Employer" not in brief
    assert "unresolved legacy value" not in brief
    assert "Fixture Applicant" in brief
    assert "2026-01-01" in brief
    assert "2026-01-01" not in apply_loop.build_generic_brief(lead(role_id="other-role"), None, bank())


def test_scalar_value_requires_current_applicant_receipt():
    answers = {"answers": {"email": "fixture@fixture.invalid"}, "_provenance": {}}
    assert "fixture@fixture.invalid" not in apply_loop.build_generic_brief(lead(), None, answers)
    answers["_provenance"]["email"] = packet_contract.answer_receipt("fixture@fixture.invalid", "applicant own words")
    assert "fixture@fixture.invalid" in apply_loop.build_generic_brief(lead(), None, answers)


@pytest.mark.parametrize("rule", ["Ask the applicant for the exact value.",
                                 {"rule": "Ask the applicant for the exact value.", "approved": True}])
def test_banded_rules_are_reference_not_applicant_approval(rule):
    answers = bank()
    answers["banded_questions"] = {"synthetic_rule": rule}
    before = copy.deepcopy(answers)
    brief = apply_loop.build_generic_brief(lead(), None, answers)
    assert "pre-approved" not in brief
    assert "apply without improvising" not in brief
    assert "BANDED-QUESTION RULES (unverified reference; applicant review required):" in brief
    assert "These rules are not evidence of applicant approval" in brief
    assert "Ask the applicant for the exact value." in brief
    assert "PREPARATION ONLY" in brief
    assert "Fixture Applicant" in brief  # scoped applicant evidence remains available
    assert answers == before


def test_unchanged_packet_stays_admissible_without_raw_values_in_manifest():
    entry = lead(); pkt = packet(entry)
    assert ready_gate.packet_admission(pkt, entry, bank())["allowed"]
    manifest = json.dumps(pkt["ready_manifest"])
    assert "Fixture Applicant" not in manifest
    assert "Private Employer" not in manifest


@pytest.mark.parametrize("change", ["scope", "value", "source", "expiry", "context", "brief", "rules"])
def test_context_or_authority_changes_invalidate_buffered_packet(change):
    entry = lead(); answers = bank(); pkt = packet(entry, answers)
    if change == "context": entry["company"] = "Different"
    elif change == "brief": pkt["brief"] += " injected"
    elif change == "rules": answers["gates"]["new_hold"] = "STOP"
    elif change == "expiry": answers["answers"]["name"]["expires_at"] = "2000-01-01T00:00:00+00:00"
    else: answers["answers"]["name"][change] = "changed"
    assert not ready_gate.packet_admission(pkt, entry, answers)["allowed"]


def test_legacy_packet_without_manifest_or_with_wrong_target_never_counts():
    entry = lead(); pkt = packet(entry)
    del pkt["ready_manifest"]
    assert not ready_gate.packet_admission(pkt, entry, bank())["allowed"]
    pkt = packet(entry); pkt["role_id"] = "another-role"
    assert not ready_gate.packet_admission(pkt, entry, bank())["allowed"]


def test_review_only_packet_is_not_executable_ready_supply():
    entry = lead(); pkt = packet(entry)
    pkt["scope"] = "preparation_only"; pkt["execution_authorized"] = False
    ready_gate.seal_packet(pkt, entry, bank())
    assert "review_only_packet" in ready_gate.packet_admission(pkt, entry, bank())["reason_codes"]
    assert ready_gate.packet_admission(pkt, entry, bank(), for_execution=False)["allowed"]


@pytest.mark.parametrize("authority", [None, "true", 1, {}, False])
def test_absent_or_nonboolean_authority_never_authorizes_a_packet(authority):
    entry = lead(); pkt = packet(entry)
    if authority is None: pkt.pop("execution_authorized")
    else: pkt["execution_authorized"] = authority
    ready_gate.seal_packet(pkt, entry, bank())
    assert "review_only_packet" in ready_gate.packet_admission(pkt, entry, bank())["reason_codes"]


def test_expired_or_future_packet_never_counts():
    entry = lead(); pkt = packet(entry)
    assert not ready_gate.packet_admission(pkt, entry, bank(), now=NOW+timedelta(hours=13))["allowed"]
    assert not ready_gate.packet_admission(pkt, entry, bank(), now=NOW-timedelta(hours=1))["allowed"]


def test_material_bytes_changes_invalidate_packet():
    entry = lead(); pkt = packet(entry)
    Path(pkt["upload_files"][0]).write_text("Changed resume")
    assert "materials_changed" in ready_gate.packet_admission(pkt, entry, bank())["reason_codes"]


def test_packet_cannot_point_materials_outside_workspace():
    entry = lead(); pkt = packet(entry)
    pkt["upload_files"] = ["/etc/passwd"]
    assert not ready_gate.packet_admission(pkt, entry, bank())["allowed"]


def test_eligible_holds_precede_materials_and_network(monkeypatch):
    def forbidden(*args): raise AssertionError("blocked lead reached HTTP or material check")
    monkeypatch.setattr(apply_loop, "live", forbidden)
    monkeypatch.setattr(apply_loop, "materials_ok", forbidden)
    assert not apply_loop.eligible(lead(fit_score=74))[0]
    assert not apply_loop.eligible(lead(d1_office_exclusion=True))[0]


def test_unconfirmed_http_is_not_eligible(monkeypatch):
    monkeypatch.setattr(apply_loop, "_tripwire_skip", lambda entry: False)
    monkeypatch.setattr(apply_loop, "blocklisted", lambda company: False)
    monkeypatch.setattr(apply_loop, "already_submitted", lambda *args: False)
    monkeypatch.setattr(apply_loop, "_canonical_queue_guard", lambda *args: (True, "fixture"))
    monkeypatch.setattr(apply_loop, "materials_ok", lambda *args: True)
    monkeypatch.setattr(apply_loop, "live", lambda url: None)
    assert not apply_loop.eligible(lead())[0]
    monkeypatch.setattr(apply_loop, "live", lambda url: True)
    assert apply_loop.eligible(lead())[0]


@pytest.mark.parametrize("container", [lambda rows: rows, lambda rows: {"rows": rows},
    lambda rows: {"entries": rows}, lambda rows: {"applications": rows}])
def test_same_employer_distinct_roles_do_not_destroy_supply(tmp_path, monkeypatch, container):
    ledger = tmp_path / "ledger.json"
    ledger.write_text(json.dumps(container([{"role_id": "already-applied", "company": "Fixture",
        "title": "Operations Director", "status": "SUBMITTED"}])))
    monkeypatch.setattr(apply_loop, "LEDGER", str(ledger))
    assert not apply_loop.already_submitted("Fixture", "Engineering Manager", "new-role")
    assert not apply_loop.already_submitted("Fixture Subsidiary", "Operations Director", "new-role")
    assert apply_loop.already_submitted("Fixture", "Other Title", "already-applied")
    assert apply_loop.already_submitted("FIXTURE", "Operations  Director", "new-role")


@pytest.mark.parametrize("contents", [None, "{bad", '{"applications":[],"rows":[]}', '{"rows":[false]}'])
def test_missing_or_corrupt_submission_history_holds_eligibility(tmp_path, monkeypatch, contents):
    ledger = tmp_path / "ledger.json"
    if contents is not None: ledger.write_text(contents)
    monkeypatch.setattr(apply_loop, "LEDGER", str(ledger))
    monkeypatch.setattr(apply_loop, "_tripwire_skip", lambda entry: False)
    monkeypatch.setattr(apply_loop, "blocklisted", lambda company: False)
    assert apply_loop.eligible(lead()) == (False, "submission history unconfirmed")


def test_canonical_admission_refuses_duplicate_or_nonadmission_queue_home():
    entry = lead()
    Path(apply_loop.QUEUE).write_text(json.dumps([entry]))
    assert apply_loop._canonical_queue_guard(entry, "standard") == (True, "ok")
    rejected = Path(apply_loop.DATA) / "queues/rejected-queue.json"
    rejected.write_text(json.dumps([entry]))
    assert not apply_loop._canonical_queue_guard(entry, "standard")[0]
    Path(apply_loop.QUEUE).write_text("[]")
    assert not apply_loop._canonical_queue_guard(entry, "standard")[0]


@pytest.mark.parametrize("status", sorted(ready_gate.LEDGER_HOLD_STATES))
def test_active_or_terminal_ledger_history_blocks_the_exact_role(status):
    entry = lead(); Path(apply_loop.QUEUE).write_text(json.dumps([entry]))
    Path(apply_loop.LEDGER).write_text(json.dumps([{"role_id": entry["role_id"], "status": status}]))
    assert not apply_loop._canonical_queue_guard(entry, "standard")[0]


def test_exact_posting_alias_in_ledger_blocks_even_with_changed_role_id_or_title():
    entry = lead()
    assert ready_gate.ledger_holds(entry, [{"role_id": "different-role", "status": "UNKNOWN_OUTCOME",
        "application_url": entry["ats_url"], "company": "Other", "title": "Other"}])


@pytest.mark.parametrize("contents", ["Fixture\n", "- Fixture\n", "## Blocked employers\n- **Fixture** — withdrawn\n## Allowed employers\n- **Other**\n"])
def test_plain_and_legacy_blocklists_are_exact_and_shared(tmp_path, contents):
    path = Path(apply_loop.DATA) / "employer-blocklist.md"
    path.write_text(contents)
    assert ready_gate.employer_blocklisted("Fixture Inc.", tmp_path)
    assert not ready_gate.employer_blocklisted("Fixture Subsidiary", tmp_path)
    assert not ready_gate.employer_blocklisted("Other", tmp_path)
    assert not ready_gate.entry_admission(lead(), workspace=tmp_path)["allowed"]
    assert apply_loop.blocklisted("Fixture")


def test_absent_blocklist_is_unknown_and_cannot_clear_admission(tmp_path):
    (Path(apply_loop.DATA) / "employer-blocklist.md").unlink()
    result = ready_gate.entry_admission(lead(), workspace=tmp_path)
    assert "blocklist_unconfirmed" in result["reason_codes"]


@pytest.mark.parametrize("status", [None, "", {}, False, 0])
def test_malformed_ledger_outcome_is_not_clearance(status):
    with pytest.raises(ValueError, match="ledger outcome"):
        ready_gate.ledger_holds(lead(), [{"role_id": "fixture-role", "status": status}])


def test_novel_ledger_outcome_for_exact_role_is_held_for_reconciliation():
    assert ready_gate.ledger_holds(lead(), [{"role_id": "fixture-role", "status": "NEW_UNRECONCILED_STATE"}])


@pytest.fixture
def refill_case(tmp_path, monkeypatch):
    entries = [lead(role_id=f"blocked-{i}", fit_score=90-i, human_hold=True) for i in range(4)] + [lead()]
    Path(apply_loop.QUEUE).write_text(json.dumps(entries))
    saved = []
    monkeypatch.setenv("KEEL_BUFFER_SCAN_LIMIT", "3")
    monkeypatch.setattr(apply_loop, "PACKETS", str(tmp_path))
    monkeypatch.setattr(apply_loop, "BUFFER_DIR", str(tmp_path / "buffer"))
    monkeypatch.setattr(apply_loop, "BUFFER_MIN_FRESH", 1)
    monkeypatch.setattr(apply_loop, "_buffer_lock", lambda: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(apply_loop, "load_buffer_state", lambda: [])
    monkeypatch.setattr(apply_loop, "save_buffer_state", lambda state: saved.extend(state))
    monkeypatch.setattr(apply_loop, "load_answer_bank", bank)
    monkeypatch.setattr(apply_loop, "BUFFER_WATERMARK", str(tmp_path / "watermark.json"))
    scanned = []
    def eligible(entry, origin):
        scanned.append(entry['role_id'])
        return ready_gate.entry_admission(entry)['allowed'], 'fixture'
    monkeypatch.setattr(apply_loop, "eligible", eligible)
    monkeypatch.setattr(apply_loop, "_launch_guard", lambda *args: (True, "fixture-task", ""))
    monkeypatch.setattr(apply_loop.prescreen, "screen_packet", lambda *args: {"verdict": "CLEAN", "reasons": []})
    monkeypatch.setattr(apply_loop.log_event, "log", lambda *args, **kwargs: None)

    def build(entry, **kwargs):
        path = tmp_path / (entry["role_id"]+".json")
        path.write_text(json.dumps(packet(entry)))
        return str(path)
    monkeypatch.setattr(apply_loop, "build_packet", build)
    def run():
        Path(apply_loop.QUEUE).write_text(json.dumps(entries))
        return apply_loop.refresh_buffer([(entry, "standard") for entry in entries])
    return entries, saved, scanned, run


@pytest.mark.parametrize("scan_limit,cycles,expected", [("3", 1, 0), ("100", 1, 1), ("3", 2, 1)])
def test_buffer_refill_scans_past_blocked_head_with_a_bounded_budget(refill_case, monkeypatch, scan_limit, cycles, expected):
    entries, saved, scanned, run = refill_case
    monkeypatch.setenv("KEEL_BUFFER_SCAN_LIMIT", scan_limit)
    for _ in range(cycles):
        before = len(scanned)
        actual = run()
        assert len(scanned) - before <= int(scan_limit)
    assert actual == expected
    if cycles == 2:
        assert scanned == ['blocked-0', 'blocked-1', 'blocked-2', 'blocked-3', 'fixture-role']
    assert [row["role_id"] for row in saved] == (["fixture-role"] if expected else [])


def test_refill_cursor_wraps_and_rechecks_changed_holds(refill_case):
    entries, saved, scanned, run = refill_case
    assert run() == 0
    entries[-1]['human_hold'] = True
    assert run() == 0  # The newly held tail must not inherit earlier eligibility.
    assert scanned[3:] == ['blocked-3', 'fixture-role', 'blocked-0']
    entries[0]['human_hold'] = False
    assert run() == 0
    assert run() == 1
    assert [row['role_id'] for row in saved] == ['blocked-0']


@pytest.mark.parametrize('position', [True, -1, 5, '3', None])
def test_refill_invalid_cursor_restarts_without_bypassing_gates(refill_case, position):
    entries, saved, scanned, run = refill_case
    assert run() == 0
    path = Path(apply_loop.BUFFER_WATERMARK)
    state = json.loads(path.read_text()); state['scan_next'] = position
    path.write_text(json.dumps(state))
    assert run() == 0 and saved == []
    assert scanned[3:] == ['blocked-0', 'blocked-1', 'blocked-2']


def test_refill_reordered_candidates_reset_cursor_and_empty_queue_is_safe(refill_case):
    entries, saved, scanned, run = refill_case
    assert run() == 0
    entries[-1]['fit_score'] = 99
    assert run() == 1
    assert scanned[3:] == ['fixture-role']
    entries.clear()
    assert run() == 0


@pytest.mark.parametrize("contents", [b"{", b"null", b"[]", b"\xff\xfe", b'{"scan_next": \xff}'])
def test_refill_corrupt_watermark_recovers_with_bounded_gate_checks(refill_case, contents):
    entries, saved, scanned, run = refill_case
    assert run() == 0
    Path(apply_loop.BUFFER_WATERMARK).write_bytes(contents)
    assert run() == 0 and saved == []
    assert scanned[3:] == ['blocked-0', 'blocked-1', 'blocked-2']
    assert run() == 1
    assert scanned[6:] == ['blocked-3', 'fixture-role']
    assert [row['role_id'] for row in saved] == ['fixture-role']


def test_refill_cursor_never_bypasses_current_launch_guard(refill_case, monkeypatch):
    entries, saved, scanned, run = refill_case
    assert run() == 0
    guards = []
    def deny(role_id, *args):
        guards.append(role_id)
        return False, None, 'synthetic current resource or policy hold'
    monkeypatch.setattr(apply_loop, '_launch_guard', deny)
    assert run() == 0 and saved == []
    assert guards == ['fixture-role']


def test_direct_legacy_preparation_never_marks_queue_inflight(tmp_path, monkeypatch):
    entry = lead(); path = tmp_path / "packet.json"; path.write_text(json.dumps(packet(entry)))
    released = []
    monkeypatch.setattr(apply_loop.sys, "argv", ["apply_loop.py"])
    monkeypatch.setattr(apply_loop, "load_queue", lambda: [entry])
    monkeypatch.setattr(apply_loop, "load_strategic_queue", lambda: [])
    monkeypatch.setattr(apply_loop, "load_answer_bank", bank)
    monkeypatch.setattr(apply_loop, "eligible", lambda *args: (True, "fixture"))
    monkeypatch.setattr(apply_loop.rate_limits, "is_allowed", lambda *args: (True, "fixture"))
    monkeypatch.setattr(apply_loop, "_buffered_fresh_packet", lambda *args: None)
    monkeypatch.setattr(apply_loop, "_launch_guard", lambda *args: (True, "fixture-task", ""))
    monkeypatch.setattr(apply_loop, "build_packet", lambda *args, **kwargs: str(path))
    monkeypatch.setattr(apply_loop.prescreen, "screen_packet", lambda *args: {"verdict": "CLEAN", "reasons": []})
    monkeypatch.setattr(apply_loop, "_release_launch_lock", lambda *args: released.append(args))
    monkeypatch.setattr(apply_loop.log_event, "log", lambda *args, **kwargs: None)
    before = copy.deepcopy(entry)
    apply_loop.main()
    assert entry == before
    assert released == [("fixture-role", "fixture-task")]
    with pytest.raises(RuntimeError, match="cannot mark IN-FLIGHT"):
        apply_loop.mark_inflight(entry["role_id"], "standard")


@pytest.mark.parametrize('error_type', [ValueError, RuntimeError])
def test_probe_error_output_omits_private_text_and_preserves_evidence_hold(tmp_path, monkeypatch, capsys, error_type):
    sentinel = 'SYNTHETIC-PRIVATE-PROBE person@example.com personal answer placeholder'
    entry = lead(posting_text='Synthetic complete posting.', posting_text_url=lead()['ats_url'])
    def fail_probe(url):
        raise error_type(sentinel)
    monkeypatch.setattr(apply_loop.form_intel, 'probe_url', fail_probe)
    monkeypatch.setattr(apply_loop, 'load_answer_bank', bank)
    destination = tmp_path / 'data/launch-packets'
    with pytest.raises(apply_loop.PacketEvidenceUnavailable):
        apply_loop.build_packet(entry, dest_dir=str(destination))
    captured = capsys.readouterr()
    assert sentinel not in captured.out + captured.err
    assert 'form_intel_unavailable' in captured.out
    assert not (destination / 'fixture-role.json').exists()
    assert not (tmp_path / 'data/form-intel/fixture-role.intel.json').exists()


def test_packet_builder_keeps_runtime_artifacts_inside_workspace(tmp_path, monkeypatch):
    entry = lead()
    entry["posting_text"] = "Synthetic posting text for a standard role."
    entry["posting_text_url"] = entry["ats_url"]
    monkeypatch.setattr(apply_loop, "BASE", str(tmp_path / "code"))
    monkeypatch.setattr(apply_loop, "load_answer_bank", bank)
    monkeypatch.setattr(apply_loop.form_intel, "probe_url",
                        lambda url: {"ats": "fixture", "questions": [],
                                     "source_url": url,
                                     "rendered_option_fetch_needed": [],
                                     "extraction_complete": True})
    monkeypatch.setattr(apply_loop.prescreen, "screen_packet", lambda *args: {"verdict": "CLEAN", "reasons": []})
    path = apply_loop.build_packet(entry, dest_dir=str(tmp_path / "data/launch-packets"))
    assert (tmp_path / "data/form-intel/fixture-role.intel.json").is_file()
    assert not (tmp_path / "code/briefs").exists()
    built = json.loads(Path(path).read_text())
    assert built["ready_manifest"]["profile"] == {"state": "absent"}
    assert built["execution_authorized"] is False
    assert built["scope"] == "preparation_only"
    assert built["form_intel_complete"] is True
    assert built["posting_text_complete"] is True
    assert built["form_intel"]["questions"] == []
    assert built["posting_text"] == entry["posting_text"]
    assert ready_gate.packet_admission(built, entry, bank(), for_execution=False)["allowed"]
    assert not ready_gate.packet_admission(built, entry, bank())["allowed"]


@pytest.mark.parametrize("evidence_complete", [False, True])
def test_incomplete_evidence_vetoes_packet_and_archives_stale_output(tmp_path, monkeypatch, evidence_complete):
    entry = lead()
    if evidence_complete:
        entry["posting_text"] = "Synthetic complete posting."
        entry["posting_text_url"] = entry["ats_url"]
    monkeypatch.setattr(apply_loop, "BASE", str(tmp_path / "code"))
    monkeypatch.setattr(apply_loop, "HOME", str(tmp_path))
    monkeypatch.setattr(apply_loop, "load_answer_bank", bank)
    monkeypatch.setattr(apply_loop.form_intel, "probe_url",
                        lambda url: {"ats": "fixture", "questions": [],
                                     "source_url": url,
                                     "rendered_option_fetch_needed": [],
                                     "extraction_complete": True})
    monkeypatch.setattr(apply_loop.prescreen, "screen_packet",
                        lambda *args: {"verdict": "CLEAN", "reasons": []})
    if evidence_complete:
        monkeypatch.setattr(apply_loop.prescreen, "screen_packet",
                            lambda *args: {"verdict": "PARK", "reasons": ["applicant attestation"]})
    destination = tmp_path / "data/launch-packets"
    destination.mkdir(parents=True)
    active = destination / "fixture-role.json"
    active.write_text('{"stale": true}', encoding="utf-8")

    expected = "applicant attestation" if evidence_complete else "Posting-text evidence"
    error = apply_loop.PacketPrescreenParked if evidence_complete else apply_loop.PacketEvidenceUnavailable
    with pytest.raises(error, match=expected):
        apply_loop.build_packet(entry, dest_dir=str(destination))

    assert not active.exists()
    archived = destination / "archive/fixture-role.json"
    assert json.loads(archived.read_text(encoding="utf-8")) == {"stale": True}


def test_stager_reconciles_nominal_and_admissible_ready(tmp_path, monkeypatch):
    entries = [lead(), lead(role_id="office-block", d1_office_exclusion=True)]
    for name, content in (("standard", {"entries": entries}), ("needs_input", []), ("ledger", [])):
        path = tmp_path / (name+".json"); path.write_text(json.dumps(content))
        monkeypatch.setattr(stage, {"standard": "STD_Q", "needs_input": "NI_Q", "ledger": "LEDGER"}[name], str(path))
    monkeypatch.setattr(stage, "STAGED", str(tmp_path / "staged.json"))
    monkeypatch.setattr(stage, "BUFFER_DIR", str(tmp_path))
    monkeypatch.setattr(stage, "_blocked_employers", lambda: [])
    monkeypatch.setattr(apply_loop, "load_answer_bank", bank)
    for entry in entries:
        (tmp_path / (entry["role_id"]+".json")).write_text(json.dumps(packet(entry)))
    plan = stage.build_plan()
    assert plan["ready_total"] == 2
    assert plan["admissible_packet_total"] == 1
    assert [row["role_id"] for row in plan["new"]] == ["fixture-role"]
    assert "d1_office_exclusion" in plan["skipped"][0]["reason"]


@pytest.mark.parametrize("evidence_error", [True, False])
def test_builder_hold_routes_only_real_questions_to_input(tmp_path, monkeypatch, evidence_error):
    entry = lead()
    monkeypatch.setattr(apply_loop.sys, "argv", ["apply_loop.py"])
    monkeypatch.setattr(apply_loop, "load_queue", lambda: [entry])
    monkeypatch.setattr(apply_loop, "load_strategic_queue", lambda: [])
    monkeypatch.setattr(apply_loop, "load_answer_bank", bank)
    monkeypatch.setattr(apply_loop, "eligible", lambda *args: (True, ""))
    monkeypatch.setattr(apply_loop.rate_limits, "is_allowed", lambda *args: (True, ""))
    monkeypatch.setattr(apply_loop, "_buffered_fresh_packet", lambda *args: None)
    monkeypatch.setattr(apply_loop, "_launch_guard", lambda *args: (True, "fixture-task", ""))
    released, parked = [], []
    monkeypatch.setattr(apply_loop, "_release_launch_lock", lambda *args: released.append(args))
    monkeypatch.setattr(apply_loop, "_park_packet_for_input", lambda *args: parked.append(args) or {"ok": True})
    error = apply_loop.PacketEvidenceUnavailable if evidence_error else apply_loop.PacketPrescreenParked
    def build(*args, **kwargs):
        raise error(["synthetic hold"])
    monkeypatch.setattr(apply_loop, "build_packet", build)
    apply_loop.main()
    assert released == [(entry["role_id"], "fixture-task")]
    assert len(parked) == (0 if evidence_error else 1)


def old_approval_packet(entry):
    pkt = packet(entry)
    pkt['brief'] = pkt['brief'].replace(
        'BANDED-QUESTION RULES (unverified reference; applicant review required):',
        'BANDED-QUESTION RULES (pre-approved — apply without improvising):')
    # Simulate a valid, fresh artifact sealed before the wording correction.
    return ready_gate.seal_packet(pkt, entry, bank())


@pytest.mark.parametrize('for_execution', [False, True])
def test_sealed_old_approval_brief_requires_rebuild(for_execution):
    entry = lead()
    pkt = old_approval_packet(entry)
    before = copy.deepcopy(pkt)
    result = ready_gate.packet_admission(pkt, entry, bank(), for_execution=for_execution)
    assert not result['allowed']
    assert 'obsolete_brief_authority' in result['reason_codes']
    assert pkt == before
    assert ready_gate.packet_admission(packet(entry), entry, bank(), for_execution=for_execution)['allowed']


def test_buffer_lookup_does_not_reuse_old_approval_brief(tmp_path, monkeypatch):
    entry = lead()
    Path(apply_loop.QUEUE).write_text(json.dumps([entry]))
    path = tmp_path / 'old-packet.json'
    path.write_text(json.dumps(old_approval_packet(entry)))
    monkeypatch.setattr(apply_loop, 'load_answer_bank', bank)
    built_at = datetime.now(timezone.utc).isoformat()
    monkeypatch.setattr(apply_loop, 'load_buffer_state', lambda: [{
        'role_id': entry['role_id'], 'packet_path': str(path),
        'built_at': built_at}])
    assert apply_loop._buffered_fresh_packet(entry['role_id']) is None
    path.write_text(json.dumps(packet(entry)))
    assert apply_loop._buffered_fresh_packet(entry['role_id']) == str(path)


@pytest.mark.parametrize('change', ['create', 'edit', 'delete', 'old'])
def test_profile_changes_invalidate_legacy_packets_and_buffer(tmp_path, monkeypatch, change):
    profile = tmp_path / 'data/applicant_profile.json'
    if change in ('edit', 'delete'):
        profile.write_text('{"verified_capabilities": []}')
    entry = lead()
    pkt = packet(entry)
    assert ready_gate.packet_admission(pkt, entry, bank(), for_execution=False)['allowed']
    if change == 'delete':
        profile.unlink()
    elif change == 'old':
        pkt['ready_manifest'].pop('profile', None)
        from safe_io import digest
        pkt['launch_integrity_sha256'] = digest({k: v for k, v in pkt.items() if k != 'launch_integrity_sha256'})
    else:
        profile.write_text('{"verified_capabilities": ["Synthetic skill"]}')
    assert not ready_gate.packet_admission(pkt, entry, bank(), for_execution=False)['allowed']
    Path(apply_loop.QUEUE).write_text(json.dumps([entry]))
    path = tmp_path / 'packet.json'
    path.write_text(json.dumps(pkt))
    monkeypatch.setattr(apply_loop, 'load_answer_bank', bank)
    monkeypatch.setattr(apply_loop, 'load_buffer_state', lambda: [{
        'role_id': entry['role_id'], 'packet_path': str(path),
        'built_at': datetime.now(timezone.utc).isoformat()}])
    assert apply_loop._buffered_fresh_packet(entry['role_id']) is None
