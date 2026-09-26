"""Evidence-first ECV evaluation. No network, inference calls, or state mutation.

This is a reference monitor over trusted-adapter *observations*. The adapter
must authenticate issuers, verify source bytes, and enumerate every material
claim. Hash agreement does not establish truth, authorship, or authorization.
"""
from collections import Counter
from itertools import combinations
import unicodedata
from . import __version__
from keel_flow.common import (
    boolean, canonical, clock, digest, hexdigest, integer, keys, number,
    records, require, text, timestamp, unique, version,
)
from .lineage import evaluate_lineage

POLICY_REVISION = "ecv-evidence-first-v1"
MAX_AGE_SECONDS = 90
MAX_REVIEW_AGE_SECONDS = 900
MAX_ITERATIONS = 3
RISK_FIELDS = {"accountability", "sensitivity", "complexity", "irreversibility", "external_impact"}
ACTION_FIELDS = {"role_id", "application_id", "lead_sha256", "packet_dependency_hash",
                 "proposal", "risk", "authority", "human_review", "iteration", "reviews"}


def strings(value, name, *, empty=False, maximum=256):
    require(type(value) is list and len(value) <= maximum, name + ": bounded list required")
    require(empty or bool(value), name + ": nonempty list required")
    for item in value:
        text(item, name, maximum=256)
    require(len(value) == len(set(value)), name + ": duplicate item")
    return value


def _window(row, *, now, max_age=None):
    observed, expires = timestamp(row["observed_at"]), timestamp(row["expires_at"])
    require(expires > observed, "invalid validity interval")
    age = (clock(now) - observed).total_seconds()
    return observed <= now < expires and (max_age is None or 0 <= age <= max_age)


def proposal_scope(action):
    """Exact authorization/review scope, not an authenticated capability token."""
    return digest({"policy_revision": POLICY_REVISION,
                   **{k: action[k] for k in ("role_id", "application_id", "lead_sha256",
                                            "packet_dependency_hash", "proposal", "risk")}})


def review_subject(envelope, action):
    """Bind reviews to proposal, risk, authority, complete evidence and registry.

    Reviewer outputs are deliberately excluded to avoid a circular digest.
    Evidence-order changes conservatively require a fresh review.
    """
    return digest({"policy_revision": POLICY_REVISION,
                   **{k: envelope[k] for k in ("schema_version", "source_revision", "export_sha256",
                                               "observed_at", "complete", "evidence", "facts", "reviewers")},
                   "action": {k: v for k, v in action.items() if k != "reviews"}})


def delegation(risk):
    """Conservative routing, not validated risk prediction or an override."""
    keys(risk, RISK_FIELDS)
    for name, value in risk.items():
        integer(value, name, maximum=5)
        require(value >= 1, "risk scores are 1..5")
    if max(risk.values()) >= 4:
        return "HUMAN_LED"
    if max(risk.values()) >= 3 or risk["external_impact"] > 1:
        return "HUMAN_REVIEW_REQUIRED"
    return "BOUNDED_LOCAL_CANDIDATE"


def _receipt(receipt, action, *, now, human=False):
    if receipt is None:
        return False, "MISSING"
    keys(receipt, {"status", "receipt_ref", "scope_sha256", "observed_at", "expires_at", "revision"})
    allowed = {"APPROVE", "REJECT", "PENDING"} if human else {"GRANTED", "DENIED", "UNKNOWN"}
    require(type(receipt["status"]) is str and receipt["status"] in allowed, "unrecognized receipt status")
    for field in ("receipt_ref", "revision"): text(receipt[field], field, maximum=256)
    hexdigest(receipt["scope_sha256"])
    current = _window(receipt, now=now)
    if receipt["scope_sha256"] != proposal_scope(action): return False, "SCOPE_MISMATCH"
    if not current: return False, "STALE_OR_FUTURE"
    return receipt["status"] == ("APPROVE" if human else "GRANTED"), receipt["status"]


def evaluate(envelope, *, now):
    """Return diagnostic eligibility; malformed envelopes raise ValueError.

    Missing, stale and conflicting evidence remain visible. Agreement cannot
    compensate for a failed authority, identity, provenance or reviewer check.
    """
    now = clock(now)
    canonical(envelope)  # global depth/size/finite-value bounds for programmatic use
    keys(envelope, {"schema_version", "source_revision", "export_sha256", "observed_at", "complete",
                    "evidence", "facts", "reviewers", "actions"})
    version(envelope); text(envelope["source_revision"], maximum=256)
    hexdigest(envelope["export_sha256"]); boolean(envelope["complete"])
    age = (now - timestamp(envelope["observed_at"])).total_seconds()
    global_reasons = []
    if not envelope["complete"]: global_reasons.append("assurance_export_incomplete")
    if not 0 <= age <= MAX_AGE_SECONDS: global_reasons.append("assurance_export_stale_or_future")

    nodes = records(envelope["evidence"], maximum=4096)
    # Graph implementation validates the entire graph before any claim is assessed.
    lineage = evaluate_lineage(nodes, [], now=now)
    invalid_ids = set(lineage["invalidated_ids"])
    evidence_map = {n["id"]: {"valid": n["id"] not in invalid_ids} for n in nodes}
    facts = unique(records(envelope["facts"], maximum=4096), "fact_id")
    facts_by_id, fact_index, scoped_values = {}, {}, {}
    for fact in facts:
        keys(fact, {"fact_id", "subject", "field", "value_sha256", "purposes", "targets", "evidence_roots"})
        for key in ("fact_id", "subject", "field"): text(fact[key], key, maximum=256)
        hexdigest(fact["value_sha256"])
        for key in ("purposes", "targets", "evidence_roots"): strings(fact[key], key)
        require(set(fact["evidence_roots"]) <= evidence_map.keys(), "fact references unknown evidence")
        facts_by_id[fact["fact_id"]] = fact
        fact_index.setdefault((fact["subject"], fact["field"]), []).append(fact)

    registry = {}
    for reviewer in unique(records(envelope["reviewers"], maximum=32), "reviewer_id"):
        keys(reviewer, {"reviewer_id", "method", "family", "independence_group"})
        for name, value in reviewer.items():
            text(value, name, maximum=256)
            require(not any(unicodedata.category(c).startswith("C") for c in value), "reviewer metadata contains control characters")
        registry[reviewer["reviewer_id"]] = {
            **reviewer,
            **{key: " ".join(unicodedata.normalize("NFKC", reviewer[key]).casefold().split())
               for key in ("method", "family", "independence_group")},
        }

    actions = unique(records(envelope["actions"], maximum=256), "role_id")
    require(bool(actions), "assurance action list must not be empty")
    require(sum(len(a.get("proposal", {}).get("claims", [])) for a in actions
                if type(a.get("proposal")) is dict and type(a["proposal"].get("claims")) is list) <= 4096,
            "total claim budget exceeded")
    application_ids = []
    output = []
    for action in actions:
        keys(action, ACTION_FIELDS)
        for field in ("application_id", "lead_sha256", "packet_dependency_hash"): hexdigest(action[field])
        application_ids.append(action["application_id"])
        integer(action["iteration"], "iteration", maximum=10000)
        tier = delegation(action["risk"])
        reasons = list(global_reasons)
        if action["iteration"] >= MAX_ITERATIONS: reasons.append("review_iteration_budget_exhausted")
        proposal = action["proposal"]
        keys(proposal, {"subject", "purpose", "target", "payload_sha256", "claims"})
        for field in ("subject", "purpose", "target"): text(proposal[field], field, maximum=256)
        if proposal["target"] != action["role_id"]: reasons.append("proposal_target_mismatch")
        hexdigest(proposal["payload_sha256"])
        claims = unique(records(proposal["claims"], maximum=256), "claim_id")
        require(bool(claims), "material claim inventory required; zero claims is not grounded")
        grounded, claim_results, needed_evidence = 0, [], set()
        field_values = {}
        for claim in claims:
            keys(claim, {"claim_id", "field", "value_sha256", "fact_id", "evidence_roots"})
            text(claim["field"], maximum=256); text(claim["fact_id"], maximum=256)
            hexdigest(claim["value_sha256"]); strings(claim["evidence_roots"], "evidence_roots")
            require(set(claim["evidence_roots"]) <= evidence_map.keys(), "claim references unknown evidence")
            needed_evidence.update(claim["evidence_roots"])
            field_values.setdefault(claim["field"], set()).add(claim["value_sha256"])
            errors = []
            fact = facts_by_id.get(claim["fact_id"])
            if fact is None:
                errors.append("fact_missing")
            else:
                if (fact["subject"], fact["field"], fact["value_sha256"]) != (
                        proposal["subject"], claim["field"], claim["value_sha256"]):
                    errors.append("identity_claim_drift")
                if proposal["purpose"] not in fact["purposes"] or proposal["target"] not in fact["targets"]:
                    errors.append("fact_scope_mismatch")
                if set(claim["evidence_roots"]) != set(fact["evidence_roots"]):
                    errors.append("fact_evidence_mismatch")
                # Picking one convenient fact cannot conceal contradictory current facts.
                scope_key = (proposal["subject"], claim["field"], proposal["purpose"], proposal["target"])
                if scope_key not in scoped_values:
                    scoped_values[scope_key] = {f["value_sha256"] for f in fact_index.get(scope_key[:2], [])
                                               if proposal["purpose"] in f["purposes"] and proposal["target"] in f["targets"]
                                               and all(evidence_map[root]["valid"] for root in f["evidence_roots"])}
                if len(scoped_values[scope_key]) > 1:
                    errors.append("canonical_fact_conflict")
            if not all(evidence_map[root]["valid"] for root in claim["evidence_roots"]):
                errors.append("evidence_dependency_invalid")
            grounded += not errors
            reasons.extend(errors)
            claim_results.append({"claim_id": claim["claim_id"], "grounded": not errors,
                                  "reasons": sorted(set(errors))})
        if any(len(values) > 1 for values in field_values.values()): reasons.append("conflicting_claim_values")

        authority_ok, authority_status = _receipt(action["authority"], action, now=now)
        human_ok, human_status = _receipt(action["human_review"], action, now=now, human=True)
        if not authority_ok: reasons.append("authority_" + authority_status.lower())
        # An explicit rejection blocks even a low-risk candidate, including stale rejections.
        if action["human_review"] is not None and action["human_review"]["status"] == "REJECT":
            reasons.append("human_rejected")
        if tier != "BOUNDED_LOCAL_CANDIDATE" and not human_ok:
            reasons.append("human_review_" + human_status.lower())

        expected_subject = review_subject(envelope, action)
        claim_ids = {c["claim_id"] for c in claims}
        reviews = unique(records(action["reviews"], maximum=32), "reviewer_id")
        valid_reviewers, verdicts, confidences = [], [], []
        for review in reviews:
            keys(review, {"reviewer_id", "subject_sha256", "verdict", "confidence", "covered_claim_ids",
                          "evidence_roots", "observed_at", "expires_at", "findings"})
            hexdigest(review["subject_sha256"])
            require(review["reviewer_id"] in registry, "unregistered reviewer")
            require(type(review["verdict"]) is str and review["verdict"] in {"PASS", "FAIL", "ABSTAIN"}, "unrecognized review verdict")
            strings(review["covered_claim_ids"], "covered_claim_ids", empty=True)
            strings(review["evidence_roots"], "evidence_roots", empty=True)
            strings(review["findings"], "findings", empty=True)
            require(set(review["evidence_roots"]) <= evidence_map.keys(), "review references unknown evidence")
            require(set(review["covered_claim_ids"]) <= claim_ids, "review references unknown claim")
            if review["confidence"] is not None:
                number(review["confidence"], "confidence", maximum=1)
                confidences.append(review["confidence"])
            valid = True
            if review["subject_sha256"] != expected_subject:
                reasons.append("review_subject_changed"); valid = False
            if not _window(review, now=now, max_age=MAX_REVIEW_AGE_SECONDS):
                reasons.append("review_stale_or_future"); valid = False
            if set(review["covered_claim_ids"]) != claim_ids or not needed_evidence <= set(review["evidence_roots"]):
                reasons.append("review_coverage_incomplete"); valid = False
            if not all(evidence_map[root]["valid"] for root in review["evidence_roots"]):
                reasons.append("review_evidence_invalid"); valid = False
            if review["findings"]: reasons.append("unresolved_review_findings")
            if review["verdict"] != "PASS": reasons.append("review_" + review["verdict"].lower())
            verdicts.append(review["verdict"])
            if valid: valid_reviewers.append(registry[review["reviewer_id"]])
        if len(set(verdicts)) > 1: reasons.append("reviewer_disagreement")
        # Diversity is asserted metadata, NOT proof of statistical independence.
        diversity = any(all(left[key] != right[key] for key in ("method", "family", "independence_group"))
                        for left, right in combinations(valid_reviewers, 2))
        if not diversity: reasons.append("reviewer_diversity_unestablished")
        reasons = sorted(set(reasons))
        output.append({"role_id": action["role_id"], "checks_passed": not reasons,
                       "state": "CHECKS_PASSED" if not reasons else "REVIEW_REQUIRED",
                       "reasons": reasons, "delegation": tier,
                       "source_grounding_index": grounded / len(claims),
                       "claim_count": len(claims), "claims": claim_results,
                       "review_subject_sha256": expected_subject,
                       "reviewer_diversity_constraint_met": diversity,
                       "review_confidence_spread": max(confidences) - min(confidences) if len(confidences) >= 2 else None,
                       "confidence_calibrated": False, "authority_observation_matched": authority_ok,
                       "human_review_observation_matched": human_ok,
                       "execution_authorized": False})
    require(len(set(application_ids)) == len(application_ids), "duplicate application identity")
    return {"schema_version": 1, "version": __version__, "policy_revision": POLICY_REVISION,
            "state": "UNVERIFIED" if global_reasons else
                     "CHECKS_PASSED" if all(r["checks_passed"] for r in output) else "REVIEW_REQUIRED",
            "evaluated_at": now.isoformat(), "source_revision": envelope["source_revision"],
            "export_sha256": envelope["export_sha256"], "assurance_sha256": digest(envelope),
            "rows": output, "invalid_evidence_count": len(lineage["invalidated_ids"]),
            "reason_counts": dict(sorted(Counter(reason for row in output for reason in row["reasons"]).items())),
            "execution_authorized": False,
            "trust_boundary": "supplied canonical observations; issuer authentication and payload extraction external",
            "effects": {"network_requests": 0, "canonical_writes": 0, "model_calls": 0}}
