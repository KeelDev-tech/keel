"""Existing scope/consent checks followed by actual answer-record binding."""
from keel_local.answers import resolve_answer
from keel_trust.common import digest, keys, records as record_array, unique, text
from .claims import _verify, snapshot
from .evidence import GroundingError


def resolve_grounded_answer(records, context, answer_bindings, document, bindings, *, root, now):
    """Never accepts a precomputed VERIFIED report as authority.

The entire selected bank record must exist in verified JSON evidence. Facts
and experience additionally require an exact grounded trust claim. Other
classes retain the old explicit authorization rules; no fact claim substitutes
for a consent, essay or attestation decision.
"""
    def blocked(reason):
        return {"status": "NEEDS_USER", "reason": reason, "execution_authorized": False}
    try:
        records, context = snapshot(records), snapshot(context)
        if type(records) is not list or type(context) is not dict or any(type(r) is not dict for r in records):
            return blocked("invalid_answer_grounding")
        original = resolve_answer(records, context, now=now)
        if original["status"] != "RESOLVED":
            return {**original, "execution_authorized": False}
        answer_bindings = snapshot(answer_bindings)
        record_array(answer_bindings, maximum=1000); unique(answer_bindings, "record_hash")
        for row in answer_bindings:
            keys(row, {"record_hash", "source_id", "selector_id", "claim_id", "claim_revision"})
        applicable = [b for b in answer_bindings if b["record_hash"] == original["record_hash"]]
        if len(applicable) != 1: return blocked("answer_record_binding_missing")
        binding = applicable[0]
        report, sources, checked = _verify(document, bindings, root=root, now=now)
        # Unrelated failed records remain fail closed for this supplied bundle.
        if report["status"] != "VERIFIED": return blocked("evidence_bundle_blocked")
        source = sources.get(binding["source_id"])
        if source is None or binding["selector_id"] not in source["selectors"]:
            return blocked("answer_record_source_unavailable")
        if digest(source["selectors"][binding["selector_id"]]) != original["record_hash"]:
            return blocked("answer_record_content_mismatch")
        source_row = next(s for s in checked["sources"] if s["source_id"] == binding["source_id"])
        if (source_row["source_ref"] != original["source_ref"] or
                source_row["verification_ref"] != original["verification_ref"]):
            return blocked("answer_provenance_mismatch")
        if context["kind"] in {"fact", "experience"}:
            text(binding["claim_id"]); text(binding["claim_revision"])
            claim = next((c for c in checked["claims"] if c["claim_id"] == binding["claim_id"]), None)
            if claim is None: return blocked("answer_claim_missing")
            scope = digest({"candidate_id": context["candidate_id"], "employer_id": context["employer_id"],
                            "posting_id": context.get("posting_id")})
            if (claim["revision"] != binding["claim_revision"] or claim["subject_id"] != context["candidate_id"]
                    or claim["predicate"] != context.get("claim_predicate")
                    or claim["value_hash"] != digest(original["value"]) or scope not in claim["allowed_scopes"]
                    or claim["kind"] != {"fact": "FACT", "experience": "EXPERIENCE"}[context["kind"]]
                    or binding["source_id"] not in {s["source_id"] for s in claim["evidence"]}):
                return blocked("answer_claim_binding_mismatch")
        elif binding["claim_id"] is not None or binding["claim_revision"] is not None:
            return blocked("fact_claim_cannot_authorize_personal_decision")
        return {**original, "record_content_verified": True,
            "trust_snapshot_sha256": report["trust_snapshot_sha256"], "grounding_sha256": digest(report),
            "truth_independently_verified": False, "reviewer_authentication_verified": False,
            "execution_authorized": False}
    except (GroundingError, ValueError, TypeError, KeyError, AttributeError, StopIteration):
        return blocked("invalid_answer_grounding")
