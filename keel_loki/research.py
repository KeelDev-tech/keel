"""Research propositions mapped to falsifiable measurements and observed runs."""
from keel_loki.common import clone, digest, require_dict, require_id, require_hash
from keel_loki.lab import validate_report, STAGES

METRICS = frozenset({"agreement", "useful_pass", "false_pass", "abstentions", "errors"})


def propose(*, research_id, paper_reference, proposition, hypothesis, comparator_stage,
            experimental_stage, metric, direction, dataset_sha256, experiment_plan_sha256):
    require_id(research_id)
    for text in (paper_reference, proposition, hypothesis):
        if type(text) is not str or not text.strip() or len(text) > 4096:
            raise ValueError("research_statement_invalid")
    if comparator_stage not in STAGES or experimental_stage not in STAGES or comparator_stage == experimental_stage:
        raise ValueError("research_comparator_invalid")
    if metric not in METRICS or direction not in ("increase", "decrease"):
        raise ValueError("research_metric_invalid")
    require_hash(dataset_sha256)
    require_hash(experiment_plan_sha256)
    return {"schema": "keel.loki.hypothesis.v1", "research_id": research_id,
        "paper_reference": paper_reference, "proposition": proposition, "hypothesis": hypothesis,
        "comparator_stage": comparator_stage, "experimental_stage": experimental_stage,
        "metric": metric, "direction": direction, "dataset_sha256": dataset_sha256,
        "experiment_plan_sha256": experiment_plan_sha256,
        "pre_registration_independently_verified": False, "execution_authorized": False}


def record_result(hypothesis, report, *, expected_hypothesis_sha256, expected_plan_sha256):
    """Derive observed values from a pinned lab run; never accept caller PASS.

    Host-owned report hashes bind content, not authenticated human identity or
    independent experiment execution. Synthetic and injected results remain so.
    """
    hypothesis = clone(hypothesis)
    require_hash(expected_hypothesis_sha256)
    require_hash(expected_plan_sha256)
    keys = {"schema", "research_id", "paper_reference", "proposition", "hypothesis", "comparator_stage",
            "experimental_stage", "metric", "direction", "dataset_sha256", "experiment_plan_sha256",
            "pre_registration_independently_verified", "execution_authorized"}
    require_dict(hypothesis, keys)
    if digest(hypothesis) != expected_hypothesis_sha256:
        raise ValueError("research_hypothesis_pin_mismatch")
    rebuilt = propose(**{k: hypothesis[k] for k in keys - {"schema", "pre_registration_independently_verified", "execution_authorized"}})
    if rebuilt != hypothesis:
        raise ValueError("research_hypothesis_invalid")
    report = validate_report(report, expected_plan_sha256=expected_plan_sha256)
    if hypothesis["experiment_plan_sha256"] != expected_plan_sha256 or hypothesis["dataset_sha256"] != report["dataset_sha256"]:
        raise ValueError("research_run_binding_mismatch")
    metric = hypothesis["metric"]
    before = report["metrics"]["stages"][hypothesis["comparator_stage"]][metric]
    after = report["metrics"]["stages"][hypothesis["experimental_stage"]][metric]
    observed_delta = None if before["value"] is None or after["value"] is None else after["value"] - before["value"]
    correct_direction = None if observed_delta is None else observed_delta > 0 if hypothesis["direction"] == "increase" else observed_delta < 0
    errors = sum(report["metrics"]["stages"][stage]["errors"]["numerator"] for stage in (hypothesis["comparator_stage"], hypothesis["experimental_stage"]))
    return {"schema": "keel.loki.research_result.v1", "research_id": hypothesis["research_id"],
        "hypothesis_sha256": expected_hypothesis_sha256, "plan_sha256": expected_plan_sha256,
        "run_sha256": digest(report), "dataset_sha256": report["dataset_sha256"],
        "synthetic": report["synthetic"], "mode": report["mode"], "split": report["split"],
        "comparator": before, "experimental": after, "observed_delta": observed_delta,
        "direction_observed": correct_direction, "comparison_errors": errors,
        "status": "INCOMPLETE" if errors or observed_delta is None else "OBSERVED",
        "statistical_significance_established": False, "causal_effect_established": False,
        "broad_cognitive_benefit_established": False, "research_proposition_proven": False,
        "execution_authorized": False}


def demo():
    from keel_loki.lab import demo as lab_demo
    report = lab_demo()
    hypothesis = propose(research_id="grounded-critique", paper_reference="User research: Fourth Cognitive Domain; hypothesis only",
        proposition="A grounded critic may reduce unsupported claims.",
        hypothesis="Critique decreases false PASS on the frozen labelled mutation fixtures.",
        comparator_stage="drafter_blind", experimental_stage="reviewer_revision", metric="false_pass", direction="decrease",
        dataset_sha256=report["dataset_sha256"], experiment_plan_sha256=report["plan_sha256"])
    return record_result(hypothesis, report, expected_hypothesis_sha256=digest(hypothesis), expected_plan_sha256=report["plan_sha256"])
