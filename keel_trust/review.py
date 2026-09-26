"""Evidence/challenge review protocol. Agreement never grants action authority."""
import hashlib
import hmac
from .common import (keys, version, text, strings, choice, require, hexdigest, timestamp,
                     unique, canonical, digest, clock)


def review_case(case, reviews, registry, *, now, trusted_review_keys=None):
    clock(now)
    keys(case, {"schema_version", "case_id", "workspace_id", "subject_hash", "human_rationale_ref", "owner", "expires_at"})
    version(case); hexdigest(case["subject_hash"])
    for field in ("case_id", "workspace_id", "human_rationale_ref"): text(case[field])
    require(case["owner"] is None or type(case["owner"]) is str, "explicit owner or null required")
    require(type(registry) is dict, "reviewer registry required")
    for identity, entry in registry.items():
        text(identity); keys(entry, {"role", "independence_group", "workspace_id"})
        choice(entry["role"], {"PRIMARY", "CHALLENGE"}, "reviewer role")
        text(entry["independence_group"]); text(entry["workspace_id"])
    unique(reviews, "review_id")
    verified, rejected = [], []
    for row in reviews:
        keys(row, {"review_id", "payload", "signature"})
        payload = row["payload"]
        keys(payload, {"schema_version", "review_id", "case_id", "workspace_id", "subject_hash", "reviewer_id", "role",
                       "decision", "evidence_refs", "issued_at", "expires_at"})
        version(payload); require(payload["review_id"] == row["review_id"], "review envelope mismatch")
        hexdigest(payload["subject_hash"]); hexdigest(row["signature"])
        for field in ("case_id", "workspace_id", "reviewer_id"): text(payload[field])
        choice(payload["role"], {"PRIMARY", "CHALLENGE"}, "review role")
        choice(payload["decision"], {"SUPPORT", "OBJECT", "INSUFFICIENT"}, "review decision")
        strings(payload["evidence_refs"], nonempty=True)
        issued, expires = timestamp(payload["issued_at"]), timestamp(payload["expires_at"])
        require(expires > issued, "invalid review interval")
        reason = None; registered = registry.get(payload["reviewer_id"])
        key = (trusted_review_keys or {}).get(payload["reviewer_id"])
        if type(key) is not bytes or len(key) < 32 or not hmac.compare_digest(row["signature"], hmac.new(key, canonical(payload), hashlib.sha256).hexdigest()):
            reason = "SIGNATURE_UNVERIFIED"
        elif registered is None or registered["role"] != payload["role"] or registered["workspace_id"] != case["workspace_id"]:
            reason = "REVIEWER_SCOPE_MISMATCH"
        elif any(payload[field] != case[field] for field in ("case_id", "workspace_id", "subject_hash")):
            reason = "CASE_OR_EVIDENCE_CHANGED"
        elif not issued <= now < expires or expires > timestamp(case["expires_at"]):
            reason = "REVIEW_EXPIRED_FUTURE_OR_OUTSIDE_CASE"
        if reason: rejected.append({"review_id": row["review_id"], "reason": reason})
        else: verified.append({**payload, "independence_group": registered["independence_group"]})
    # Repeated or conflicting current opinions from one identity cannot create a quorum.
    identities = {r["reviewer_id"] for r in verified}
    if timestamp(case["expires_at"]) <= now:
        status = "CASE_EXPIRED"
    elif not case["owner"] or not case["owner"].strip():
        status = "ASSIGN_HUMAN_OWNER"
    elif any(r["decision"] == "OBJECT" for r in verified):
        status = "DISAGREEMENT_REQUIRES_HUMAN_REVIEW"
    elif rejected or any(r["decision"] == "INSUFFICIENT" for r in verified):
        status = "MORE_VERIFIED_EVIDENCE_REQUIRED"
    elif len(verified) != len(identities):
        status = "REVIEW_IDENTITY_REPEATED"
    elif {r["role"] for r in verified} != {"PRIMARY", "CHALLENGE"}:
        status = "PRIMARY_AND_CHALLENGE_REQUIRED"
    elif ({r["independence_group"] for r in verified if r["role"] == "PRIMARY"}
          & {r["independence_group"] for r in verified if r["role"] == "CHALLENGE"}):
        status = "SEPARATE_REVIEW_PROVENANCE_REQUIRED"
    else:
        status = "READY_FOR_HUMAN_REVIEW"
    return {"case_id": case["case_id"], "subject_hash": case["subject_hash"], "status": status,
            "owner": case["owner"], "verified_review_ids": [r["review_id"] for r in verified], "rejected": rejected,
            "human_rationale_ref": case["human_rationale_ref"], "independent_judgment_established": False,
            "execution_authorized": False, "queue_writes": 0,
            "boundary": "signatures identify configured reviewers; they do not prove independent reasoning or evidence truth"}
