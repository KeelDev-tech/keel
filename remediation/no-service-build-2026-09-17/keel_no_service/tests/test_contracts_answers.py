import copy
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from keel_local import contracts as c
from keel_local.answers import resolve_answer, question_fingerprint, brief_answer

NOW = datetime(2026, 9, 17, 12, tzinfo=timezone.utc)


def answer():
    return {"schema_version": 1, "candidate_id": "synthetic-candidate", "kind": "consent",
            "question_hash": "qhash", "field_type": "checkbox", "quarantined": False,
            "source_ref": "fixture-source", "verification_ref": "fixture-bank-review",
            "authorization_ref": "fixture-human-review", "issued_at": "2026-09-17T10:00:00Z",
            "expires_at": "2026-09-18T00:00:00Z", "scope": {"type": "employer", "employer_id": "acme"},
            "value": False}


CONTEXT = {"candidate_id": "synthetic-candidate", "kind": "consent", "question_hash": "qhash",
           "field_type": "checkbox", "employer_id": "acme", "posting_id": "10"}


class Contracts(unittest.TestCase):
    def test_numeric_traps(self):
        for value in (True, False, "1", float("inf"), float("nan"), -1):
            with self.subTest(value=value), self.assertRaises(c.ContractError):
                c.number(value)

    def test_integer_exact_type(self):
        for value in (True, 1.0, -1):
            with self.assertRaises(c.ContractError): c.integer(value)

    def test_json_ambiguous_or_nonfinite(self):
        for raw in ('{"a":1,"a":2}', '[NaN]', '[Infinity]', '[1e999]'):
            with self.assertRaises((ValueError, c.ContractError)): c.strict_json(raw)

    def test_version_requires_exact_supported_integer(self):
        for version in (True, 2, "1", None):
            with self.assertRaises(c.ContractError): c.versioned({"schema_version": version})

    def test_timestamp_requires_zone(self):
        with self.assertRaises(c.ContractError): c.timestamp("2026-09-17T10:00:00")

    def test_identity_candidate_employer_separation(self):
        base = c.application_identity("alice", "greenhouse", "acme", "1")
        self.assertNotEqual(base, c.application_identity("bob", "greenhouse", "acme", "1"))
        self.assertNotEqual(base, c.application_identity("alice", "greenhouse", "other", "1"))
        self.assertEqual(base, c.application_identity("alice", "GREENHOUSE", "acme", "1"))

    def test_malformed_urls(self):
        for url in ("http://a.test", "file:///a", "https://u:p@a.test/x", "https://a.test:444/x", "https://a.test/\nx"):
            self.assertFalse(c.public_url_shape(url))

    def test_approved_asset_remains_original_after_same_name_replacement(self):
        with tempfile.TemporaryDirectory() as d:
            source = Path(d)/"resume.pdf"; source.write_bytes(b"approved bytes")
            record = c.freeze_asset(source, Path(d)/"blobs", "application/pdf")
            source.write_bytes(b"replacement bytes")
            self.assertEqual(c.approved_bytes(Path(d)/"blobs", record), b"approved bytes")

    def test_modified_blob_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            source = Path(d)/"file"; source.write_bytes(b"original")
            asset = c.freeze_asset(source, Path(d)/"blobs", "text/plain")
            (Path(d)/"blobs"/asset["sha256"]).write_bytes(b"modified")
            with self.assertRaises(c.ContractError): c.approved_bytes(Path(d)/"blobs", asset)

    def test_blob_path_traversal(self):
        with self.assertRaises(c.ContractError): c.approved_bytes("/tmp", {"sha256":"../x", "size":1})

    def test_rights_never_default_cleared(self):
        with self.assertRaises(c.ContractError):
            c.release_bundle("connector", "account", "v1", "caption", [{"sha256":"a"}], {})

    def test_account_bound_to_release(self):
        asset = {"sha256": "a"}; rights={"a":{"status":"cleared", "evidence_ref":"e", "reviewer_ref":"r", "inspection_ref":"i"}}
        _, first = c.release_bundle("c", "account-1", "r1", "caption", [asset], rights)
        _, second = c.release_bundle("c", "account-2", "r1", "caption", [asset], rights)
        self.assertNotEqual(first, second)


class Answers(unittest.TestCase):
    def resolve(self, record=None, **changes):
        ctx = {**CONTEXT, **changes}
        return resolve_answer([record or answer()], ctx, now=NOW)

    def test_false_consent_is_valid_answer(self):
        result = self.resolve(); self.assertEqual(result["status"], "RESOLVED")
        self.assertIs(result["value"], False)

    def test_employer_scope_never_crosses(self):
        self.assertEqual(self.resolve(employer_id="other")["status"], "NEEDS_USER")

    def test_global_consent_quarantined(self):
        record = answer(); record["scope"]={"type":"global_fact", "explicit_global_authorization_ref":"x"}
        self.assertEqual(self.resolve(record)["status"], "NEEDS_USER")

    def test_future_expired_and_unverified_records(self):
        for key, value in (("issued_at", "2026-09-18T00:00:00Z"), ("expires_at", "2026-09-17T11:00:00Z"),
                           ("verification_ref", ""), ("quarantined", True), ("schema_version", True)):
            record = answer(); record[key] = value
            with self.subTest(key=key): self.assertEqual(self.resolve(record)["status"], "NEEDS_USER")

    def test_unaided_never_auto_authorized(self):
        for kind in ("no_ai", "unaided", "original_unassisted"):
            self.assertEqual(self.resolve(kind=kind)["reason"], "unaided_work_boundary")

    def test_question_and_field_type_must_match(self):
        self.assertEqual(self.resolve(field_type="radio")["status"], "NEEDS_USER")
        self.assertEqual(self.resolve(question_hash="new")["status"], "NEEDS_USER")

    def test_conflicting_answers_abstain(self):
        a,b = answer(), answer(); b["value"] = True
        result = resolve_answer([a,b], CONTEXT, now=NOW)
        self.assertEqual(result["reason"], "conflicting_answers")

    def test_fact_scope_requires_explicit_global_authority(self):
        a=answer(); a.update(kind="fact", scope={"type":"global_fact"})
        self.assertEqual(self.resolve(a, kind="fact")["status"], "NEEDS_USER")
        a["scope"]["explicit_global_authorization_ref"]="fixture-reviewed-global-fact"
        self.assertEqual(self.resolve(a, kind="fact")["status"], "RESOLVED")

    def test_attestation_allowlist_is_required(self):
        a=answer(); a.update(kind="attestation", attestation_key="arbitration")
        self.assertEqual(self.resolve(a, kind="attestation")["status"], "NEEDS_USER")
        self.assertEqual(self.resolve(a, kind="attestation", authorized_attestation_keys=["arbitration"])["status"], "RESOLVED")

    def test_brief_keeps_scope_and_provenance(self):
        result = brief_answer(self.resolve())
        self.assertEqual(result["scope"]["employer_id"], "acme")
        self.assertIn("verification_ref", result)

    def test_options_bind_question_fingerprint(self):
        self.assertNotEqual(question_fingerprint("Consent?", "radio", ["Yes","No"]),
                            question_fingerprint("Consent?", "radio", ["Agree","Decline"]))

    def test_resolved_scope_does_not_alias_mutable_bank_record(self):
        a=answer(); resolved=self.resolve(a);a["scope"]["employer_id"]="changed"
        self.assertEqual(resolved["scope"]["employer_id"],"acme")
