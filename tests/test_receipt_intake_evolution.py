"""Offline receipts remain observations; injected host validation is explicit."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engines"))
from outcome_tracking.receipt_intake import ReceiptStore, ProviderValidation, grade_claim
from outcome_tracking import evidence_gate
from safe_io import digest
import inbox_listener as inbox
import log_event
import outcome_analytics as oa

NOW = datetime(2026, 9, 24, 1, tzinfo=timezone.utc)
CLAIM = {"role_id": "R", "attempt_id": "A", "application_id": "P", "company": "Fixture",
         "date_submitted": "2026-09-23T22:00:00+00:00", "status": "SUBMITTED", "fit_score": 86}


def receipt(**kwargs):
    return {"kind": "imported_mail", "source": "fixture-mail", "receipt_id": "mail-1",
            "content_sha256": "a" * 64, "received_at": "2026-09-23T23:00:00+00:00",
            "recorded_at": NOW.isoformat(), "outcome": "AUTO_ACK", "role_id": "R",
            "attempt_id": "A", "application_id": "P", **kwargs}


def host_validator(record, claim):
    # Synthetic adapter only: production code must authenticate independently.
    return ProviderValidation(digest(record), digest(claim), "fixture-provider", NOW.isoformat(), True)


@pytest.mark.parametrize("kind", ["imported_mail", "manual_attestation", "provider_receipt"])
def test_no_receipt_kind_or_flags_can_self_certify(tmp_path, kind):
    store = ReceiptStore(tmp_path / "receipts.json")
    store.put(receipt(kind=kind, verified=True, authenticated=True), now=NOW)
    grade = grade_claim(CLAIM, store.snapshot(), now=NOW)
    assert grade["observed"] and not grade["provider_verified"]
    assert grade["manual_attested"] == (kind == "manual_attestation")
    assert "verified" not in store.snapshot()[0][1]


def test_host_validator_must_be_registered_and_bind_both_inputs(tmp_path):
    store = ReceiptStore(tmp_path / "receipts.json")
    store.put(receipt(kind="provider_receipt"), now=NOW)
    assert grade_claim(CLAIM, store.snapshot(), now=NOW, validators={"other": host_validator})["provider_verified"] is False
    assert grade_claim(CLAIM, store.snapshot(), now=NOW, validators={"fixture-mail": host_validator})["provider_verified"] is True
    forged = lambda record, claim: ProviderValidation("b" * 64, digest(claim), "fixture", NOW.isoformat(), True)
    assert not grade_claim(CLAIM, store.snapshot(), now=NOW, validators={"fixture-mail": forged})["provider_verified"]


@pytest.mark.parametrize("change", [{"role_id": "OTHER"}, {"attempt_id": "OTHER"},
                                     {"application_id": "OTHER"}, {"attempt_id": ""}])
def test_receipt_identifiers_cannot_cross_applications(tmp_path, change):
    store = ReceiptStore(tmp_path / "receipts.json")
    store.put(receipt(kind="provider_receipt", **change), now=NOW)
    result = grade_claim(CLAIM, store.snapshot(), now=NOW, validators={"fixture-mail": host_validator})
    assert not result["observed"] and not result["provider_verified"]


def test_replay_idempotent_conflicting_receipt_persists_hold(tmp_path):
    path = tmp_path / "receipts.json"
    store = ReceiptStore(path)
    assert store.put(receipt(), now=NOW)["status"] == "recorded"
    assert store.put(receipt(), now=NOW)["status"] == "duplicate"
    assert store.put(receipt(content_sha256="b" * 64), now=NOW)["status"] == "held_conflict"
    reopened = ReceiptStore(path)
    assert reopened.put(receipt(), now=NOW)["status"] == "held_conflict"
    assert len(reopened.snapshot()) == 1
    assert grade_claim(CLAIM, reopened.snapshot(), now=NOW)["held"]


@pytest.mark.parametrize("change", [{"received_at": "2026-09-25T00:00:00Z"},
                                     {"recorded_at": "2026-09-25T00:00:00Z"},
                                     {"received_at": "not-a-date"}, {"received_at": "2026-09-23T23:00:00"},
                                     {"content_sha256": "not-a-hash"}, {"role_id": 4}])
def test_bad_receipt_data_refused_before_write(tmp_path, change):
    store = ReceiptStore(tmp_path / "receipts.json")
    with pytest.raises(ValueError):
        store.put(receipt(**change), now=NOW)
    assert not store.path.exists()


def test_corrupt_store_preserved_and_report_incomplete(tmp_path):
    path = tmp_path / "receipts.json"
    path.write_text('{"version":1,"version":1}')
    with pytest.raises(ValueError):
        ReceiptStore(path).put(receipt(), now=NOW)
    report = evidence_gate.coverage_report([CLAIM], tmp_path / "events.jsonl", now=NOW, receipts_path=path,
                                          provider_validators={"fixture-mail": host_validator})
    assert not report["receipts_complete"] and report["verified"] == 0
    assert path.read_text() == '{"version":1,"version":1}'


def test_evidence_gate_uses_validated_receipts_without_telemetry(tmp_path):
    path = tmp_path / "receipts.json"
    ReceiptStore(path).put(receipt(kind="provider_receipt"), now=NOW)
    report = evidence_gate.coverage_report([CLAIM], tmp_path / "events.jsonl", now=NOW, receipts_path=path,
                                          provider_validators={"fixture-mail": host_validator})
    assert report["verified"] == 1 and report["unevidenced"] == report["pending"] == 0
    assert report["observed_receipts"] == 1 and not report["telemetry_complete"]


def test_unavailable_or_future_provider_check_fails_closed(tmp_path):
    store = ReceiptStore(tmp_path / "receipts.json")
    store.put(receipt(kind="provider_receipt"), now=NOW)
    def fail(record, claim):
        raise OSError("offline")
    future = lambda record, claim: ProviderValidation(digest(record), digest(claim), "fixture", "2026-09-25T00:00:00Z", True)
    for validator in (fail, future, lambda record, claim: {"verified": True}):
        result = grade_claim(CLAIM, store.snapshot(), now=NOW, validators={"fixture-mail": validator})
        assert not result["provider_verified"] and result["warnings"]


def test_analytics_holds_crossed_exact_ids_and_duplicate_roles():
    rows = [CLAIM, {**CLAIM, "attempt_id": "B", "application_id": "Q"}]
    event = {"ts": NOW.isoformat(), "role_id": "R", "outcome": "AUTO_ACK"}
    linked, _, held = oa.link_responses(rows, [event])
    assert not linked and len(held) == 1
    linked, _, held = oa.link_responses(rows, [{**event, "attempt_id": "B", "application_id": "P"}])
    assert not linked and len(held) == 1
    linked, _, held = oa.link_responses(rows, [{**event, "attempt_id": "B", "application_id": "Q"}])
    assert list(linked) == [1] and not held


def test_outer_and_inner_event_ids_conflict_and_corrupt_events_raise(tmp_path):
    path = tmp_path / "events.jsonl"
    event = {"ts": NOW.isoformat(), "event_type": "employer_response", "role_id": "R",
             "details": {"role_id": "OTHER", "outcome": "AUTO_ACK"}}
    path.write_text(json.dumps(event) + "\n")
    loaded = oa.load_responses(path)
    assert loaded[0]["identity_conflict"] == "role_id"
    assert oa.link_responses([CLAIM], loaded)[2]
    path.write_text(json.dumps(event) + "\n{bad")
    with pytest.raises(ValueError):
        oa.load_responses(path)


def test_inbox_never_credits_latest_company_or_every_lane():
    rows = [CLAIM, {**CLAIM, "role_id": "OTHER", "attempt_id": "B", "application_id": "Q", "fit_score": 99}]
    msg = {"date": "Wed, 23 Sep 2026 23:00:00 +0000"}
    assert inbox.resolve_message(msg, rows, "fixture")[0] is None
    assert inbox.linked_fit_score("fixture", {"fixture": rows}) is None
    assert inbox.resolve_message({**msg, "role_id": "R", "attempt_id": "A"}, rows, "fixture")[0] == 0
    assert inbox.linked_fit_score("fixture", {"fixture": rows}, role_id="R", attempt_id="A") == 86


def test_maildir_blocks_path_escape_and_sender_lookalikes(tmp_path):
    reader = inbox.MaildirReader(tmp_path)
    with pytest.raises(ValueError):
        reader.read("../outside.eml")
    other = tmp_path.parent / "outside.eml"
    other.write_text("Subject: data\n\nbody")
    (tmp_path / "escape.eml").symlink_to(other)
    with pytest.raises(ValueError):
        reader.read("escape.eml")
    assert inbox.is_linkedin_notification("Name <hit-reply@linkedin.com>")
    assert not inbox.is_linkedin_notification("Name <hit-reply@linkedin.com.attacker.test>")
    assert not inbox.is_indeed_notification('"indeed.com" <x@attacker.test>')


def test_live_inbox_retry_after_logger_failure_and_checkpoint_crash(tmp_path):
    ledger, events, out = tmp_path / "ledger.json", tmp_path / "events.jsonl", tmp_path / "report"
    ledger.write_text(json.dumps([CLAIM]))
    message = {"id": "one.eml", "receipt_id": "message-id", "role_id": "R", "attempt_id": "A",
               "application_id": "P", "subject": "Thank you for applying to Fixture",
               "from": "jobs@fixture.test", "date": "Wed, 23 Sep 2026 23:00:00 +0000", "body_text": "Received"}
    class Source:
        def triage(self, **kwargs): return [message]
        def read(self, ident): return message
    with patch.object(inbox, "LEDGER", str(ledger)), patch.object(log_event, "EVENTS", str(events)):
        with patch.object(log_event, "log", side_effect=OSError("disk error")):
            with pytest.raises(OSError): inbox.main(["--live", "--out", str(out)], Source())
        assert not (out / "seen-message-ids.json").exists()
        real_atomic = inbox.atomic_json
        def fail_checkpoint(path, value):
            if str(path).endswith("seen-message-ids.json"):
                raise OSError("crash after append")
            return real_atomic(path, value)
        with patch.object(inbox, "atomic_json", side_effect=fail_checkpoint):
            with pytest.raises(OSError): inbox.main(["--live", "--out", str(out)], Source())
        assert len(events.read_text().splitlines()) == 1
        report = inbox.main(["--live", "--out", str(out)], Source())
        assert len(events.read_text().splitlines()) == 1
        assert json.loads((out / "seen-message-ids.json").read_text()) == ["one.eml"]
        assert report["ack_coverage"] == "1/1"
        assert report["classifications"][0]["role_id"] == "R"


def test_explicit_rejection_not_misclassified_as_ack():
    label, _ = inbox.classify("Your application for Manager", "Thank you for applying. We are not moving forward.", "x@example.org")
    assert label == "REJECTION"


def test_receipt_dates_need_timezone_and_url_path_case_is_identity():
    assert inbox.message_time("2026-09-23T23:00:00") is None
    row = {**CLAIM, "posting_url": "https://example.org/jobs/RoleA"}
    event = {"ts": NOW.isoformat(), "posting_url": "https://example.org/jobs/rolea"}
    linked, unlinked, held = oa.link_responses([row], [event])
    assert not linked and unlinked == [event] and not held


def _live_fixture(tmp_path, *, observed_attempt=True):
    ledger, events, out = tmp_path / "ledger.json", tmp_path / "events.jsonl", tmp_path / "report"
    ledger.write_text(json.dumps([CLAIM]))
    message = {"id": "one.eml", "receipt_id": "message-id", "role_id": "R",
               "subject": "Thank you for applying to Fixture", "from": "jobs@fixture.test",
               "date": "Wed, 23 Sep 2026 23:00:00 +0000", "body_text": "Received"}
    if observed_attempt:
        message.update(attempt_id="A", application_id="P")
    class Source:
        def triage(self, **kwargs): return [message]
        def read(self, ident): return message
    return ledger, events, out, message, Source()


def test_rescoring_identical_receipt_replays_frozen_event_payload(tmp_path):
    ledger, events, out, message, source = _live_fixture(tmp_path)
    with patch.object(inbox, "LEDGER", str(ledger)), patch.object(log_event, "EVENTS", str(events)):
        inbox.main(["--live", "--out", str(out)], source)
        original = events.read_bytes()
        ledger.write_text(json.dumps([{**CLAIM, "fit_score": 90}]))
        report = inbox.main(["--live", "--out", str(out)], source)
    assert report["ack_coverage"] == "1/1"
    assert events.read_bytes() == original
    assert json.loads(original)["details"]["fit_score"] == 86


def test_receipt_conflict_removes_report_and_historical_analytics_credit(tmp_path):
    ledger, events, out, message, source = _live_fixture(tmp_path)
    with patch.object(inbox, "LEDGER", str(ledger)), patch.object(log_event, "EVENTS", str(events)):
        inbox.main(["--live", "--out", str(out)], source)
        message["body_text"] = "Changed bytes on same receipt identity"
        report = inbox.main(["--live", "--out", str(out)], source)
        dry_report = inbox.main(["--out", str(out)], source)
    for result in (report, dry_report):
        assert result["ack_coverage"] == "0/1"
        assert result["lane_outcome_stats"] == {}
        assert result["classifications"][0]["attribution"] == "held_conflicting_receipt"
    persisted = json.loads(next(out.glob("inbox-outcomes-*.json")).read_text())
    assert persisted["ack_coverage"] == "0/1" and persisted["lane_outcome_stats"] == {}
    observations = oa.load_responses(events, receipts_path=out / "receipt-observations.json")
    assert observations[0]["identity_conflict"] == "receipt_conflict"
    linked, _, held = oa.link_responses([CLAIM], observations)
    assert not linked and len(held) == 1


def test_resolved_attempt_remains_distinct_from_observed_mail_identity(tmp_path):
    ledger, events, out, message, source = _live_fixture(tmp_path, observed_attempt=False)
    with patch.object(inbox, "LEDGER", str(ledger)), patch.object(log_event, "EVENTS", str(events)):
        report = inbox.main(["--live", "--out", str(out)], source)
    assert report["ack_coverage"] == "1/1"  # exact unique role correlation only
    record = ReceiptStore(out / "receipt-observations.json").snapshot()[0][1]
    assert record["role_id"] == "R" and record["attempt_id"] == record["application_id"] == ""
    assert record["observed_identity"]["attempt_id"] == ""
    assert record["resolved_identity"]["attempt_id"] == "A"
    assert not grade_claim(CLAIM, ReceiptStore(out / "receipt-observations.json").snapshot(), now=NOW)["observed"]
    event = json.loads(events.read_text())
    assert event["details"]["observed_identity"]["attempt_id"] == ""
    assert event["details"]["resolved_identity"]["attempt_id"] == "A"


def test_missing_receipt_store_holds_new_telemetry_projection(tmp_path):
    ledger, events, out, message, source = _live_fixture(tmp_path)
    with patch.object(inbox, "LEDGER", str(ledger)), patch.object(log_event, "EVENTS", str(events)):
        inbox.main(["--live", "--out", str(out)], source)
    observations = oa.load_responses(events, receipts_path=tmp_path / "missing.json")
    assert observations[0]["identity_conflict"] == "receipt_state_unavailable"
    assert not oa.link_responses([CLAIM], observations)[0]


def test_same_batch_conflict_withholds_both_candidates_before_any_emission(tmp_path):
    ledger, events, out, message, _ = _live_fixture(tmp_path)
    other = {**message, "id": "two.eml", "body_text": "Conflicting body"}
    class Source:
        def triage(self, **kwargs): return [message, other]
        def read(self, ident): return message if ident == "one.eml" else other
    with patch.object(inbox, "LEDGER", str(ledger)), patch.object(log_event, "EVENTS", str(events)):
        report = inbox.main(["--live", "--out", str(out)], Source())
    assert report["ack_coverage"] == "0/1" and report["lane_outcome_stats"] == {}
    assert all(row["attribution"] == "held_conflicting_receipt" for row in report["classifications"])
    assert not events.exists()
