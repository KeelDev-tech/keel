"""Offline synthetic connector demonstration and local capacity measurement.

Run ``python -m keel_connector.demo``; add ``--benchmark`` for the 20/30-role
measurement. JSON goes to stdout. Fixture setup writes only temporary synthetic
attachments; report evaluation is separate and does not create evidence.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import timedelta
import json
import math
import os
from pathlib import Path
import platform
from statistics import median
from tempfile import TemporaryDirectory
from time import perf_counter_ns

from keel_flow.common import canonical
from keel_live.review import Principal

from .adapter import (LATENCY_BUDGET_SECONDS, MAX_DEPTH, MAX_NODES, MAX_OUTPUT_BYTES,
                      MAX_REPORT_ROLES, MAX_SNAPSHOT_BYTES, SUPPORTED_ROLES,
                      ReadinessAdapter)
from .synthetic import NOW, make_workspace, principal_for, rebind


SCENARIOS = ("current", "stale", "incomplete", "malformed", "cross_user",
             "revoked_grant", "pending_human", "expired_source", "revoked_approval")


def _adapter(document, directory, *, provider=None, now=NOW):
    principal = principal_for(document)
    return ReadinessAdapter(workspace_id=document["workspace_id"], synthetic=True,
                            principal_provider=provider or (lambda: principal),
                            snapshot_provider=lambda: document,
                            attachment_root=directory, host_clock=lambda: now)


def _files(directory):
    return {path.name: path.read_bytes() for path in directory.iterdir() if path.is_file()}


def _scenario(name, original, directory):
    document = deepcopy(original)
    principal = principal_for(document)
    now, provider = NOW, None
    grant_reads = []
    if name == "stale":
        now += timedelta(seconds=91)
    elif name == "incomplete":
        document["flow"]["complete"] = False
        rebind(document)
    elif name == "malformed":
        document["assurance"]["export_sha256"] = "invalid-synthetic-digest"
    elif name == "cross_user":
        other = Principal("synthetic-other-reader", "synthetic:other-authority",
                          "synthetic-other-workspace", frozenset({"review:read"}), ())
        provider = lambda: other
    elif name == "revoked_grant":
        revoked = Principal(principal.actor_id, principal.authority_record_ref,
                            principal.workspace_id, frozenset(), ())

        def provider():
            grant_reads.append(True)
            return principal if len(grant_reads) == 1 else revoked
    elif name == "pending_human":
        document["revision_sources"]["roles"][0]["sources"]["approval"] = {
            "source_ref": "synthetic:pending", "source_version": "fixture-v1",
            "observed_at": NOW.isoformat(),
            "expires_at": (NOW + timedelta(seconds=60)).isoformat(),
            "absence": {"kind": "HUMAN_DECISION_REQUIRED",
                        "decision_request_ref": "synthetic:existing-request"}}
    elif name == "expired_source":
        document["revision_sources"]["roles"][0]["sources"]["policy"]["expires_at"] = (
            NOW - timedelta(seconds=1)).isoformat()
    elif name == "revoked_approval":
        document["revision_sources"]["roles"][0]["sources"]["approval"]["record"]["revoked"] = True

    before, files_before = canonical(document), _files(directory)
    raw = _adapter(document, directory, provider=provider, now=now).call("list_application_blockers")
    report = json.loads(raw)
    checks = {"execution_unauthorized": report["execution_authorized"] is False,
              "bounded_output": len(raw) <= MAX_OUTPUT_BYTES,
              "input_unchanged": canonical(document) == before,
              "attachments_unchanged": _files(directory) == files_before}
    errors = {"malformed": "INVALID_SNAPSHOT", "cross_user": "ACCESS_DENIED",
              "revoked_grant": "ACCESS_DENIED"}
    if name in errors:
        checks["expected_error"] = (report["ok"] is False
                                     and report.get("error", {}).get("code") == errors[name])
        checks["no_partial_applications"] = "applications" not in report
        if name == "revoked_grant":
            checks["grant_rechecked_before_output"] = len(grant_reads) == 2
    else:
        rows = report.get("applications", [])
        checks["successful_synthetic_report"] = report.get("ok") is True and report.get("synthetic") is True
        checks["one_role_accounted_for"] = report.get("workspace_roles") == report.get("returned_roles") == len(rows) == 1
        checks["qualified_count"] = sum(row["capture_review_checks_passed"] for row in rows) == int(name == "current")
        checks["current_matches_evidence"] = report.get("current") is (name not in {"stale", "incomplete"})
        if name in {"stale", "incomplete"}:
            checks["refresh_unknown"] = all(row["state"] == "UNKNOWN" and row["next_step"] == {
                "code": "REFRESH_COMPLETE_WORKSPACE", "responsibility": "system"} for row in rows)
            checks["original_observation_preserved"] = report.get("observed_at") == original["flow"]["observed_at"]
        elif name == "pending_human":
            checks["existing_human_request"] = all(row["next_step"] == {
                "code": "REVIEW_EXISTING_APPROVAL_REQUEST", "responsibility": "human"} for row in rows)
        elif name in {"expired_source", "revoked_approval"}:
            component, status = ("policy", "STALE") if name == "expired_source" else ("approval", "REVOKED")
            checks["source_status_preserved"] = all(any(source["component"] == component and source["status"] == status
                for source in row["sources"]) for row in rows)
    return {"scenario": name, "synthetic": True, "passed": all(checks.values()),
            "checks": checks, "output_bytes": len(raw), "report": report}


def _shape(document):
    pending, nodes, depth = [(document, 0)], 0, 0
    while pending:
        value, level = pending.pop()
        nodes += 1
        depth = max(depth, level)
        if type(value) is dict:
            pending.extend((item, level + 1) for item in value.values())
        elif type(value) is list:
            pending.extend((item, level + 1) for item in value)
    return {"input_bytes": len(canonical(document)), "input_nodes": nodes, "input_depth": depth}


def _machine():
    try:
        memory = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (AttributeError, OSError, ValueError):
        memory = None
    try:
        available_cpus = len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        available_cpus = None
    cpu_model = None
    try:
        with Path("/proc/cpuinfo").open(encoding="utf-8") as stream:
            for line in stream:
                if line.startswith("model name"):
                    cpu_model = line.partition(":")[2].strip()[:256]
                    break
    except OSError:
        pass
    memory_limit = None
    try:
        with Path("/sys/fs/cgroup/memory.max").open(encoding="ascii") as stream:
            value = stream.read(64).strip()
        memory_limit = int(value) if value.isdecimal() else None
    except (OSError, ValueError):
        pass
    return {"python": platform.python_version(), "python_implementation": platform.python_implementation(),
            "system": platform.system(), "release": platform.release(), "machine": platform.machine(),
            "cpu_model": cpu_model, "logical_cpus": os.cpu_count(), "available_cpus": available_cpus,
            "physical_memory_bytes": memory, "cgroup_memory_limit_bytes": memory_limit,
            "resource_basis": "Host-reported resources; shared capacity is not reserved."}


def _measurement(document, directory, profile, samples):
    if profile == "missing_sources":
        document = deepcopy(document)
        document["revision_sources"] = None
    adapter = _adapter(document, directory)
    count = len(document["flow"]["leads"])
    shape = _shape(document)
    files_before, before = _files(directory), canonical(document)
    expected_passed = count if profile == "complete" else 0

    def valid(report):
        rows = report.get("applications", [])
        return (report.get("ok") is True and report.get("synthetic") is True
                and report.get("current") is (profile == "complete") and report.get("execution_authorized") is False
                and report.get("workspace_roles") == report.get("returned_roles") == len(rows) == count
                and sum(row["capture_review_checks_passed"] for row in rows) == expected_passed
                and (profile == "complete" or all(row["state"] == "UNKNOWN" and row["next_step"] == {
                    "code": "REFRESH_SOURCE_EXPORT", "responsibility": "system"} for row in rows)))

    # Warm filesystem caches/imports outside the reported sample window.
    warmup = adapter.call("list_application_blockers")
    samples_valid = valid(json.loads(warmup))
    durations, output_sizes = [], []
    for _ in range(samples):
        start = perf_counter_ns()
        raw = adapter.call("list_application_blockers")
        durations.append((perf_counter_ns() - start) / 1_000_000)
        output_sizes.append(len(raw))
        samples_valid = samples_valid and valid(json.loads(raw))
    ranked = sorted(durations)
    p95 = ranked[math.ceil(.95 * len(ranked)) - 1]
    attachment_count = len(files_before) if profile == "complete" else 0
    attachment_bytes = sum(map(len, files_before.values())) if profile == "complete" else 0
    checks = {"all_results_expected": samples_valid,
              "latency_budget_met": p95 <= LATENCY_BUDGET_SECONDS * 1000,
              "output_budget_met": max(output_sizes) <= MAX_OUTPUT_BYTES,
              "input_byte_bound_met": shape["input_bytes"] <= MAX_SNAPSHOT_BYTES,
              "input_node_bound_met": shape["input_nodes"] <= MAX_NODES,
              "input_depth_bound_met": shape["input_depth"] <= MAX_DEPTH,
              "input_unchanged": before == canonical(document),
              "attachments_unchanged": files_before == _files(directory)}
    return {"profile": profile, "roles": count, "samples": samples, "warmup_calls": 1,
            **shape, "attachment_count": attachment_count, "attachment_bytes": attachment_bytes,
            "fixture_attachment_count": len(files_before), "fixture_attachment_bytes": sum(map(len, files_before.values())),
            "output_max_bytes": max(output_sizes), "p50_ms": round(median(durations), 3),
            "p95_ms": round(p95, 3), "max_ms": round(max(durations), 3),
            "sample_ms": [round(value, 3) for value in durations],
            "checks": checks, "passed": all(checks.values())}


def _benchmark(directory, samples):
    measurements = []
    for count in (SUPPORTED_ROLES, MAX_REPORT_ROLES):
        root = directory / str(count)
        document = make_workspace(root, count)
        for profile in ("complete", "missing_sources"):
            measurements.append(_measurement(document, root, profile, samples))
    overload_root = directory / "overload"
    overload_document = make_workspace(overload_root, MAX_REPORT_ROLES + 1)
    raw = _adapter(overload_document, overload_root).call("list_application_blockers")
    overload = json.loads(raw)
    overload_passed = (overload.get("ok") is False
                       and overload.get("error", {}).get("code") == "WORKLOAD_EXCEEDED"
                       and "applications" not in overload and len(raw) <= MAX_OUTPUT_BYTES)
    ratio = MAX_REPORT_ROLES / SUPPORTED_ROLES
    return {"synthetic": True, "passed": ratio == 1.5 and overload_passed and all(row["passed"] for row in measurements),
            "supported_roles": SUPPORTED_ROLES, "headroom_roles": MAX_REPORT_ROLES, "headroom_ratio": ratio,
            "samples_per_profile": samples, "measurement": "Full adapter.call through encoded JSON, including both grant checks.",
            "percentile_method": "p50 median; p95 nearest rank", "clock": "perf_counter_ns",
            "budgets": {"p95_ms": LATENCY_BUDGET_SECONDS * 1000, "output_bytes": MAX_OUTPUT_BYTES},
            "environment": _machine(), "measurements": measurements,
            "overload": {"roles": MAX_REPORT_ROLES + 1, "passed": overload_passed,
                         "output_bytes": len(raw), "report": overload},
            "limitations": ["Local warmed-cache serial measurements; no endpoint, network or concurrent workload tested.",
                            "Headroom is 1.5 times the role count for these declared profiles, not every input dimension.",
                            "Synthetic attachments are 58 bytes each; large attachments and arbitrary record sizes were not benchmarked.",
                            "Missing-source profiles retain setup files but reference and evaluate no attachments."]}


def run_demo(*, benchmark=False, samples=11):
    """Return synthetic evidence; caller may serialize it without a file write."""
    if type(samples) is not int or not 5 <= samples <= 51:
        raise ValueError("samples must be an integer from 5 through 51")
    with TemporaryDirectory(prefix="keel-connector-demo-") as temporary:
        directory = Path(temporary)
        fixture_root = directory / "scenarios"
        document = make_workspace(fixture_root)
        scenarios = [_scenario(name, document, fixture_root) for name in SCENARIOS]
        result = {"schema": "keel.connector.synthetic_demo.v1", "synthetic": True,
                  "passed": all(row["passed"] for row in scenarios), "scenarios": scenarios,
                  "execution_authorized": False,
                  "setup": "Explicit synthetic fixture writes in TemporaryDirectory; deleted after the run.",
                  "verification_boundary": "Offline report behavior only; no Muse interoperability or approval established."}
        if benchmark:
            result["benchmark"] = _benchmark(directory / "benchmark", samples)
            result["passed"] = result["passed"] and result["benchmark"]["passed"]
        return result


def _samples(value):
    try:
        parsed = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("samples must be an integer from 5 through 51") from None
    if not 5 <= parsed <= 51:
        raise argparse.ArgumentTypeError("samples must be an integer from 5 through 51")
    return parsed


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--benchmark", action="store_true", help="measure supported 20-role and 30-role headroom profiles")
    parser.add_argument("--samples", type=_samples, default=11, help="samples per benchmark profile, from 5 through 51 (default: 11)")
    args = parser.parse_args(argv)
    report = run_demo(benchmark=args.benchmark, samples=args.samples)
    print(json.dumps(report, sort_keys=True, separators=(",", ":"), allow_nan=False))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
