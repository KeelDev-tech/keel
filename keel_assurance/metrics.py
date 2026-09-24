"""Bounded, offline research telemetry. No metric grants execution authority.

Labels and ground-truth references are caller-supplied; these functions do not
authenticate them. IDs must denote independent evaluation units, not merely
different rows. Aggregate uncertainty intervals are descriptive Wilson
intervals, not guarantees under correlated observations or distribution shift.

ECS is a *Keel adaptation*, not a validated scale from the author's paper.
Brier loss combines calibration, discrimination and outcome uncertainty; it is
not named or treated as a pure calibration measure here.
"""
from __future__ import annotations

import math
from decimal import Decimal, ROUND_CEILING

MAX_RECORDS = 10000
MAX_ID_LENGTH = 256
ECS_VERSION = "keel.ecs.descriptive.v1"
CONFORMAL_METHOD = "split-conformal-binary-inverse-probability.v1"
_COMPONENTS = ("source_grounding", "calibration", "adversarial_correction")


class MetricsError(ValueError):
    """Malformed or inconsistent research input; no silent coercion."""


def _number(value, name, low=0.0, high=1.0):
    if type(value) not in (int, float):
        raise MetricsError(f"{name}: finite number required, not a boolean")
    try:
        finite = math.isfinite(value)
    except OverflowError as exc:
        raise MetricsError(f"{name}: outside finite numeric range") from exc
    if not finite or not low <= value <= high:
        raise MetricsError(f"{name}: finite number in [{low}, {high}] required")
    return float(value)


def _integer(value, name, low=0, high=MAX_RECORDS):
    if type(value) is not int or not low <= value <= high:
        raise MetricsError(f"{name}: integer in [{low}, {high}] required")
    return value


def _boolean(value, name, nullable=False):
    if nullable and value is None:
        return None
    if type(value) is not bool:
        raise MetricsError(f"{name}: boolean required")
    return value


def _text(value, name, maximum=MAX_ID_LENGTH):
    if (type(value) is not str or not value or value != value.strip()
            or len(value) > maximum or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise MetricsError(f"{name}: bounded nonempty string without control characters required")
    return value


def _sequence(value, name, maximum=MAX_RECORDS):
    if type(value) not in (list, tuple) or len(value) > maximum:
        raise MetricsError(f"{name}: list or tuple with at most {maximum} entries required")
    return value


def _fields(record, required, optional=()):
    if type(record) is not dict:
        raise MetricsError("record: plain object required")
    if not set(required) <= record.keys() or record.keys() - set(required) - set(optional):
        raise MetricsError("record: missing required or unknown fields")


def _unique_ids(values, name):
    result = []
    seen = set()
    for value in _sequence(values, name):
        value = _text(value, name)
        if value in seen:
            raise MetricsError(f"{name}: duplicate ID {value!r}")
        seen.add(value)
        result.append(value)
    return result


def _predictions(records):
    result, seen = [], set()
    for record in _sequence(records, "predictions"):
        _fields(record, ("id", "probability", "outcome"), ("abstained",))
        identity = _text(record["id"], "id")
        if identity in seen:
            raise MetricsError(f"duplicate prediction ID {identity!r}; conflicting labels are not merged")
        seen.add(identity)
        result.append({
            "id": identity,
            "probability": _number(record["probability"], "probability"),
            "outcome": _boolean(record["outcome"], "outcome", nullable=True),
            "abstained": _boolean(record.get("abstained", False), "abstained"),
        })
    return result


def _ratio(numerator, denominator):
    return numerator / denominator if denominator else None


def wilson_interval(successes, total, *, z=1.959963984540054):
    """Wilson interval (95% at default z); null for zero observations.

    Bounded counts protect accidental unbounded inputs. This is a binomial
    interval; independent trials are an assumption, not checked here.
    """
    total = _integer(total, "total")
    successes = _integer(successes, "successes", high=total)
    z = _number(z, "z", low=0.000001, high=10.0)
    if total == 0:
        return None
    p = successes / total
    denominator = 1 + z * z / total
    center = (p + z * z / (2 * total)) / denominator
    margin = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denominator
    return [max(0.0, center - margin), min(1.0, center + margin)]


def evaluate_predictions(records, *, bins=10, threshold=0.5):
    """Evaluate binary probabilities, preserving abstentions and unknown labels.

    Input: [{id: str, probability: float, outcome: bool | None,
             abstained?: bool}]. Numeric labels 0/1 are deliberately rejected.
    Brier loss and positive-class ECE use ALL known outcomes, including
    abstentions; selective accuracy uses only answered, labeled predictions.
    Bins are equal-width, left-inclusive/right-exclusive except final bin.
    ECE is sample- and bin-dependent and can be misleading for small samples.
    """
    rows = _predictions(records)
    bins = _integer(bins, "bins", low=1, high=100)
    threshold = _number(threshold, "threshold")
    labeled = [row for row in rows if row["outcome"] is not None]
    answered = [row for row in rows if not row["abstained"]]
    answered_labeled = [row for row in labeled if not row["abstained"]]
    correct = sum((r["probability"] >= threshold) == r["outcome"] for r in labeled)
    answered_correct = sum((r["probability"] >= threshold) == r["outcome"] for r in answered_labeled)
    groups = [[] for _ in range(bins)]
    for row in labeled:
        groups[min(int(row["probability"] * bins), bins - 1)].append(row)
    reliability = []
    weighted_error = 0.0
    for index, group in enumerate(groups):
        count = len(group)
        positives = sum(row["outcome"] for row in group)
        predicted = math.fsum(row["probability"] for row in group) / count if count else None
        observed = _ratio(positives, count)
        gap = abs(predicted - observed) if count else None
        weighted_error += count * gap if count else 0.0
        reliability.append({
            "lower": index / bins, "upper": (index + 1) / bins,
            "upper_inclusive": index == bins - 1, "count": count,
            "mean_probability": predicted, "observed_positive_rate": observed,
            "absolute_gap": gap, "positive_rate_wilson": wilson_interval(positives, count),
        })
    return {
        "schema_version": 1, "method": "binary-probability-telemetry.v1",
        "execution_authority": False,
        "counts": {"total": len(rows), "labeled": len(labeled),
                   "unknown_outcomes": len(rows) - len(labeled),
                   "answered": len(answered), "abstained": len(rows) - len(answered),
                   "answered_labeled": len(answered_labeled),
                   "answered_unknown": len(answered) - len(answered_labeled),
                   "correct_all_labeled": correct, "correct_answered_labeled": answered_correct},
        "coverage": _ratio(len(answered), len(rows)),
        "labeled_coverage": _ratio(len(answered_labeled), len(labeled)),
        "brier_loss": _ratio(math.fsum((r["probability"] - int(r["outcome"])) ** 2 for r in labeled), len(labeled)),
        "ece": _ratio(weighted_error, len(labeled)),
        "accuracy_all_labeled": _ratio(correct, len(labeled)),
        "selective_accuracy": _ratio(answered_correct, len(answered_labeled)),
        "selective_accuracy_wilson": wilson_interval(answered_correct, len(answered_labeled)),
        "threshold": threshold, "bins": reliability,
        "limitations": ["Caller-supplied outcomes are not independently authenticated.",
                        "Brier loss is not a pure calibration measure.",
                        "ECE depends on sample size and binning; no release threshold is supplied.",
                        "Selective accuracy excludes abstentions; coverage must accompany it."],
    }


def evaluate_challenges(records):
    """Ground-reference-backed adversarial correction and regression telemetry.

    Input records: {id, challenged: bool, revised: bool,
      before_correct: bool | None, after_correct: bool | None,
      ground_truth_ref: str | None}. A supplied reference is an audit pointer,
    not proof that a label is true. Missing labels or references are excluded.
    Duplicate IDs and contradictory revision flags are rejected.

    ACR = wrong-to-correct / eligible challenged initially-wrong cases.
    Harm rate = correct-to-wrong / eligible challenged initially-correct cases.
    Correction precision = wrong-to-correct / eligible challenged revisions,
    including ineffective revisions in the denominator.
    """
    rows, seen = [], set()
    for row in _sequence(records, "challenges"):
        _fields(row, ("id", "challenged", "revised", "before_correct", "after_correct", "ground_truth_ref"))
        identity = _text(row["id"], "id")
        if identity in seen:
            raise MetricsError(f"duplicate challenge ID {identity!r}")
        seen.add(identity)
        challenged = _boolean(row["challenged"], "challenged")
        revised = _boolean(row["revised"], "revised")
        before = _boolean(row["before_correct"], "before_correct", nullable=True)
        after = _boolean(row["after_correct"], "after_correct", nullable=True)
        ref = row["ground_truth_ref"]
        if ref is not None:
            _text(ref, "ground_truth_ref", maximum=2048)
        if before is not None and after is not None and before != after and not revised:
            raise MetricsError("correctness changed but revised is false")
        rows.append((challenged, revised, before, after, ref))
    challenged_rows = [row for row in rows if row[0]]
    eligible = [row for row in challenged_rows if row[2] is not None and row[3] is not None and row[4] is not None]
    incomplete = sum(row[2] is None or row[3] is None for row in challenged_rows)
    ungrounded = sum(row[4] is None for row in challenged_rows)
    wrong = sum(row[2] is False for row in eligible)
    right = len(eligible) - wrong
    corrections = sum(row[2] is False and row[3] is True for row in eligible)
    regressions = sum(row[2] is True and row[3] is False for row in eligible)
    revisions = sum(row[1] for row in eligible)
    return {
        "schema_version": 1, "method": "grounded-challenge-telemetry.v1",
        "execution_authority": False,
        "counts": {"total": len(rows), "challenged": len(challenged_rows),
                   "not_challenged": len(rows) - len(challenged_rows), "eligible": len(eligible),
                   "missing_labels": incomplete, "missing_ground_truth_ref": ungrounded,
                   "excluded_union": len(challenged_rows) - len(eligible),
                   "initially_incorrect": wrong, "initially_correct": right,
                   "revisions": revisions, "corrections": corrections,
                   "harmful_revisions": regressions,
                   "ineffective_or_unnecessary_revisions": revisions - corrections - regressions},
        "acr": _ratio(corrections, wrong), "acr_wilson": wilson_interval(corrections, wrong),
        "harmful_revision_rate": _ratio(regressions, right),
        "harmful_revision_rate_wilson": wilson_interval(regressions, right),
        "correction_precision": _ratio(corrections, revisions),
        "correction_precision_wilson": wilson_interval(corrections, revisions),
        "limitations": ["References and correctness labels are caller-supplied, not authenticated.",
                        "Missing-label and missing-reference counts can overlap; excluded_union does not.",
                        "Observed correction is not a causal estimate of reviewer benefit.",
                        "Correlated or repeatedly counted cases invalidate independent-trial interpretation."],
    }


def ecs_score(*, source_grounding, calibration, adversarial_correction, weights=None):
    """Versioned, unvalidated descriptive composite; higher inputs are better.

    Components are normalized to [0,1] by the caller; calibration may be
    1-ECE but never implicitly 1-Brier. Equal positive weights are a Keel
    engineering choice, not claimed author-prescribed or empirically fitted.
    Missing any component yields null, never an optimistic re-normalization.
    A custom positive weight for each component is permitted and reported.
    """
    components = dict(zip(_COMPONENTS, (source_grounding, calibration, adversarial_correction)))
    for name, value in components.items():
        if value is not None:
            components[name] = _number(value, name)
    if weights is None:
        weights = dict.fromkeys(_COMPONENTS, 1.0)
    else:
        _fields(weights, _COMPONENTS)
        weights = {name: _number(value, f"weight.{name}", low=0.000001, high=1000000)
                   for name, value in weights.items()}
    total_weight = math.fsum(weights.values())
    normalized = {name: weights[name] / total_weight for name in _COMPONENTS}
    missing = [name for name in _COMPONENTS if components[name] is None]
    score = None if missing else math.fsum(components[name] * normalized[name] for name in _COMPONENTS)
    return {"schema_version": 1, "version": ECS_VERSION, "score": score,
            "components": components, "normalized_weights": normalized,
            "missing_components": missing, "empirically_validated": False,
            "execution_authority": False, "status": "unavailable" if missing else "descriptive_only",
            "method_note": "Keel engineering adaptation: weighted arithmetic mean, not a validated research scale."}


def _quantile(scores, alpha):
    # Decimal avoids rounding an exact integer rank just above its boundary.
    rank = int(((len(scores) + 1) * (Decimal(1) - Decimal(str(alpha)))).to_integral_value(rounding=ROUND_CEILING))
    return rank, 1.0 if rank > len(scores) else sorted(scores)[rank - 1]


def fit_conformal(calibration_records, *, alpha=0.1, training_ids=(), test_ids=()):
    """Fit binary split-conformal prediction sets using held-out labeled cases.

    Records use the evaluate_predictions schema. Calibration must contain
    known outcomes and no abstentions. IDs across training/calibration/test
    partitions must be distinct. ID checks cannot detect semantic duplicates,
    training leakage omitted by the caller, or a model trained on calibration.

    Score is 1 - probability(true class); rank ceil((n+1)*(1-alpha))
    includes the finite-sample correction. Rank > n produces both classes.
    Marginal coverage requires exchangeable samples and a fixed predictor;
    this function cannot establish those assumptions or conditional coverage.
    """
    rows = _predictions(calibration_records)
    if not rows or any(row["outcome"] is None or row["abstained"] for row in rows):
        raise MetricsError("calibration requires nonempty, fully labeled, non-abstained records")
    alpha = _number(alpha, "alpha")
    if alpha in (0.0, 1.0):
        raise MetricsError("alpha must be strictly between zero and one")
    training = _unique_ids(training_ids, "training_ids")
    test = _unique_ids(test_ids, "test_ids")
    calibration = [row["id"] for row in rows]
    partitions = (set(training), set(calibration), set(test))
    if any(partitions[i] & partitions[j] for i in range(3) for j in range(i + 1, 3)):
        raise MetricsError("training/calibration/test ID leakage")
    scores = [1.0 - row["probability"] if row["outcome"] else row["probability"] for row in rows]
    rank, threshold = _quantile(scores, alpha)
    return {"schema_version": 1, "method": CONFORMAL_METHOD, "alpha": alpha,
            "calibration_size": len(rows), "quantile_rank": rank,
            "threshold": threshold, "finite_sample_vacuous": rank > len(rows),
            "calibration_ids": calibration, "training_ids": training, "test_ids": test,
            "nonconformity_scores": scores}


def conformal_sets(model, test_records):
    """Apply a checked split-conformal model; output sets never authorize actions.

    Test labels may be null and are unused in set construction. Non-singleton
    sets, including empty sets, request abstention. A singleton is merely an
    uncertainty estimate, not permission to execute. Fit metadata is checked
    for internal consistency, not cryptographic authenticity.
    """
    _fields(model, ("schema_version", "method", "alpha", "calibration_size", "quantile_rank",
                    "threshold", "finite_sample_vacuous", "calibration_ids", "training_ids",
                    "test_ids", "nonconformity_scores"))
    if type(model["schema_version"]) is not int or model["schema_version"] != 1 or model["method"] != CONFORMAL_METHOD:
        raise MetricsError("unsupported conformal schema or method")
    alpha = _number(model["alpha"], "alpha")
    if alpha in (0.0, 1.0):
        raise MetricsError("alpha must be strictly between zero and one")
    calibration = _unique_ids(model["calibration_ids"], "calibration_ids")
    training = _unique_ids(model["training_ids"], "training_ids")
    test = _unique_ids(model["test_ids"], "test_ids")
    n = _integer(model["calibration_size"], "calibration_size", low=1)
    scores = [_number(value, "nonconformity_score") for value in _sequence(model["nonconformity_scores"], "scores")]
    if len(calibration) != n or len(scores) != n:
        raise MetricsError("inconsistent conformal calibration size")
    if set(calibration) & set(training) or set(test) & (set(training) | set(calibration)):
        raise MetricsError("training/calibration/test ID leakage")
    rank, threshold = _quantile(scores, alpha)
    if (_integer(model["quantile_rank"], "quantile_rank", low=1, high=MAX_RECORDS + 1) != rank
            or _number(model["threshold"], "threshold") != threshold
            or _boolean(model["finite_sample_vacuous"], "finite_sample_vacuous") != (rank > n)):
        raise MetricsError("inconsistent conformal rank or threshold")
    rows = _predictions(test_records)
    used, reserved = set(calibration) | set(training), set(test)
    predictions = []
    for row in rows:
        if row["id"] in used:
            raise MetricsError("test ID leaked into calibration or training")
        if reserved and row["id"] not in reserved:
            raise MetricsError("test ID is outside declared held-out partition")
        p = row["probability"]
        labels = ([False] if p <= threshold else []) + ([True] if 1.0 - p <= threshold else [])
        predictions.append({"id": row["id"], "labels": labels,
                            "abstain": row["abstained"] or len(labels) != 1})
    return {"schema_version": 1, "method": CONFORMAL_METHOD, "execution_authority": False,
            "alpha": alpha, "calibration_size": n, "threshold": threshold,
            "finite_sample_vacuous": rank > n, "predictions": predictions,
            "limitations": ["Marginal coverage requires a fixed predictor and exchangeable calibration/test samples.",
                            "ID separation does not prove absence of model-training or semantic leakage.",
                            "Temporal shift, selection bias, correlated samples and tuning on calibration break assumptions.",
                            "No per-case correctness, conditional coverage, safety or execution permission is guaranteed."]}
