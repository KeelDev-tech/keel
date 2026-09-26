"""Deterministic, evidence-aware workflow verification (no test execution).

The caller is a trusted test adapter: it must bind observations to the tested
build/scope and verify the referenced evidence bytes. A digest is neither
authentication nor proof a browser journey ran. Results never authorize actions.
Run snapshots are pure values; the host must retain them in append-only storage.
"""
from collections import Counter
from copy import deepcopy

from keel_flow.common import boolean, canonical, digest, hexdigest, keys, records, require, text, version
from maintenance_workbench.keel_maint.contracts import identifier

STATUSES = frozenset({"PASS", "FAIL", "TOOL_ERROR", "BLOCKED", "UNVERIFIED", "SKIPPED", "FLAKY"})
MAX_CHECKS = 256
POLICY_REVISION = "workflow-evidence-v1"


def evidence_records(value):
    """Validate bounded evidence references without claiming to fetch them."""
    records(value, "evidence", maximum=256)
    seen = set()
    for item in value:
        keys(item, {"ref", "sha256"})
        text(item["ref"], "evidence ref", maximum=2048)
        hexdigest(item["sha256"])
        require(item["ref"] not in seen, "duplicate evidence reference")
        seen.add(item["ref"])
    return value


def _spec(spec):
    keys(spec, {"schema_version", "run_id", "build_sha256", "workflow_scope", "checks"})
    version(spec)
    identifier(spec["run_id"])
    hexdigest(spec["build_sha256"])
    text(spec["workflow_scope"], "workflow scope", maximum=1024)
    records(spec["checks"], "checks", maximum=MAX_CHECKS)
    require(bool(spec["checks"]), "at least one expected check required")
    checks = {}
    for check in spec["checks"]:
        keys(check, {"check_id", "depends_on"})
        cid = identifier(check["check_id"])
        require(cid not in checks, "duplicate check id")
        deps = check["depends_on"]
        require(type(deps) is list and len(deps) <= MAX_CHECKS, "bounded dependencies required")
        for dep in deps:
            identifier(dep)
        require(len(set(deps)) == len(deps), "duplicate dependency")
        checks[cid] = sorted(deps)
    for cid, deps in checks.items():
        require(all(dep in checks for dep in deps), "unknown dependency")
        require(cid not in deps, "self dependency")
    pending = dict(checks)
    order = []
    while pending:
        available = sorted(cid for cid, deps in pending.items() if all(dep not in pending for dep in deps))
        require(bool(available), "dependency cycle")
        order.extend(available)
        for cid in available:
            del pending[cid]
    return checks, order


def aggregate_run(spec, observations):
    """Aggregate one exact build and workflow; malformed inputs raise ValueError.

    Observations is an envelope with run_id/build_sha256/workflow_scope/checks.
    Each check has check_id, status, evidence, finding_code and detail. Missing
    rows and unsupported PASS/FAIL claims become UNVERIFIED. Failed dependencies
    replace descendant verdicts with BLOCKED and share the original root IDs.
    Original reports remain in the snapshot, including ignored descendant claims.
    """
    canonical({"spec": spec, "observations": observations})
    checks, order = _spec(spec)
    keys(observations, {"run_id", "build_sha256", "workflow_scope", "checks"})
    for field in ("run_id", "build_sha256", "workflow_scope"):
        require(observations[field] == spec[field], "observation " + field + " mismatch")
    records(observations["checks"], "observed checks", maximum=MAX_CHECKS)
    observed = {}
    evidence_refs = {}
    for row in observations["checks"]:
        keys(row, {"check_id", "status", "evidence", "finding_code", "detail"})
        cid = identifier(row["check_id"])
        require(cid in checks, "unexpected observed check")
        require(cid not in observed, "duplicate observed check")
        require(type(row["status"]) is str and row["status"] in STATUSES, "invalid check status")
        identifier(row["finding_code"])
        text(row["detail"], "check detail", maximum=4096)
        evidence_records(row["evidence"])
        for item in row["evidence"]:
            previous = evidence_refs.setdefault(item["ref"], item["sha256"])
            require(previous == item["sha256"], "evidence reference digest collision")
        observed[cid] = row
    evaluated = {}
    roots = {}
    for cid in order:
        row = observed.get(cid)
        reported = row["status"] if row else None
        dependencies = [dep for dep in checks[cid] if evaluated[dep]["status"] != "PASS"]
        if dependencies:
            status, reason = "BLOCKED", "dependency_not_passed"
            root_ids = sorted({root for dep in dependencies for root in evaluated[dep]["root_ids"]})
        else:
            status = reported or "UNVERIFIED"
            reason = "reported" if row else "missing_observation"
            if status in {"PASS", "FAIL"} and not row["evidence"]:
                status, reason = "UNVERIFIED", "missing_evidence"
            if status == "BLOCKED":
                status, reason = "UNVERIFIED", "blocked_without_failed_dependency"
            root_ids = []
            if status != "PASS":
                code = row["finding_code"] if reason == "reported" else reason
                root_id = digest({"check_id": cid, "finding_code": code, "status": status})
                roots[root_id] = {"root_id": root_id, "check_id": cid, "status": status,
                                  "finding_code": code, "evidence": deepcopy(row["evidence"] if row else [])}
                root_ids = [root_id]
        evaluated[cid] = {"check_id": cid, "status": status, "reported_status": reported,
                          "reason": reason, "root_ids": root_ids,
                          "blocked_by": dependencies,
                          "evidence": deepcopy(row["evidence"] if row else [])}
    normalized_spec = deepcopy(spec)
    normalized_spec["checks"] = [{"check_id": cid, "depends_on": checks[cid]} for cid in sorted(checks)]
    normalized_observations = deepcopy(observations)
    normalized_observations["checks"] = [deepcopy(observed[cid]) for cid in sorted(observed)]
    counts = Counter(row["status"] for row in evaluated.values())
    all_pass = counts["PASS"] == len(checks)
    result = {
        "schema_version": 1, "policy_revision": POLICY_REVISION,
        "spec": normalized_spec, "observations": normalized_observations,
        "status": "PASS" if all_pass else ("FAIL" if counts["FAIL"] else "INCOMPLETE"),
        "all_pass": all_pass,
        "complete": sum(counts[state] for state in ("PASS", "FAIL")) == len(checks),
        "counts": {status: counts[status] for status in sorted(STATUSES)},
        "checks": [evaluated[cid] for cid in sorted(evaluated)],
        "root_findings": [roots[rid] for rid in sorted(roots)],
        "execution_authorized": False,
    }
    result["run_sha256"] = digest(result)
    return result


def validate_run(run):
    """Reject altered snapshots; this consistency check is not authentication."""
    canonical(run)
    require(type(run) is dict and "spec" in run and "observations" in run, "verification snapshot required")
    expected = aggregate_run(run["spec"], run["observations"])
    require(canonical(run) == canonical(expected), "verification snapshot content mismatch")
    return expected


def compare_runs(previous, current, *, allow_build_change=False):
    """Compare comparable immutable snapshots without rewriting their history.

    A disappearing finding is resolved only when its originating check passes.
    Blocked/missing evidence keeps it unresolved. FLAKY requires an explicit
    observed FLAKY result; a single changed result does not prove flakiness.
    The default requires the same build, scope and dependency contract. Explicit
    allow_build_change permits tracking observed repairs across builds with an
    identical scope/check graph; it cannot establish that the code change caused
    the changed outcome. Both complete snapshots remain in the comparison.
    """
    boolean(allow_build_change, "allow_build_change")
    previous, current = validate_run(previous), validate_run(current)
    require(previous["spec"]["run_id"] != current["spec"]["run_id"], "distinct run ids required")
    for field in ("workflow_scope", "checks"):
        require(previous["spec"][field] == current["spec"][field], "runs not comparable: " + field)
    changed_build = previous["spec"]["build_sha256"] != current["spec"]["build_sha256"]
    require(not changed_build or allow_build_change, "runs not comparable: build_sha256")
    before = {r["root_id"]: r for r in previous["root_findings"]}
    after = {r["root_id"]: r for r in current["root_findings"]}
    checks_before = {row["check_id"]: row for row in previous["checks"]}
    checks_after = {row["check_id"]: row for row in current["checks"]}
    disappeared = set(before) - set(after)
    resolved = {rid for rid in disappeared if checks_after[before[rid]["check_id"]]["status"] == "PASS"}
    return {
        "schema_version": 1,
        "comparison_scope": "CROSS_BUILD" if changed_build else "SAME_BUILD",
        "previous_build_sha256": previous["spec"]["build_sha256"],
        "current_build_sha256": current["spec"]["build_sha256"],
        "build_change_causality_established": False,
        "previous_run_sha256": previous["run_sha256"], "current_run_sha256": current["run_sha256"],
        "previous_run": previous, "current_run": current,
        "new": sorted(set(after) - set(before)), "recurring": sorted(set(before) & set(after)),
        "resolved": sorted(resolved), "unresolved": sorted(disappeared - resolved),
        "flaky": sorted(cid for cid, row in checks_after.items() if row["reported_status"] == "FLAKY"),
        "status_changes": [{"check_id": cid, "before": checks_before[cid]["status"], "after": checks_after[cid]["status"]}
                           for cid in sorted(checks_after) if checks_before[cid]["status"] != checks_after[cid]["status"]],
        "execution_authorized": False,
    }
