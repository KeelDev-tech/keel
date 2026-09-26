"""Local preregistered policy experiments. Proposals never authorize execution.

The database is trusted host state, not a sandbox or a signature system. The
host validator must authenticate the event, its bindings, and the truth of the
assertions it approves; accepting an arbitrary JSON boolean is not adequate.
"""
from datetime import datetime, timezone
from fractions import Fraction
import hashlib
import json
import math
import os
import stat
from pathlib import Path
import re
import secrets
import sqlite3


class LearningError(ValueError):
    """Stable, content-free rejection."""


def require(condition, code):
    if not condition:
        raise LearningError(code)


def canonical(value):
    try:
        raw = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
        require(len(raw.encode()) <= 16_000_000, "input_too_large")
        return raw
    except (TypeError, ValueError, OverflowError, RecursionError):
        raise LearningError("invalid_json") from None


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def token(value):
    require(type(value) is str and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", value), "invalid_id")
    return value


def number(value, low, high, code="invalid_number"):
    require(type(value) in (float, int) and math.isfinite(value) and low <= value <= high, code)
    return value


def stamp(value):
    try:
        require(type(value) is str, "timestamp_required")
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        require(dt.utcoffset() is not None, "timezone_required")
        return dt.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        raise LearningError("invalid_timestamp") from None


def bindings(value):
    require(type(value) is dict and set(value) == {"source_sha256", "config_sha256", "model_sha256", "calibration_sha256"}, "bindings_required")
    require(all(type(v) is str and re.fullmatch(r"[a-f0-9]{64}", v) for v in value.values()), "invalid_binding_hash")
    return value


def distribution(value, actions):
    require(type(value) is dict and set(value) == set(actions), "all_action_probabilities_required")
    for v in value.values():
        number(v, 0, 1, "invalid_probability")
    fractions = {a: Fraction(str(value[a])) for a in actions}
    require(sum(fractions.values()) == 1, "probabilities_must_sum_exactly_one")
    denominator = math.lcm(*(v.denominator for v in fractions.values()))
    require(denominator <= 10**12, "probability_precision_limit")
    return {a: int(v * denominator) for a, v in fractions.items()}, denominator


def validate_plan(plan):
    plan = json.loads(canonical(plan))
    require(type(plan) is dict and set(plan) == {"schema", "experiment_id", "bindings", "actions", "strata", "objective", "predictor_frozen_at", "predictor_training_sha256", "delta", "min_samples", "min_ess", "min_improvement", "synthetic"}, "plan_fields_invalid")
    require(plan["schema"] == "keel.learning.experiment.v1", "plan_schema_invalid")
    token(plan["experiment_id"])
    bindings(plan["bindings"])
    actions = plan["actions"]
    require(type(actions) is list and 2 <= len(actions) <= 32 and len(set(actions)) == len(actions), "actions_invalid")
    for action in actions:
        token(action)
    require(type(plan["strata"]) is dict and 1 <= len(plan["strata"]) <= 64, "strata_invalid")
    for sid, row in plan["strata"].items():
        token(sid)
        require(type(row) is dict and set(row) == {"logging", "candidate", "baseline", "predictions"}, "stratum_fields_invalid")
        for key in ("logging", "candidate", "baseline"):
            distribution(row[key], actions)
        require(type(row["predictions"]) is dict and set(row["predictions"]) == set(actions), "frozen_predictions_required")
        for prediction in row["predictions"].values():
            number(prediction, 0, 1, "prediction_out_of_bounds")
    token(plan["objective"])
    stamp(plan["predictor_frozen_at"])
    require(type(plan["predictor_training_sha256"]) is str and re.fullmatch(r"[a-f0-9]{64}", plan["predictor_training_sha256"]), "training_pin_required")
    number(plan["delta"], 1e-12, .25, "invalid_delta")
    require(type(plan["min_samples"]) is int and 1 <= plan["min_samples"] <= 100000, "min_samples_invalid")
    number(plan["min_ess"], 1, 100000, "min_ess_invalid")
    number(plan["min_improvement"], 0, 1)
    require(type(plan["synthetic"]) is bool, "synthetic_flag_required")
    return plan


def bounded_confidence_sequence(values, *, lower, upper, delta):
    """Hoeffding + union bound: delta_n=delta/[n(n+1)].

    Simultaneously covers the running mean of conditional expectations when
    bounded observations are adapted and their declared bounds are valid.
    No IID claim is needed for that target. A fixed population mean additionally
    needs constant conditional mean; arbitrary drift has no future guarantee.
    """
    number(lower, -1e12, 1e12)
    number(upper, -1e12, 1e12)
    require(lower < upper, "bounds_invalid")
    number(delta, 1e-12, .5)
    require(type(values) is list and len(values) <= 100000, "sample_limit")
    result, total = [], 0.0
    for n, value in enumerate(values, 1):
        number(value, lower, upper, "observation_outside_bound")
        total = math.fsum((total, value))
        mean = total / n
        radius = (upper - lower) * math.sqrt(math.log(2 * n * (n + 1) / delta) / (2 * n))
        result.append({"n": n, "estimate": mean,
            "lower": max(lower, math.nextafter(mean - radius, -math.inf)),
            "upper": min(upper, math.nextafter(mean + radius, math.inf)),
            "radius": math.nextafter(radius, math.inf)})
    return result


class ExperimentRegistry:
    """One canonical host-private DB; immutable plans and outcomes.

    The host authenticates register/decision/outcome/evaluate events. Alpha is
    spent when registering and is never refunded. Reopening cannot reset it.
    A different database is a new family, not a way to reset global error risk.
    """
    def __init__(self, path, *, validator, alpha_budget=.05, clock=None):
        require(callable(validator), "host_validator_required")
        number(alpha_budget, 1e-12, .25)
        self.validator = validator
        self.clock = clock or (lambda: datetime.now(timezone.utc).isoformat())
        self.path = None
        if str(path) != ":memory:":
            path = Path(path)
            require(path.is_absolute() and ".." not in path.parts, "absolute_db_path_required")
            require(not any(p.is_symlink() for p in (path, *path.parents)), "symlink_db_path_forbidden")
            parent = path.parent.stat()
            require(stat.S_ISDIR(parent.st_mode) and parent.st_uid == os.getuid() and not stat.S_IMODE(parent.st_mode) & 0o077, "private_parent_required")
            self.parent_identity = (parent.st_dev, parent.st_ino)
            fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            try:
                info = os.fstat(fd)
                require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and info.st_nlink == 1 and not stat.S_IMODE(info.st_mode) & 0o077, "private_regular_db_required")
                self.path = path
                self.db_identity = (info.st_dev, info.st_ino)
            finally:
                os.close(fd)
        self.db = sqlite3.connect(str(path), timeout=10)
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript("""
          CREATE TABLE IF NOT EXISTS settings(id INTEGER PRIMARY KEY CHECK(id=1), alpha REAL NOT NULL);
          CREATE TABLE IF NOT EXISTS experiments(id TEXT PRIMARY KEY, plan TEXT NOT NULL, pin TEXT NOT NULL, registered_at TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS decisions(seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL, experiment TEXT NOT NULL REFERENCES experiments(id), unit TEXT NOT NULL, stratum TEXT NOT NULL, action TEXT NOT NULL, probabilities TEXT NOT NULL, decided_at TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS outcomes(decision TEXT PRIMARY KEY REFERENCES decisions(id), outcome TEXT NOT NULL, observed_at TEXT NOT NULL);
        """)
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO settings VALUES(1,?)", (alpha_budget,))
            require(self.db.execute("SELECT alpha FROM settings").fetchone()[0] == alpha_budget, "alpha_budget_changed")

    def _check_storage(self):
        if self.path is None:
            return
        try:
            require(not any(p.is_symlink() for p in (self.path, *self.path.parents)), "db_path_changed")
            parent = self.path.parent.stat()
            require((parent.st_dev, parent.st_ino) == self.parent_identity and parent.st_uid == os.getuid()
                    and not stat.S_IMODE(parent.st_mode) & 0o077, "db_parent_changed")
            info = self.path.stat()
            require((info.st_dev, info.st_ino) == self.db_identity, "db_identity_changed")
            for candidate in (self.path, *(Path(str(self.path)+suffix) for suffix in ("-journal", "-wal", "-shm"))):
                if candidate.exists() or candidate.is_symlink():
                    item = candidate.lstat()
                    require(stat.S_ISREG(item.st_mode) and item.st_uid == os.getuid() and item.st_nlink == 1
                            and not stat.S_IMODE(item.st_mode) & 0o077, "private_regular_db_required")
        except OSError:
            raise LearningError("db_path_unavailable") from None

    def close(self):
        self.db.close()

    def _authorize(self, event, proof):
        self._check_storage()
        snapshot = json.loads(canonical(event))
        try:
            accepted = self.validator(snapshot, proof)
        except Exception:
            accepted = False
        require(accepted is True, "host_attestation_rejected")

    def register(self, plan, *, proof):
        plan = validate_plan(plan)
        now = self.clock()
        require(stamp(plan["predictor_frozen_at"]) < stamp(now), "predictor_not_frozen_before_experiment")
        pin = digest(plan)
        self._authorize({"kind": "register", "plan": plan, "registered_at": now,
            "required_assertions": ["predictor_trained_without_experiment_outcomes", "policies_fixed_before_outcomes", "eligible_unit_population_declared", "reward_objective_and_horizon_predeclared"]}, proof)
        self.db.execute("BEGIN IMMEDIATE")
        try:
            require(self.db.execute("SELECT 1 FROM experiments WHERE id=?", (plan["experiment_id"],)).fetchone() is None, "experiment_already_frozen")
            spent = sum((Fraction(str(json.loads(r[0])["delta"])) for r in self.db.execute("SELECT plan FROM experiments")), Fraction(0))
            require(spent + Fraction(str(plan["delta"])) <= Fraction(str(self.db.execute("SELECT alpha FROM settings").fetchone()[0])), "confidence_budget_exhausted")
            self.db.execute("INSERT INTO experiments VALUES(?,?,?,?)", (plan["experiment_id"], canonical(plan), pin, now))
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise
        return pin

    def _plan(self, experiment_id, expected_pin):
        self._check_storage()
        row = self.db.execute("SELECT plan,pin,registered_at FROM experiments WHERE id=?", (token(experiment_id),)).fetchone()
        require(row is not None, "experiment_unknown")
        require(row[1] == expected_pin and digest(json.loads(row[0])) == expected_pin, "experiment_pin_mismatch")
        return validate_plan(json.loads(row[0])), row[2]

    def decide(self, experiment_id, *, expected_pin, decision_id, unit_id, stratum, current_bindings, proof, synthetic_draw=None):
        """Randomize locally, commit the full distribution, then return action.

        Does not invoke an agent, browse a source, or approve any operation.
        One cluster/unit per experiment is accepted. Caller cannot pick an
        observed action and retrospectively invent its selection probability.
        """
        plan, registered = self._plan(experiment_id, expected_pin)
        token(decision_id); token(unit_id); token(stratum)
        require(bindings(current_bindings) == plan["bindings"], "bindings_drifted")
        require(stratum in plan["strata"], "unknown_stratum")
        probabilities = plan["strata"][stratum]["logging"]
        weights, denominator = distribution(probabilities, plan["actions"])
        require(synthetic_draw is None or plan["synthetic"], "production_random_override_forbidden")
        now = self.clock()
        require(stamp(now) >= stamp(registered), "clock_reversed")
        self._authorize({"kind": "decision", "experiment_id": experiment_id, "experiment_pin": expected_pin,
            "decision_id": decision_id, "unit_id": unit_id, "stratum": stratum, "bindings": current_bindings,
            "decided_at": now, "logging_probabilities": probabilities,
            "required_assertions": ["unit_not_previously_observed", "no_outcome_based_enrollment", "stratum_pre_action", "all_actions_permitted_for_unit"]}, proof)
        draw = secrets.randbelow(denominator) if synthetic_draw is None else synthetic_draw
        require(type(draw) is int and 0 <= draw < denominator, "random_draw_invalid")
        for action in plan["actions"]:
            if draw < weights[action]:
                break
            draw -= weights[action]
        self.db.execute("BEGIN IMMEDIATE")
        try:
            require(self.db.execute("SELECT 1 FROM decisions WHERE experiment=? AND unit=?", (experiment_id, unit_id)).fetchone() is None, "repeated_unit_cluster")
            require(self.db.execute("SELECT 1 FROM decisions WHERE id=?", (decision_id,)).fetchone() is None, "duplicate_decision")
            self.db.execute("INSERT INTO decisions(id,experiment,unit,stratum,action,probabilities,decided_at) VALUES(?,?,?,?,?,?,?)", (decision_id, experiment_id, unit_id, stratum, action, canonical(probabilities), now))
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise
        return {"decision_id": decision_id, "action": action, "action_probability": probabilities[action], "logging_probabilities": probabilities, "experiment_pin": expected_pin, "execution_authorized": False}

    def record_outcome(self, decision_id, *, reward, human_minutes, executed_action, observation_sha256, current_bindings, proof):
        self._check_storage()
        row = self.db.execute("SELECT d.action,d.decided_at,e.plan FROM decisions d JOIN experiments e ON e.id=d.experiment WHERE d.id=?", (token(decision_id),)).fetchone()
        require(row is not None, "decision_must_precede_outcome")
        require(executed_action == row[0], "assigned_action_not_followed")
        require(bindings(current_bindings) == json.loads(row[2])["bindings"], "bindings_drifted")
        require(bindings(current_bindings) == json.loads(row[2])["bindings"], "bindings_drifted")
        number(reward, 0, 1, "reward_out_of_bounds")
        number(human_minutes, 0, 1000000, "actual_effort_required")
        require(type(observation_sha256) is str and re.fullmatch(r"[a-f0-9]{64}", observation_sha256), "observation_pin_required")
        now = self.clock()
        require(stamp(now) >= stamp(row[1]), "clock_reversed")
        outcome = {"reward": reward, "human_minutes": human_minutes, "executed_action": executed_action, "observation_sha256": observation_sha256}
        self._authorize({"kind": "outcome", "decision_id": decision_id, "outcome": outcome, "bindings": current_bindings, "observed_at": now,
            "required_assertions": ["authenticated_observation", "actual_human_effort", "objective_horizon_complete", "executed_action_confirmed"]}, proof)
        try:
            with self.db:
                self.db.execute("INSERT INTO outcomes VALUES(?,?,?)", (decision_id, canonical(outcome), now))
        except sqlite3.IntegrityError:
            raise LearningError("outcome_already_frozen") from None

    def evaluate(self, experiment_id, *, expected_pin, current_bindings, proof):
        plan, _ = self._plan(experiment_id, expected_pin)
        holds = []
        if bindings(current_bindings) != plan["bindings"]:
            holds.append("BINDINGS_DRIFTED")
        if plan["synthetic"]:
            holds.append("SYNTHETIC_ONLY")
        rows = self.db.execute("SELECT d.id,d.unit,d.stratum,d.action,d.probabilities,o.outcome FROM decisions d LEFT JOIN outcomes o ON o.decision=d.id WHERE d.experiment=? ORDER BY d.seq", (experiment_id,)).fetchall()
        evaluated_at = self.clock()
        stamp(evaluated_at)
        snapshot_sha256 = digest(rows)
        self._authorize({"kind": "evaluate", "experiment_pin": expected_pin, "bindings": current_bindings,
            "evaluated_at": evaluated_at, "snapshot_sha256": snapshot_sha256,
            "observation_sha256s": [json.loads(r[5])["observation_sha256"] for r in rows if r[5] is not None],
            "required_assertions": ["fresh_nonreplayed_proof", "no_revoked_observations", "truthful_cluster_ids", "no_unlogged_enrollment", "stable_target_population", "no_detected_distribution_drift"]}, proof)
        if any(r[5] is None for r in rows):
            holds.append("INCOMPLETE_OUTCOMES")
        # Complete prefix only, not faster outcomes selected from later rows.
        complete = []
        for row in rows:
            if row[5] is None:
                break
            complete.append(row)
        overlap = True
        max_weight, max_difference_weight = 0.0, 0.0
        for table in plan["strata"].values():
            for action in plan["actions"]:
                p, pi, base = (table[k][action] for k in ("logging", "candidate", "baseline"))
                if (pi > 0 or base > 0) and p == 0:
                    overlap = False
                elif p > 0:
                    max_weight = max(max_weight, pi / p, base / p)
                    max_difference_weight = max(max_difference_weight, abs(pi - base) / p)
        if not overlap:
            holds.append("NO_OVERLAP")
        if max_weight > 10000:
            holds.append("WEIGHTS_TOO_LARGE")
        estimates, differences, weights = [], [], {"candidate": [], "baseline": []}
        minutes, seen_obs = [], set()
        if overlap and max_weight <= 10000:
            for _, _, sid, action, raw_probabilities, raw_outcome in complete:
                table, outcome = plan["strata"][sid], json.loads(raw_outcome)
                require(json.loads(raw_probabilities) == table["logging"], "logged_propensities_changed")
                if outcome["observation_sha256"] in seen_obs:
                    holds.append("REPEATED_OBSERVATION")
                seen_obs.add(outcome["observation_sha256"])
                p, m, reward = table["logging"][action], table["predictions"], outcome["reward"]
                require(p > 0, "impossible_logged_action")
                pair = {}
                for policy in ("candidate", "baseline"):
                    w = table[policy][action] / p
                    weights[policy].append(w)
                    pair[policy] = math.fsum(table[policy][a] * m[a] for a in plan["actions"]) + w * (reward - m[action])
                estimates.append(pair)
                differences.append(pair["candidate"] - pair["baseline"])
                minutes.append(outcome["human_minutes"])
        n = len(differences)
        ess = {key: math.fsum(w)**2 / math.fsum(x*x for x in w) if any(w) else 0.0 for key, w in weights.items()}
        if n < plan["min_samples"]:
            holds.append("INSUFFICIENT_SAMPLES")
        if min(ess.values()) < plan["min_ess"]:
            holds.append("INSUFFICIENT_EFFECTIVE_SAMPLES")
        bound = 1 + max_difference_weight
        sequence = bounded_confidence_sequence(differences, lower=-bound, upper=bound, delta=plan["delta"]) if differences else []
        interval = dict(sequence[-1]) if sequence else None
        if interval:
            interval["lower"] = max(-1.0, interval["lower"])
            interval["upper"] = min(1.0, interval["upper"])
            if interval["lower"] > interval["upper"]:
                holds.append("CONFIDENCE_SET_EMPTY_CHECK_ASSUMPTIONS")
        improved = interval is not None and interval["lower"] > plan["min_improvement"]
        if not improved:
            holds.append("IMPROVEMENT_NOT_ESTABLISHED")
        return {"schema": "keel.learning.policy-evaluation.v1", "experiment_pin": expected_pin,
            "bindings": plan["bindings"], "snapshot_sha256": snapshot_sha256, "evaluated_at": evaluated_at, "status": "HOLD" if holds else "PROPOSAL_ONLY",
            "hold_reasons": sorted(set(holds)), "registered_decisions": len(rows), "complete_prefix": n,
            "candidate_value": math.fsum(r["candidate"] for r in estimates) / n if n else None,
            "baseline_value": math.fsum(r["baseline"] for r in estimates) / n if n else None,
            "improvement_confidence_sequence": interval, "effective_sample_size": ess,
            "maximum_importance_weight": max_weight, "overlap": overlap,
            "recorded_human_minutes": math.fsum(minutes), "method": "frozen_predictor_paired_doubly_robust_hoeffding_time_union",
            "estimand": "running_average_conditional_policy_reward_difference",
            "reward_per_minute_causal_claim": False, "synthetic": plan["synthetic"],
            "human_review_required": True, "execution_authorized": False, "schedule_writes": 0,
            "assumptions_independently_proven": False}


def source_budget_proposal(document, evaluation, *, budget_minutes, now, plan=None, stratum=None, validator=None, proof=None):
    """Bind evaluated source actions to a capped, non-executing budget preview.

    Missing authenticated action semantics gives HOLD. Each action must be an
    exact source ID; the host attests that experiment units match budget units.
    Fractional minutes are rounded down, unused budget is not reassigned.
    """
    from engines.source_feedback import propose
    report = propose(document, budget_minutes=budget_minutes, now=now)
    require(type(evaluation) is dict and evaluation.get("schema") == "keel.learning.policy-evaluation.v1", "evaluation_required")
    report["statistical_evaluation"] = json.loads(canonical(evaluation))
    report["status"] = "HOLD"
    report["hold_reason"] = "HOST_MUST_BIND_EVALUATED_ACTIONS_TO_SOURCE_ALLOCATION"
    for source in report["sources"]:
        source["proposed_minutes"] = 0
    report["allocated_minutes"] = 0
    report["unallocated_minutes"] = budget_minutes
    if plan is None or not callable(validator):
        return report
    plan = validate_plan(plan)
    require(digest(plan) == evaluation.get("experiment_pin"), "evaluation_plan_mismatch")
    require(stratum in plan["strata"], "unknown_stratum")
    source_by_id = {r["source_id"]: r for r in report["sources"]}
    require(set(source_by_id) == set(plan["actions"]), "source_action_mapping_mismatch")
    try:
        event = {"kind": "source_allocation", "plan_pin": digest(plan),
            "evaluation_sha256": digest(evaluation), "feedback_sha256": report["input_sha256"],
            "stratum": stratum, "budget_minutes": budget_minutes, "proposal_at": now.isoformat(),
            "bindings": plan["bindings"], "required_assertions": ["fresh_nonreplayed_proof", "evaluation_from_authentic_registry", "current_bindings_match", "action_ids_are_exact_sources", "experimental_unit_matches_allocated_minute", "objective_horizon_matches", "population_matches_stratum"]}
        accepted = validator(json.loads(canonical(event)), proof)
    except Exception:
        accepted = False
    require(accepted is True, "host_attestation_rejected")
    if evaluation.get("status") != "PROPOSAL_ONLY" or evaluation.get("hold_reasons") or plan["synthetic"]:
        report["hold_reason"] = "STATISTICAL_EVIDENCE_HELD"
        return report
    probabilities = plan["strata"][stratum]["candidate"]
    if any(probabilities[sid] > 0 and row["hold_reasons"] for sid, row in source_by_id.items()):
        report["hold_reason"] = "SOURCE_EVIDENCE_OR_PERMISSION_HELD"
        return report
    for sid, row in source_by_id.items():
        row["proposed_minutes"] = min(row["cap_minutes"], int(Fraction(str(probabilities[sid])) * budget_minutes))
    allocated = sum(r["proposed_minutes"] for r in source_by_id.values())
    report.update(status="PROPOSAL_ONLY" if allocated else "HOLD", allocated_minutes=allocated,
        unallocated_minutes=budget_minutes-allocated, hold_reason=None if allocated else "NO_ALLOCATABLE_MINUTES",
        method="frozen_candidate_probabilities_rounded_down_with_source_caps", off_policy_estimate=True,
        allocation_rounding_or_caps_may_change_policy=True, causal_improvement_established=False)
    return report
