"""Security regressions use only synthetic manifests and temporary state."""
import copy
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timezone
from unittest.mock import patch


# PORTED 2026-09-21 from Keel_Security_Repair_2026-09-21 (vendor handoff).
# The vendor's runner creates a "keel" namespace package pointing at the
# package root; replicate here so "from keel.privacy import ..." resolves
# to this candidate's keel/ dir.
import sys as _sys, types as _types
_keel_ns = _types.ModuleType("keel")
_keel_ns.__path__ = [str(Path(__file__).resolve().parents[2])]
_keel_ns.__package__ = "keel"
_sys.modules["keel"] = _keel_ns
from keel.privacy import release_manifest as rm
from keel.privacy import publication_guard as guard
from keel.privacy.inventory import ArtifactRecord
from keel.privacy import counsel_decision as cd, _ed25519, gate_release


def manifest():
    return rm.build_release([ArtifactRecord(
        artifact_id="fixture", name="fixture.txt", sha256="a" * 64,
        size_bytes=3, media_type="text/plain")]).to_dict()


def reseal(value):
    body = {k: value[k] for k in ("release_id", "created_at", "artifacts", "metadata")}
    value["manifest_digest"] = hashlib.sha256(rm.canonical(body).encode()).hexdigest()
    return value


class ReleaseValidationRegressions(unittest.TestCase):
    def test_empty_release_is_not_authorized(self):
        value = manifest()
        value["artifacts"] = []
        reseal(value)
        with tempfile.TemporaryDirectory() as state:
            self.assertFalse(guard.check_release(value, state_dir=state)["allowed"])

    def test_manifest_integrity_checked_before_approval_lookup(self):
        value = manifest()
        value["metadata"]["intended_destination"] = "changed.invalid"
        with patch.object(guard, "check_egress", return_value={"allowed": True}) as lookup:
            verdict = guard.check_release(value)
        self.assertFalse(verdict["allowed"])
        self.assertEqual(lookup.call_count, 0)

    def test_invalid_shapes_return_denial_without_lookup(self):
        for value in (None, [], {}, {"release_id": "fixture", "artifacts": []}):
            with self.subTest(value=value), patch.object(guard, "check_egress") as lookup:
                self.assertFalse(guard.check_release(value)["allowed"])
                lookup.assert_not_called()

    def test_self_hashed_invalid_manifests_rejected(self):
        mutations = [
            lambda v: v.update(artifacts=[]),
            lambda v: v.update(release_id=""),
            lambda v: v.update(created_at="not-time"),
            lambda v: v.update(metadata=[]),
            lambda v: v["artifacts"][0].update(sha256="z" * 64),
            lambda v: v["artifacts"][0].update(artifact_id=""),
            lambda v: v["artifacts"][0].update(size_bytes=-1),
            lambda v: v["artifacts"][0].update(size_bytes=True),
            lambda v: v["artifacts"].append(dict(v["artifacts"][0])),
        ]
        for index, mutate in enumerate(mutations):
            with self.subTest(index=index):
                value = manifest()
                mutate(value)
                reseal(value)
                self.assertFalse(rm.verify_manifest(value)[0])

    def test_duplicate_id_with_distinct_digest_rejected(self):
        value = manifest()
        duplicate = dict(value["artifacts"][0], sha256="b" * 64)
        value["artifacts"].append(duplicate)
        reseal(value)
        self.assertFalse(rm.verify_manifest(value)[0])

    def test_unhashed_extra_top_level_field_rejected(self):
        value = manifest()
        value["approved"] = True
        self.assertFalse(rm.verify_manifest(value)[0])

    def test_metadata_is_detached_from_caller(self):
        metadata = {"nested": {"destination": "fixture.invalid"}}
        value = rm.build_release([ArtifactRecord(
            artifact_id="fixture", name="fixture", sha256="a" * 64,
            size_bytes=1, media_type="text/plain")], metadata=metadata)
        metadata["nested"]["destination"] = "changed.invalid"
        self.assertTrue(rm.verify_manifest(value)[0])
        self.assertEqual(value.metadata["nested"]["destination"], "fixture.invalid")

    def test_build_rejects_nonhex_digest(self):
        with self.assertRaises(ValueError):
            rm.build_release([ArtifactRecord(artifact_id="fixture", name="fixture",
                sha256="z" * 64, size_bytes=1, media_type="text/plain")])

    def test_load_rejects_tampered_manifest(self):
        value = manifest()
        value["metadata"]["changed"] = True
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "manifest.json"
            path.write_text(json.dumps(value))
            with self.assertRaises(ValueError):
                rm.load_manifest(str(path))

    def test_load_rejects_duplicate_json_fields(self):
        value = manifest()
        raw = json.dumps(value)
        raw = '{"release_id": "shadow",' + raw[1:]
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "manifest.json"
            path.write_text(raw)
            with self.assertRaises(ValueError):
                rm.load_manifest(str(path))

    def test_save_rejects_invalid_manifest(self):
        value = rm.ReleaseManifest(**manifest())
        value.artifacts.clear()
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "manifest.json"
            with self.assertRaises(ValueError):
                rm.save_manifest(value, str(path))
            self.assertFalse(path.exists())

    def test_valid_manifest_roundtrip_preserved(self):
        value = rm.ReleaseManifest(**manifest())
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "manifest.json"
            rm.save_manifest(value, str(path))
            self.assertEqual(rm.load_manifest(str(path)).to_dict(), value.to_dict())


class ApprovalValidationRegressions(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = self.temp.name
        self.clock = datetime(2026, 1, 3, tzinfo=timezone.utc)
        self.seed = bytes(range(32))  # synthetic reviewer key only
        with patch.object(cd, "_utcnow", return_value=datetime(2026, 1, 1, tzinfo=timezone.utc)):
            cd.register_authority({
                "authority_reference": "FIXTURE-AUTH", "authority_name": "Fixture",
                "authority_type": "external_privacy_counsel", "scope_note": "synthetic",
                "engagement_start": "2025-01-01T00:00:00Z",
                "registered_by": "fixture", "registration_evidence": "synthetic",
                "reviewer_public_key": _ed25519.derive_public_key(self.seed).hex(),
            }, state_dir=self.state)
        self.clock_patch = patch.object(cd, "_utcnow", return_value=self.clock)
        self.clock_patch.start()
        self.addCleanup(self.clock_patch.stop)

    def approve(self, *, decision="approved", conditions=None,
                decided_at="2026-01-02T00:00:00Z", expires_at="2026-02-01T00:00:00Z"):
        fields = dict(release_id="fixture-release", artifact_digest="a" * 64,
                      decision=decision, actor="Fixture", authority_reference="FIXTURE-AUTH",
                      decided_at=decided_at, expires_at=expires_at,
                      conditions=[] if conditions is None else conditions, packet_digest="b" * 64)
        signed = dict(fields, reviewer_signature=_ed25519.sign(
            self.seed, cd.decision_signing_payload(**fields)).hex())
        return cd.record_decision(signed, state_dir=self.state)

    def verdict(self):
        return guard.check_egress("fixture-release", "a" * 64, state_dir=self.state)

    def test_unconditional_signed_approval_still_allows(self):
        self.approve()
        self.assertTrue(self.verdict()["allowed"])

    def test_conditional_approval_is_not_unrestricted_egress(self):
        self.approve(decision="approved_with_conditions", conditions=["remove sensitive fields"])
        self.assertFalse(self.verdict()["allowed"])

    def test_approved_label_with_conditions_still_denies(self):
        self.approve(conditions=["internal destination only"])
        self.assertFalse(self.verdict()["allowed"])

    def test_conditional_label_without_evaluator_still_denies(self):
        self.approve(decision="approved_with_conditions")
        self.assertFalse(self.verdict()["allowed"])

    def test_future_decision_not_effective(self):
        self.approve(decided_at="2026-01-04T00:00:00Z")
        self.assertFalse(self.verdict()["allowed"])

    def test_expiry_boundary_is_exclusive(self):
        self.approve(expires_at="2026-01-03T00:00:00Z")
        self.assertFalse(self.verdict()["allowed"])

    def test_naive_signed_timestamp_rejected(self):
        with self.assertRaises(cd.DecisionError):
            self.approve(decided_at="2026-01-02T00:00:00")

    def test_empty_registry_stays_denied(self):
        with tempfile.TemporaryDirectory() as state:
            self.assertFalse(guard.check_egress("fixture-release", "a" * 64,
                                                state_dir=state)["allowed"])

    def test_signature_stays_required(self):
        record = self.approve()
        record["reviewer_signature"] = "0" * 128
        with self.assertRaises(cd.DecisionError):
            cd.record_decision(record, state_dir=self.state)

    def test_verdict_metadata_comes_from_verified_snapshot(self):
        self.approve()
        original = cd._SealedLedger.replay
        decision_reads = 0
        def replay(ledger):
            nonlocal decision_reads
            result = original(ledger)
            if ledger.path.name == "decisions.jsonl":
                decision_reads += 1
                if decision_reads > 1:
                    return True, [{"record_type": "revocation", "release_id": "fixture-release",
                                   "artifact_digest": "a" * 64}], "synthetic intervening revocation"
            return result
        with patch.object(cd._SealedLedger, "replay", replay):
            verdict = self.verdict()
        self.assertTrue(verdict["allowed"])
        self.assertEqual(verdict["decision"]["decision"], "approved")
        self.assertEqual(decision_reads, 1)


class ArtifactHashRegressions(unittest.TestCase):
    def test_regular_file_digest_preserved(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "fixture"
            path.write_bytes(b"fixture")
            self.assertEqual(gate_release.sha256_file(str(path)), hashlib.sha256(b"fixture").hexdigest())

    def test_symlink_not_accepted_as_artifact(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "fixture"
            path.write_bytes(b"fixture")
            link = Path(temp) / "link"
            link.symlink_to(path)
            with self.assertRaises((OSError, ValueError)):
                gate_release.sha256_file(str(link))

    def test_replacement_during_hashing_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "fixture"
            path.write_bytes(b"fixture")
            real_fstat = os.fstat
            calls = 0
            def replace_after_read(fd):
                nonlocal calls
                result = real_fstat(fd)
                calls += 1
                if calls == 2:
                    replacement = Path(temp) / "other"
                    replacement.write_bytes(b"changed")
                    os.replace(replacement, path)
                return result
            with patch.object(os, "fstat", side_effect=replace_after_read):
                with self.assertRaises(ValueError):
                    gate_release.sha256_file(str(path))

    def test_cli_missing_artifact_returns_denial(self):
        with tempfile.TemporaryDirectory() as temp, patch("builtins.print"):
            self.assertEqual(gate_release.main(["--release-id", "fixture", "--artifact",
                                               str(Path(temp)/"missing")]), 1)


class Ed25519AuthorityRegressions(unittest.TestCase):
    # Independent public vectors: RFC 8032 section 7.1, tests 1-3.
    # https://www.rfc-editor.org/rfc/rfc8032#section-7.1
    VECTORS = [
        ("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60",
         "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a", "",
         "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e06522490155"
         "5fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b"),
        ("4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb",
         "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c", "72",
         "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da"
         "085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00"),
        ("c5aa8df43f9f837bedb7442f31dcb7b166d38535076f094b85ce3a2e0b4458f7",
         "fc51cd8e6218a1a38da47ed00230f0580816ed13ba3303ac5deb911548908025", "af82",
         "6291d657deec24024827e69c3abe01a30ce548a284743a445e3680d7db5ac3ac"
         "18ff9b538d16f290ae67f760984dc6594a7c15e9716ed28dc027beceea1ec40a"),
    ]

    def test_rfc8032_sign_verify_vectors(self):
        for index, vector in enumerate(self.VECTORS, 1):
            seed, public, message, signature = map(bytes.fromhex, vector)
            with self.subTest(vector=index):
                self.assertEqual(_ed25519.derive_public_key(seed), public)
                self.assertEqual(_ed25519.sign(seed, message), signature)
                self.assertTrue(_ed25519.verify(public, message, signature))

    def test_identity_key_cannot_verify_arbitrary_messages(self):
        identity = bytes.fromhex("01" + "00" * 31)
        forged = identity + bytes(32)
        for message in (b"fixture-one", b"fixture-two", b""):
            with self.subTest(message=message):
                self.assertFalse(_ed25519.verify(identity, message, forged))

    def test_small_order_keys_cannot_verify(self):
        identity = bytes.fromhex("01" + "00" * 31)
        points = [identity, bytes(32), (_ed25519._Q - 1).to_bytes(32, "little")]
        for public in points:
            with self.subTest(public=public.hex()):
                self.assertFalse(_ed25519.verify(public, b"fixture", identity + bytes(32)))

    def test_noncanonical_point_encodings_rejected(self):
        encodings = [(_ed25519._Q + 1).to_bytes(32, "little"),
                     (1 | (1 << 255)).to_bytes(32, "little"),
                     (_ed25519._Q).to_bytes(32, "little")]
        for encoded in encodings:
            with self.subTest(encoded=encoded.hex()), self.assertRaises(ValueError):
                _ed25519._decodepoint(encoded)

    def test_weak_key_registration_denied(self):
        with tempfile.TemporaryDirectory() as state, self.assertRaises(cd.DecisionError):
            cd.register_authority({
                "authority_reference": "WEAK-FIXTURE", "authority_name": "Fixture",
                "authority_type": "external_privacy_counsel", "scope_note": "synthetic",
                "engagement_start": "2025-01-01T00:00:00Z", "registered_by": "fixture",
                "registration_evidence": "synthetic", "reviewer_public_key": "01" + "00"*31,
            }, state_dir=state)

    def test_signature_scalar_malleability_rejected(self):
        _, public_hex, message_hex, signature_hex = self.VECTORS[0]
        signature = bytes.fromhex(signature_hex)
        scalar = int.from_bytes(signature[32:], "little") + _ed25519._L
        forged = signature[:32] + scalar.to_bytes(32, "little")
        self.assertFalse(_ed25519.verify(bytes.fromhex(public_hex), bytes.fromhex(message_hex), forged))

    def test_signature_low_order_r_rejected_for_genuine_key(self):
        _, public_hex, message_hex, signature_hex = self.VECTORS[0]
        signature = bytes.fromhex(signature_hex)
        for encoded in (bytes(32), (_ed25519._Q - 1).to_bytes(32, "little")):
            with self.subTest(encoded=encoded.hex()):
                self.assertFalse(_ed25519.verify(bytes.fromhex(public_hex), bytes.fromhex(message_hex),
                                                 encoded + signature[32:]))


if __name__ == "__main__":
    unittest.main()
