"""Pinned, bounded repeated evidence-review experiments.

These experiments compare declared systems on operator-supplied labels. They
do not certify labels, weights, held-out independence or production quality.
``source_sha256`` is an in-process caller declaration: the command-line adapter
must compute it from actual source bytes. Dataset labels never enter requests.
"""
from dataclasses import asdict
import hashlib
import math
import random
import time

from keel_agent.models import validate_assessment
from keel_eval import evaluation as _evaluation
from keel_eval.evaluation import (
    EvaluationError, _MODEL_ERRORS, _canonical, _config, _hash, _keys, _snapshot, _token,
    run_local, validate_dataset,
)


NO_CALL_ERROR_CODES = frozenset({
    "local_model_not_enabled", "model_call_budget_exhausted", "wall_budget_exhausted",
    "wall_budget_insufficient_for_deadline", "not_run_after_rate_limit",
})
ALL_ERROR_CODES = frozenset(_MODEL_ERRORS | NO_CALL_ERROR_CODES | {"model_adapter_error"})


class BenchmarkError(ValueError):
    """Stable, content-free benchmark contract failures."""


def _fail(code):
    raise BenchmarkError(code)


def digest(value):
    """Hash bounded canonical JSON; no file-path or network interpretation."""
    try:
        return hashlib.sha256(_canonical(value)).hexdigest()
    except EvaluationError as exc:
        raise BenchmarkError(str(exc)) from None


def _validate_shape(plan):
    try:
        plan = _snapshot(plan)
        _keys(plan, ("schema", "experiment_id", "dataset_sha256", "source_sha256",
                     "trials", "seed", "systems", "limits"), "plan_schema_invalid")
        if plan["schema"] != "keel.bench.plan.v1":
            _fail("plan_version_invalid")
        _token(plan["experiment_id"], "experiment_id_invalid")
        _hash(plan["dataset_sha256"], "dataset_sha256_invalid")
        _hash(plan["source_sha256"], "source_sha256_invalid")
        if type(plan["trials"]) is not int or not 1 <= plan["trials"] <= 20:
            _fail("trials_invalid")
        if type(plan["seed"]) is not int or not 0 <= plan["seed"] <= 2**32 - 1:
            _fail("seed_invalid")
        systems = plan["systems"]
        if type(systems) is not list or not 2 <= len(systems) <= 8:
            _fail("systems_count_invalid")
        identifiers, configs, baselines = set(), set(), 0
        for system in systems:
            _keys(system, ("system_id", "kind", "model_config", "declared_weights_sha256"),
                  "system_schema_invalid")
            identifier = _token(system["system_id"], "system_id_invalid")
            if identifier in identifiers:
                _fail("duplicate_system_id")
            identifiers.add(identifier)
            if system["kind"] == "abstain_baseline":
                if system["model_config"] is not None or system["declared_weights_sha256"] is not None:
                    _fail("baseline_model_metadata_forbidden")
                baselines += 1
                if baselines > 1:
                    _fail("duplicate_abstain_baseline")
            elif system["kind"] == "local_model":
                config = _config(system["model_config"])
                if system["declared_weights_sha256"] is not None:
                    _hash(system["declared_weights_sha256"], "declared_weights_sha256_invalid")
                # Changing only an attribution name (or 60 versus 60.0) does
                # not create a different declared comparison system.
                effective = asdict(config)
                del effective["reviewer_id"]
                effective["timeout_seconds"] = float(effective["timeout_seconds"])
                identity = digest(effective)
                if identity in configs:
                    _fail("duplicate_effective_model_config")
                configs.add(identity)
            else:
                _fail("system_kind_invalid")
        limits = plan["limits"]
        _keys(limits, ("max_model_calls", "max_wall_seconds"), "limits_schema_invalid")
        if type(limits["max_model_calls"]) is not int or not 1 <= limits["max_model_calls"] <= 4096:
            _fail("model_call_limit_invalid")
        seconds = limits["max_wall_seconds"]
        if type(seconds) not in (int, float) or not math.isfinite(seconds) or not 1 <= seconds <= 86400:
            _fail("wall_limit_invalid")
        return plan
    except EvaluationError as exc:
        raise BenchmarkError(str(exc)) from None


def plan_digest(plan):
    """Pin the complete validated plan, including systems and call limits."""
    return digest(_validate_shape(plan))


def validate_plan(plan, dataset, *, source_sha256):
    """Return an isolated plan after matching independent dataset/source pins."""
    plan = _validate_shape(plan)
    try:
        dataset = validate_dataset(dataset)
        _hash(source_sha256, "source_sha256_invalid")
    except EvaluationError as exc:
        raise BenchmarkError(str(exc)) from None
    if plan["dataset_sha256"] != digest(dataset):
        _fail("plan_dataset_mismatch")
    if plan["source_sha256"] != source_sha256:
        _fail("plan_source_mismatch")
    if len(dataset["cases"]) * len(plan["systems"]) * plan["trials"] > 4096:
        _fail("experiment_row_limit")
    return plan


def make_plan(dataset, systems, *, experiment_id, source_sha256, trials=3, seed=0,
              max_model_calls=4096, max_wall_seconds=3600):
    """Freeze an explicit, bounded plan without performing model calls."""
    try:
        dataset = validate_dataset(dataset)
    except EvaluationError as exc:
        raise BenchmarkError(str(exc)) from None
    plan = {"schema": "keel.bench.plan.v1", "experiment_id": experiment_id,
            "dataset_sha256": digest(dataset), "source_sha256": source_sha256,
            "trials": trials, "seed": seed, "systems": systems,
            "limits": {"max_model_calls": max_model_calls, "max_wall_seconds": max_wall_seconds}}
    return validate_plan(plan, dataset, source_sha256=source_sha256)


def _schedule(dataset, plan):
    # Each case visits every system before moving to the next case. Rotate the
    # seeded system permutation, balancing which system runs first. No global
    # RNG state is consumed or changed. Trials are zero-based.
    rng = random.Random(plan["seed"])
    systems = list(range(len(plan["systems"])))
    rng.shuffle(systems)
    ordinal = 0
    for trial in range(plan["trials"]):
        cases = list(range(len(dataset["cases"])))
        rng.shuffle(cases)
        for case_index in cases:
            offset = ordinal % len(systems)
            for system_index in systems[offset:] + systems[:offset]:
                yield trial, case_index, system_index
            ordinal += 1


def schedule(plan, dataset):
    """Return the exact reproducible (system ID, zero-based trial, case ID) order.

    This validates internal consistency only; execution also requires the
    independent source and reviewed plan digest supplied to run_experiment.
    """
    plan = _validate_shape(plan)
    plan = validate_plan(plan, dataset, source_sha256=plan["source_sha256"])
    try:
        dataset = validate_dataset(dataset)
    except EvaluationError as exc:
        raise BenchmarkError(str(exc)) from None
    return [(plan["systems"][system_index]["system_id"], trial,
             dataset["cases"][case_index]["case_id"])
            for trial, case_index, system_index in _schedule(dataset, plan)]


def run_experiment(dataset, plan, *, expected_plan_sha256, source_sha256,
                   allow_model_calls=False, transport=None):
    """Execute a pinned schedule once; errors remain in every denominator.

    Model calls require an explicit boolean opt-in. There is no retry, resume,
    cloud fallback, download or execution capability. A rate limit stops every
    later model across all systems/trials. The unchanged configured deadline
    must fit the remaining wall budget before a request may start. A supplied
    transport is trusted test code and labelled INJECTED, never real inference.
    """
    try:
        dataset = validate_dataset(dataset)
        _hash(expected_plan_sha256, "expected_plan_sha256_invalid")
    except EvaluationError as exc:
        raise BenchmarkError(str(exc)) from None
    plan = validate_plan(plan, dataset, source_sha256=source_sha256)
    pin = plan_digest(plan)
    if pin != expected_plan_sha256:
        _fail("plan_pin_mismatch")
    if type(allow_model_calls) is not bool:
        _fail("model_call_opt_in_invalid")
    if transport is not None and not callable(transport):
        _fail("transport_not_callable")
    mode = ("BASELINE_ONLY" if not allow_model_calls else
            "INJECTED" if transport is not None else "LOCAL_LOOPBACK")
    configs = {row["system_id"]: _config(row["model_config"])
               for row in plan["systems"] if row["kind"] == "local_model"}
    records, calls, rate_limited, stop_reason = [], 0, False, None
    started = time.monotonic()
    for trial, case_index, system_index in _schedule(dataset, plan):
        case, system = dataset["cases"][case_index], plan["systems"][system_index]
        row = {"system_id": system["system_id"], "trial": trial,
               "case_id": case["case_id"], "subject_sha256": digest(case["subject"]),
               "expected_verdict": case["expected_verdict"], "observed_verdict": "ERROR",
               "latency_ms": None, "error_code": None, "assessment_sha256": None,
               "request_sha256": None, "response_sha256": None, "model_calls_attempted": 0}
        remaining = plan["limits"]["max_wall_seconds"] - max(0.0, time.monotonic() - started)
        is_model = system["kind"] == "local_model"
        if is_model and rate_limited:
            row["error_code"] = "not_run_after_rate_limit"
        elif remaining <= 0:
            row["error_code"] = "wall_budget_exhausted"
            stop_reason = stop_reason or row["error_code"]
        elif not is_model:
            baseline_started = time.monotonic()
            assessment = validate_assessment({"verdict": "ABSTAIN",
                "covered_claim_ids": case["subject"]["required_claim_ids"],
                "findings": ["Deterministic baseline abstains without assessing evidence."]},
                case["subject"]["required_claim_ids"])
            row.update(observed_verdict="ABSTAIN", assessment_sha256=digest(assessment),
                       latency_ms=round(max(0.0, time.monotonic() - baseline_started) * 1000, 6))
        elif not allow_model_calls:
            row["error_code"] = "local_model_not_enabled"
        elif calls >= plan["limits"]["max_model_calls"]:
            row["error_code"] = "model_call_budget_exhausted"
            stop_reason = stop_reason or row["error_code"]
        elif remaining < configs[system["system_id"]].timeout_seconds:
            row["error_code"] = "wall_budget_insufficient_for_deadline"
            stop_reason = stop_reason or row["error_code"]
        else:
            # run_local projects only subject+subject digest into the request.
            # Labels and rationales remain private to evaluation/scoring.
            single = dict(dataset, cases=[case])
            attempted, deadline_error = 0, None
            def bounded_send(config, payload, timeout):
                nonlocal attempted, deadline_error
                # Serialization/validation time also consumes the wall budget.
                # Recheck at the last point before the actual transport call.
                available = (plan["limits"]["max_wall_seconds"] -
                             max(0.0, time.monotonic() - started))
                if available < timeout:
                    deadline_error = ("wall_budget_exhausted" if available <= 0 else
                                      "wall_budget_insufficient_for_deadline")
                    raise RuntimeError("benchmark_deadline_preflight")
                attempted += 1
                send = _evaluation.loopback_transport if transport is None else transport
                return send(config, payload, timeout)
            report = run_local(single, configs[system["system_id"]],
                               declared_weights_sha256=system["declared_weights_sha256"],
                               transport=bounded_send)
            result = report["cases"][0]
            for key in ("observed_verdict", "latency_ms", "error_code", "assessment_sha256",
                        "request_sha256", "response_sha256"):
                row[key] = result.get(key)
            if deadline_error is not None:
                # run_local counts entry into the wrapper; this experiment
                # counts only attempted calls to the actual selected adapter.
                row.update(observed_verdict="ERROR", error_code=deadline_error,
                           latency_ms=None, assessment_sha256=None,
                           request_sha256=None, response_sha256=None)
                stop_reason = stop_reason or deadline_error
            row["model_calls_attempted"] = attempted
            calls += attempted
            if row["error_code"] == "model_http_429":
                rate_limited, stop_reason = True, "model_http_429"
        records.append(row)
    wall_ms = round(max(0.0, time.monotonic() - started) * 1000, 6)
    return {"schema": "keel.bench.run.v1", "plan": plan, "plan_sha256": pin,
            "dataset_sha256": plan["dataset_sha256"], "source_sha256": source_sha256,
            "synthetic": dataset["synthetic"], "split": dataset["split"], "mode": mode,
            "records": records, "wall_ms": wall_ms, "stop_reason": stop_reason,
            "model_calls_attempted": calls, "execution_authorized": False,
            "model_quality_validated": False, "production_deployed": False}
