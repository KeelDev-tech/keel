"""Offline authoritative-store tests. Synthetic validators authenticate no service."""
from concurrent.futures import ProcessPoolExecutor
from dataclasses import replace
import multiprocessing
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest

from security.actions.approval_gate import PersistentApprovalStore
from security.execution import ActionEnvelope, Boundary, Denied, Outcome
from security.execution.durable import DurableHostAdapter, HostValidation, SQLiteAuthority


def operator(request, context, now):
    return "synthetic-operator" if context == "synthetic-session" else None


def current_policy(db, approval, phase, now):
    return "synthetic-policy-v1"


def fixed_clock():
    return 1000


def consume_process(args):
    path, approval_id = args
    authority = SQLiteAuthority(path, canonical_store_id="test", operator_validator=operator, clock=fixed_clock)
    store = PersistentApprovalStore(authority, current_validator=current_policy)
    return store.consume(approval_id) is not None


def envelope(**changes):
    value = ActionEnvelope("worker", "role", "attempt", "approval", "browser.submit",
                           "https://jobs.example.com/apply", b"exact-content", (), 1100,
                           "nonce", "policy-v1")
    return replace(value, **changes)


def host_validation(db, action, phase, now):
    return HostValidation(actor=action.actor, attempt_id=action.attempt_id,
                          envelope_digest=action.digest, policy_revision=action.policy_revision,
                          authority_revision="host-rev1", checked_at=now, expires_at=now + 20,
                          lease_until=1100, consent=True, destination_current=True,
                          content_current=True, safe_mode=False, no_ai=False, rate_limited=False,
                          budget_scope="budget", authority_subjects=(("credential", "host-session"),))


def evidence(db, action, outcome, now):
    return outcome.evidence_id == "verified-evidence"


def reserve_process(path):
    authority = SQLiteAuthority(path, canonical_store_id="test", operator_validator=operator, clock=fixed_clock)
    host = DurableHostAdapter(authority, host_validator=host_validation, outcome_validator=evidence)
    try:
        reservation = host.reserve(envelope(), 1000)
        return host.begin_dispatch(reservation, envelope(), 1000) == envelope().digest
    except Denied:
        return False


class DurableHostTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "authority.sqlite3"
        self.time = 1000
        self.authority = SQLiteAuthority.create(self.path, canonical_store_id="test", operator_validator=operator,
                         operator_context="synthetic-session", clock=lambda: self.time)
        self.authority.set_budget("budget", 2, operator_context="synthetic-session")
        self.host = DurableHostAdapter(self.authority, host_validator=host_validation, outcome_validator=evidence)
        self.action = envelope()

    def tearDown(self):
        self.temp.cleanup()

    def grant(self, action=None):
        self.host.grant_envelope(action or self.action, operator_context="synthetic-session")

    def reserve(self):
        self.grant()
        return self.host.reserve(self.action, self.time)

    def test_defaults_are_unbound(self):
        result = Boundary(handlers={"browser.submit": lambda _: Outcome("submitted", "verified-evidence")},
                          clock=lambda: self.time).execute(self.action)
        self.assertEqual(result.code, "host_unbound")

    def test_create_requires_authenticated_operator(self):
        with self.assertRaises(Denied):
            SQLiteAuthority.create(self.path.with_name("denied.db"), canonical_store_id="test",
                operator_validator=operator, operator_context="arbitrary-model-string", clock=fixed_clock)
        self.assertFalse(self.path.with_name("denied.db").exists())

    def test_grant_requires_authenticated_operator(self):
        with self.assertRaises(Denied):
            self.host.grant_envelope(self.action, operator_context="forged")
        with self.assertRaises(Denied):
            self.host.reserve(self.action, self.time)

    def test_unknown_is_committed_before_handler(self):
        self.grant()
        calls = []
        def handler(action):
            self.assertEqual(self.authority.inspect_attempt(action.attempt_id)["state"], "UNKNOWN")
            calls.append(action.digest)
            return Outcome("submitted", "verified-evidence")
        boundary = Boundary(self.host, {"browser.submit": handler}, clock=lambda: self.time)
        self.assertEqual(boundary.execute(self.action).status, "submitted")
        self.assertEqual(boundary.execute(self.action).status, "held")
        self.assertEqual(len(calls), 1)

    def test_dispatch_restart_replay_cannot_send_again(self):
        reservation = self.reserve()
        self.host.begin_dispatch(reservation, self.action, self.time)
        reopened = SQLiteAuthority(self.path, canonical_store_id="test", operator_validator=operator, clock=lambda: self.time)
        host = DurableHostAdapter(reopened, host_validator=host_validation, outcome_validator=evidence)
        with self.assertRaises(Denied):
            host.reserve(self.action, self.time)
        with self.assertRaises(Denied):
            host.begin_dispatch(reservation, self.action, self.time)
        self.assertEqual(reopened.inspect_attempt("attempt")["state"], "UNKNOWN")

    def test_unknown_blocks_a_new_attempt_for_same_role(self):
        reservation = self.reserve()
        self.host.begin_dispatch(reservation, self.action, self.time)
        other = envelope(attempt_id="attempt2", approval_id="approval2", nonce="nonce2")
        self.grant(other)
        with self.assertRaises(Denied):
            self.host.reserve(other, self.time)

    def test_not_submitted_requires_evidence_and_new_approval_for_retry(self):
        reservation = self.reserve()
        self.host.begin_dispatch(reservation, self.action, self.time)
        with self.assertRaises(Denied):
            self.host.record_outcome(reservation, self.action, Outcome("not_submitted", "missing-receipt"), self.time)
        self.host.record_outcome(reservation, self.action, Outcome("not_submitted", "verified-evidence"), self.time)
        other = envelope(attempt_id="attempt2", approval_id="approval2", nonce="nonce2")
        self.grant(other)
        self.assertEqual(self.host.reserve(other, self.time).attempt_id, "attempt2")

    def test_duplicate_outcome_is_idempotent_and_conflict_denied(self):
        reservation = self.reserve()
        self.host.begin_dispatch(reservation, self.action, self.time)
        outcome = Outcome("submitted", "verified-evidence")
        self.host.record_outcome(reservation, self.action, outcome, self.time)
        self.time = 1200  # Late identical acknowledgement remains valid.
        self.host.record_outcome(reservation, self.action, outcome, self.time)
        self.host.note_unknown(reservation, self.action, self.time)
        with self.assertRaises(Denied):
            self.host.record_outcome(reservation, self.action, Outcome("not_submitted", "verified-evidence"), self.time)
        self.assertEqual(self.authority.inspect_attempt("attempt")["state"], "SUBMITTED")

    def test_expiry_prevents_dispatch_but_allows_late_bound_evidence(self):
        reservation = self.reserve()
        self.host.begin_dispatch(reservation, self.action, self.time)
        self.time = 1200
        self.host.record_outcome(reservation, self.action, Outcome("submitted", "verified-evidence"), self.time)
        self.assertEqual(self.authority.inspect_attempt("attempt")["state"], "SUBMITTED")

    def test_mutated_envelope_fields_cannot_use_grant(self):
        self.grant()
        for change in ({"payload": b"changed"}, {"actor": "other"}, {"policy_revision": "new-policy"},
                       {"nonce": "other"}, {"role_id": "other"}, {"destination": "https://other.example.com/apply"}):
            with self.subTest(change=change), self.assertRaises(Denied):
                self.host.reserve(replace(self.action, **change), self.time)

    def test_revocation_between_reserve_and_dispatch(self):
        reservation = self.reserve()
        self.authority.revoke("credential", "host-session", reason="logout", operator_context="synthetic-session")
        with self.assertRaises(Denied):
            self.host.begin_dispatch(reservation, self.action, self.time)
        self.assertEqual(self.authority.inspect_attempt("attempt")["state"], "RESERVED")

    def test_revocation_survives_restart(self):
        self.grant()
        self.authority.revoke("identity", "worker", reason="disabled", operator_context="synthetic-session")
        reopened = SQLiteAuthority(self.path, canonical_store_id="test", operator_validator=operator, clock=lambda: self.time)
        host = DurableHostAdapter(reopened, host_validator=host_validation, outcome_validator=evidence)
        with self.assertRaises(Denied):
            host.reserve(self.action, self.time)

    def test_429_is_absolute_durable_stop(self):
        reservation = self.reserve()
        self.authority.record_429(evidence_id="http429", validate_evidence=lambda *args: True)
        self.time = 1040
        with self.assertRaises(Denied):
            self.host.begin_dispatch(reservation, self.action, self.time)
        self.assertEqual(self.authority.inspect_attempt("attempt")["state"], "RESERVED")

    def test_unverified_429_does_not_change_state(self):
        with self.assertRaises(Denied):
            self.authority.record_429(evidence_id="http429", validate_evidence=lambda *args: False)
        reservation = self.reserve()
        self.host.begin_dispatch(reservation, self.action, self.time)

    def test_budget_exhaustion_is_atomic_between_reserved_workers(self):
        self.authority.set_budget("budget", 1, operator_context="synthetic-session")
        left = self.reserve()
        other = envelope(role_id="role2", attempt_id="attempt2", approval_id="approval2", nonce="nonce2")
        self.grant(other)
        right = self.host.reserve(other, self.time)
        self.host.begin_dispatch(left, self.action, self.time)
        with self.assertRaises(Denied):
            self.host.begin_dispatch(right, other, self.time)
        self.assertEqual(self.authority.inspect_attempt("attempt2")["state"], "RESERVED")

    def test_stale_host_snapshot_refused(self):
        self.host.host_validator = lambda *args: replace(host_validation(*args), checked_at=950)
        with self.assertRaises(Denied):
            self.grant()

    def test_authority_revision_change_refused(self):
        reservation = self.reserve()
        self.host.host_validator = lambda *args: replace(host_validation(*args), authority_revision="host-rev2")
        with self.assertRaises(Denied):
            self.host.begin_dispatch(reservation, self.action, self.time)

    def test_expiry_advancing_during_validator_refused(self):
        reservation = self.reserve()
        def slow(db, action, phase, now):
            snapshot = host_validation(db, action, phase, now)
            self.time += 101
            return snapshot
        self.host.host_validator = slow
        with self.assertRaises(Denied):
            self.host.begin_dispatch(reservation, self.action, self.time)
        self.assertEqual(self.authority.inspect_attempt("attempt")["state"], "RESERVED")

    def test_callback_exception_rolls_back_consumption(self):
        self.grant()
        def broken(*args):
            raise RuntimeError("host unavailable")
        self.host.host_validator = broken
        with self.assertRaises(RuntimeError):
            self.host.reserve(self.action, self.time)
        self.assertIsNone(self.authority.inspect_attempt("attempt"))
        self.host.host_validator = host_validation
        self.host.reserve(self.action, self.time)

    def test_clock_regression_refused(self):
        self.time = 999
        with self.assertRaises(Denied):
            self.grant()

    def test_invalid_creation_clocks_refused_before_file_creation(self):
        for value in (True, float("inf"), float("nan"), -1, "1000"):
            with self.subTest(value=value), self.assertRaises(Denied):
                SQLiteAuthority.create(self.path.with_name("bad-clock.db"), canonical_store_id="test",
                    operator_validator=operator, operator_context="synthetic-session", clock=lambda: value)
            self.assertFalse(self.path.with_name("bad-clock.db").exists())

    def test_passive_open_does_not_create_missing_database(self):
        absent = self.path.with_name("absent.db")
        with self.assertRaises(FileNotFoundError):
            SQLiteAuthority(absent, canonical_store_id="test", operator_validator=operator)
        self.assertFalse(absent.exists())

    def test_wrong_canonical_store_identity_refused(self):
        with self.assertRaises(Denied):
            SQLiteAuthority(self.path, canonical_store_id="wrong", operator_validator=operator, clock=fixed_clock)

    def test_symlink_and_hardlink_database_refused(self):
        for mode in ("symlink", "hardlink"):
            path = self.path.with_name(mode + ".db")
            os.symlink(self.path, path) if mode == "symlink" else os.link(self.path, path)
            try:
                with self.assertRaises(Denied):
                    SQLiteAuthority(path, canonical_store_id="test", operator_validator=operator, clock=fixed_clock)
            finally:
                path.unlink()

    def test_clock_watermark_survives_failed_expiry_then_restart(self):
        self.grant()
        self.time = 1101
        with self.assertRaises(ValueError):
            self.host.reserve(self.action, self.time)
        self.time = 1001
        with self.assertRaises(Denied):
            SQLiteAuthority(self.path, canonical_store_id="test", operator_validator=operator,
                            clock=lambda: self.time)

    def test_expiry_is_rechecked_after_waiting_for_database_writer(self):
        self.grant()
        locker = sqlite3.connect(self.path, isolation_level=None)
        locker.execute("BEGIN IMMEDIATE")
        entered = threading.Event()
        errors = []
        def worker():
            entered.set()
            try:
                self.host.reserve(self.action, 1000)
            except Exception as exc:
                errors.append(exc)
        thread = threading.Thread(target=worker)
        thread.start()
        self.assertTrue(entered.wait(2))
        self.time = 1101
        locker.rollback(); locker.close()
        thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIsNone(self.authority.inspect_attempt("attempt"))

    def test_operator_can_cancel_only_proven_predispatch_reservation(self):
        reservation = self.reserve()
        self.host.cancel_reserved(reservation, self.action, reason="operator changed plans",
                                  operator_context="synthetic-session")
        self.assertEqual(self.authority.inspect_attempt("attempt")["state"], "NOT_SUBMITTED")
        with self.assertRaises(Denied):
            self.host.begin_dispatch(reservation, self.action, self.time)
        other = envelope(attempt_id="attempt2", approval_id="approval2", nonce="nonce2")
        self.grant(other)
        self.host.reserve(other, self.time)

    def test_unknown_cannot_be_cancelled_without_evidence(self):
        reservation = self.reserve()
        self.host.begin_dispatch(reservation, self.action, self.time)
        with self.assertRaises(Denied):
            self.host.cancel_reserved(reservation, self.action, reason="no receipt yet",
                                      operator_context="synthetic-session")
        self.assertEqual(self.authority.inspect_attempt("attempt")["state"], "UNKNOWN")

    def test_uncertain_predispatch_commit_requires_operator_and_independent_proof(self):
        reservation = self.reserve()
        self.host.note_unknown(reservation, self.action, self.time)
        options = {"evidence_id": "host-dispatch-audit", "reason": "host proved no dispatch",
                   "operator_context": "synthetic-session"}
        with self.assertRaises(Denied):
            self.host.resolve_pending_not_dispatched(reservation, self.action, **options)
        self.host.no_dispatch_validator = lambda *args: False
        with self.assertRaises(Denied):
            self.host.resolve_pending_not_dispatched(reservation, self.action, **options)
        self.host.no_dispatch_validator = lambda *args: True
        with self.assertRaises(Denied):
            self.host.resolve_pending_not_dispatched(reservation, self.action,
                        **dict(options, operator_context="forged"))
        self.host.resolve_pending_not_dispatched(reservation, self.action, **options)
        self.assertEqual(self.authority.inspect_attempt("attempt")["state"], "NOT_SUBMITTED")
        with self.assertRaises(Denied):
            self.host.reserve(self.action, self.time)

    def test_postdispatch_unknown_cannot_use_predispatch_proof_path(self):
        reservation = self.reserve()
        self.host.begin_dispatch(reservation, self.action, self.time)
        self.host.no_dispatch_validator = lambda *args: True
        with self.assertRaises(Denied):
            self.host.resolve_pending_not_dispatched(reservation, self.action,
                evidence_id="host-audit", reason="missing receipt", operator_context="synthetic-session")

    def test_cross_process_reserve_and_dispatch_has_one_winner(self):
        self.grant()
        with ProcessPoolExecutor(max_workers=4, mp_context=multiprocessing.get_context("spawn")) as pool:
            results = list(pool.map(reserve_process, [str(self.path)] * 8))
        self.assertEqual(sum(results), 1)
        self.assertEqual(self.authority.inspect_attempt("attempt")["state"], "UNKNOWN")


class PersistentApprovalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "authority.sqlite3"
        self.time = 1000
        self.authority = SQLiteAuthority.create(self.path, canonical_store_id="test", operator_validator=operator,
                         operator_context="synthetic-session", clock=lambda: self.time)
        self.store = PersistentApprovalStore(self.authority, current_validator=current_policy)

    def tearDown(self):
        self.temp.cleanup()

    def grant(self):
        return self.store.grant("worker", "deploy", {"value": "exact"}, "synthetic-operator", 50,
                                operator_context="synthetic-session")

    def test_restart_consumption_is_single_use(self):
        grant = self.grant()
        self.assertIsNotNone(self.store.consume(grant.approval_id))
        reopened = SQLiteAuthority(self.path, canonical_store_id="test", operator_validator=operator, clock=fixed_clock)
        store = PersistentApprovalStore(reopened, current_validator=current_policy)
        self.assertIsNone(store.consume(grant.approval_id))

    def test_cross_process_consumption_has_one_winner(self):
        grant = self.grant()
        with ProcessPoolExecutor(max_workers=4, mp_context=multiprocessing.get_context("spawn")) as pool:
            results = list(pool.map(consume_process, [(str(self.path), grant.approval_id)] * 12))
        self.assertEqual(sum(results), 1)

    def test_returned_handle_mutation_cannot_change_authority(self):
        grant = self.grant()
        grant.resource_digest = "forged"
        grant.expires_at = "2099-01-01T00:00:00+00:00"
        self.assertIsNone(self.store.find_match("worker", "deploy", {"value": "different"}))
        self.time = 1050
        self.assertIsNone(self.store.consume(grant.approval_id))

    def test_revocation_persists(self):
        grant = self.grant()
        self.store.revoke(grant.approval_id, reason="cancel", operator_context="synthetic-session")
        self.assertIsNone(self.store.consume(grant.approval_id))

    def test_stale_policy_and_identity_fail_closed(self):
        grant = self.grant()
        self.store.current_validator = lambda *args: "new-authority"
        self.assertIsNone(self.store.consume(grant.approval_id))
        self.store.current_validator = current_policy
        self.authority.revoke("identity", "worker", reason="disabled", operator_context="synthetic-session")
        self.assertIsNone(self.store.consume(grant.approval_id))

    def test_caller_string_is_not_authenticated_approver(self):
        with self.assertRaises(Denied):
            self.store.grant("worker", "deploy", {}, "synthetic-operator")
        with self.assertRaises(Denied):
            self.store.grant("worker", "deploy", {}, "forged", operator_context="synthetic-session")

    def test_clear_cannot_reset_consumed_authority(self):
        with self.assertRaises(RuntimeError):
            self.store.clear()


if __name__ == "__main__":
    unittest.main()
