"""Bounded learning from inert evidence; this module never performs an action.

Learning artifacts begin as candidates. Only an explicit, reproducible
evaluation receipt can promote one. Retrieval and replay return data plans;
they do not grant authority or call tools.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import time

from keel_machine.common import PrivateDB, canonical, clone, ident, require, sha

SCHEMA = "keel.evolution.v1"
MAX_ARTIFACTS = 4096
MAX_EVIDENCE = 64
MAX_STEPS = 64
MAX_INVESTIGATIONS = 512


def _text(value, code, maximum=4096):
    require(type(value) is str and 0 < len(value) <= maximum, code)
    return value


def _now(clock):
    value = clock()
    require(type(value) in (int, float) and math.isfinite(value) and 0 <= value < 2**53,
            "evolution_clock_invalid")
    return float(value)


def _json(value, limit=65536):
    return canonical(value, limit=limit)


def _decode(value):
    require(type(value) is bytes, "evolution_storage_corrupt")
    try:
        return json.loads(value)
    except (ValueError, TypeError, UnicodeError):
        raise ValueError("evolution_storage_corrupt") from None


class EvolutionEngine:
    """Private local evidence, procedure, scenario and promotion registry."""

    @classmethod
    def create(cls, home, *, clock=time.time):
        home = Path(os.path.abspath(home))
        require(home.parent.resolve(strict=True) == home.parent, "evolution_parent_symlink")
        home.mkdir(mode=0o700, exist_ok=False)
        return cls(home, clock=clock)

    def __init__(self, home, *, clock=time.time):
        self.home = Path(os.path.abspath(home))
        self.clock = clock
        self.db = PrivateDB(self.home / "evolution.sqlite3")
        with self.db.transaction() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS evolution_meta(
                singleton INTEGER PRIMARY KEY CHECK(singleton=1), schema TEXT NOT NULL,
                created_at REAL NOT NULL, last_now REAL NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS evidence(
                evidence_id TEXT PRIMARY KEY, task_family TEXT NOT NULL, strategy TEXT NOT NULL,
                outcome TEXT NOT NULL CHECK(outcome IN ('PASS','FAIL','UNKNOWN')),
                utility_milli INTEGER NOT NULL, context_json BLOB NOT NULL,
                evidence_sha256 TEXT NOT NULL, observed_at REAL NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS artifacts(
                artifact_id TEXT PRIMARY KEY, kind TEXT NOT NULL CHECK(kind IN ('LESSON','PROCEDURE')),
                task_family TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('CANDIDATE','SIMULATION','PROMOTED','RETIRED','HELD')),
                body_json BLOB NOT NULL, body_sha256 TEXT NOT NULL, evidence_json BLOB NOT NULL,
                created_at REAL NOT NULL, updated_at REAL NOT NULL, reason TEXT)""")
            db.execute("""CREATE TABLE IF NOT EXISTS scenarios(
                scenario_id TEXT PRIMARY KEY, fingerprint_sha256 TEXT UNIQUE NOT NULL,
                task_family TEXT NOT NULL, fixture_json BLOB NOT NULL,
                invariant_json BLOB NOT NULL, source_evidence_id TEXT,
                created_at REAL NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS evaluations(
                evaluation_id TEXT PRIMARY KEY, artifact_id TEXT NOT NULL,
                receipt_json BLOB NOT NULL, receipt_sha256 TEXT NOT NULL,
                decision TEXT NOT NULL CHECK(decision IN ('PROMOTE','REJECT')),
                created_at REAL NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS community_candidates(
                fingerprint_sha256 TEXT PRIMARY KEY, task_family TEXT NOT NULL,
                fixture_sha256 TEXT NOT NULL, invariant_sha256 TEXT NOT NULL,
                state TEXT NOT NULL CHECK(state IN ('QUARANTINED','VALIDATED')),
                imported_at REAL NOT NULL)""")
            row = db.execute("SELECT * FROM evolution_meta WHERE singleton=1").fetchone()
            now = _now(clock)
            if row is None:
                db.execute("INSERT INTO evolution_meta VALUES(1,?,?,?)", (SCHEMA, now, now))
            else:
                require(row["schema"] == SCHEMA and now >= row["last_now"],
                        "evolution_metadata_invalid")
                db.execute("UPDATE evolution_meta SET last_now=? WHERE singleton=1", (now,))

    def _transaction(self):
        return self.db.transaction()

    def _clock(self, db):
        now = _now(self.clock)
        row = db.execute("SELECT schema,last_now FROM evolution_meta WHERE singleton=1").fetchone()
        require(row is not None and row["schema"] == SCHEMA and now >= row["last_now"],
                "evolution_clock_regressed_or_corrupt")
        db.execute("UPDATE evolution_meta SET last_now=? WHERE singleton=1", (now,))
        return now

    @staticmethod
    def _capacity(db):
        count = db.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0]
        require(count < MAX_ARTIFACTS, "evolution_artifact_capacity_exhausted")

    def observe(self, evidence_id, task_family, strategy, outcome, *,
                evidence_sha256, context, utility_milli=0):
        ident(evidence_id); ident(task_family); ident(strategy); sha(evidence_sha256)
        require(outcome in ("PASS", "FAIL", "UNKNOWN"), "evolution_outcome_invalid")
        require(type(utility_milli) is int and -1_000_000 <= utility_milli <= 1_000_000,
                "evolution_utility_invalid")
        raw = _json(context)
        with self._transaction() as db:
            now = self._clock(db)
            existing = db.execute("SELECT * FROM evidence WHERE evidence_id=?", (evidence_id,)).fetchone()
            values = (task_family, strategy, outcome, utility_milli, raw, evidence_sha256)
            if existing is not None:
                require(tuple(existing[k] for k in ("task_family","strategy","outcome","utility_milli",
                        "context_json","evidence_sha256")) == values, "evolution_evidence_conflict")
                return self._evidence(existing, replay=True)
            db.execute("INSERT INTO evidence VALUES(?,?,?,?,?,?,?,?)",
                       (evidence_id, *values, now))
            row = db.execute("SELECT * FROM evidence WHERE evidence_id=?", (evidence_id,)).fetchone()
            return self._evidence(row, replay=False)

    @staticmethod
    def _evidence(row, replay=False):
        return {"evidence_id": row["evidence_id"], "task_family": row["task_family"],
                "strategy": row["strategy"], "outcome": row["outcome"],
                "utility_milli": row["utility_milli"], "context": _decode(row["context_json"]),
                "evidence_sha256": row["evidence_sha256"], "observed_at": row["observed_at"],
                "replay": replay}

    def propose_lesson(self, artifact_id, task_family, statement, evidence_ids,
                       *, applicability, invalidators):
        body = {"statement": _text(statement, "lesson_statement_invalid", 8192),
                "applicability": applicability, "invalidators": invalidators}
        return self._propose(artifact_id, "LESSON", task_family, body, evidence_ids)

    def propose_procedure(self, artifact_id, task_family, steps, evidence_ids,
                          *, required_state, mutable_inputs, validators):
        require(type(steps) is list and 1 <= len(steps) <= MAX_STEPS,
                "procedure_steps_invalid")
        require(type(mutable_inputs) is list and len(mutable_inputs) <= 64 and
                len(set(mutable_inputs)) == len(mutable_inputs), "procedure_inputs_invalid")
        for name in mutable_inputs:
            ident(name)
        require(type(validators) is list and 1 <= len(validators) <= 64, "procedure_validators_invalid")
        for value in validators:
            sha(value)
        body = {"steps": steps, "required_state": required_state,
                "mutable_inputs": mutable_inputs, "validators": validators}
        return self._propose(artifact_id, "PROCEDURE", task_family, body, evidence_ids)

    def _propose(self, artifact_id, kind, task_family, body, evidence_ids):
        ident(artifact_id); ident(task_family)
        require(type(evidence_ids) is list and 1 <= len(evidence_ids) <= MAX_EVIDENCE and
                len(set(evidence_ids)) == len(evidence_ids), "artifact_evidence_invalid")
        for value in evidence_ids:
            ident(value)
        raw, links = _json(body), _json(sorted(evidence_ids))
        body_sha = hashlib.sha256(raw).hexdigest()
        with self._transaction() as db:
            now = self._clock(db)
            rows = [row for value in evidence_ids
                    if (row := db.execute("SELECT evidence_id,outcome FROM evidence WHERE evidence_id=?",
                                          (value,)).fetchone()) is not None]
            require(len(rows) == len(evidence_ids), "artifact_evidence_missing")
            require(any(row["outcome"] == "PASS" for row in rows), "artifact_positive_evidence_missing")
            existing = db.execute("SELECT * FROM artifacts WHERE artifact_id=?", (artifact_id,)).fetchone()
            if existing is not None:
                require(existing["kind"] == kind and existing["task_family"] == task_family and
                        existing["body_json"] == raw and existing["evidence_json"] == links,
                        "artifact_identity_conflict")
                return self._artifact(existing)
            self._capacity(db)
            db.execute("INSERT INTO artifacts VALUES(?,?,?,?,?,?,?,?,?,NULL)",
                       (artifact_id, kind, task_family, "CANDIDATE", raw, body_sha, links, now, now))
            return self._artifact(db.execute("SELECT * FROM artifacts WHERE artifact_id=?",
                                             (artifact_id,)).fetchone())

    @staticmethod
    def _artifact(row):
        return {"artifact_id": row["artifact_id"], "kind": row["kind"],
                "task_family": row["task_family"], "state": row["state"],
                "body": _decode(row["body_json"]), "body_sha256": row["body_sha256"],
                "evidence_ids": _decode(row["evidence_json"]), "reason": row["reason"]}

    def evaluate(self, evaluation_id, artifact_id, receipt):
        """Screen declared inert cases; success is simulation-scoped only."""
        ident(evaluation_id); ident(artifact_id)
        required = {"schema","dataset_sha256","harness_sha256","champion_score_milli",
                    "candidate_score_milli","holdout_cases","regressions","false_passes",
                    "repeats","artifact_body_sha256"}
        require(type(receipt) is dict and set(receipt) == required and
                receipt["schema"] == "keel.evolution.evaluation.v1", "evaluation_receipt_invalid")
        for name in ("dataset_sha256","harness_sha256","artifact_body_sha256"):
            sha(receipt[name])
        for name in ("champion_score_milli","candidate_score_milli","holdout_cases",
                     "regressions","false_passes","repeats"):
            require(type(receipt[name]) is int and 0 <= receipt[name] < 2**31,
                    "evaluation_number_invalid")
        raw = _json(receipt)
        receipt_sha = hashlib.sha256(raw).hexdigest()
        with self._transaction() as db:
            now = self._clock(db)
            artifact = db.execute("SELECT * FROM artifacts WHERE artifact_id=?", (artifact_id,)).fetchone()
            require(artifact is not None, "evaluation_artifact_missing")
            require(artifact["state"] in ("CANDIDATE","SIMULATION"), "evaluation_artifact_not_eligible")
            require(receipt["artifact_body_sha256"] == artifact["body_sha256"],
                    "evaluation_artifact_revision_mismatch")
            promoted = (receipt["holdout_cases"] >= 10 and receipt["repeats"] >= 3 and
                        receipt["regressions"] == 0 and receipt["false_passes"] == 0 and
                        receipt["candidate_score_milli"] > receipt["champion_score_milli"])
            decision = "PROMOTE" if promoted else "REJECT"
            prior = db.execute("SELECT * FROM evaluations WHERE evaluation_id=?", (evaluation_id,)).fetchone()
            if prior is not None:
                require(prior["artifact_id"] == artifact_id and prior["receipt_json"] == raw and
                        prior["decision"] == decision, "evaluation_identity_conflict")
            else:
                db.execute("INSERT INTO evaluations VALUES(?,?,?,?,?,?)",
                           (evaluation_id, artifact_id, raw, receipt_sha, decision, now))
            if promoted and artifact["state"] == "CANDIDATE":
                db.execute("UPDATE artifacts SET state='SIMULATION',updated_at=?,reason=NULL WHERE artifact_id=?",
                           (now, artifact_id))
            return {"evaluation_id": evaluation_id, "artifact_id": artifact_id,
                    "decision": decision, "receipt_sha256": receipt_sha,
                    "state": "SIMULATION" if promoted else artifact["state"],
                    "production_qualified": False}

    def qualify(self, evaluation_id, artifact_id, plan, dataset, *, baseline, candidate,
                adjudication_validator):
        """Run Keel's pinned paired harness and promote only a QUALIFIED report."""
        ident(evaluation_id); ident(artifact_id)
        from keel_eval.reliability import run_paired
        report = run_paired(plan, dataset, baseline=baseline, candidate=candidate,
                            adjudication_validator=adjudication_validator)
        require(report.get("status") == "QUALIFIED" and report.get("mode") == "LOCAL_CALLBACKS"
                and report.get("synthetic") is False
                and report.get("adjudication_and_independence_attested") is True
                and report.get("execution_authorized") is False,
                "evolution_reliability_not_qualified")
        raw = _json(report, limit=1048576)
        receipt_sha = hashlib.sha256(raw).hexdigest()
        with self._transaction() as db:
            now = self._clock(db)
            artifact = db.execute("SELECT * FROM artifacts WHERE artifact_id=?", (artifact_id,)).fetchone()
            require(artifact is not None and artifact["state"] in ("CANDIDATE","SIMULATION","PROMOTED"),
                    "evaluation_artifact_not_eligible")
            prior = db.execute("SELECT * FROM evaluations WHERE evaluation_id=?", (evaluation_id,)).fetchone()
            if prior is not None:
                require(prior["artifact_id"] == artifact_id and prior["receipt_json"] == raw and
                        prior["decision"] == "PROMOTE", "evaluation_identity_conflict")
            else:
                db.execute("INSERT INTO evaluations VALUES(?,?,?,?,?,?)",
                           (evaluation_id, artifact_id, raw, receipt_sha, "PROMOTE", now))
            db.execute("UPDATE artifacts SET state='PROMOTED',updated_at=?,reason=NULL WHERE artifact_id=?",
                       (now, artifact_id))
        return {"evaluation_id": evaluation_id, "artifact_id": artifact_id,
                "decision": "PROMOTE", "state": "PROMOTED",
                "receipt_sha256": receipt_sha, "production_qualified": True,
                "quality_scope": report["quality_scope"]}

    def retire(self, artifact_id, reason):
        ident(artifact_id); _text(reason, "retirement_reason_invalid", 256)
        with self._transaction() as db:
            now = self._clock(db)
            row = db.execute("SELECT state FROM artifacts WHERE artifact_id=?", (artifact_id,)).fetchone()
            require(row is not None, "retirement_artifact_missing")
            db.execute("UPDATE artifacts SET state='RETIRED',reason=?,updated_at=? WHERE artifact_id=?",
                       (reason, now, artifact_id))
        return {"artifact_id": artifact_id, "state": "RETIRED", "reason": reason}

    def retrieve(self, task_family, context, *, limit=8):
        ident(task_family)
        require(type(limit) is int and 1 <= limit <= 32, "retrieval_limit_invalid")
        _json(context)
        with self._transaction() as db:
            self._clock(db)
            rows = db.execute("""SELECT * FROM artifacts
                WHERE task_family=? AND kind='LESSON' AND state='PROMOTED'""",
                (task_family,)).fetchall()
            rows = sorted(rows, key=lambda row: (-len(_decode(row["evidence_json"])),
                          -row["updated_at"], row["artifact_id"]))[:limit]
            return {"schema": SCHEMA, "task_family": task_family,
                    "context_sha256": hashlib.sha256(_json(context)).hexdigest(),
                    "lessons": [self._artifact(row) for row in rows],
                    "execution_authorized": False}

    def tournament(self, artifact_id, cases, *, harness_sha256, repeats=3):
        """Create a paired held-out receipt from already adjudicated inert cases.

        This scorer never invokes a candidate, model, prompt, tool or callback.
        Case production and train/holdout separation remain the caller's job.
        """
        ident(artifact_id); sha(harness_sha256)
        require(type(repeats) is int and 3 <= repeats <= 100, "tournament_repeats_invalid")
        require(type(cases) is list and 10 <= len(cases) <= 10000,
                "tournament_cases_invalid")
        normalized, seen = [], set()
        for case in cases:
            require(type(case) is dict and set(case) == {"case_id","champion","candidate"},
                    "tournament_case_fields_invalid")
            ident(case["case_id"])
            require(case["case_id"] not in seen, "tournament_case_duplicate")
            seen.add(case["case_id"])
            require(case["champion"] in ("PASS","FAIL","FALSE_PASS") and
                    case["candidate"] in ("PASS","FAIL","FALSE_PASS"),
                    "tournament_case_result_invalid")
            normalized.append(dict(case))
        normalized.sort(key=lambda row: row["case_id"])
        with self._transaction() as db:
            self._clock(db)
            artifact = db.execute("SELECT * FROM artifacts WHERE artifact_id=?", (artifact_id,)).fetchone()
            require(artifact is not None, "tournament_artifact_missing")
        champion_passes = sum(row["champion"] == "PASS" for row in normalized)
        candidate_passes = sum(row["candidate"] == "PASS" for row in normalized)
        receipt = {"schema": "keel.evolution.evaluation.v1",
            "dataset_sha256": hashlib.sha256(_json(normalized, limit=1048576)).hexdigest(),
            "harness_sha256": harness_sha256,
            "champion_score_milli": champion_passes * 1000 // len(normalized),
            "candidate_score_milli": candidate_passes * 1000 // len(normalized),
            "holdout_cases": len(normalized),
            "regressions": sum(row["champion"] == "PASS" and row["candidate"] != "PASS"
                               for row in normalized),
            "false_passes": sum(row["candidate"] == "FALSE_PASS" for row in normalized),
            "repeats": repeats, "artifact_body_sha256": artifact["body_sha256"]}
        return receipt

    def replay_plan(self, artifact_id, current_state, inputs):
        ident(artifact_id)
        require(type(inputs) is dict, "replay_inputs_invalid")
        _json(current_state); _json(inputs)
        with self._transaction() as db:
            self._clock(db)
            row = db.execute("SELECT * FROM artifacts WHERE artifact_id=?", (artifact_id,)).fetchone()
            require(row is not None and row["kind"] == "PROCEDURE", "replay_procedure_missing")
            artifact = self._artifact(row); body = artifact["body"]
            require(artifact["state"] in ("SIMULATION","PROMOTED"), "replay_procedure_not_promoted")
            require(current_state == body["required_state"], "replay_precondition_mismatch")
            require(set(inputs) == set(body["mutable_inputs"]), "replay_input_binding_mismatch")
            return {"schema": "keel.evolution.replay-plan.v1", "artifact_id": artifact_id,
                    "artifact_body_sha256": artifact["body_sha256"], "steps": clone(body["steps"]),
                    "inputs": clone(inputs), "validators": list(body["validators"]),
                    "simulation_only": artifact["state"] == "SIMULATION",
                    "requires_fresh_authority": True, "execution_authorized": False}

    def failure_to_scenario(self, task_family, failure, *, fixture, invariant,
                            source_evidence_id=None):
        ident(task_family)
        if source_evidence_id is not None:
            ident(source_evidence_id)
        require(type(invariant) is dict and set(invariant) == {"path","operator","expected"}
                and type(invariant["path"]) is list and 1 <= len(invariant["path"]) <= 16
                and invariant["operator"] in ("equals","not_equals","present"),
                "scenario_invariant_invalid")
        for component in invariant["path"]:
            ident(component)
        document = {"task_family": task_family, "failure": failure,
                    "fixture": fixture, "invariant": invariant}
        fingerprint = hashlib.sha256(_json(document)).hexdigest()
        scenario_id = "scenario-" + fingerprint[:24]
        fixture_raw, invariant_raw = _json(fixture), _json(invariant)
        with self._transaction() as db:
            now = self._clock(db)
            if source_evidence_id is not None:
                require(db.execute("SELECT 1 FROM evidence WHERE evidence_id=?",
                                   (source_evidence_id,)).fetchone() is not None,
                        "scenario_source_evidence_missing")
            existing = db.execute("SELECT * FROM scenarios WHERE fingerprint_sha256=?",
                                  (fingerprint,)).fetchone()
            if existing is None:
                db.execute("INSERT INTO scenarios VALUES(?,?,?,?,?,?,?)",
                           (scenario_id, fingerprint, task_family, fixture_raw, invariant_raw,
                            source_evidence_id, now))
            return {"scenario_id": scenario_id, "fingerprint_sha256": fingerprint,
                    "task_family": task_family, "fixture": clone(fixture),
                    "invariant": clone(invariant), "source_evidence_id": source_evidence_id,
                    "executable_code": False}

    def run_scenario(self, scenario_id, observed):
        """Evaluate one stored invariant using a fixed, non-executable DSL."""
        ident(scenario_id); _json(observed)
        with self._transaction() as db:
            self._clock(db)
            row = db.execute("SELECT * FROM scenarios WHERE scenario_id=?", (scenario_id,)).fetchone()
            require(row is not None, "scenario_missing")
            invariant = _decode(row["invariant_json"])
        cursor, present = observed, True
        for component in invariant["path"]:
            if type(cursor) is not dict or component not in cursor:
                present = False; cursor = None; break
            cursor = cursor[component]
        operator = invariant["operator"]
        passed = (present if operator == "present" else
                  present and cursor == invariant["expected"] if operator == "equals" else
                  not present or cursor != invariant["expected"])
        return {"schema": "keel.evolution.scenario-result.v1", "scenario_id": scenario_id,
                "fingerprint_sha256": row["fingerprint_sha256"], "passed": bool(passed),
                "operator": operator, "observed_value_sha256": hashlib.sha256(_json(cursor)).hexdigest(),
                "execution_authorized": False}

    def prioritize(self, investigations, *, budget_milli):
        """Rank bounded investigations by declared value of information per cost."""
        require(type(investigations) is list and 1 <= len(investigations) <= MAX_INVESTIGATIONS,
                "investigations_invalid")
        require(type(budget_milli) is int and 0 <= budget_milli <= 10**9,
                "investigation_budget_invalid")
        ranked = []
        for item in investigations:
            required = {"investigation_id","affected_tasks","information_gain_milli",
                        "success_probability_milli","cost_milli","deadline_weight_milli",
                        "authority_required"}
            require(type(item) is dict and set(item) == required, "investigation_fields_invalid")
            ident(item["investigation_id"])
            for key in required - {"investigation_id","authority_required"}:
                require(type(item[key]) is int and 0 <= item[key] <= 1_000_000,
                        "investigation_value_invalid")
            require(type(item["authority_required"]) is bool, "investigation_authority_invalid")
            benefit = (item["affected_tasks"] * item["information_gain_milli"] *
                       item["success_probability_milli"] * (1000 + item["deadline_weight_milli"]))
            denominator = max(1, item["cost_milli"] * 1_000_000)
            score = benefit // denominator
            ranked.append({**item, "priority_score": score,
                           "eligible": not item["authority_required"]})
        ranked.sort(key=lambda row: (-row["priority_score"], row["cost_milli"],
                                     row["investigation_id"]))
        selected, spent = [], 0
        for row in ranked:
            if row["eligible"] and spent + row["cost_milli"] <= budget_milli:
                selected.append(row["investigation_id"]); spent += row["cost_milli"]
        return {"schema": "keel.evolution.investigation-plan.v1", "ranked": ranked,
                "selected": selected, "budget_milli": budget_milli, "spent_milli": spent,
                "execution_authorized": False}

    def route_compute(self, tasks, *, advanced_budget_units):
        """Plan reasoning depth from declared difficulty; invokes no model."""
        require(type(tasks) is list and 1 <= len(tasks) <= 1024, "routing_tasks_invalid")
        require(type(advanced_budget_units) is int and 0 <= advanced_budget_units <= 1024,
                "routing_budget_invalid")
        rows = []
        for task in tasks:
            required = {"task_id","ambiguity_milli","risk_milli","novelty_milli",
                        "deterministic_procedure_available"}
            require(type(task) is dict and set(task) == required, "routing_task_fields_invalid")
            ident(task["task_id"])
            for key in ("ambiguity_milli","risk_milli","novelty_milli"):
                require(type(task[key]) is int and 0 <= task[key] <= 1000,
                        "routing_task_value_invalid")
            require(type(task["deterministic_procedure_available"]) is bool,
                    "routing_procedure_flag_invalid")
            difficulty = 4 * task["risk_milli"] + 3 * task["ambiguity_milli"] + 2 * task["novelty_milli"]
            if task["deterministic_procedure_available"] and difficulty < 3000:
                tier, units = "DETERMINISTIC", 0
            elif difficulty < 5500:
                tier, units = "STANDARD_REVIEW", 0
            else:
                tier, units = "ADVANCED_REVIEW", 1
            rows.append({**task, "difficulty_score": difficulty, "requested_tier": tier,
                         "advanced_units": units})
        rows.sort(key=lambda row: (-row["difficulty_score"], row["task_id"]))
        remaining = advanced_budget_units
        for row in rows:
            if row["requested_tier"] == "ADVANCED_REVIEW" and remaining:
                row["assigned_tier"] = "ADVANCED_REVIEW"; remaining -= 1
            elif row["requested_tier"] == "ADVANCED_REVIEW":
                row["assigned_tier"] = "HELD_BUDGET"
            else:
                row["assigned_tier"] = row["requested_tier"]
        return {"schema": "keel.evolution.compute-plan.v1", "tasks": rows,
                "advanced_budget_units": advanced_budget_units,
                "advanced_units_assigned": advanced_budget_units - remaining,
                "model_calls": 0, "execution_authorized": False}

    def bind_action_contract(self, replay_plan, *, target, evidence_bindings,
                             authority_revision_sha256):
        """Bind an inert replay plan to exact evidence and target revisions."""
        require(type(replay_plan) is dict
                and replay_plan.get("schema") == "keel.evolution.replay-plan.v1"
                and replay_plan.get("execution_authorized") is False,
                "action_contract_plan_invalid")
        sha(replay_plan.get("artifact_body_sha256")); sha(authority_revision_sha256)
        require(type(evidence_bindings) is dict and 1 <= len(evidence_bindings) <= 128,
                "action_contract_evidence_invalid")
        for name, revision in evidence_bindings.items():
            ident(name); sha(revision)
        body = {"schema": "keel.evolution.action-contract.v1",
                "artifact_id": replay_plan["artifact_id"],
                "artifact_body_sha256": replay_plan["artifact_body_sha256"],
                "target": clone(target), "target_sha256": hashlib.sha256(_json(target)).hexdigest(),
                "evidence_bindings": dict(sorted(evidence_bindings.items())),
                "authority_revision_sha256": authority_revision_sha256,
                "validators": list(replay_plan["validators"]),
                "execution_authorized": False}
        return {**body, "contract_sha256": hashlib.sha256(_json(body)).hexdigest()}

    def validate_action_contract(self, contract, *, current_target,
                                 current_evidence_bindings, current_authority_revision_sha256):
        require(type(contract) is dict and contract.get("schema") == "keel.evolution.action-contract.v1",
                "action_contract_invalid")
        digest = contract.get("contract_sha256")
        body = {key: value for key, value in contract.items() if key != "contract_sha256"}
        require(type(digest) is str and digest == hashlib.sha256(_json(body)).hexdigest(),
                "action_contract_digest_invalid")
        sha(current_authority_revision_sha256)
        _json(current_target); _json(current_evidence_bindings)
        current = (contract["target_sha256"] == hashlib.sha256(_json(current_target)).hexdigest()
                   and contract["evidence_bindings"] == current_evidence_bindings
                   and contract["authority_revision_sha256"] == current_authority_revision_sha256)
        return {"schema": "keel.evolution.action-contract-check.v1",
                "contract_sha256": digest, "bindings_current": current,
                "status": "CURRENT_REQUIRES_ACTION_GATE" if current else "STALE",
                "execution_authorized": False}

    def export_scenario_index(self):
        """Export privacy-minimized failure fingerprints, never fixtures or evidence."""
        with self._transaction() as db:
            self._clock(db)
            rows = db.execute("SELECT * FROM scenarios ORDER BY scenario_id").fetchall()
            entries = [{"scenario_id": row["scenario_id"], "task_family": row["task_family"],
                        "fingerprint_sha256": row["fingerprint_sha256"],
                        "fixture_sha256": hashlib.sha256(row["fixture_json"]).hexdigest(),
                        "invariant_sha256": hashlib.sha256(row["invariant_json"]).hexdigest()}
                       for row in rows]
        body = {"schema": "keel.evolution.scenario-index.v1", "entries": entries,
                "contains_fixture_content": False, "contains_evidence_ids": False,
                "execution_authorized": False}
        return {**body, "index_sha256": hashlib.sha256(_json(body)).hexdigest()}

    def import_scenario_index(self, document):
        """Quarantine a shared hash index; local matching evidence is still required."""
        required = {"schema","entries","contains_fixture_content","contains_evidence_ids",
                    "execution_authorized","index_sha256"}
        require(type(document) is dict and set(document) == required,
                "community_index_fields_invalid")
        digest = document["index_sha256"]
        body = {key: document[key] for key in required if key != "index_sha256"}
        require(document["schema"] == "keel.evolution.scenario-index.v1"
                and document["contains_fixture_content"] is False
                and document["contains_evidence_ids"] is False
                and document["execution_authorized"] is False
                and digest == hashlib.sha256(_json(body)).hexdigest(),
                "community_index_invalid")
        entries = document["entries"]
        require(type(entries) is list and len(entries) <= 4096, "community_entries_invalid")
        with self._transaction() as db:
            now = self._clock(db); imported = validated = 0; seen = set()
            for row in entries:
                fields = {"scenario_id","task_family","fingerprint_sha256",
                          "fixture_sha256","invariant_sha256"}
                require(type(row) is dict and set(row) == fields, "community_entry_invalid")
                ident(row["scenario_id"]); ident(row["task_family"])
                for name in ("fingerprint_sha256","fixture_sha256","invariant_sha256"):
                    sha(row[name])
                require(row["fingerprint_sha256"] not in seen, "community_entry_duplicate")
                seen.add(row["fingerprint_sha256"])
                # SQLite builds do not consistently ship sha3; compare bytes in Python.
                candidate = db.execute("SELECT * FROM scenarios WHERE fingerprint_sha256=? AND task_family=?",
                                       (row["fingerprint_sha256"], row["task_family"])).fetchone()
                matches = bool(candidate and hashlib.sha256(candidate["fixture_json"]).hexdigest() == row["fixture_sha256"]
                               and hashlib.sha256(candidate["invariant_json"]).hexdigest() == row["invariant_sha256"])
                state = "VALIDATED" if matches else "QUARANTINED"
                prior = db.execute("SELECT * FROM community_candidates WHERE fingerprint_sha256=?",
                                   (row["fingerprint_sha256"],)).fetchone()
                values = (row["task_family"], row["fixture_sha256"], row["invariant_sha256"])
                if prior is None:
                    db.execute("INSERT INTO community_candidates VALUES(?,?,?,?,?,?)",
                        (row["fingerprint_sha256"], *values, state, now)); imported += 1
                else:
                    require(tuple(prior[k] for k in ("task_family","fixture_sha256","invariant_sha256")) == values,
                            "community_identity_conflict")
                    if matches and prior["state"] != "VALIDATED":
                        db.execute("UPDATE community_candidates SET state='VALIDATED' WHERE fingerprint_sha256=?",
                                   (row["fingerprint_sha256"],))
                validated += matches
        return {"schema": "keel.evolution.community-import.v1", "imported": imported,
                "validated_by_exact_local_scenario": validated,
                "quarantined": len(entries) - validated, "execution_authorized": False}

    def status(self):
        with self._transaction() as db:
            self._clock(db)
            states = {row[0] + ":" + row[1]: row[2] for row in
                      db.execute("SELECT kind,state,COUNT(*) FROM artifacts GROUP BY kind,state")}
            return {"schema": SCHEMA, "status": "OBSERVED",
                    "evidence": db.execute("SELECT COUNT(*) FROM evidence").fetchone()[0],
                    "artifacts": states,
                    "scenarios": db.execute("SELECT COUNT(*) FROM scenarios").fetchone()[0],
                    "community_candidates": db.execute("SELECT COUNT(*) FROM community_candidates").fetchone()[0],
                    "evaluations": db.execute("SELECT COUNT(*) FROM evaluations").fetchone()[0],
                    "execution_authorized": False}
