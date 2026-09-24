"""Behavioral tests for exact approval, recovery and durable duplicate control."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import threading
import unittest

from keel_workflow.delivery import (
    Bundle, DeliveryError, DeliveryStore, MAX_ATTACHMENT_BYTES,
    SyntheticAdapter, build_bundle,
    validate_bundle,
)


REVISIONS = {"answer": "a1", "fact": "f1", "evidence": "e1", "policy": "p1"}


def bundle(**overrides):
    values = dict(workspace_id="test-workspace", role_id="role-1",
                  action="SIMULATE_SUBMISSION", destination="https://synthetic.invalid/apply/1",
                  account_id="synthetic-account", revisions=REVISIONS,
                  content={"answers": {"motivation": "Synthetic answer"}},
                  attachments={"resume.txt": b"Synthetic resume version one"})
    values.update(overrides)
    return build_bundle(**values)


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "delivery.sqlite"
        self.store = DeliveryStore(self.path)
        self.bundle = bundle()
        self.approval = self.store.approve(self.bundle, approver_id="human-test",
                                           authority_ref="synthetic-authority-1", expires_at=200, now=100)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def run_attempt(self, *, bundle_value=None, approval_id=None, key="attempt-key", mode="confirmed", **overrides):
        args = dict(approval_id=approval_id or self.approval["approval_id"],
                    idempotency_key=key, adapter=SyntheticAdapter(mode=mode),
                    current_revisions=REVISIONS, current_account_id="synthetic-account",
                    current_authority_ref="synthetic-authority-1", now=101)
        args.update(overrides)
        return self.store.run_simulation(bundle_value or self.bundle, **args)

    def test_exact_bundle_simulation_never_reports_submission(self):
        result = self.run_attempt()
        self.assertEqual(result["status"], "SIMULATED_CONFIRMED")
        self.assertFalse(result["execution_authorized"])
        self.assertFalse(result["submitted"])
        self.assertTrue(result["receipt"]["simulation"])
        self.assertEqual(result["receipt"]["bundle_sha256"], self.bundle.sha256)

    def test_source_file_edits_cannot_change_approved_copy(self):
        path = Path(self.temp.name) / "resume.txt"
        path.write_bytes(b"approved bytes")
        snapshot = bundle(attachments={"resume.txt": path})
        approval = self.store.approve(snapshot, approver_id="human", authority_ref="synthetic-authority-1", expires_at=200, now=100)
        path.write_bytes(b"edited after approval")
        loaded = self.store.load_bundle(snapshot.sha256)
        self.assertEqual(loaded.attachments, (("resume.txt", b"approved bytes"),))
        changed = bundle(attachments={"resume.txt": path})
        self.assertNotEqual(changed.sha256, snapshot.sha256)
        with self.assertRaisesRegex(DeliveryError, "exact bundle"):
            self.run_attempt(bundle_value=changed, approval_id=approval["approval_id"])

    def test_mutable_source_content_is_snapshotted(self):
        content = {"answers": ["approved"]}
        snapshot = bundle(content=content)
        content["answers"][0] = "changed"
        exposed = snapshot.document
        exposed["content"]["answers"][0] = "also changed"
        self.assertEqual(snapshot.document["content"]["answers"], ["approved"])

    def test_public_validator_rejects_forged_attachment_bytes(self):
        forged = replace(self.bundle, attachments=(("resume.txt", b"changed"),))
        with self.assertRaisesRegex(DeliveryError, "do not match"):
            validate_bundle(forged)
        self.assertEqual(validate_bundle(self.bundle), self.bundle.document)

    def test_aware_datetime_and_iso_timestamps(self):
        for value in (datetime.fromtimestamp(101, tz=timezone.utc), "1970-01-01T00:01:41Z"):
            with self.subTest(value=value):
                result = self.run_attempt(now=value)
                self.assertEqual(result["status"], "SIMULATED_CONFIRMED")
        for value in (datetime.fromtimestamp(101), "1970-01-01T00:01:41", True, float("nan")):
            with self.subTest(value=value), self.assertRaises(DeliveryError):
                self.run_attempt(now=value)

    def test_edited_fields_invalidate_approval(self):
        edits = [dict(destination="https://synthetic.invalid/other"), dict(account_id="other"),
                 dict(workspace_id="other"), dict(role_id="other"), dict(content={"new": "answer"}),
                 dict(revisions={**REVISIONS, "policy": "p2"}),
                 dict(attachments={"resume.txt": b"different resume"})]
        for change in edits:
            with self.subTest(change=change):
                changed = bundle(**change)
                with self.assertRaises(DeliveryError):
                    self.run_attempt(bundle_value=changed,
                                     current_account_id=changed.document["account_id"],
                                     current_revisions=changed.document["revisions"])

    def test_bundle_digest_does_not_trust_supplied_attachment_hash(self):
        forged = replace(self.bundle, attachments=(("resume.txt", b"unapproved bytes"),))
        with self.assertRaisesRegex(DeliveryError, "do not match"):
            self.run_attempt(bundle_value=forged)

    def test_persisted_attachment_tampering_blocks_use(self):
        self.store.connection.execute("UPDATE workflow_attachments SET content=?", (b"tampered",))
        with self.assertRaisesRegex(DeliveryError, "do not match"):
            self.run_attempt()

    def test_current_context_must_match_approval(self):
        edits = [dict(current_account_id="other-account"),
                 dict(current_revisions={**REVISIONS, "fact": "f2"}),
                 dict(current_revisions={**REVISIONS, "policy": "p2"}),
                 dict(current_authority_ref="new-authority")]
        for change in edits:
            with self.subTest(change=change), self.assertRaises(DeliveryError):
                self.run_attempt(**change)

    def test_expired_or_future_approval_blocks(self):
        for timestamp in (99, 200, 201):
            with self.subTest(timestamp=timestamp), self.assertRaisesRegex(DeliveryError, "expired"):
                self.run_attempt(now=timestamp)

    def test_revocation_is_durable(self):
        self.store.revoke(self.approval["approval_id"], reason="user declined", now=100)
        self.store.close()
        self.store = DeliveryStore(self.path)
        with self.assertRaisesRegex(DeliveryError, "revoked"):
            self.run_attempt()

    def test_same_key_returns_one_attempt_and_no_second_receipt_event(self):
        first = self.run_attempt()
        second = self.run_attempt()
        self.assertEqual(first, second)
        self.assertEqual(sum(event["kind"] == "simulation_attempt_reserved" for event in self.store.events()), 1)
        self.assertEqual(sum(event["kind"] == "simulation_attempt_reconciled" for event in self.store.events()), 1)

    def test_unknown_timeout_prevents_new_idempotency_key_retry(self):
        first = self.run_attempt(mode="timeout")
        self.assertEqual(first["status"], "UNKNOWN")
        with self.assertRaisesRegex(DeliveryError, "UNKNOWN"):
            self.run_attempt(key="attempt-key-2")
        self.assertEqual(self.run_attempt(mode="confirmed")["status"], "UNKNOWN")

    def test_crash_after_reservation_survives_restart(self):
        result, created = self.store._reserve(
            self.bundle, approval_id=self.approval["approval_id"], idempotency_key="crash-key",
            current_revisions=REVISIONS, current_account_id="synthetic-account",
            current_authority_ref="synthetic-authority-1", now=101)
        self.assertTrue(created)
        self.store.close()
        self.store = DeliveryStore(self.path)
        self.assertEqual(self.store.get_attempt(result["attempt_id"])["status"], "UNKNOWN")
        with self.assertRaisesRegex(DeliveryError, "UNKNOWN"):
            self.run_attempt(key="fresh-key")

    def test_modified_bundle_and_account_cannot_evade_unknown(self):
        self.run_attempt(mode="timeout")
        changed = bundle(account_id="second-account", content={"different": "answer"})
        approval = self.store.approve(changed, approver_id="human", authority_ref="synthetic-authority-1", expires_at=200, now=101)
        with self.assertRaisesRegex(DeliveryError, "UNKNOWN"):
            self.run_attempt(bundle_value=changed, approval_id=approval["approval_id"], key="new-key", current_account_id="second-account")

    def test_confirmed_scope_prevents_duplicate_with_new_key(self):
        self.run_attempt()
        with self.assertRaisesRegex(DeliveryError, "SIMULATED_CONFIRMED"):
            self.run_attempt(key="new-key")

    def test_alternate_endpoint_does_not_evade_role_duplicate_gate(self):
        self.run_attempt(mode="timeout")
        changed = bundle(destination="https://synthetic.invalid/alternate-api/1")
        approval = self.store.approve(changed, approver_id="human", authority_ref="synthetic-authority-1", expires_at=200, now=101)
        with self.assertRaisesRegex(DeliveryError, "UNKNOWN"):
            self.run_attempt(bundle_value=changed, approval_id=approval["approval_id"], key="alternate-endpoint")

    def test_trusted_not_sent_reconciliation_allows_new_attempt(self):
        first = self.run_attempt(mode="timeout")
        reconciled = self.store.reconcile(first["attempt_id"], outcome="not_sent",
                                         evidence_ref="synthetic://independent-check/no-effect",
                                         reconciler_id="trusted-host", now=102)
        self.assertEqual(reconciled["status"], "NOT_SENT")
        next_result = self.run_attempt(key="new-key", now=103)
        self.assertEqual(next_result["status"], "SIMULATED_CONFIRMED")
        self.assertNotEqual(first["attempt_id"], next_result["attempt_id"])

    def test_not_sent_retry_still_requires_current_approval(self):
        self.run_attempt(mode="not_sent")
        self.store.revoke(self.approval["approval_id"], reason="withdrawn", now=102)
        with self.assertRaisesRegex(DeliveryError, "revoked"):
            self.run_attempt(key="new-key", now=103)

    def test_terminal_receipt_cannot_be_replaced_or_reopened(self):
        result = self.run_attempt()
        with self.assertRaisesRegex(DeliveryError, "UNKNOWN"):
            self.store.reconcile(result["attempt_id"], outcome="not_sent", evidence_ref="synthetic://new", reconciler_id="host", now=102)

    def test_duplicate_exact_receipt_is_idempotent(self):
        result = self.run_attempt()
        before = len(self.store.events())
        again = self.store.reconcile(result["attempt_id"], outcome="confirmed", evidence_ref=result["evidence_ref"],
                                     reconciler_id="host", now=102, receipt=result["receipt"])
        self.assertEqual(result, again)
        self.assertEqual(len(self.store.events()), before)

    def test_receipt_requires_every_exact_binding(self):
        result = self.run_attempt(mode="timeout")
        receipt = self.store.make_synthetic_receipt(result["attempt_id"], evidence_ref="synthetic://receipt")
        for key in ("attempt_id", "bundle_sha256", "destination", "account_id", "workspace_id", "role_id", "action", "receipt_id", "evidence_ref", "simulation"):
            with self.subTest(key=key), self.assertRaisesRegex(DeliveryError, "Receipt"):
                self.store.reconcile(result["attempt_id"], outcome="confirmed", evidence_ref="synthetic://receipt",
                                     reconciler_id="host", now=102, receipt={**receipt, key: "wrong"})
        self.assertEqual(self.store.get_attempt(result["attempt_id"])["status"], "UNKNOWN")

    def test_reconciliation_requires_evidence_reference(self):
        result = self.run_attempt(mode="timeout")
        with self.assertRaisesRegex(DeliveryError, "evidence_ref"):
            self.store.reconcile(result["attempt_id"], outcome="not_sent", evidence_ref="", reconciler_id="host", now=102)

    def test_concurrent_different_keys_only_reserve_one_scope(self):
        barrier = threading.Barrier(2)

        def contender(key):
            store = DeliveryStore(self.path)
            try:
                barrier.wait(timeout=10)
                return store.run_simulation(self.bundle, approval_id=self.approval["approval_id"], idempotency_key=key,
                                            adapter=SyntheticAdapter("timeout"), current_revisions=REVISIONS,
                                            current_account_id="synthetic-account", current_authority_ref="synthetic-authority-1", now=101)
            except DeliveryError as exc:
                return str(exc)
            finally:
                store.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(contender, ["race-1", "race-2"]))
        self.assertEqual(sum(isinstance(result, dict) for result in results), 1)
        self.assertEqual(sum("UNKNOWN" in result for result in results if isinstance(result, str)), 1)

    def test_concurrent_same_key_returns_one_persistent_attempt(self):
        barrier = threading.Barrier(2)

        def contender(_):
            store = DeliveryStore(self.path)
            try:
                barrier.wait(timeout=10)
                return store.run_simulation(self.bundle, approval_id=self.approval["approval_id"], idempotency_key="same-key",
                                            adapter=SyntheticAdapter("timeout"), current_revisions=REVISIONS,
                                            current_account_id="synthetic-account", current_authority_ref="synthetic-authority-1", now=101)
            finally:
                store.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(contender, range(2)))
        self.assertEqual(results[0]["attempt_id"], results[1]["attempt_id"])

    def test_live_intent_and_external_adapters_are_rejected(self):
        with self.assertRaisesRegex(DeliveryError, "simulation approvals"):
            self.store.approve(bundle(action="SUBMIT_APPLICATION"), approver_id="human", authority_ref="auth", expires_at=200, now=100)

        class ExternalAdapter:
            def deliver(self, **kwargs):
                raise AssertionError("Must never invoke external adapter")

        with self.assertRaisesRegex(DeliveryError, "built-in synthetic"):
            self.run_attempt(adapter=ExternalAdapter())

    def test_attachment_symlinks_and_oversize_files_rejected(self):
        real = Path(self.temp.name) / "real.txt"
        real.write_text("bytes")
        link = Path(self.temp.name) / "link.txt"
        link.symlink_to(real)
        with self.assertRaisesRegex(DeliveryError, "symlinks"):
            bundle(attachments={"resume.txt": link})
        with self.assertRaisesRegex(DeliveryError, "size limit"):
            bundle(attachments={"resume.txt": b"x" * (MAX_ATTACHMENT_BYTES + 1)})

    def test_database_is_durable_private_and_not_symlinked(self):
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        for suffix in ("-wal", "-shm"):
            sidecar = Path(str(self.path) + suffix)
            if sidecar.exists():
                self.assertEqual(sidecar.stat().st_mode & 0o777, 0o600)
        with self.assertRaisesRegex(DeliveryError, "durable"):
            DeliveryStore(":memory:")
        link = Path(self.temp.name) / "link.sqlite"
        link.symlink_to(self.path)
        with self.assertRaisesRegex(DeliveryError, "symlinks"):
            DeliveryStore(link)

    def test_outbox_is_metadata_and_resume_cursor_is_stable(self):
        self.run_attempt()
        events = self.store.events()
        encoded = json.dumps(events)
        self.assertNotIn("Synthetic resume version one", encoded)
        self.assertNotIn("Synthetic answer", encoded)
        self.assertTrue(all(event["data"]["execution_authorized"] is False for event in events))
        self.assertEqual(self.store.events(after_sequence=events[-1]["sequence"]), [])


if __name__ == "__main__":
    unittest.main()
