"""Regression tests for the publication control state machine.

Stdlib only (unittest). Run with:  python3 tests/test_state_machine.py
or:  python3 -m unittest discover -s tests
"""

import sys
import os
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from state_machine import (
    STATES,
    FREEZE_INDEX,
    PublicationControlError,
    IllegalTransitionError,
    EvidenceMissingError,
    PublicationDisabledError,
    CounselVerificationError,
    ByteMismatchError,
    PublicationStateMachine,
    sha256_hex,
)


ALLOW = lambda release_id, digest: True   # stub verifier: approves everything
DENY = lambda release_id, digest: False   # stub verifier: denies everything
RELEASE = b"keel-release-bytes-v1"        # canonical frozen bytes used in tests


def make(release_id="rel-1", verifier=ALLOW, enabled=True):
    return PublicationStateMachine(
        release_id=release_id, counsel_verifier=verifier, publication_enabled=enabled
    )


def drive_to(m, target):
    """Walk a fresh machine to `target` through the legitimate public API."""
    if STATES.index(target) >= 1:
        m.record_evidence("test_results", {"passed": True, "suite": "unit", "t": 1})
        m.advance_to_tested()
    if STATES.index(target) >= 2:
        m.record_evidence("red_team_report", {"report": "rt-1"})
        m.record_evidence("secret_scan", {"clean": True})
        m.advance_to_security_reviewed()
    if STATES.index(target) >= 3:
        m.record_evidence("privacy_analysis", {"approved": True})
        m.advance_to_privacy_review_ready()
    if STATES.index(target) >= 4:
        m.freeze_release(RELEASE)
    if STATES.index(target) >= 5:
        m.record_g1_decision(True, "counsel@example", "looks fine")
        m.advance_to_g1_approved()
    if STATES.index(target) >= 6:
        m.authorize_publication()
    if STATES.index(target) >= 7:
        m.publish(RELEASE)
    assert m.state == target, f"drive_to failed: {m.state} != {target}"
    return m


class TestHappyPath(unittest.TestCase):
    def test_full_flow_reaches_published(self):
        m = make()
        drive_to(m, "PUBLISHED")
        self.assertEqual(m.state, "PUBLISHED")
        self.assertTrue(m.is_published)

    def test_receipt_pins_frozen_digest(self):
        m = make()
        drive_to(m, "PUBLICATION_AUTHORIZED")
        digest_at_freeze = m.frozen_digest
        receipt = m.publish(RELEASE)
        self.assertEqual(receipt["artifact_digest"], digest_at_freeze)
        self.assertEqual(receipt["artifact_digest"], sha256_hex(RELEASE))
        self.assertEqual(receipt["release_id"], m.release_id)

    def test_strictly_ordered_history(self):
        m = make()
        drive_to(m, "PUBLISHED")
        transitions = [h for h in m.history if h["event"] == "transition"]
        got = [h["to"] for h in transitions]
        self.assertEqual(got, list(STATES[1:]))  # every state visited, in order


class TestNoSkipsNoBackward(unittest.TestCase):
    def test_every_skip_rejected(self):
        # from each state i, every forward jump j > i+1 is rejected
        for i in range(len(STATES)):
            for j in range(i + 2, len(STATES)):
                m = make()
                drive_to(m, STATES[i])
                with self.assertRaises(
                    IllegalTransitionError, msg=f"skip {STATES[i]} -> {STATES[j]}"
                ):
                    m._transition_to(STATES[j], "attempted skip")

    def test_every_backward_jump_rejected(self):
        # the only legal backward move is the mutation reset (check_mutation),
        # never a raw transition
        for i in range(1, len(STATES)):
            for j in range(0, i):
                m = make()
                drive_to(m, STATES[i])
                with self.assertRaises(
                    IllegalTransitionError, msg=f"backward {STATES[i]} -> {STATES[j]}"
                ):
                    m._transition_to(STATES[j], "attempted backward jump")

    def test_unknown_state_rejected(self):
        m = make()
        with self.assertRaises(IllegalTransitionError):
            m._transition_to("NOT_A_STATE", "bogus")


class TestEvidenceGates(unittest.TestCase):
    def test_tested_needs_passing_tests(self):
        m = make()
        with self.assertRaises(EvidenceMissingError):
            m.advance_to_tested()
        m.record_evidence("test_results", {"passed": False})
        with self.assertRaises(EvidenceMissingError):
            m.advance_to_tested()

    def test_security_needs_redteam_and_clean_scan(self):
        m = make()
        drive_to(m, "TESTED")
        m.record_evidence("red_team_report", {"report": "rt-1"})
        with self.assertRaises(EvidenceMissingError):  # scan missing
            m.advance_to_security_reviewed()
        m.record_evidence("secret_scan", {"clean": False})
        with self.assertRaises(EvidenceMissingError):  # scan not clean
            m.advance_to_security_reviewed()

    def test_privacy_needs_approved_analysis(self):
        m = make()
        drive_to(m, "SECURITY_REVIEWED")
        m.record_evidence("privacy_analysis", {"approved": False})
        with self.assertRaises(EvidenceMissingError):
            m.advance_to_privacy_review_ready()

    def test_public_methods_reject_wrong_state(self):
        m = make()
        with self.assertRaises(IllegalTransitionError):
            m.advance_to_security_reviewed()  # still PRIVATE_BUILD
        with self.assertRaises(IllegalTransitionError):
            m.freeze_release(RELEASE)
        with self.assertRaises(IllegalTransitionError):
            m.record_g1_decision(True, "x")
        with self.assertRaises(IllegalTransitionError):
            m.authorize_publication()
        with self.assertRaises(IllegalTransitionError):
            m.publish(RELEASE)


class TestMutationReset(unittest.TestCase):
    def test_mutation_after_freeze_resets(self):
        m = make()
        drive_to(m, "G1_APPROVED")
        self.assertTrue(m.check_mutation(b"tampered-bytes"))
        self.assertEqual(m.state, "PRIVACY_REVIEW_READY")

    def test_mutation_resets_from_any_post_freeze_state(self):
        for target in ("PUBLICATION_READY_PENDING_G1", "G1_APPROVED", "PUBLICATION_AUTHORIZED"):
            m = make()
            drive_to(m, target)
            self.assertTrue(m.check_mutation(b"changed"), f"no reset from {target}")
            self.assertEqual(m.state, "PRIVACY_REVIEW_READY")

    def test_reset_discards_freeze_and_downstream(self):
        m = make()
        drive_to(m, "G1_APPROVED")
        old_digest = m.frozen_digest
        m.check_mutation(b"changed")
        self.assertIsNone(m.frozen_digest)
        self.assertNotIn("counsel_packet", m.evidence)
        # the old digest no longer binds anything: a fresh freeze produces a new pin
        new_digest = m.freeze_release(b"changed")
        self.assertNotEqual(new_digest, old_digest)
        self.assertEqual(m.frozen_digest, sha256_hex(b"changed"))

    def test_no_reset_when_bytes_unchanged(self):
        m = make()
        drive_to(m, "PUBLICATION_AUTHORIZED")
        self.assertFalse(m.check_mutation(RELEASE))
        self.assertEqual(m.state, "PUBLICATION_AUTHORIZED")

    def test_no_reset_before_freeze(self):
        m = make()
        drive_to(m, "TESTED")
        self.assertFalse(m.check_mutation(b"anything"))
        self.assertEqual(m.state, "TESTED")

    def test_mutation_blocks_publish(self):
        # mutate then try to continue: machine must refuse the old authorization path
        m = make()
        drive_to(m, "PUBLICATION_AUTHORIZED")
        m.check_mutation(b"mutated")
        with self.assertRaises(IllegalTransitionError):
            m.publish(RELEASE)  # now in PRIVACY_REVIEW_READY; publish invalid

    def test_reset_logged(self):
        m = make()
        drive_to(m, "G1_APPROVED")
        m.check_mutation(b"changed")
        events = [h["event"] for h in m.history]
        self.assertIn("mutation_reset", events)


class TestPublishExactBytes(unittest.TestCase):
    def test_publish_non_approved_bytes_blocked(self):
        m = make()
        drive_to(m, "PUBLICATION_AUTHORIZED")
        with self.assertRaises(ByteMismatchError):
            m.publish(b"almost-the-same-release")
        self.assertEqual(m.state, "PUBLICATION_AUTHORIZED")  # still parked, no partial publish
        self.assertFalse(m.is_published)

    def test_single_bit_flip_blocked(self):
        m = make()
        drive_to(m, "PUBLICATION_AUTHORIZED")
        tampered = bytearray(RELEASE)
        tampered[0] ^= 0x01
        with self.assertRaises(ByteMismatchError):
            m.publish(bytes(tampered))

    def test_counsel_packet_binds_frozen_digest(self):
        m = make()
        drive_to(m, "PUBLICATION_READY_PENDING_G1")
        packet = m.evidence["counsel_packet"]
        self.assertEqual(packet["artifact_digest"], m.frozen_digest)
        self.assertEqual(packet["artifact_digest"], sha256_hex(RELEASE))


class TestG1Verification(unittest.TestCase):
    def test_no_verifier_denies_by_default(self):
        m = make(verifier=None)
        drive_to(m, "PUBLICATION_READY_PENDING_G1")
        m.record_g1_decision(True, "human")
        with self.assertRaises(CounselVerificationError):
            m.advance_to_g1_approved()

    def test_denying_verifier_blocks(self):
        m = make(verifier=DENY)
        drive_to(m, "PUBLICATION_READY_PENDING_G1")
        m.record_g1_decision(True, "human")
        with self.assertRaises(CounselVerificationError):
            m.advance_to_g1_approved()

    def test_exploding_verifier_denies(self):
        def boom(release_id, digest):
            raise RuntimeError("backend down")

        m = make(verifier=boom)
        drive_to(m, "PUBLICATION_READY_PENDING_G1")
        m.record_g1_decision(True, "human")
        with self.assertRaises(CounselVerificationError):
            m.advance_to_g1_approved()

    def test_g1_denial_cannot_advance(self):
        m = make()
        drive_to(m, "PUBLICATION_READY_PENDING_G1")
        with self.assertRaises(CounselVerificationError):
            m.record_g1_decision(False, "human", "not yet")
        with self.assertRaises(CounselVerificationError):
            m.advance_to_g1_approved()

    def test_verifier_flipping_false_blocks_authorization(self):
        calls = {"n": 0}

        def flip(release_id, digest):
            calls["n"] += 1
            return calls["n"] == 1  # passes at G1_APPROVED, fails at authorize

        m = make(verifier=flip)
        drive_to(m, "PUBLICATION_READY_PENDING_G1")
        m.record_g1_decision(True, "human")
        m.advance_to_g1_approved()
        with self.assertRaises(CounselVerificationError):
            m.authorize_publication()

    def test_verifier_receives_release_id_and_digest(self):
        seen = {}

        def spy(release_id, digest):
            seen["release_id"] = release_id
            seen["digest"] = digest
            return True

        m = make(release_id="rel-spy", verifier=spy)
        drive_to(m, "PUBLICATION_READY_PENDING_G1")
        m.record_g1_decision(True, "human")
        m.advance_to_g1_approved()
        self.assertEqual(seen["release_id"], "rel-spy")
        self.assertEqual(seen["digest"], sha256_hex(RELEASE))


class TestPublicationDisabled(unittest.TestCase):
    def test_flag_defaults_false(self):
        m = PublicationStateMachine(release_id="rel-d")
        self.assertFalse(m.publication_enabled)

    def test_published_unreachable_while_disabled(self):
        m = make(enabled=False)
        drive_to(m, "G1_APPROVED")
        with self.assertRaises(PublicationDisabledError):
            m.authorize_publication()
        self.assertEqual(m.state, "G1_APPROVED")

    def test_disabled_machine_cannot_publish_directly(self):
        m = make(enabled=False)
        drive_to(m, "G1_APPROVED")
        m.set_publication_enabled(True)
        m.authorize_publication()          # now legal
        m.set_publication_enabled(False)   # disable again before publish
        with self.assertRaises(PublicationDisabledError):
            m.publish(RELEASE)
        self.assertFalse(m.is_published)

    def test_g1_unresolved_blocks_everything(self):
        # G1 unresolved == machine stuck at PUBLICATION_READY_PENDING_G1:
        # authorize and publish are wrong-state AND would fail gates anyway
        m = make(enabled=True, verifier=None)
        drive_to(m, "PUBLICATION_READY_PENDING_G1")
        with self.assertRaises(IllegalTransitionError):
            m.authorize_publication()
        with self.assertRaises(IllegalTransitionError):
            m.publish(RELEASE)
        self.assertFalse(m.is_published)

    def test_enable_is_coordinator_controlled(self):
        m = make(enabled=False)
        drive_to(m, "G1_APPROVED")
        m.set_publication_enabled(True)
        m.authorize_publication()
        m.publish(RELEASE)
        self.assertTrue(m.is_published)


class TestSnapshot(unittest.TestCase):
    def test_snapshot_roundtrip(self):
        m = make(release_id="rel-snap")
        drive_to(m, "G1_APPROVED")
        snap = m.snapshot()
        m2 = PublicationStateMachine.restore(snap, counsel_verifier=ALLOW)
        self.assertEqual(m2.state, "G1_APPROVED")
        self.assertEqual(m2.frozen_digest, m.frozen_digest)
        self.assertTrue(m2.publication_enabled)
        # verifier is re-injected: mutation guard still works on the restore
        self.assertTrue(m2.check_mutation(b"changed"))
        self.assertEqual(m2.state, "PRIVACY_REVIEW_READY")


if __name__ == "__main__":
    unittest.main(verbosity=2)
