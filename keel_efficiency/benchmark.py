"""Replayable, matched-cohort resource/quality evaluation; standard library only.

The bundled demo executes deterministic local callbacks. Its token and latency
numbers are *synthetic estimates*, not measurements of a model or billing system.
Custom traces are declared by the caller: this module cannot authenticate billing
exports. Only a host-supplied validator can establish correctness; trace claims
and model self-scores never establish success. This module dispatches no models.
"""
from copy import deepcopy
import hashlib
import json


SCHEMA = "keel.efficiency.benchmark.v1"
RESOURCES = ("calls", "input_tokens", "output_tokens", "cached_input_tokens",
             "external_credit_micros", "compute_ms")
MAX_TASKS = 1000
MAX_ATTEMPTS = 64
MAX_VALUE = 10**15


def _json(value):
    try:
        result = json.dumps(value, sort_keys=True, separators=(",", ":"),
                            ensure_ascii=True, allow_nan=False)
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise ValueError("benchmark values must be finite JSON data") from exc
    if len(result) > 8 * 1024 * 1024:
        raise ValueError("benchmark JSON exceeds 8 MiB")
    return result


def _hash(value):
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _name(value, label):
    if not isinstance(value, str) or not value.strip() or len(value) > 256:
        raise ValueError(label + " must be a nonempty string of at most 256 characters")
    return value


def _integer(value, label):
    if type(value) is not int or not 0 <= value <= MAX_VALUE:
        raise ValueError(label + " must be a bounded nonnegative integer")
    return value


def _ratio(numerator, denominator):
    return None if denominator == 0 else numerator / denominator


def _resources(value):
    if not isinstance(value, dict) or set(value) != set(RESOURCES):
        raise ValueError("resources must contain exactly " + ", ".join(RESOURCES))
    for key, amount in value.items():
        if amount is not None:
            _integer(amount, key)
    cached, total = value["cached_input_tokens"], value["input_tokens"]
    if cached is not None and total is not None and cached > total:
        raise ValueError("cached input tokens are a subset of input tokens")
    return value


def _validate_tasks(tasks):
    if not isinstance(tasks, list) or not 1 <= len(tasks) <= MAX_TASKS:
        raise ValueError("tasks must be a nonempty list of at most 1000 tasks")
    identities = set()
    for task in tasks:
        if not isinstance(task, dict) or "task_id" not in task or "input" not in task:
            raise ValueError("each task needs task_id and input")
        task_id = _name(task["task_id"], "task_id")
        if task_id in identities:
            raise ValueError("duplicate task identity")
        identities.add(task_id)
    _json(tasks)
    return identities


def _validate_traces(traces, task_ids):
    if not isinstance(traces, list) or len(traces) != len(task_ids):
        raise ValueError("every profile must contain the complete matched task cohort")
    by_task, attempt_ids = {}, set()
    for trace in traces:
        if not isinstance(trace, dict):
            raise ValueError("task trace must be an object")
        task_id = _name(trace.get("task_id"), "task_id")
        if task_id not in task_ids or task_id in by_task:
            raise ValueError("profile task cohort differs or contains duplicates")
        if type(trace.get("claimed_success")) is not bool or "output" not in trace:
            raise ValueError("trace needs boolean claimed_success and output")
        attempts = trace.get("attempts")
        if not isinstance(attempts, list) or len(attempts) > MAX_ATTEMPTS:
            raise ValueError("attempts must be a list of at most 64 attempts")
        for attempt in attempts:
            if not isinstance(attempt, dict):
                raise ValueError("attempt must be an object")
            identity = _name(attempt.get("attempt_id"), "attempt_id")
            if identity in attempt_ids:
                raise ValueError("attempt IDs must be unique within each profile")
            attempt_ids.add(identity)
            if attempt.get("status") not in ("success", "failed", "unknown"):
                raise ValueError("attempt status must be success, failed, or unknown")
            _resources(attempt.get("resources"))
            if "latency_ms" not in attempt:
                raise ValueError("latency_ms must be explicit; unknown values use None")
            latency = attempt["latency_ms"]
            if latency is not None:
                _integer(latency, "latency_ms")
        # No-attempt results can represent reuse or deterministic work, but the
        # source must be stated. Host validation still runs on reused answers.
        if not attempts and trace.get("result_source") not in ("reused", "deterministic", "abstained"):
            raise ValueError("zero-attempt trace needs an explicit result_source")
        if trace.get("result_source") == "abstained" and trace["claimed_success"]:
            raise ValueError("abstained trace cannot claim success")
        by_task[task_id] = trace
    _json(traces)
    return by_task


def _profile(name, tasks, traces, budget, validator, manifest):
    totals = {resource: 0 for resource in RESOURCES}
    unknown = {resource: 0 for resource in RESOURCES}
    counts = {"tasks": len(tasks), "attempts": 0, "failed_attempts": 0,
              "unknown_attempts": 0, "retry_attempts": 0, "claimed_successes": 0,
              "verified_successes": 0, "false_passes": 0, "abstentions": 0,
              "validator_errors": 0, "reused_results": 0}
    rows, latency_sum, latency_unknown = [], 0, 0
    for task in tasks:
        trace = traces[task["task_id"]]
        task_resources = {resource: 0 for resource in RESOURCES}
        task_unknown = {resource: 0 for resource in RESOURCES}
        attempts = trace["attempts"]
        counts["attempts"] += len(attempts)
        counts["retry_attempts"] += max(0, len(attempts) - 1)
        task_latency, task_latency_unknown = 0, 0
        for attempt in attempts:
            counts["failed_attempts"] += int(attempt["status"] == "failed")
            counts["unknown_attempts"] += int(attempt["status"] == "unknown")
            for resource, amount in attempt["resources"].items():
                if amount is None:
                    task_unknown[resource] += 1
                    unknown[resource] += 1
                else:
                    task_resources[resource] += amount
                    totals[resource] += amount
            if attempt["latency_ms"] is None:
                task_latency_unknown += 1
                latency_unknown += 1
            else:
                task_latency += attempt["latency_ms"]
                latency_sum += attempt["latency_ms"]
        claimed = trace["claimed_success"]
        correct, validator_error = False, None
        if claimed:
            try:
                answer = validator(deepcopy(task), deepcopy(trace["output"]))
                if type(answer) is not bool:
                    raise TypeError("host validator must return a boolean")
                correct = answer
            except Exception as exc:
                # Error text can contain sensitive input; retain only the type.
                validator_error = type(exc).__name__
                counts["validator_errors"] += 1
        counts["claimed_successes"] += int(claimed)
        counts["verified_successes"] += int(claimed and correct)
        counts["false_passes"] += int(claimed and not correct)
        counts["abstentions"] += int(not claimed)
        counts["reused_results"] += int(trace.get("result_source") == "reused")
        rows.append({"task_id": task["task_id"], "task_sha256": _hash(task),
                     "claimed_success": claimed, "host_validated": correct,
                     "verified_success": claimed and correct,
                     "false_pass": claimed and not correct,
                     "validator_error": validator_error,
                     "attempts": len(attempts), "resources_known": task_resources,
                     "unknown_resource_attempts": task_unknown,
                     "attempt_latency_ms_known": task_latency,
                     "unknown_latency_attempts": task_latency_unknown})
    success = counts["verified_successes"]
    breached = [key for key, cap in budget.items() if totals[key] > cap]
    budget_unknown = [key for key in budget if unknown[key]]
    budget_status = "EXCEEDED" if breached else "UNKNOWN" if budget_unknown else "WITHIN"
    report = {
        "schema": SCHEMA + ".profile", "profile": name, **deepcopy(manifest),
        "trace_sha256": _hash([traces[task["task_id"]] for task in tasks]),
        "counts": counts,
        "quality": {"verified_success_rate": success / len(tasks),
                    "answer_coverage": counts["claimed_successes"] / len(tasks),
                    "correctness_among_claims": _ratio(success, counts["claimed_successes"]),
                    "false_pass_rate_among_claims": _ratio(counts["false_passes"], counts["claimed_successes"])},
        "resources": {"known_totals": totals, "unknown_attempts": unknown,
                      "complete": not any(unknown.values()),
                      "per_verified_success": {key: _ratio(amount, success) if not unknown[key] else None
                                               for key, amount in totals.items()},
                      "input_tokens_include_cached_tokens": True},
        "latency": {"sum_attempt_ms_known": latency_sum,
                    "unknown_attempts": latency_unknown,
                    "mean_sum_attempt_ms_per_task": latency_sum / len(tasks) if not latency_unknown else None,
                    "is_wall_clock_makespan": False},
        "budget_status": budget_status, "budget_exceeded_resources": breached,
        "budget_unknown_resources": budget_unknown, "tasks": rows,
        "execution_authorized": False,
    }
    return report


def _validate_report(report):
    """Reject malformed or internally inconsistent replay summaries.

    This checks conservation, not authenticity. A caller can invent consistent
    traces; authoritative host validation and measurement provenance remain the
    caller's responsibility.
    """
    try:
        if type(report) is not dict or report.get("schema") != SCHEMA + ".profile":
            raise ValueError("expected a profile report produced by run_benchmark")
        _name(report["profile"], "profile")
        for key in ("cohort_sha256", "validator_id", "source_id", "resource_semantics", "accounting_scope"):
            _name(report[key], key)
        if report["provenance"] not in ("synthetic", "measured"):
            raise ValueError("invalid provenance")
        budget = report["budget"]
        if type(budget) is not dict or not budget or set(budget) - set(RESOURCES):
            raise ValueError("invalid report budget")
        for key, cap in budget.items():
            _integer(cap, key)
        rows = report["tasks"]
        if type(rows) is not list or not 1 <= len(rows) <= MAX_TASKS:
            raise ValueError("invalid report task list")
        ids, sums, unknowns = set(), {key: 0 for key in RESOURCES}, {key: 0 for key in RESOURCES}
        success = claimed = false_passes = validator_errors = attempts = latency = latency_unknown = 0
        for row in rows:
            _name(row["task_id"], "task_id")
            _name(row["task_sha256"], "task_sha256")
            if row["task_id"] in ids:
                raise ValueError("duplicate report task")
            ids.add(row["task_id"])
            for key in ("claimed_success", "host_validated", "verified_success", "false_pass"):
                if type(row[key]) is not bool:
                    raise ValueError("report outcome must be boolean")
            if row["verified_success"] != (row["claimed_success"] and row["host_validated"]) or row["false_pass"] != (row["claimed_success"] and not row["host_validated"]):
                raise ValueError("inconsistent task validation")
            if row["validator_error"] is not None:
                _name(row["validator_error"], "validator_error")
                if row["host_validated"]:
                    raise ValueError("validator error cannot validate task")
                validator_errors += 1
            success += row["verified_success"]
            claimed += row["claimed_success"]
            false_passes += row["false_pass"]
            attempts += _integer(row["attempts"], "attempts")
            latency += _integer(row["attempt_latency_ms_known"], "latency")
            latency_unknown += _integer(row["unknown_latency_attempts"], "unknown_latency_attempts")
            for field, accumulator in (("resources_known", sums), ("unknown_resource_attempts", unknowns)):
                if set(row[field]) != set(RESOURCES):
                    raise ValueError("report resource dimensions differ")
                for key, amount in row[field].items():
                    accumulator[key] += _integer(amount, key)
        expected_counts = {"tasks": len(rows), "attempts": attempts, "claimed_successes": claimed,
                           "verified_successes": success, "false_passes": false_passes,
                           "abstentions": len(rows) - claimed, "validator_errors": validator_errors}
        if any(type(report["counts"][key]) is not int or report["counts"][key] != value for key, value in expected_counts.items()):
            raise ValueError("report counts do not conserve task outcomes")
        resources = report["resources"]
        if resources["known_totals"] != sums or resources["unknown_attempts"] != unknowns or resources["complete"] is not (not any(unknowns.values())):
            raise ValueError("report resources do not conserve attempts")
        exceeded = any(sums[key] > cap for key, cap in budget.items())
        expected_status = "EXCEEDED" if exceeded else "UNKNOWN" if any(unknowns[key] for key in budget) else "WITHIN"
        if report["budget_status"] != expected_status:
            raise ValueError("inconsistent budget status")
        if report["quality"]["answer_coverage"] != claimed / len(rows):
            raise ValueError("inconsistent coverage")
        if report["latency"]["sum_attempt_ms_known"] != latency or report["latency"]["unknown_attempts"] != latency_unknown:
            raise ValueError("inconsistent latency")
    except (KeyError, TypeError, AttributeError) as exc:
        raise ValueError("malformed profile report") from exc


def compare_profiles(baseline, candidate):
    """Pair complete profile reports, refusing mismatched evaluation conditions.

    Percentage resource reductions are descriptive and only emitted for complete
    usage, matched conditions, budgets met, no additional false passes, and an
    unchanged per-task correctness/coverage outcome. No statistical significance
    or production savings is inferred from one replay.
    """
    for report in (baseline, candidate):
        _validate_report(report)
    fields = ("cohort_sha256", "budget", "validator_id", "provenance", "source_id", "resource_semantics", "accounting_scope")
    mismatches = [field for field in fields if baseline.get(field) != candidate.get(field)]
    if mismatches:
        return {"schema": SCHEMA + ".comparison", "comparable": False,
                "reasons": ["mismatched " + field for field in mismatches],
                "resource_reduction_percent": None, "production_savings_proven": False}
    before = {row["task_id"]: row for row in baseline["tasks"]}
    after = {row["task_id"]: row for row in candidate["tasks"]}
    if set(before) != set(after) or len(before) != len(baseline["tasks"]) or len(after) != len(candidate["tasks"]):
        raise ValueError("invalid matched report task cohort")
    paired = {"both_verified": 0, "candidate_only_verified": 0,
              "baseline_only_verified": 0, "neither_verified": 0}
    same_outcomes = True
    task_deltas = []
    for task_id, first in before.items():
        second = after[task_id]
        if first["task_sha256"] != second["task_sha256"]:
            raise ValueError("task content differs despite matching cohort hash")
        a, b = first["verified_success"], second["verified_success"]
        paired["both_verified" if a and b else "candidate_only_verified" if b else
               "baseline_only_verified" if a else "neither_verified"] += 1
        same_outcomes &= all(first[key] == second[key] for key in
                             ("claimed_success", "verified_success", "false_pass", "validator_error"))
        task_deltas.append({"task_id": task_id,
                            "verified_success_delta": int(b) - int(a),
                            "resource_delta": {key: None if first["unknown_resource_attempts"][key] or
                                                second["unknown_resource_attempts"][key] else
                                                second["resources_known"][key] - first["resources_known"][key]
                                               for key in RESOURCES}})
    reasons = []
    if not same_outcomes:
        reasons.append("task correctness or answer coverage differs")
    if not baseline["resources"]["complete"] or not candidate["resources"]["complete"]:
        reasons.append("resource usage is incomplete")
    if baseline["budget_status"] != "WITHIN" or candidate["budget_status"] != "WITHIN":
        reasons.append("budget exceeded or uncertain")
    if baseline["counts"]["verified_successes"] == 0:
        reasons.append("no verified successes")
    if any(report["counts"]["false_passes"] or report["counts"]["validator_errors"]
           for report in (baseline, candidate)):
        reasons.append("false passes or validator errors observed")
    reductions = None
    if not reasons:
        reductions = {key: None if baseline["resources"]["known_totals"][key] == 0 else
                      100 * (baseline["resources"]["known_totals"][key] - candidate["resources"]["known_totals"][key]) /
                      baseline["resources"]["known_totals"][key] for key in RESOURCES}
    return {"schema": SCHEMA + ".comparison", "baseline": baseline["profile"],
            "candidate": candidate["profile"], "comparable": True,
            "equal_task_outcomes": bool(same_outcomes), "paired_correctness": paired,
            "verified_success_delta": candidate["counts"]["verified_successes"] - baseline["counts"]["verified_successes"],
            "false_pass_delta": candidate["counts"]["false_passes"] - baseline["counts"]["false_passes"],
            "answer_coverage_delta": candidate["quality"]["answer_coverage"] - baseline["quality"]["answer_coverage"],
            "resource_reduction_percent": reductions, "reduction_withheld_reasons": reasons,
            "sum_attempt_latency_delta_ms": None if baseline["latency"]["unknown_attempts"] or
                candidate["latency"]["unknown_attempts"] else candidate["latency"]["sum_attempt_ms_known"] -
                baseline["latency"]["sum_attempt_ms_known"],
            "paired_tasks": task_deltas, "production_savings_proven": False,
            "statistical_significance_established": False,
            "synthetic_estimates": baseline["provenance"] == "synthetic"}


def _demo():
    tasks = [{"task_id": "task-" + str(i), "input": values, "expected": sum(values)}
             for i, values in enumerate(([2, 3], [4, 7], [2, 3], [6, 9]))]
    profiles = {}
    for profile in ("baseline", "context_and_reuse"):
        cache, traces = {}, []
        for task in tasks:
            key = tuple(task["input"])
            reuse = profile == "context_and_reuse" and key in cache
            attempts = []
            if not reuse:
                # Actual deterministic callback; all resource values below are
                # hypothetical units for checking replay/accounting, not timings.
                output = sum(task["input"])
                for step in range(2 if task["task_id"] == "task-1" else 1):
                    failed = task["task_id"] == "task-1" and step == 0
                    attempts.append({"attempt_id": task["task_id"] + ":" + str(step),
                                     "status": "failed" if failed else "success",
                                     "resources": {"calls": 1, "input_tokens": 300 if profile == "baseline" else 80,
                                                   "output_tokens": 0 if failed else 20,
                                                   "cached_input_tokens": 0, "external_credit_micros": 0,
                                                   "compute_ms": 10}, "latency_ms": 10})
                cache[key] = output
            else:
                output = cache[key]
            traces.append({"task_id": task["task_id"], "claimed_success": True,
                           "output": output, "attempts": attempts,
                           "result_source": "reused" if reuse else "deterministic"})
        profiles[profile] = traces
    budget = {"calls": 8, "input_tokens": 2000, "output_tokens": 200, "cached_input_tokens": 0,
              "external_credit_micros": 0, "compute_ms": 100}
    return tasks, profiles, budget


def run_benchmark(*, tasks=None, profiles=None, budget=None, validator=None,
                  validator_id=None, provenance="synthetic", source_id=None):
    """Replay explicit task/attempt traces, or run the built-in offline demo.

    ``tasks`` is [{task_id, input, ...host-owned expected evidence}]. ``profiles``
    maps names to complete lists of {task_id, claimed_success: bool, output,
    attempts: [{attempt_id, status: success|failed|unknown, resources, latency_ms}]}.
    Resources require every RESOURCES key; unknown quantities/latencies use None.
    Input tokens include cached input tokens. Failed/retry attempts are retained.
    A zero-attempt trace needs result_source=reused|deterministic|abstained.

    ``validator(task, output)`` is trusted host code returning a strict boolean.
    For custom traces all provenance/validator identifiers and a budget are
    required. Measured provenance means *caller-declared* measurements, never
    independently authenticated billing. Budgets are matched evaluation limits,
    not pre-dispatch enforcement: over-budget traces are reported, not erased.
    """
    demo = tasks is None and profiles is None
    if demo:
        if any(value is not None for value in (budget, validator, validator_id, source_id)) or provenance != "synthetic":
            raise ValueError("demo accepts no custom evaluation configuration")
        tasks, profiles, budget = _demo()
        validator = lambda task, output: type(output) is int and output == task["expected"]
        validator_id = "exact-integer-sum-v1"
        source_id = "bundled-local-callback-synthetic-estimates-v1"
    if provenance not in ("synthetic", "measured"):
        raise ValueError("provenance must be synthetic or measured")
    _name(validator_id, "validator_id")
    _name(source_id, "source_id")
    if not callable(validator):
        raise ValueError("custom replay needs a host validator callable")
    task_ids = _validate_tasks(tasks)
    if not isinstance(profiles, dict) or not 1 <= len(profiles) <= 16:
        raise ValueError("profiles must map 1..16 profile names to traces")
    if not isinstance(budget, dict) or not budget or set(budget) - set(RESOURCES):
        raise ValueError("budget must set at least one recognized resource cap")
    for key, cap in budget.items():
        _integer(cap, key + " budget")
    # Validate every trace before any host callback, then snapshot input to keep
    # callback mutation from changing provenance or another profile's tasks.
    prepared = {}
    for name, traces in profiles.items():
        _name(name, "profile name")
        prepared[name] = deepcopy(_validate_traces(traces, task_ids))
    tasks, budget = deepcopy(tasks), deepcopy(budget)
    manifest = {"cohort_sha256": _hash(sorted(tasks, key=lambda task: task["task_id"])),
                "budget": budget, "validator_id": validator_id, "provenance": provenance,
                "source_id": source_id, "resource_semantics": "integer-separate-units-v1",
                "accounting_scope": "submitted attempts; unrecorded orchestration or external activity excluded",
                "measurement_trust": "synthetic estimates" if provenance == "synthetic" else "caller-declared; not independently authenticated"}
    results = {name: _profile(name, tasks, traces, budget, validator, manifest)
               for name, traces in prepared.items()}
    names = list(results)
    comparisons = [compare_profiles(results[names[0]], results[name]) for name in names[1:]]
    return {"schema": SCHEMA, **manifest, "demo": demo, "profiles": results,
            "comparisons": comparisons, "paid_services_required": False,
            "replay_dispatches_model_calls": False, "execution_authorized": False,
            "production_savings_proven": False}
