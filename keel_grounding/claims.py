"""Bind the existing trust graph to exact values in actual local files.

Trust exports are host assertions, not authentication. Exact equality proves
traceability, not entailment of reviewed prose or truth of a source assertion.
"""
from keel_trust.common import canonical, strict_json, digest, keys, records, unique, text
from keel_trust.evidence import evaluate
from .evidence import GroundingError, resolve_source, _ReadBudget

MAX_TOTAL_EVIDENCE_BYTES = 32 * 1024 * 1024


def snapshot(value):
    try:
        return strict_json(canonical(value))
    except (ValueError, TypeError, RecursionError) as exc:
        raise GroundingError("invalid_bounded_json") from exc


def _verify(document, bindings, *, root, now):
    document, bindings = snapshot(document), snapshot(bindings)
    try:
        trust = evaluate(document, now=now)
        keys(bindings, {"schema", "trust_snapshot_sha256", "sources", "claims"})
        if bindings["schema"] != "keel.grounding.bindings.v1":
            raise GroundingError("unsupported_bindings_schema")
        if bindings["trust_snapshot_sha256"] != digest(document):
            raise GroundingError("trust_snapshot_changed")
        records(bindings["sources"], maximum=128); unique(bindings["sources"], "source_id")
        records(bindings["claims"], maximum=1000); unique(bindings["claims"], "claim_id")
        sources = {s["source_id"]: s for s in document["sources"]}
        claims = {c["claim_id"]: c for c in document["claims"]}
        if not sources or {s["source_id"] for s in bindings["sources"]} != set(sources):
            raise GroundingError("source_binding_coverage_mismatch")
        if {c["claim_id"] for c in bindings["claims"]} != set(claims):
            raise GroundingError("claim_binding_coverage_mismatch")
        resolved, source_rows = {}, []
        original_sources = {r["source_id"]: r for r in trust["sources"]}
        read_budget = _ReadBudget(MAX_TOTAL_EVIDENCE_BYTES)
        for binding in bindings["sources"]:
            sid = binding["source_id"]
            reasons = list(original_sources[sid]["reasons"])
            result = None
            try:
                # Debit bytes at the actual read, including hash, JSON and
                # selector failures. Counting only successful resolutions let
                # malformed evidence evade the advertised cumulative limit.
                result = resolve_source(root, sources[sid], binding, _budget=read_budget)
            except GroundingError as exc:
                reasons.append(exc.code)
            if result is not None and not reasons:
                resolved[sid] = result
            source_rows.append({"source_id": sid, "revision": sources[sid]["revision"],
                "content_sha256": sources[sid]["content_hash"],
                "status": "BLOCKED" if reasons else "VERIFIED", "reasons": sorted(set(reasons)),
                "byte_binding_verified": not reasons,
                "source_authenticity_verified": False, "can_issue_instructions": False})
        original_claims = {r["claim_id"]: r for r in trust["claims"]}
        claim_rows = []
        for binding in bindings["claims"]:
            keys(binding, {"claim_id", "revision", "value", "selections"})
            cid = binding["claim_id"]; claim = claims[cid]
            reasons = list(original_claims[cid]["reasons"])
            if binding["revision"] != claim["revision"]: reasons.append("claim_revision_changed")
            if digest(binding["value"]) != claim["value_hash"]: reasons.append("claim_value_hash_mismatch")
            selections = records(binding["selections"], maximum=100)
            unique(selections, "source_id")
            if {s["source_id"] for s in selections} != {s["source_id"] for s in claim["evidence"]}:
                reasons.append("claim_evidence_coverage_mismatch")
            for selection in selections:
                keys(selection, {"source_id", "selector_id"}); text(selection["selector_id"])
                selected = resolved.get(selection["source_id"], {}).get("selectors", {})
                if selection["selector_id"] not in selected:
                    reasons.append("selected_evidence_unavailable")
                elif canonical(selected[selection["selector_id"]]) != canonical(binding["value"]):
                    reasons.append("selected_value_mismatch")
            claim_rows.append({"claim_id": cid, "revision": claim["revision"],
                "value_hash": claim["value_hash"], "status": "BLOCKED" if reasons else "VERIFIED",
                "reasons": sorted(set(reasons)), "exact_source_value_verified": not reasons,
                "truth_independently_verified": False, "wording_entailment_verified": False})
        bad_claims = {r["claim_id"] for r in claim_rows if r["status"] != "VERIFIED"}
        artifact_rows = {r["artifact_id"]: {**r, "reasons": list(r["reasons"])} for r in trust["artifacts"]}
        for artifact in document["artifacts"]:
            row = artifact_rows[artifact["artifact_id"]]
            for statement in artifact["statements"]:
                if statement["claim_id"] in bad_claims:
                    row["reasons"].append("grounding_claim_blocked:" + statement["claim_id"])
        # Existing reducer already detects cycles; this adds file failures and
        # propagates them transitively without ever clearing an old reason.
        for _ in range(len(artifact_rows)):
            changed = False
            for artifact in document["artifacts"]:
                row = artifact_rows[artifact["artifact_id"]]
                for parent in artifact["depends_on"]:
                    reason = "grounding_dependency_blocked:" + parent
                    if artifact_rows[parent]["reasons"] and reason not in row["reasons"]:
                        row["reasons"].append(reason); changed = True
            if not changed: break
        for row in artifact_rows.values():
            row["reasons"] = sorted(set(row["reasons"]))
            row["status"] = "BLOCKED" if row["reasons"] else "VERIFIED"
        rows = source_rows + claim_rows + list(artifact_rows.values())
        report = {"schema": "keel.grounding.evidence.v1", "workspace_id": document["workspace_id"],
            "status": "VERIFIED" if all(r["status"] == "VERIFIED" for r in rows) else "BLOCKED",
            "trust_snapshot_sha256": digest(document), "bindings_sha256": digest(bindings),
            "evaluated_at": now.isoformat(), "sources": source_rows, "claims": claim_rows,
            "artifacts": list(artifact_rows.values()), "execution_authorized": False,
            "source_authenticity_verified": False, "reviewer_authentication_verified": False,
            "truth_independently_verified": False, "queue_writes": 0,
            "boundary": "Exact local file values at verification time; host must authenticate exports and reviews, and recheck before use."}
        return report, resolved, document
    except GroundingError:
        raise
    except (ValueError, KeyError, TypeError, AttributeError, RecursionError) as exc:
        raise GroundingError("invalid_grounding_contract") from exc


def verify_grounding(document, bindings, *, root, now):
    """Public report omits selected values and source passages."""
    return _verify(document, bindings, root=root, now=now)[0]
