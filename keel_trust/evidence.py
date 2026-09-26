"""Claim lineage and transitive material invalidation from canonical exports.

Reported verification is a caller assertion. This reducer does not authenticate
documents, issue consent, infer natural-language truth, or edit canonical state.
"""
from collections import defaultdict, deque
from .common import (envelope, unique, keys, text, strings, choice, hexdigest,
                     timestamp, require, digest, records)


def evaluate(document, *, now):
    current = envelope(document, {"sources", "claims", "artifacts"}, now=now)
    workspace = document["workspace_id"]
    unique(document["sources"], "source_id"); unique(document["claims"], "claim_id"); unique(document["artifacts"], "artifact_id")
    source_map, source_rows = {}, []
    for row in document["sources"]:
        keys(row, {"source_id", "workspace_id", "publisher_id", "source_ref", "revision", "content_hash", "origin",
                   "observed_at", "expires_at", "status", "verification_ref"})
        require(row["workspace_id"] == workspace, "source workspace mismatch")
        for field in ("publisher_id", "source_ref", "revision"): text(row[field], field)
        hexdigest(row["content_hash"])
        choice(row["origin"], {"APPLICANT_RECORD", "EMPLOYER", "INDEPENDENT", "EXTERNAL_UNTRUSTED"}, "origin")
        choice(row["status"], {"ACTIVE", "DISPUTED", "REVOKED"}, "source status")
        issued, expires = timestamp(row["observed_at"]), timestamp(row["expires_at"])
        require(expires > issued, "source expiry must follow observation")
        if row["verification_ref"] is not None: text(row["verification_ref"])
        reasons = []
        if not current: reasons.append("EXPORT_NOT_CURRENT")
        if not issued <= now < expires: reasons.append("SOURCE_STALE_OR_FUTURE")
        if row["status"] != "ACTIVE": reasons.append("SOURCE_" + row["status"])
        source_map[row["source_id"]] = (row, reasons)
        source_rows.append({"source_id": row["source_id"], "origin": row["origin"], "reasons": reasons,
                            "usable_as_evidence": not reasons, "can_issue_instructions": False})
    claims, grouped, conflicts = {}, defaultdict(list), defaultdict(set)
    for row in document["claims"]:
        keys(row, {"claim_id", "workspace_id", "subject_id", "predicate", "value_hash", "revision", "kind", "basis",
                   "review_state", "approval_ref", "allowed_scopes", "allowed_wording", "evidence", "expires_at", "conflicts_with"})
        require(row["workspace_id"] == workspace, "claim workspace mismatch")
        for field in ("subject_id", "predicate", "revision"): text(row[field], field)
        hexdigest(row["value_hash"])
        choice(row["kind"], {"FACT", "EXPERIENCE", "ROLE_REQUIREMENT"}, "claim kind")
        choice(row["basis"], {"SELF_ATTESTED", "EMPLOYER_STATED", "CORROBORATED", "ADAPTER_VERIFIED"}, "claim basis")
        choice(row["review_state"], {"APPROVED", "PENDING", "DISPUTED", "REVOKED"}, "review state")
        strings(row["allowed_scopes"], "allowed scopes", nonempty=True)
        strings(row["allowed_wording"], "allowed wording", nonempty=True)
        strings(row["conflicts_with"], "conflicts")
        if row["approval_ref"] is not None: text(row["approval_ref"])
        reasons, origins, publishers, verified = [], set(), set(), False
        if not current: reasons.append("EXPORT_NOT_CURRENT")
        if timestamp(row["expires_at"]) <= now: reasons.append("CLAIM_EXPIRED")
        if row["review_state"] != "APPROVED" or row["approval_ref"] is None: reasons.append("CLAIM_NOT_APPROVED")
        bindings = records(row["evidence"], "claim evidence", maximum=100)
        require(bindings, "claim requires evidence bindings")
        unique(bindings, "source_id")
        for binding in bindings:
            keys(binding, {"source_id", "revision", "content_hash"}); text(binding["revision"]); hexdigest(binding["content_hash"])
            require(binding["source_id"] in source_map, "unknown evidence source")
            source, bad = source_map[binding["source_id"]]
            if bad: reasons.append("EVIDENCE_UNAVAILABLE")
            if source["revision"] != binding["revision"] or source["content_hash"] != binding["content_hash"]:
                reasons.append("EVIDENCE_CHANGED")
            origins.add(source["origin"]); publishers.add(source["publisher_id"])
            verified |= source["verification_ref"] is not None and source["origin"] != "EXTERNAL_UNTRUSTED"
        if row["basis"] == "SELF_ATTESTED" and "APPLICANT_RECORD" not in origins: reasons.append("BASIS_UNSUPPORTED")
        if row["basis"] == "EMPLOYER_STATED" and "EMPLOYER" not in origins: reasons.append("BASIS_UNSUPPORTED")
        if row["basis"] == "CORROBORATED" and (len(publishers) < 2 or origins == {"EXTERNAL_UNTRUSTED"}): reasons.append("CORROBORATION_INSUFFICIENT")
        if row["basis"] == "ADAPTER_VERIFIED" and not verified: reasons.append("VERIFICATION_REFERENCE_MISSING")
        claims[row["claim_id"]] = (row, reasons)
        # A pending conflicting value also blocks reuse until reconciled; a revoked historical value does not.
        if row["review_state"] != "REVOKED" and timestamp(row["expires_at"]) > now:
            for scope in row["allowed_scopes"]: grouped[(row["subject_id"], row["predicate"], scope)].append(row)
    for cid, (row, _) in claims.items():
        require(set(row["conflicts_with"]) <= claims.keys() and cid not in row["conflicts_with"], "invalid claim conflict reference")
        for other in row["conflicts_with"]:
            conflicts[cid].add(other); conflicts[other].add(cid)
    for rows in grouped.values():
        if len({row["value_hash"] for row in rows}) > 1:
            for row in rows: conflicts[row["claim_id"]].update(other["claim_id"] for other in rows if other["claim_id"] != row["claim_id"])
    claim_rows = []
    for cid, (row, reasons) in sorted(claims.items()):
        if conflicts[cid]: reasons.append("CONTRADICTORY_CLAIMS")
        claim_rows.append({"claim_id": cid, "revision": row["revision"], "basis": row["basis"],
                           "status": "VALID_FOR_REVIEW" if not reasons else "BLOCKED", "reasons": sorted(set(reasons)),
                           "conflicts_with": sorted(conflicts[cid]), "truth_independently_verified": False})
    artifacts = {row["artifact_id"]: row for row in document["artifacts"]}
    findings, dependents, degree = {}, defaultdict(list), {}
    for aid, row in artifacts.items():
        keys(row, {"artifact_id", "workspace_id", "revision", "scope", "statements", "depends_on"})
        require(row["workspace_id"] == workspace, "artifact workspace mismatch")
        text(row["revision"]); text(row["scope"]); strings(row["depends_on"], "artifact dependencies")
        require(set(row["depends_on"]) <= artifacts.keys() and aid not in row["depends_on"], "invalid artifact dependency")
        statements = records(row["statements"], "statements", maximum=1000)
        require(statements or row["depends_on"], "artifact must declare its claims or dependencies")
        reasons = [] if current else ["EXPORT_NOT_CURRENT"]
        for item in statements:
            keys(item, {"claim_id", "claim_revision", "wording"}); text(item["wording"])
            require(item["claim_id"] in claims, "artifact references unknown claim")
            claim, errors = claims[item["claim_id"]]
            if errors: reasons.append("CLAIM_BLOCKED:" + item["claim_id"])
            if item["claim_revision"] != claim["revision"]: reasons.append("CLAIM_REVISION_CHANGED:" + item["claim_id"])
            if row["scope"] not in claim["allowed_scopes"]: reasons.append("CLAIM_SCOPE_MISMATCH:" + item["claim_id"])
            if item["wording"] not in claim["allowed_wording"]: reasons.append("WORDING_NOT_APPROVED:" + item["claim_id"])
        if any(artifacts[parent]["scope"] != row["scope"] for parent in row["depends_on"]):
            reasons.append("DEPENDENCY_SCOPE_MISMATCH")
        findings[aid] = reasons; degree[aid] = len(row["depends_on"])
        for parent in row["depends_on"]: dependents[parent].append(aid)
    queue = deque(sorted(aid for aid in artifacts if degree[aid] == 0)); visited = set()
    while queue:
        aid = queue.popleft(); visited.add(aid)
        for child in dependents[aid]:
            if findings[aid]: findings[child].append("DEPENDENCY_INVALID:" + aid)
            degree[child] -= 1
            if degree[child] == 0: queue.append(child)
    for aid in set(artifacts) - visited: findings[aid].append("DEPENDENCY_CYCLE_OR_BLOCKED_BY_CYCLE")
    artifact_rows = [{"artifact_id": aid, "revision": artifacts[aid]["revision"],
                      "status": "INVALID" if reasons else "VALID_FOR_REVIEW", "reasons": sorted(set(reasons)),
                      "release_authorized": False} for aid, reasons in sorted(findings.items())]
    return {"schema_version": 1, "workspace_id": workspace, "snapshot_sha256": digest(document), "current": current,
            "sources": source_rows, "claims": claim_rows, "artifacts": artifact_rows,
            "invalidated_artifact_ids": [row["artifact_id"] for row in artifact_rows if row["status"] == "INVALID"],
            "execution_authorized": False, "queue_writes": 0,
            "evidence_boundary": "canonical-export assertions; referenced documents and reviewers are not authenticated by this reducer"}
