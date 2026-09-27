"""Stratified, cluster-aware diagnostics and an executed protocol benchmark.

This module never qualifies artifacts or authorizes actions. Its concentration
bounds are conditional on independently sampled, predeclared task clusters for a
fixed candidate. Synthetic cases demonstrate implementation behavior only.
"""
import hashlib
import inspect
import json
import math
from pathlib import Path

from keel_agent import models
from .evaluation import EvaluationError, VERDICTS, _sha, _snapshot, validate_dataset
from .reliability import Runner, _expected, evaluate_trials, freeze_plan, run_paired


MAX_FAMILIES = 16
RESERVED_COMPARISONS = 3 * (1 + len(VERDICTS) + MAX_FAMILIES) + 6


def _alpha(value):
    if type(value) not in (int, float) or not math.isfinite(value) or not 1e-9 <= value <= .5:
        raise EvaluationError("frontier_alpha_invalid")


def cluster_interval(values, *, alpha, comparisons, lower=0, upper=1):
    """Conservative two-sided Hoeffding confidence sequence at sample count n.

    delta_n = alpha / (comparisons * n * (n+1)). The telescoping sum of
    1/(n*(n+1)) is one. A union bound over n and the predeclared comparisons
    controls their combined error by alpha; repeated views are not new samples.
    This is deliberately wider than modern mixture/stitching boundaries.
    """
    _alpha(alpha)
    if type(comparisons) is not int or not 1 <= comparisons <= 256:
        raise EvaluationError("frontier_comparisons_invalid")
    if ((lower, upper) not in ((0, 1), (-1, 1)) or type(values) is not list
            or len(values) > 256):
        raise EvaluationError("frontier_bound_input_invalid")
    if any(type(v) not in (int, float) or not math.isfinite(v) or not lower <= v <= upper for v in values):
        raise EvaluationError("frontier_bound_value_invalid")
    n = len(values)
    if not n:
        return {"clusters": 0, "mean": None, "lower": lower, "upper": upper,
                "alpha_at_n": None}
    delta = alpha / (comparisons * n * (n + 1))
    mean = math.fsum(values) / n
    radius = (upper - lower) * math.sqrt(math.log(2 / delta) / (2 * n))
    return {"clusters": n, "mean": mean, "lower": max(lower, mean - radius),
            "upper": min(upper, mean + radius), "alpha_at_n": delta}


def zero_failure_sample_size(*, maximum_rate, alpha=.05):
    """Exact one-sided zero-failure binomial sample planning, not a guarantee.

    For n independent identically distributed Bernoulli opportunities with zero
    failures, the upper limit is 1-alpha**(1/n). This is a single planned look;
    using it after repeated peeking or dependent repeats invalidates the claim.
    """
    _alpha(alpha)
    if (type(maximum_rate) not in (int, float) or not math.isfinite(maximum_rate)
            or not 1e-9 <= maximum_rate < 1):
        raise EvaluationError("frontier_maximum_rate_invalid")
    n = math.ceil(math.log(alpha) / math.log1p(-maximum_rate))
    return {"independent_opportunities_required": n, "observed_failures_required": 0,
            "upper_failure_rate": -math.expm1(math.log(alpha) / n), "alpha": alpha,
            "method": "one_sided_exact_binomial_zero_failures_fixed_look",
            "execution_authorized": False}


def _means(group):
    return [math.fsum(values) / len(values) for _, values in sorted(group.items())]


def audit_paired(plan, dataset, trials, *, alpha=.05):
    """Revalidate a frozen transcript and expose outcome/family blind spots.

    Exactly one case tag names its task family. Family assignments and labels
    are therefore pinned by the existing dataset/plan digest before execution.
    Imported transcripts remain untrusted replay even if every score is high.
    """
    _alpha(alpha)
    dataset = validate_dataset(dataset)
    plan, trials = _snapshot(plan), _snapshot(trials)
    replay = evaluate_trials(plan, dataset, trials)
    if any(len(case["tags"]) != 1 for case in dataset["cases"]):
        raise EvaluationError("frontier_one_frozen_family_per_case_required")
    families = sorted({case["tags"][0] for case in dataset["cases"]})
    if len(families) > MAX_FAMILIES:
        raise EvaluationError("frontier_family_limit")
    # Relabeling identical inputs cannot manufacture independent clusters.
    observed = {}
    for case in dataset["cases"]:
        fingerprint = _sha(case["subject"])
        cluster = plan["adjudications"][case["case_id"]]["cluster_id"]
        if fingerprint in observed and observed[fingerprint] != cluster:
            raise EvaluationError("frontier_duplicate_subject_cross_cluster")
        observed[fingerprint] = cluster
    by_case = {case["case_id"]: case for case in dataset["cases"]}
    strata = ["overall", *("label:" + label for label in VERDICTS),
              *("family:" + family for family in families)]
    groups = {name: {runner: {} for runner in ("baseline", "candidate")}
              for name in strata}
    risks = {runner: {kind: {} for kind in ("unsafe_pass", "unnecessary_abstention", "error")}
             for runner in ("baseline", "candidate")}
    matrices = {runner: {gold: {v: 0 for v in (*VERDICTS, "ERROR")} for gold in VERDICTS}
                for runner in ("baseline", "candidate")}
    for row in trials:
        case = by_case[row["case_id"]]
        gold = _expected(case, row["fault"])
        cluster = plan["adjudications"][case["case_id"]]["cluster_id"]
        runner, verdict = row["runner"], row["verdict"]
        matrices[runner][gold][verdict] += 1
        for name in ("overall", "label:" + gold, "family:" + case["tags"][0]):
            groups[name][runner].setdefault(cluster, []).append(int(gold == verdict))
        risks[runner]["error"].setdefault(cluster, []).append(int(verdict == "ERROR"))
        if gold != "PASS":
            risks[runner]["unsafe_pass"].setdefault(cluster, []).append(int(verdict == "PASS"))
        if gold != "ABSTAIN":
            risks[runner]["unnecessary_abstention"].setdefault(cluster, []).append(int(verdict == "ABSTAIN"))
    # Reserve all 16 family slots, even if a sampled prefix omits some families.
    # Three intervals per stratum (two accuracies and paired gain), six risks.
    comparisons = RESERVED_COMPARISONS
    intervals = {}
    for name, group in groups.items():
        pair = {runner: cluster_interval(_means(values), alpha=alpha, comparisons=comparisons)
                for runner, values in group.items()}
        differences = []
        for cluster in sorted(group["baseline"]):
            left, right = group["baseline"][cluster], group["candidate"][cluster]
            differences.append(math.fsum(right) / len(right) - math.fsum(left) / len(left))
        pair["paired_gain"] = cluster_interval(differences, alpha=alpha, comparisons=comparisons,
                                               lower=-1, upper=1)
        intervals[name] = pair
    risk_intervals = {runner: {kind: cluster_interval(_means(values), alpha=alpha,
                                                     comparisons=comparisons)
                               for kind, values in kinds.items()}
                      for runner, kinds in risks.items()}
    balanced = {}
    for runner in matrices:
        recalls = {gold: (matrix[gold] / sum(matrix.values()) if sum(matrix.values()) else None)
                   for gold, matrix in matrices[runner].items()}
        balanced[runner] = {"recall_by_expected_verdict": recalls,
                            "macro_recall": (math.fsum(recalls.values()) / len(VERDICTS)
                                             if all(v is not None for v in recalls.values()) else None)}
    labels = {case["expected_verdict"] for case in dataset["cases"]}
    reasons = []
    if labels != set(VERDICTS):
        reasons.append("original_dataset_missing_outcome_class")
    if dataset["synthetic"]:
        reasons.append("synthetic_fixture_only")
    reasons.extend(("transcript_is_not_execution_attestation", "cluster_independence_not_authenticated",
                    "confidence_bounds_are_conditional_not_authority"))
    return {"schema": "keel.frontier.audit.v1", "status": "DIAGNOSTIC_ONLY",
            "plan_sha256": plan["plan_sha256"], "dataset_sha256": plan["dataset_sha256"],
            "trials_sha256": _sha(trials), "synthetic": dataset["synthetic"],
            "audit_source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "declared_cluster_count": replay["independent_cluster_count"],
            "trial_count": len(trials), "families": families, "confusion_matrices": matrices,
            "balanced_metrics": balanced, "cluster_intervals": intervals,
            "risk_intervals": risk_intervals, "alpha": alpha, "simultaneous_comparisons": comparisons,
            "reported_intervals": 3 * len(strata) + 6,
            "bound_method": "two_sided_hoeffding_union_over_cluster_count_and_metrics",
            "alpha_spending": "alpha/(comparisons*n*(n+1))",
            "population_estimand": "equal_weight_cluster_mean_for_each_predeclared_stratum",
            "assumptions": ["fixed_candidate_and_family_definitions_before_sampling",
                            "independent_completed_clusters_in_predeclared_order",
                            "no_cluster_splitting_or_adaptive_case_selection",
                            "repeats_and_fault_views_stay_within_the_original_cluster",
                            "separate_candidates_require_a_separate_shared_error_budget"],
            "reasons": reasons, "execution_authorized": False, "production_qualified": False}


def _constant(subject, config, seed):
    return config["verdict"]


def _response_runner(subject, config, seed):
    """Invoke the installed response validator, never a model or transport."""
    if hashlib.sha256(Path(inspect.getsourcefile(models)).read_bytes()).hexdigest() != config["validator_sha256"]:
        raise EvaluationError("frontier_validator_changed")
    if len(subject["evidence"]) != 1:
        return "ABSTAIN"
    raw = json.loads(subject["evidence"][0]["text"])
    response = models.HTTPResult(raw["status"], raw["headers"], raw["body"].encode("utf-8"))
    reviewer = models.ReviewerConfig("benchmark", "ollama", "http://127.0.0.1:11434/api/chat", "fixture")
    try:
        models._assessment_from_response(reviewer, response, subject["required_claim_ids"])
    except models.ModelError:
        return "FAIL"
    return "PASS"


def _fixture():
    families = ("transport", "identity", "claim_coverage", "findings", "tool_injection", "strict_json")
    dataset = {"schema": "keel.eval.dataset.v1", "dataset_id": "balanced-response-protocol-v1",
               "synthetic": True, "split": "held_out", "label_source": "synthetic_fixture", "cases": []}
    adjudications = {}
    for index, family in enumerate(families):
        for variant, expected in (("valid", "PASS"), ("invalid", "FAIL"), ("missing", "ABSTAIN")):
            case_id = family + "-" + variant
            claim_id = "claim-" + case_id
            declared_verdict = VERDICTS[index % len(VERDICTS)]
            assessment = {"verdict": declared_verdict, "covered_claim_ids": [claim_id],
                          "findings": [] if declared_verdict == "PASS" else ["Synthetic unresolved evidence."]}
            envelope = {"model": "fixture", "done": True, "created_at": "synthetic-receipt",
                        "message": {"role": "assistant", "content": json.dumps(assessment)}}
            response = {"status": 200, "headers": {"content-type": "application/json"}, "body": ""}
            if variant == "invalid":
                if family == "transport":
                    response["status"] = 429
                elif family == "identity":
                    envelope["model"] = "unrequested-model"
                elif family == "claim_coverage":
                    assessment["covered_claim_ids"] = ["unrelated-claim"]
                elif family == "findings":
                    assessment["verdict"] = "PASS"
                    assessment["findings"] = ["Unresolved contradiction."]
                elif family == "tool_injection":
                    envelope["message"]["tool_calls"] = [{"name": "submit_application"}]
            envelope["message"]["content"] = json.dumps(assessment)
            response["body"] = json.dumps(envelope)
            if variant == "invalid" and family == "strict_json":
                response["body"] = response["body"][:-1] + ',"model":"fixture"}'
            case = {"case_id": case_id, "tags": [family], "expected_verdict": expected,
                    "label_rationale": "Synthetic response boundary: accept coherent, reject invalid, abstain absent.",
                    "subject": {"required_claim_ids": [claim_id],
                                "claims": [{"claim_id": claim_id, "text": "Response must satisfy the closed local protocol."}],
                                "evidence": ([] if variant == "missing" else
                                             [{"evidence_id": "response", "text": json.dumps(response)}])}}
            dataset["cases"].append(case)
            adjudications[case_id] = {"cluster_id": "template-" + family,
                                     "adjudicator_id": "synthetic-template", "record_sha256": _sha(case)}
    return validate_dataset(dataset), adjudications


def run_benchmark(*, repeats=3):
    """Execute 18 balanced protocol cases, two views, against two weak baselines.

    The six template clusters are NOT independent real-world observations. The
    comparisons test this validator boundary, not model reasoning, browsing,
    application success, competitive superiority, or production readiness.
    """
    dataset, adjudications = _fixture()
    candidate = Runner(_response_runner, {
        "validator_sha256": hashlib.sha256(Path(inspect.getsourcefile(models)).read_bytes()).hexdigest()})
    comparisons = []
    for verdict in ("PASS", "ABSTAIN"):
        baseline = Runner(_constant, {"verdict": verdict})
        plan = freeze_plan(dataset, baseline=baseline, candidate=candidate, adjudications=adjudications,
                           repeats=repeats, faults=("none", "reversed_evidence"))
        report = run_paired(plan, dataset, baseline=baseline, candidate=candidate)
        # Split the family-wise budget across both planned baseline comparisons.
        audit = audit_paired(plan, dataset, report["trials"], alpha=.025)
        comparisons.append({"baseline": "constant_" + verdict.lower(), "audit": audit,
                            "execution": report, "plan": plan})
    correct = all(item["execution"]["metrics"]["candidate"]["accuracy"] == 1 for item in comparisons)
    return {"schema": "keel.frontier.benchmark.v1", "status": "SMOKE_PASSED" if correct else "SMOKE_FAILED",
            "synthetic": True, "task_scope": "local_model_response_protocol_validation",
            "cases": len(dataset["cases"]), "template_clusters": 6,
            "actual_callback_invocations": sum(len(item["execution"]["trials"]) for item in comparisons),
            "overall_alpha_budget": .05, "comparisons": comparisons, "dataset": dataset,
            "limitations": ["not_a_model_reasoning_benchmark", "not_independent_real_world_samples",
                            "two_constant_baselines_are_not_market_competitors",
                            "synthetic_cases_are_public_and_not_qualification_holdouts"],
            "execution_authorized": False, "production_qualified": False}
