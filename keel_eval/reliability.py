"""Frozen, paired reliability experiments; reports cannot authorize execution.

Only trusted local callbacks run here. This is a measuring harness, not a sandbox:
callbacks must enforce their own deadlines and may not perform real submissions.
Repeated trials share a case cluster and never inflate the independent sample size.
"""
from dataclasses import dataclass
import hashlib
import inspect
import marshal
import math
from pathlib import Path
import time

from .evaluation import (EvaluationError, VERDICTS, _canonical, _hash, _keys,
                         _sha, _snapshot, _token, dataset_digest, validate_dataset)

MAX_TRIALS = 8192
FAULTS = ("none", "missing_evidence", "reversed_evidence")


def _fail(code):
    raise EvaluationError(code)


def _number(value, low, high, code):
    if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
        _fail(code)


@dataclass(frozen=True)
class Runner:
    """Trusted callback(subject, config, seed) -> PASS/FAIL/ABSTAIN.

    Source and config pins bind this callback's containing file and configuration;
    they are not an attestation of imported dependencies or model weights.
    """
    callback: object
    config: dict

    def pin(self):
        if not inspect.isfunction(self.callback):
            _fail("plain_python_callback_required")
        if self.callback.__closure__:
            _fail("runner_closure_not_pinned_use_config")
        path = inspect.getsourcefile(self.callback)
        if not path:
            _fail("runner_source_unavailable")
        source = Path(path).read_bytes()
        if len(source) > 8 * 1024 * 1024:
            _fail("runner_source_too_large")
        config = _snapshot(self.config)
        if type(config) is not dict:
            _fail("runner_config_invalid")
        return {"source_sha256": hashlib.sha256(source).hexdigest(),
                "bytecode_sha256": hashlib.sha256(marshal.dumps(self.callback.__code__)).hexdigest(),
                "defaults_sha256": _sha({"positional": list(self.callback.__defaults__ or ()),
                                          "keyword": self.callback.__kwdefaults__ or {}}),
                "config_sha256": _sha(config), "entrypoint": self.callback.__qualname__}


def freeze_plan(dataset, *, baseline, candidate, adjudications, repeats=3,
                faults=("none",), seed=0, thresholds=None):
    """Pin labels, clusters, adjudication references, code/config and all gates.

    Adjudications are opaque references supplied by a host, never authentication.
    A trusted validator must verify the complete pinned plan before qualification.
    """
    dataset = validate_dataset(dataset)
    if dataset["split"] != "held_out":
        _fail("held_out_dataset_required")
    if type(repeats) is not int or not 1 <= repeats <= 32:
        _fail("repeats_invalid")
    if type(seed) is not int or not 0 <= seed < 2**32:
        _fail("seed_invalid")
    faults = list(faults)
    if not faults or any(type(f) is not str or f not in FAULTS for f in faults) or len(faults) != len(set(faults)):
        _fail("faults_invalid")
    if 2 * len(dataset["cases"]) * repeats * len(faults) > MAX_TRIALS:
        _fail("trial_budget_exceeded")
    adjudications = _snapshot(adjudications)
    ids = {c["case_id"] for c in dataset["cases"]}
    if type(adjudications) is not dict or set(adjudications) != ids:
        _fail("adjudication_coverage_invalid")
    for row in adjudications.values():
        _keys(row, ("cluster_id", "adjudicator_id", "record_sha256"), "adjudication_invalid")
        _token(row["cluster_id"], "cluster_id_invalid")
        _token(row["adjudicator_id"], "adjudicator_id_invalid")
        _hash(row["record_sha256"], "adjudication_digest_invalid")
    gates = thresholds if thresholds is not None else {
        "min_clusters": 20, "min_accuracy_gain": 0, "max_false_pass_rate": 0,
        "max_error_rate": 0, "max_abstention_rate": 1, "max_instability_rate": 0, "alpha": .05}
    gates = _snapshot(gates)
    _keys(gates, ("min_clusters", "min_accuracy_gain", "max_false_pass_rate", "max_error_rate",
                  "max_abstention_rate", "max_instability_rate", "alpha"), "thresholds_invalid")
    if type(gates["min_clusters"]) is not int or not 1 <= gates["min_clusters"] <= 256:
        _fail("minimum_clusters_invalid")
    _number(gates["min_accuracy_gain"], -1, 1, "accuracy_gain_invalid")
    for field in ("max_false_pass_rate", "max_error_rate", "max_abstention_rate", "max_instability_rate"):
        _number(gates[field], 0, 1, "rate_threshold_invalid")
    _number(gates["alpha"], .000001, .5, "alpha_invalid")
    plan = {"schema": "keel.reliability.plan.v1", "dataset_sha256": dataset_digest(dataset),
            "synthetic": dataset["synthetic"], "repeats": repeats, "faults": faults,
            "seed": seed, "baseline": baseline.pin(), "candidate": candidate.pin(),
            "adjudications": adjudications, "thresholds": gates,
            "harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "status": "PROPOSED", "execution_authorized": False}
    return {**plan, "plan_sha256": _sha(plan)}


def _plan(plan, dataset):
    plan = _snapshot(plan)
    _keys(plan, ("schema", "dataset_sha256", "synthetic", "repeats", "faults", "seed", "baseline",
                 "candidate", "adjudications", "thresholds", "harness_sha256", "status",
                 "execution_authorized", "plan_sha256"), "plan_schema_invalid")
    if (plan["schema"] != "keel.reliability.plan.v1" or type(plan["synthetic"]) is not bool
            or plan["status"] != "PROPOSED" or plan["execution_authorized"] is not False):
        _fail("plan_contract_invalid")
    digest = plan.pop("plan_sha256", None)
    if digest != _sha(plan):
        _fail("frozen_plan_changed")
    if plan["dataset_sha256"] != dataset_digest(dataset):
        _fail("dataset_changed")
    if plan["harness_sha256"] != hashlib.sha256(Path(__file__).read_bytes()).hexdigest():
        _fail("harness_changed")
    plan["plan_sha256"] = digest
    # A caller can recompute a SHA-256. Revalidate every bounded field before
    # constructing schedules; the digest alone is neither a schema nor authority.
    for key in ("baseline", "candidate"):
        pin = plan[key]
        _keys(pin, ("source_sha256", "bytecode_sha256", "defaults_sha256", "config_sha256", "entrypoint"),
              "runner_pin_schema_invalid")
        for field in ("source_sha256", "bytecode_sha256", "defaults_sha256", "config_sha256"):
            _hash(pin[field], "runner_pin_digest_invalid")
        if type(pin["entrypoint"]) is not str or not 1 <= len(pin["entrypoint"]) <= 1024:
            _fail("runner_entrypoint_invalid")
    if type(plan["faults"]) is not list:
        _fail("faults_invalid")
    class DeclaredPin:
        def __init__(self, value):
            self.value = value
        def pin(self):
            return self.value
    reconstructed = freeze_plan(dataset, baseline=DeclaredPin(plan["baseline"]),
                                candidate=DeclaredPin(plan["candidate"]),
                                adjudications=plan["adjudications"], repeats=plan["repeats"],
                                faults=plan["faults"], seed=plan["seed"], thresholds=plan["thresholds"])
    if reconstructed != plan:
        _fail("plan_contract_invalid")
    return plan


def _subject(case, fault):
    subject = _snapshot(case["subject"])
    if fault == "missing_evidence":
        subject["evidence"] = []
    elif fault == "reversed_evidence":
        subject["evidence"].reverse()
    return subject


def _expected(case, fault):
    # Under the existing evidence rubric, claims without evidence must abstain.
    return "ABSTAIN" if fault == "missing_evidence" else case["expected_verdict"]


def run_paired(plan, dataset, *, baseline, candidate, adjudication_validator=None):
    """Actually invoke both pinned local runners on every frozen trial.

    Pair order alternates to reduce order effects. A callback receives only a
    fresh subject, config and shared seed, never case IDs, labels or rationale.
    Exceptions become ERROR and remain in all trial denominators.
    """
    dataset = validate_dataset(dataset)
    plan = _plan(plan, dataset)
    for name, runner in (("baseline", baseline), ("candidate", candidate)):
        if type(runner) is not Runner or runner.pin() != plan[name]:
            _fail("runner_pin_mismatch")
    if adjudication_validator is not None and not callable(adjudication_validator):
        _fail("adjudication_validator_invalid")
    attested = (adjudication_validator(_snapshot(plan), _snapshot(dataset)) is True
                if adjudication_validator is not None else False)
    records = []
    index = 0
    for case in dataset["cases"]:
        for repeat in range(plan["repeats"]):
            for fault in plan["faults"]:
                pair_seed = (plan["seed"] + index) % 2**32
                pair = (("baseline", baseline), ("candidate", candidate))
                if index % 2:
                    pair = tuple(reversed(pair))
                for name, runner in pair:
                    start = time.monotonic()
                    try:
                        verdict = runner.callback(_subject(case, fault), _snapshot(runner.config), pair_seed)
                        if type(verdict) is not str or verdict not in VERDICTS:
                            verdict = "ERROR"
                    except Exception:
                        verdict = "ERROR"  # Never leak a callback's exception prose.
                    records.append({"case_id": case["case_id"], "repeat": repeat,
                                    "fault": fault, "runner": name, "verdict": verdict,
                                    "latency_ms": max(0, time.monotonic() - start) * 1000})
                index += 1
    # A callback cannot mutate its config/file during the experiment and retain a pin.
    if baseline.pin() != plan["baseline"] or candidate.pin() != plan["candidate"]:
        _fail("runner_changed_during_run")
    return _summarize(plan, dataset, records, attested=attested, mode="LOCAL_CALLBACKS")


def evaluate_trials(plan, dataset, records):
    """Validate/scoring of imported trials; replay alone can never qualify code."""
    dataset = validate_dataset(dataset)
    plan = _plan(plan, dataset)
    return _summarize(plan, dataset, records, attested=False, mode="REPLAY")


def _summarize(plan, dataset, records, *, attested, mode):
    records = _snapshot(records)
    expected_keys = {(case["case_id"], repeat, fault, runner)
                     for case in dataset["cases"] for repeat in range(plan["repeats"])
                     for fault in plan["faults"] for runner in ("baseline", "candidate")}
    if type(records) is not list or len(records) != len(expected_keys):
        _fail("trial_coverage_invalid")
    by_key = {}
    for row in records:
        _keys(row, ("case_id", "repeat", "fault", "runner", "verdict", "latency_ms"), "trial_invalid")
        if type(row["repeat"]) is not int:
            _fail("trial_repeat_invalid")
        key = tuple(row[k] for k in ("case_id", "repeat", "fault", "runner"))
        if key not in expected_keys or key in by_key:
            _fail("trial_coverage_invalid")
        if row["verdict"] not in (*VERDICTS, "ERROR"):
            _fail("trial_verdict_invalid")
        _number(row["latency_ms"], 0, 86400000, "trial_latency_invalid")
        by_key[key] = row
    metrics = {}
    case_differences = {}
    for runner in ("baseline", "candidate"):
        counts = {"total": 0, "correct": 0, "false_pass": 0, "nonpass_labels": 0,
                  "errors": 0, "abstentions": 0, "case_fault_pairs": 0,
                  "unstable_case_fault_pairs": 0, "all_repeats_correct_pairs": 0}
        for case in dataset["cases"]:
            for fault in plan["faults"]:
                verdicts = [by_key[(case["case_id"], repeat, fault, runner)]["verdict"]
                            for repeat in range(plan["repeats"])]
                counts["case_fault_pairs"] += 1
                counts["unstable_case_fault_pairs"] += len(set(verdicts)) > 1
                counts["all_repeats_correct_pairs"] += all(v == _expected(case, fault) for v in verdicts)
            scores = []
            for repeat in range(plan["repeats"]):
                for fault in plan["faults"]:
                    verdict = by_key[(case["case_id"], repeat, fault, runner)]["verdict"]
                    gold = _expected(case, fault)
                    counts["total"] += 1
                    counts["correct"] += verdict == gold
                    counts["false_pass"] += verdict == "PASS" and gold != "PASS"
                    counts["nonpass_labels"] += gold != "PASS"
                    counts["errors"] += verdict == "ERROR"
                    counts["abstentions"] += verdict == "ABSTAIN"
                    scores.append(int(verdict == gold))
            mean = sum(scores) / len(scores)
            case_differences[case["case_id"]] = case_differences.get(case["case_id"], 0) + (
                mean if runner == "candidate" else -mean)
        counts["accuracy"] = counts["correct"] / counts["total"]
        counts["false_pass_rate"] = (counts["false_pass"] / counts["nonpass_labels"]
                                      if counts["nonpass_labels"] else None)
        counts["error_rate"] = counts["errors"] / counts["total"]
        counts["abstention_rate"] = counts["abstentions"] / counts["total"]
        counts["instability_rate"] = counts["unstable_case_fault_pairs"] / counts["case_fault_pairs"]
        counts["all_repeats_correct_rate"] = counts["all_repeats_correct_pairs"] / counts["case_fault_pairs"]
        metrics[runner] = counts
    clusters = {}
    for case_id, diff in case_differences.items():
        clusters.setdefault(plan["adjudications"][case_id]["cluster_id"], []).append(diff)
    means = [sum(v) / len(v) for v in clusters.values()]
    average = sum(means) / len(means)
    gates = plan["thresholds"]
    # Hoeffding for independent bounded cluster means in [-1, 1]. Repeats and
    # related cases remain within clusters; independence is a host attestation.
    radius = math.sqrt(2 * math.log(1 / gates["alpha"]) / len(means))
    lower = max(-1, average - radius)
    candidate = metrics["candidate"]
    reasons = []
    if len(means) < gates["min_clusters"]:
        reasons.append("too_few_independent_clusters")
    if lower < gates["min_accuracy_gain"]:
        reasons.append("gain_lower_bound_below_threshold")
    if candidate["false_pass_rate"] is None:
        reasons.append("no_nonpass_labels")
    elif candidate["false_pass_rate"] > gates["max_false_pass_rate"]:
        reasons.append("false_pass_threshold_exceeded")
    for metric in ("error_rate", "abstention_rate", "instability_rate"):
        if candidate[metric] > gates["max_" + metric]:
            reasons.append(metric + "_threshold_exceeded")
    eligible = not reasons
    if not attested:
        reasons.append("adjudication_and_holdout_independence_unattested")
    if dataset["synthetic"]:
        reasons.append("synthetic_fixture_only")
    if mode == "REPLAY":
        reasons.append("replay_is_not_observed_runner_execution")
    status = ("QUALIFIED" if not reasons else "PROPOSED" if eligible else "BLOCKED")
    return {"schema": "keel.reliability.report.v1", "status": status, "mode": mode,
            "plan_sha256": plan["plan_sha256"], "dataset_sha256": plan["dataset_sha256"],
            "synthetic": dataset["synthetic"], "adjudication_and_independence_attested": attested,
            "metrics": metrics, "independent_cluster_count": len(means),
            "paired_cluster_accuracy_gain": average, "gain_lower_bound": lower,
            "bound_method": "one_sided_hoeffding_cluster_means", "alpha": gates["alpha"],
            "reasons": reasons, "trials": records, "execution_authorized": False,
            "quality_scope": "Only this frozen task distribution and declared source/config pins; no deployment authority."}


def import_regressions(records, *, dataset_id, adjudication_validator):
    """Import explicitly sanitized failure cases into a DEVELOPMENT dataset.

    Only hashes, IDs, fixed error categories, verdicts and an already sanitized
    closed subject are accepted. Raw logs/messages/attachments are rejected,
    never copied or heuristically redacted. Trusted host validation must check
    both adjudication provenance and permission to retain the sanitized subject.
    """
    _token(dataset_id, "dataset_id_invalid")
    if not callable(adjudication_validator):
        _fail("trusted_adjudication_validator_required")
    records = _snapshot(records)
    if type(records) is not list or not 1 <= len(records) <= 256:
        _fail("regression_count_invalid")
    cases, provenance = [], []
    for record in records:
        _keys(record, ("case_id", "failure_kind", "expected_verdict", "sanitized_subject",
                       "adjudicator_id", "adjudication_sha256", "source_event_sha256", "synthetic"), "regression_whitelist_violation")
        if type(record["synthetic"]) is not bool:
            _fail("regression_synthetic_flag_required")
        if record["failure_kind"] not in ("false_pass", "false_block", "abstention", "error", "inconsistent"):
            _fail("failure_kind_invalid")
        _token(record["adjudicator_id"], "adjudicator_invalid")
        for key in ("adjudication_sha256", "source_event_sha256"):
            _hash(record[key], "provenance_digest_invalid")
        if adjudication_validator(_snapshot(record)) is not True:
            _fail("regression_adjudication_or_privacy_unverified")
        cases.append({"case_id": record["case_id"], "tags": [record["failure_kind"]],
                      "expected_verdict": record["expected_verdict"],
                      "label_rationale": "Adjudication reference: " + record["adjudication_sha256"],
                      "subject": record["sanitized_subject"]})
        provenance.append({k: record[k] for k in ("case_id", "adjudicator_id", "adjudication_sha256", "source_event_sha256", "synthetic")})
    synthetic = any(r["synthetic"] for r in records)
    dataset = validate_dataset({"schema": "keel.eval.dataset.v1", "dataset_id": dataset_id,
                                "synthetic": synthetic, "split": "development",
                                "label_source": "synthetic_fixture" if synthetic else "operator_supplied", "cases": cases})
    return {"dataset": dataset, "provenance": provenance, "status": "PROPOSED", "execution_authorized": False,
            "privacy_and_adjudication_host_attested": True, "independently_authenticated": False,
            "held_out_eligible": False, "reason": "Observed failures used for development cannot silently become independent holdout cases."}


def _demo_baseline(subject, config, seed):
    return "PASS"


def _demo_candidate(subject, config, seed):
    return "ABSTAIN" if not subject["evidence"] else "FAIL"


def synthetic_demo():
    """Real callback invocations over artificial cases; never production evidence."""
    dataset = {"schema": "keel.eval.dataset.v1", "dataset_id": "synthetic-reliability-demo",
               "synthetic": True, "split": "held_out", "label_source": "synthetic_fixture",
               "cases": [{"case_id": "case-" + str(i), "tags": ["synthetic"],
                          "expected_verdict": "FAIL", "label_rationale": "Synthetic contradiction.",
                          "subject": {"required_claim_ids": ["claim"],
                                      "claims": [{"claim_id": "claim", "text": "Fixture flag is true."}],
                                      "evidence": [{"evidence_id": "evidence", "text": "Fixture flag is false."}]}}
                         for i in range(24)]}
    adjudications = {c["case_id"]: {"cluster_id": c["case_id"], "adjudicator_id": "synthetic-fixture",
                                    "record_sha256": _sha(c)} for c in dataset["cases"]}
    baseline, candidate = Runner(_demo_baseline, {}), Runner(_demo_candidate, {})
    plan = freeze_plan(dataset, baseline=baseline, candidate=candidate, adjudications=adjudications,
                       faults=FAULTS)
    return {"plan": plan, "report": run_paired(plan, dataset, baseline=baseline, candidate=candidate)}
