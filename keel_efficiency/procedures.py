"""Durable data-only procedures promoted by one held-out qualification trial.

This registry never runs a step, imports generated code, or reuses an approval.
Callers supply verified traces, host-scored outcomes, and an IID declaration;
statistical guarantees depend on those declarations being true. Qualification
applies only to the pinned source/model/policy revisions and scope.

The database directory must be operator-owned and trusted. Files are created
private and final symlinks/nonregular paths rejected; this is not protection
against hostile concurrent changes to parent directories. Holdout identity
checks are exact task-ID checks, not semantic duplicate detection. The registry
does not automatically evict historical training/evaluation IDs. Observed clock
regressions permanently hold the scope, including after reopening the database.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import stat
import time
from typing import Callable, Mapping, Sequence


class ProcedureError(ValueError):
    pass


def _canonical(value: object) -> str:
    active: set[int] = set()
    nodes = [0]

    def visit(item: object, depth: int):
        nodes[0] += 1
        if depth > 32 or nodes[0] > 100000:
            raise ProcedureError("procedure JSON complexity exceeded")
        kind = type(item)
        if kind in (dict, list):
            if id(item) in active:
                raise ProcedureError("cyclic procedure data")
            active.add(id(item))
            if kind is dict:
                if any(type(key) is not str for key in item):
                    raise ProcedureError("procedure JSON keys must be strings")
                for key, child in item.items():
                    if len(key) > 262144:
                        raise ProcedureError("procedure JSON key too large")
                    visit(child, depth + 1)
            else:
                for child in item:
                    visit(child, depth + 1)
            active.remove(id(item))
        elif kind is str:
            if len(item) > 1024 * 1024:
                raise ProcedureError("procedure JSON string too large")
        elif kind is int:
            if abs(item) > 2**53 - 1:
                raise ProcedureError("procedure JSON integer out of range")
        elif kind is float:
            if not math.isfinite(item):
                raise ProcedureError("procedure JSON number must be finite")
        elif item is not None and kind is not bool:
            raise ProcedureError("procedure data must be strict JSON")

    visit(value, 0)
    try:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
        if len(encoded.encode("utf-8")) > 1024 * 1024:
            raise ProcedureError("procedure data exceeds 1 MiB")
        return encoded
    except (TypeError, ValueError, RecursionError) as exc:
        raise ProcedureError("procedure data must be finite bounded JSON") from exc


def _text(value: object, label: str, limit: int = 256) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.encode("utf-8")) > limit:
        raise ProcedureError(f"{label} must be a nonempty bounded string")
    return value


def _number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ProcedureError(f"{label} must be finite")
    return float(value)


def _bindings(values: Mapping[str, str]) -> dict[str, str]:
    if not isinstance(values, Mapping) or not values or len(values) > 256:
        raise ProcedureError("source_bindings must contain 1..256 revisions")
    return {_text(key, "binding ID"): _text(value, "revision") for key, value in values.items()}


@dataclass(frozen=True)
class TraceStep:
    operation: str
    arguments: Mapping[str, object]
    evidence_refs: tuple[str, ...]

    def record(self) -> dict:
        _text(self.operation, "operation", 256)
        if not isinstance(self.arguments, Mapping):
            raise ProcedureError("arguments must be a JSON object")
        if not isinstance(self.evidence_refs, (tuple, list)) or not 1 <= len(self.evidence_refs) <= 128:
            raise ProcedureError("each step needs 1..128 evidence references")
        for ref in self.evidence_refs:
            _text(ref, "evidence reference", 2048)
        # JSON copying detaches caller-owned mutable arguments. They remain inert.
        return json.loads(_canonical({"operation": self.operation, "arguments": dict(self.arguments),
                                      "evidence_refs": list(self.evidence_refs)}))


@dataclass(frozen=True)
class EvaluationCase:
    task_id: str
    success: bool
    unsafe: bool = False

    def record(self) -> dict:
        _text(self.task_id, "evaluation task_id")
        if type(self.success) is not bool or type(self.unsafe) is not bool:
            raise ProcedureError("evaluation outcomes must be booleans")
        return asdict(self)


@dataclass(frozen=True)
class QualificationPolicy:
    min_evaluations: int = 60
    max_error_upper: float = 0.05
    confidence: float = 0.95

    def __post_init__(self):
        if type(self.min_evaluations) is not int or not 1 <= self.min_evaluations <= 10000:
            raise ProcedureError("min_evaluations must be in 1..10000")
        if not 0 < _number(self.max_error_upper, "max_error_upper") < 1:
            raise ProcedureError("max_error_upper must be between 0 and 1")
        if not 0.5 < _number(self.confidence, "confidence") < 1:
            raise ProcedureError("confidence must be between 0.5 and 1")


def binomial_error_upper(errors: int, trials: int, confidence: float = 0.95) -> float:
    """Exact one-sided Clopper-Pearson upper bound for fixed IID trials.

    Not valid for adaptively stopped trials or correlated rows treated as IID.
    Computed in log space; zero-error and all-error cases have direct forms.
    """
    if type(errors) is not int or type(trials) is not int or not 0 <= errors <= trials <= 10000 or trials == 0:
        raise ProcedureError("expected 0 <= errors <= trials <= 10000 with trials > 0")
    confidence = _number(confidence, "confidence")
    if not 0.5 < confidence < 1:
        raise ProcedureError("confidence must be between 0.5 and 1")
    if errors == trials:
        return 1.0
    if errors == 0:
        return -math.expm1(math.log1p(-confidence) / trials)
    target = math.log1p(-confidence)
    coefficients = [math.lgamma(trials + 1) - math.lgamma(i + 1) - math.lgamma(trials - i + 1)
                    for i in range(errors + 1)]
    low, high = errors / trials, 1.0
    for _ in range(64):
        probability = (low + high) / 2
        if probability >= 1.0:
            break
        logp, logq = math.log(probability), math.log1p(-probability)
        terms = [coefficient + i * logp + (trials - i) * logq for i, coefficient in enumerate(coefficients)]
        peak = max(terms)
        logcdf = peak + math.log(math.fsum(math.exp(term - peak) for term in terms))
        if logcdf > target:
            low = probability
        else:
            high = probability
    return high


class ProcedureRegistry:
    def __init__(self, path: str | Path = ":memory:", *, scope: str = "local",
                 clock: Callable[[], float] = time.time):
        self.scope = _text(scope, "scope")
        self.clock = clock
        try:
            raw = os.fspath(path)
        except TypeError as exc:
            raise ProcedureError("registry requires a path or :memory:") from exc
        if not isinstance(raw, str) or not raw or "\x00" in raw or raw.startswith("file:"):
            raise ProcedureError("registry requires a path or :memory:")
        if raw != ":memory:":
            destination = Path(raw).absolute()
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            flags = os.O_WRONLY | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
            try:
                descriptor = os.open(destination, flags, 0o600)
            except OSError as exc:
                raise ProcedureError("registry path must be a private regular file") from exc
            try:
                if destination.is_symlink() or not stat.S_ISREG(os.fstat(descriptor).st_mode):
                    raise ProcedureError("registry path must be a private regular file")
                os.fchmod(descriptor, 0o600)
            finally:
                os.close(descriptor)
            raw = str(destination)
        self.db = sqlite3.connect(raw, timeout=5, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA foreign_keys=ON;
            CREATE TABLE IF NOT EXISTS efficiency_procedures (
                scope TEXT NOT NULL, id TEXT NOT NULL, name TEXT NOT NULL, version TEXT NOT NULL,
                body TEXT NOT NULL, state TEXT NOT NULL, qualification TEXT, revoked_reason TEXT,
                PRIMARY KEY(scope,id), UNIQUE(scope,name,version));
            CREATE TABLE IF NOT EXISTS efficiency_training_tasks (
                scope TEXT NOT NULL, procedure_id TEXT NOT NULL, task_id TEXT NOT NULL,
                PRIMARY KEY(scope,procedure_id,task_id));
            CREATE TABLE IF NOT EXISTS efficiency_evaluation_tasks (
                scope TEXT NOT NULL, task_id TEXT NOT NULL, procedure_id TEXT NOT NULL,
                PRIMARY KEY(scope,task_id));
            CREATE TABLE IF NOT EXISTS efficiency_procedure_clock (
                scope TEXT PRIMARY KEY, high_water REAL NOT NULL, clock_hold INTEGER NOT NULL);
        """)

    def close(self):
        self.db.close()

    def _checked_now(self) -> tuple[float, bool]:
        """Persist a sticky clock hold; nesting reuses the caller transaction."""
        now = _number(self.clock(), "clock")

        def check():
            row = self.db.execute("SELECT high_water,clock_hold FROM efficiency_procedure_clock WHERE scope=?",
                                  (self.scope,)).fetchone()
            high_water = now if row is None else max(now, row["high_water"])
            held = False if row is None else bool(row["clock_hold"] or now < row["high_water"])
            self.db.execute("INSERT INTO efficiency_procedure_clock VALUES (?,?,?) "
                            "ON CONFLICT(scope) DO UPDATE SET high_water=excluded.high_water,clock_hold=excluded.clock_hold",
                            (self.scope, high_water, int(held)))
            return now, held

        if self.db.in_transaction:
            return check()
        with self._transaction():
            return check()

    @contextmanager
    def _transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def _row(self, candidate_id: str):
        _text(candidate_id, "candidate_id")
        row = self.db.execute("SELECT * FROM efficiency_procedures WHERE scope=? AND id=?",
                              (self.scope, candidate_id)).fetchone()
        if row is None:
            raise ProcedureError("unknown procedure in this scope")
        body = json.loads(row["body"])
        if hashlib.sha256(_canonical(body).encode()).hexdigest() != candidate_id:
            raise ProcedureError("procedure integrity failure")
        return row, body

    def compile_trace(self, *, name: str, version: str, training_task_ids: Sequence[str],
                      steps: Sequence[TraceStep], source_bindings: Mapping[str, str], expires_at: float,
                      success: bool, verified: bool, policy: QualificationPolicy | None = None) -> dict:
        if success is not True or verified is not True:
            raise ProcedureError("only explicitly successful host-verified traces can be compiled")
        if not isinstance(training_task_ids, (list, tuple)) or not 1 <= len(training_task_ids) <= 10000:
            raise ProcedureError("training_task_ids must contain 1..10000 IDs")
        ids = [_text(task, "training task_id") for task in training_task_ids]
        if len(set(ids)) != len(ids):
            raise ProcedureError("duplicate training task_id")
        if not isinstance(steps, (tuple, list)) or not 1 <= len(steps) <= 128 or any(not isinstance(s, TraceStep) for s in steps):
            raise ProcedureError("steps must contain 1..128 TraceStep objects")
        now, clock_held = self._checked_now()
        if clock_held:
            raise ProcedureError("CLOCK_REGRESSION: registry scope is held")
        expiry = _number(expires_at, "expires_at")
        if expiry <= now:
            raise ProcedureError("expires_at must be in the future")
        policy = QualificationPolicy() if policy is None else policy
        if not isinstance(policy, QualificationPolicy):
            raise ProcedureError("policy must be a QualificationPolicy")
        body = {"schema": "keel.efficiency.procedure.v1", "scope": self.scope,
                "name": _text(name, "name"), "version": _text(version, "version"),
                "training_task_ids": sorted(ids), "steps": [s.record() for s in steps],
                "source_bindings": _bindings(source_bindings), "expires_at": expiry,
                "qualification_policy": asdict(policy), "execution_authorized": False,
                "approval_reusable": False, "kind": "data_only"}
        encoded = _canonical(body)
        candidate_id = hashlib.sha256(encoded.encode()).hexdigest()
        with self._transaction():
            for task in ids:
                if self.db.execute("SELECT 1 FROM efficiency_evaluation_tasks WHERE scope=? AND task_id=?", (self.scope, task)).fetchone():
                    raise ProcedureError("held-out evaluation task cannot become training data in this registry scope")
            existing = self.db.execute("SELECT id FROM efficiency_procedures WHERE scope=? AND name=? AND version=?",
                                       (self.scope, name, version)).fetchone()
            if existing and existing["id"] != candidate_id:
                raise ProcedureError("procedure name/version is immutable")
            self.db.execute("INSERT OR IGNORE INTO efficiency_procedures VALUES (?,?,?,?,?,'CANDIDATE',NULL,NULL)",
                            (self.scope, candidate_id, name, version, encoded))
            self.db.executemany("INSERT OR IGNORE INTO efficiency_training_tasks VALUES (?,?,?)",
                                [(self.scope, candidate_id, task) for task in ids])
            current_state = self.db.execute("SELECT state FROM efficiency_procedures WHERE scope=? AND id=?",
                                            (self.scope, candidate_id)).fetchone()["state"]
        return {"status": current_state, "candidate_id": candidate_id, "execution_authorized": False}

    def qualify(self, candidate_id: str, *, evaluations: Sequence[EvaluationCase],
                current_bindings: Mapping[str, str], independent_tasks: bool = False) -> dict:
        if independent_tasks is not True:
            return self._hold("IID_TASK_DECLARATION_REQUIRED")
        if not isinstance(evaluations, (list, tuple)) or not 1 <= len(evaluations) <= 10000:
            raise ProcedureError("evaluations must contain 1..10000 cases")
        if any(not isinstance(case, EvaluationCase) for case in evaluations):
            raise ProcedureError("evaluations must contain EvaluationCase objects")
        records = [case.record() for case in evaluations]
        ids = [case["task_id"] for case in records]
        if len(set(ids)) != len(ids):
            return self._hold("DUPLICATE_EVALUATION_TASK")
        current = _bindings(current_bindings)
        with self._transaction():
            now, clock_held = self._checked_now()
            if clock_held:
                return self._hold("CLOCK_REGRESSION")
            row, body = self._row(candidate_id)
            reason = self._eligibility(row, body, current, now)
            if reason:
                return self._hold(reason)
            if row["qualification"] is not None:
                return self._hold("QUALIFICATION_TRIAL_ALREADY_CONSUMED")
            policy = QualificationPolicy(**body["qualification_policy"])
            if len(records) < policy.min_evaluations:
                return self._hold("INSUFFICIENT_EVALUATIONS")
            for task in ids:
                if self.db.execute("SELECT 1 FROM efficiency_training_tasks WHERE scope=? AND task_id=?", (self.scope, task)).fetchone():
                    return self._hold("TRAIN_EVALUATION_OVERLAP")
                if self.db.execute("SELECT 1 FROM efficiency_evaluation_tasks WHERE scope=? AND task_id=?", (self.scope, task)).fetchone():
                    return self._hold("EVALUATION_TASK_ALREADY_CONSUMED")
            errors = sum(not record["success"] or record["unsafe"] for record in records)
            unsafe = sum(record["unsafe"] for record in records)
            upper = binomial_error_upper(errors, len(records), policy.confidence)
            passed = upper <= policy.max_error_upper and unsafe == 0
            evaluated_at, clock_held = self._checked_now()
            if clock_held:
                return self._hold("CLOCK_REGRESSION")
            if evaluated_at >= body["expires_at"]:
                return self._hold("EXPIRED")
            result = {"status": "QUALIFIED" if passed else "HOLD",
                      "reason": "HELD_OUT_BOUND_PASSED" if passed else "QUALITY_BOUND_FAILED",
                      "candidate_id": candidate_id, "evaluations": len(records), "errors": errors,
                      "unsafe_results": unsafe, "error_upper": upper, "success_lower": 1 - upper,
                      "confidence": policy.confidence, "bound_method": "one_sided_clopper_pearson_fixed_iid",
                      "evaluation_task_ids": sorted(ids), "evaluated_at": evaluated_at,
                      "evaluation_cases": sorted(records, key=lambda record: record["task_id"]),
                      "independent_tasks_declared": True, "sample_design": "single_fixed_trial",
                      "execution_authorized": False, "approval_reusable": False}
            self.db.execute("UPDATE efficiency_procedures SET state=?,qualification=? WHERE scope=? AND id=?",
                            ("QUALIFIED" if passed else "HELD", _canonical(result), self.scope, candidate_id))
            self.db.executemany("INSERT INTO efficiency_evaluation_tasks VALUES (?,?,?)",
                                [(self.scope, task, candidate_id) for task in ids])
            return result

    @staticmethod
    def _hold(reason: str) -> dict:
        return {"status": "HOLD", "reason": reason, "execution_authorized": False, "approval_reusable": False}

    def _eligibility(self, row, body: dict, current: Mapping[str, str], now: float) -> str | None:
        if row["state"] == "REVOKED":
            return "REVOKED"
        if now >= body["expires_at"]:
            return "EXPIRED"
        if current != body["source_bindings"]:
            return "STALE_BINDINGS"
        return None

    def get_qualified(self, candidate_id: str, *, current_bindings: Mapping[str, str], expected_version: str) -> dict:
        now, clock_held = self._checked_now()
        if clock_held:
            return self._hold("CLOCK_REGRESSION")
        row, body = self._row(candidate_id)
        reason = self._eligibility(row, body, _bindings(current_bindings), now)
        if reason:
            return self._hold(reason)
        if body["version"] != _text(expected_version, "expected_version"):
            return self._hold("VERSION_MISMATCH")
        if row["state"] != "QUALIFIED" or row["qualification"] is None:
            return self._hold("NOT_QUALIFIED")
        return {"status": "QUALIFIED", "candidate_id": candidate_id, "procedure": body,
                "qualification": json.loads(row["qualification"]), "execution_authorized": False,
                "approval_reusable": False, "fresh_authorization_required": True}

    def revoke(self, candidate_id: str, *, reason: str) -> dict:
        _text(reason, "revocation reason", 2048)
        with self._transaction():
            self._row(candidate_id)
            self.db.execute("UPDATE efficiency_procedures SET state='REVOKED',revoked_reason=? WHERE scope=? AND id=?",
                            (reason, self.scope, candidate_id))
        return {"status": "REVOKED", "candidate_id": candidate_id, "execution_authorized": False}
