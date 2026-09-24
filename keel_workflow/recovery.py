"""Recovery evidence evaluation: healthy infrastructure is not restored data.

This module performs no backup, restore, database call, or execution. The host
must enumerate the actual restored objects and authenticate/check all evidence.
Digest equality alone cannot show that an inventory came from a trusted host.
"""
from keel_flow.common import boolean, canonical, digest, hexdigest, keys, records, require, text, version
from maintenance_workbench.keel_maint.contracts import identifier
from .verification import evidence_records, validate_run

REQUIRED_CHECKS = frozenset({"authorization", "integrity", "synthetic_workflow"})
MAX_OBJECTS = 10000


def _objects(rows, *, expected):
    records(rows, "recovery objects", maximum=MAX_OBJECTS)
    if expected:
        require(bool(rows), "nonempty expected application manifest required")
    result = {}
    for row in rows:
        keys(row, {"object_id", "kind", "version", "sha256"})
        text(row["object_id"], "object id", maximum=512)
        identifier(row["kind"])
        text(row["version"], "object version", maximum=128)
        hexdigest(row["sha256"])
        require(row["object_id"] not in result, "duplicate recovery object")
        result[row["object_id"]] = row
    return result


def _inputs(expected_manifest, observed_inventory):
    canonical({"expected": expected_manifest, "observed": observed_inventory})
    keys(expected_manifest, {"schema_version", "snapshot_id", "build_sha256", "objects"})
    version(expected_manifest)
    identifier(expected_manifest["snapshot_id"])
    hexdigest(expected_manifest["build_sha256"])
    keys(observed_inventory, {"snapshot_id", "build_sha256", "provider_healthy", "inventory_complete", "objects", "evidence"})
    identifier(observed_inventory["snapshot_id"])
    hexdigest(observed_inventory["build_sha256"])
    boolean(observed_inventory["provider_healthy"])
    boolean(observed_inventory["inventory_complete"])
    evidence_records(observed_inventory["evidence"])
    return _objects(expected_manifest["objects"], expected=True), _objects(observed_inventory["objects"], expected=False)


def recovery_scope(expected_manifest, observed_inventory):
    """Exact scope for the recovery QA run; changes require fresh observations."""
    _inputs(expected_manifest, observed_inventory)
    return "recovery:" + digest({"expected_manifest": expected_manifest, "observed_inventory": observed_inventory})


def evaluate_recovery(expected_manifest, observed_inventory, verification_run):
    """Return review readiness over supplied evidence, never restore authority.

    Required check IDs are authorization, integrity, synthetic_workflow. Every
    expected verification check must pass, including additional host checks.
    Run scope must equal recovery_scope(expected_manifest, observed_inventory).
    """
    expected, observed = _inputs(expected_manifest, observed_inventory)
    run = validate_run(verification_run)
    issues = []
    def issue(code, object_id=None):
        issues.append({"code": code, "object_id": object_id})
    if expected_manifest["snapshot_id"] != observed_inventory["snapshot_id"]:
        issue("snapshot_mismatch")
    if expected_manifest["build_sha256"] != observed_inventory["build_sha256"]:
        issue("build_mismatch")
    if not observed_inventory["provider_healthy"]:
        issue("provider_unhealthy")
    if not observed_inventory["inventory_complete"]:
        issue("inventory_incomplete")
    if not observed_inventory["evidence"]:
        issue("inventory_evidence_missing")
    for oid in sorted(set(expected) - set(observed)):
        issue("missing_object", oid)
    for oid in sorted(set(observed) - set(expected)):
        issue("unexpected_object", oid)
    for oid in sorted(set(expected) & set(observed)):
        for field in ("kind", "version", "sha256"):
            if expected[oid][field] != observed[oid][field]:
                issue(field + "_mismatch", oid)
    if run["spec"]["build_sha256"] != expected_manifest["build_sha256"]:
        issue("verification_build_mismatch")
    if run["spec"]["workflow_scope"] != recovery_scope(expected_manifest, observed_inventory):
        issue("verification_scope_mismatch")
    present = {row["check_id"] for row in run["checks"]}
    for cid in sorted(REQUIRED_CHECKS - present):
        issue("required_check_missing", cid)
    if not run["all_pass"]:
        issue("workflow_not_verified")
    return {
        "schema_version": 1, "status": "READY_FOR_REVIEW" if not issues else "NOT_READY",
        "ready": not issues, "issues": issues, "expected_object_count": len(expected),
        "observed_object_count": len(observed),
        "manifest_sha256": digest(expected_manifest), "inventory_sha256": digest(observed_inventory),
        "verification_run_sha256": run["run_sha256"], "execution_authorized": False,
    }
