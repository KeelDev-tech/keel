"""Sanctioned local QRESOLVE write-path regressions with synthetic evidence."""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest import mock

from engines import tray_answer as actuator


class QresolveActuatorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.qdir = self.root / "data" / "queues"
        self.hdir = self.root / "hidden_files"
        self.qdir.mkdir(parents=True)
        self.hdir.mkdir()
        self.bank = self.root / "data" / "answer_bank.json"
        self.ni = self.qdir / "needs_input-queue.json"
        self.std = self.qdir / "standard-queue.json"
        self.write(self.std, [])
        self.write(self.ni, [])
        self.write(self.bank, {"answers": {"contact_email": {
            "value": "candidate@example.com", "scope": "global",
            "provenance": "the applicant's own words 2026-09-20"}}})
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        for module, key, value in (
            (actuator, "NI_Q", str(self.ni)),
            (actuator, "STD_Q", str(self.std)),
            (actuator, "ANSWERS_LOG", str(self.hdir / "tray-answers.jsonl")),
            (actuator.tray, "QDIR", str(self.qdir)),
            (actuator.tray, "BANK", str(self.bank)),
        ):
            self.stack.enter_context(mock.patch.object(module, key, value))
        old_lock = actuator.queue_io.get_lock_path()
        actuator.queue_io.set_lock_path(str(self.hdir / "queue.lock"))
        self.addCleanup(actuator.queue_io.set_lock_path, old_lock)
        self.validator = mock.Mock(side_effect=lambda request, *_: request["decision"])
        self.classifier = mock.Mock(return_value={"class": "FACT"})
        self.stack.enter_context(mock.patch.dict("sys.modules", {
            "qresolve": types.SimpleNamespace(validate_application=self.validator),
            "qresolve_semantics": types.SimpleNamespace(classify=self.classifier),
        }))
        self.stack.enter_context(mock.patch("subprocess.run", side_effect=AssertionError("network verification forbidden")))

    @staticmethod
    def write(path, value):
        path.write_text(json.dumps(value), encoding="utf-8")

    def lead(self, rid="role-1", question="What is your email address?"):
        return {"role_id": rid, "employer": "Example Co", "title": "Coordinator",
                "status": "PARKED-NEEDS-INPUT", "fit_score": 80,
                "unresolved": [question], "queue_notes": "",
                "status_reason": question, "gate_note": question,
                "last_verify_attempt": "2026-09-20T00:00:00Z"}

    def request(self, rid="role-1", question="What is your email address?", *, rewrite=True):
        if rewrite:
            self.write(self.ni, [self.lead(rid, question)])
        cards = actuator.tray.collect_cards()
        card = next(c for c in cards.values() if c["question"] == question)
        fingerprint = hashlib.sha256(question.encode()).hexdigest()[:16]
        decision = {"card_key": card["key"], "fingerprint": fingerprint,
                    "class": "FACT", "action": "auto_apply", "confidence": 0.99,
                    "rationale": "existing authorized bank evidence", "route": None,
                    "answer": "candidate@example.com", "bank_key": "contact_email",
                    "evidence": [{"source": "answer_bank.json", "pointer": "/answers/contact_email"}],
                    "context_sha256": "a" * 64, "evidence_sha256": "b" * 64,
                    "config_sha256": "c" * 64, "policy_version": "qresolve.v1",
                    "target_role_ids": [rid]}
        decision["decision_id"] = actuator._qresolve_digest(decision)
        path = self.root / f"{rid}-request.json"
        self.write(path, {"schema": "keel.qresolve.request.v1", "decision": decision})
        return path, decision

    def invoke(self, request, live=True):
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            rc = actuator.main(["--qresolve-request", str(request), "--bank", str(self.bank)]
                               + (["--live"] if live else []))
        return rc, json.loads(stream.getvalue().strip().splitlines()[-1])

    def journal(self):
        path = self.hdir / "qresolve-resolutions.jsonl"
        return [json.loads(row) for row in path.read_text().splitlines()] if path.exists() else []

    def test_five_facts_reuse_bank_offline_and_remain_pending_verification(self):
        bank_before = self.bank.read_bytes()
        questions = ["What is your email address?", "What is your full legal name?",
                     "What is your phone number?", "What is your current city?",
                     "What is your postal code?"]
        self.write(self.ni, [self.lead(f"role-{i}", q) for i, q in enumerate(questions)])
        for i, question in enumerate(questions):
            request, decision = self.request(f"role-{i}", question, rewrite=False)
            rc, receipt = self.invoke(request)
            self.assertEqual((rc, receipt["status"]), (0, "APPLIED"))
            self.assertEqual(receipt["removed_blockers"], 1)
            self.assertEqual(receipt["changed_leads"], 1)
            self.assertEqual(receipt["fully_unblocked"], 1)
            self.assertFalse(receipt["ready_verified"])
            self.assertEqual(receipt["revival"], "queued_for_canonical_verify_retry")
            self.assertNotIn("answer", receipt)
            records = self.journal()
            self.assertEqual(records[-2]["action"], "INTENT")
            self.assertEqual(records[-1]["action"], "auto_applied")
            self.assertEqual(records[-1]["decision_id"], decision["decision_id"])
        self.assertEqual(self.bank.read_bytes(), bank_before)
        for row in json.loads(self.ni.read_text()):
            self.assertEqual(row["unresolved"], [])
            self.assertEqual(row["status"], "PARKED-NEEDS-INPUT")
            self.assertIsNone(row["last_verify_attempt"])
            self.assertTrue(actuator.is_verify_only(row))
            self.assertNotIn("applicant's own words", row["queue_notes"])
            self.assertIn("reuse of approved bank evidence", row["queue_notes"])
        self.assertEqual(len(json.loads((self.hdir / "qresolve-resolved.json").read_text())), 5)

    def test_dry_run_has_no_file_changes_including_lock_diagnostics(self):
        request, _ = self.request()
        before = {str(p): (p.read_bytes(), p.stat().st_mtime_ns)
                  for p in self.root.rglob("*") if p.is_file()}
        rc, receipt = self.invoke(request, live=False)
        self.assertEqual((rc, receipt["status"], receipt["reason"]), (0, "NO_CHANGE", "dry_run"))
        after = {str(p): (p.read_bytes(), p.stat().st_mtime_ns)
                 for p in self.root.rglob("*") if p.is_file()}
        self.assertEqual(before, after)

    def test_empty_evidence_or_nonfact_cannot_write(self):
        request, _ = self.request()
        before = self.ni.read_bytes()
        for updates in ({"evidence": []}, {"class": "TRENT-ONLY"}, {"class": "JUDGMENT"},
                        {"class": "STRUCTURAL"}, {"action": "draft"}, {"bank_key": None}):
            with self.subTest(updates=updates):
                value = json.loads(request.read_text())
                value["decision"].update(updates)
                self.write(request, value)
                rc, receipt = self.invoke(request)
                self.assertEqual((rc, receipt["status"]), (3, "HOLD"))
                self.assertEqual(self.ni.read_bytes(), before)
                self.assertFalse(any(row["action"] == "INTENT" for row in self.journal()))
                request, _ = self.request()

    def test_validator_refuses_stale_evidence_before_backups_or_writes(self):
        request, _ = self.request()
        before = self.ni.read_bytes()
        self.validator.side_effect = ValueError("changed bank evidence")
        rc, receipt = self.invoke(request)
        self.assertEqual((rc, receipt["status"]), (3, "HOLD"))
        self.assertEqual(before, self.ni.read_bytes())
        self.assertFalse(list(self.qdir.glob("_backup-*")))
        self.assertEqual(self.journal()[-1]["action"], "held")

    def test_structural_manual_route_is_never_answered_or_banked(self):
        request, decision = self.request(question="Application route is apply-by-email")
        self.classifier.return_value = {"class": "STRUCTURAL"}
        before = self.ni.read_bytes(), self.bank.read_bytes()
        with contextlib.redirect_stdout(io.StringIO()):
            rc = actuator.main(["--live", "--key", decision["card_key"], "--answer", "Yes",
                                "--bank", str(self.bank), "--bank-new", "contact_email"])
        self.assertEqual(rc, 3)
        self.assertEqual(before, (self.ni.read_bytes(), self.bank.read_bytes()))
        self.assertFalse(list(self.qdir.glob("_backup-*")))
        rc, receipt = self.invoke(request)
        self.assertEqual((rc, receipt["status"]), (3, "HOLD"))

    def test_every_write_is_locked_backed_up_and_intent_precedes_queue(self):
        request, _ = self.request()
        original = actuator.queue_io.atomic_write_json
        events = []

        def checked(path, content):
            self.assertGreater(getattr(actuator.queue_io._state, "depth", 0), 0)
            events.append(str(path))
            if str(path) in {str(self.ni), str(self.std)}:
                backups = list(self.qdir.glob("_backup-*"))
                self.assertEqual(len(backups), 1)
                for name in (self.ni.name, self.std.name, self.bank.name):
                    self.assertTrue((backups[0] / name).is_file())
                self.assertEqual(self.journal()[-1]["action"], "INTENT")
            return original(path, content)

        with mock.patch.object(actuator.queue_io, "atomic_write_json", side_effect=checked):
            rc, receipt = self.invoke(request)
        self.assertEqual((rc, receipt["status"]), (0, "APPLIED"))
        self.assertNotIn(str(self.bank), events)
        backup = next(self.qdir.glob("_backup-*"))
        self.assertEqual(json.loads((backup / self.ni.name).read_text())[0]["unresolved"],
                         ["What is your email address?"])

    def test_partial_queue_commit_holds_replay_and_new_decision(self):
        request, _ = self.request()
        original = actuator.queue_io.atomic_write_json

        def fail_after_queue(path, content):
            result = original(path, content)
            if str(path) == str(self.ni):
                raise OSError("injected interruption after rename")
            return result

        with mock.patch.object(actuator.queue_io, "atomic_write_json", side_effect=fail_after_queue):
            rc, receipt = self.invoke(request)
        self.assertEqual((rc, receipt["status"]), (3, "HOLD"))
        self.assertEqual([row["action"] for row in self.journal()], ["INTENT", "held"])
        self.assertEqual(json.loads(self.ni.read_text())[0]["unresolved"], [])
        self.assertFalse((self.hdir / "qresolve-resolved.json").exists())
        self.assertEqual(self.invoke(request)[1]["reason"], "incomplete_prior_intent")
        value = json.loads(request.read_text())
        value["decision"]["decision_id"] = "e" * 64
        self.write(request, value)
        self.assertEqual(self.invoke(request)[1]["reason"], "incomplete_prior_intent")

    def test_crash_after_intent_no_automatic_retry(self):
        request, _ = self.request()
        before = self.ni.read_bytes()
        original = actuator.queue_io.atomic_write_json

        def fatal_before_queue(path, content):
            if str(path) == str(self.ni):
                raise SystemExit("process cut")
            return original(path, content)

        with mock.patch.object(actuator.queue_io, "atomic_write_json", side_effect=fatal_before_queue):
            with self.assertRaises(SystemExit):
                self.invoke(request)
        self.assertEqual(before, self.ni.read_bytes())
        self.assertEqual([row["action"] for row in self.journal()], ["INTENT"])
        self.assertEqual(self.invoke(request)[1]["reason"], "incomplete_prior_intent")

    def test_failure_to_durably_append_intent_does_not_change_queue(self):
        request, _ = self.request()
        before = self.ni.read_bytes(), self.bank.read_bytes()
        with mock.patch.object(actuator, "_qresolve_append", side_effect=OSError("fsync unavailable")):
            rc, receipt = self.invoke(request)
        self.assertEqual((rc, receipt["status"]), (3, "HOLD"))
        self.assertEqual(before, (self.ni.read_bytes(), self.bank.read_bytes()))
        self.assertFalse((self.hdir / "qresolve-resolved.json").exists())

    def test_resolution_map_failure_keeps_intent_pending(self):
        request, _ = self.request()
        original = actuator.queue_io.atomic_write_json

        def fail_resolution(path, content):
            if str(path).endswith("qresolve-resolved.json"):
                raise OSError("resolution persistence unavailable")
            return original(path, content)

        with mock.patch.object(actuator.queue_io, "atomic_write_json", side_effect=fail_resolution):
            rc, receipt = self.invoke(request)
        self.assertEqual((rc, receipt["status"]), (3, "HOLD"))
        self.assertEqual(json.loads(self.ni.read_text())[0]["unresolved"], [])
        self.assertFalse(any(row["action"] == "auto_applied" for row in self.journal()))
        self.assertEqual(self.invoke(request)[1]["reason"], "incomplete_prior_intent")

    def test_completed_replay_is_noop(self):
        request, _ = self.request()
        self.assertEqual(self.invoke(request)[1]["status"], "APPLIED")
        before = self.ni.read_bytes(), self.bank.read_bytes(), self.journal()
        rc, receipt = self.invoke(request)
        self.assertEqual((rc, receipt["status"], receipt["reason"]),
                         (0, "NO_CHANGE", "already_recorded"))
        self.assertEqual(receipt["removed_blockers"], 0)
        self.assertEqual(before, (self.ni.read_bytes(), self.bank.read_bytes(), self.journal()))

    def test_duplicate_role_identity_and_malformed_audit_fail_closed(self):
        request, _ = self.request()
        self.write(self.std, [self.lead()])
        before = self.ni.read_bytes()
        self.assertEqual(self.invoke(request)[1]["status"], "HOLD")
        self.assertEqual(before, self.ni.read_bytes())
        self.write(self.std, [])
        (self.hdir / "qresolve-resolutions.jsonl").write_bytes(b'{"action":"INTENT"')
        self.assertEqual(self.invoke(request)[1]["status"], "HOLD")
        self.assertEqual(before, self.ni.read_bytes())

    def test_fifo_and_symlink_inputs_hold_without_blocking_or_queue_changes(self):
        import os
        request, _ = self.request()
        before = self.ni.read_bytes()
        for path in (request, self.ni, self.hdir / "qresolve-resolutions.jsonl",
                     self.hdir / "qresolve-resolved.json"):
            with self.subTest(path=path.name):
                old = path.read_bytes() if path.exists() else None
                if path.exists():
                    path.unlink()
                os.mkfifo(path)
                try:
                    self.assertEqual(self.invoke(request)[1]["status"], "HOLD")
                finally:
                    path.unlink()
                    if old is not None:
                        path.write_bytes(old)
        alias = self.root / "alias"
        alias.symlink_to(self.root, target_is_directory=True)
        self.assertEqual(self.invoke(alias / request.name)[1]["status"], "HOLD")
        self.assertEqual(before, self.ni.read_bytes())

    def test_oversized_metadata_is_refused_before_mutation(self):
        request, _ = self.request()
        before = self.ni.read_bytes()
        oversized = self.hdir / "qresolve-resolutions.jsonl"
        with oversized.open("wb") as stream:
            stream.seek(16 * 1024 * 1024)
            stream.write(b"\n")
        self.assertEqual(self.invoke(request)[1]["status"], "HOLD")
        self.assertEqual(before, self.ni.read_bytes())

    def test_cli_disallows_manual_answer_bank_or_scope_in_auto_mode(self):
        request, _ = self.request()
        for extra in (["--answer", "Yes"], ["--bank-key", "contact_email"],
                      ["--bank-new", "new"], ["--scope", "global"], ["--key", "other"]):
            with self.subTest(extra=extra), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    actuator.main(["--qresolve-request", str(request), *extra])
                self.assertEqual(raised.exception.code, 2)

    def test_concurrent_bank_writer_finishes_before_evidence_revalidation(self):
        import subprocess
        import sys
        import threading

        request, _ = self.request()
        before = self.ni.read_bytes()
        child_code = """
import fcntl, json, os, sys
path = sys.argv[1]
with open(path + '.lock', 'a+') as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    print('LOCKED', flush=True)
    if sys.stdin.readline().strip() != 'release':
        raise SystemExit(2)
    with open(path) as stream:
        bank = json.load(stream)
    bank['answers']['contact_email']['value'] = 'updated@example.com'
    with open(path + '.new', 'w') as stream:
        json.dump(bank, stream)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(path + '.new', path)
"""
        attempted = threading.Event()
        validated = threading.Event()
        original_lock = actuator.safe_io.file_lock
        result = []

        @contextlib.contextmanager
        def observe_lock(path, **kwargs):
            self.assertGreater(getattr(actuator.queue_io._state, "depth", 0), 0)
            self.assertEqual(str(path), str(self.bank) + ".lock")
            attempted.set()
            with original_lock(path, **kwargs):
                yield

        def changed_evidence(request, *args):
            validated.set()
            self.assertIn(str(self.bank) + ".lock", actuator.safe_io._state.held)
            self.assertEqual(json.loads(self.bank.read_text())["answers"]["contact_email"]["value"],
                             "updated@example.com")
            raise ValueError("bank evidence changed while waiting for its writer")

        self.validator.side_effect = changed_evidence
        with subprocess.Popen([sys.executable, "-I", "-c", child_code, str(self.bank)],
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True) as child:
            self.assertEqual(child.stdout.readline().strip(), "LOCKED")
            thread = threading.Thread(target=lambda: result.append(self.invoke(request)), daemon=True)
            with mock.patch.object(actuator.safe_io, "file_lock", side_effect=observe_lock):
                thread.start()
                try:
                    self.assertTrue(attempted.wait(2))
                    self.assertFalse(validated.wait(0.1))
                    self.assertTrue(thread.is_alive())
                    self.assertEqual(before, self.ni.read_bytes())
                finally:
                    child.communicate("release\n", timeout=5)
                    thread.join(5)
            self.assertEqual(child.returncode, 0)
            self.assertFalse(thread.is_alive())
        self.assertTrue(validated.is_set())
        self.assertEqual((result[0][0], result[0][1]["status"]), (3, "HOLD"))
        self.assertEqual(before, self.ni.read_bytes())
        self.assertFalse(any(row["action"] == "INTENT" for row in self.journal()))

    def test_targets_reloaded_after_lock_and_no_stale_clear(self):
        request, _ = self.request()
        original = actuator.queue_io.queue_lock

        @contextlib.contextmanager
        def intervening_write(**kwargs):
            with original(**kwargs):
                row = self.lead(question="What is your telephone number?")
                self.write(self.ni, [row])
                yield

        with mock.patch.object(actuator.queue_io, "queue_lock", side_effect=intervening_write):
            rc, receipt = self.invoke(request)
        self.assertEqual((rc, receipt["status"]), (3, "HOLD"))
        self.assertEqual(json.loads(self.ni.read_text())[0]["unresolved"],
                         ["What is your telephone number?"])
        self.assertFalse(any(row["action"] == "INTENT" for row in self.journal()))


if __name__ == "__main__":
    unittest.main()
