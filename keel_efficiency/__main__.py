"""Local resource governance, bounded planning and offline efficiency evaluation."""
import argparse
from dataclasses import asdict
import json
import sqlite3
import sys

from keel_agent.io import read_json, write_private


def _context_sources(records):
    from .context import Binding, ContextSource
    result = []
    for raw in records:
        row = dict(raw)
        row["dependencies"] = tuple(Binding(**b) for b in row.get("dependencies", []))
        result.append(ContextSource(**row))
    return result


def _context_result(value):
    result = asdict(value)
    result.pop("encoded")  # Exact packet is present; avoid duplicate context.
    return result


def dispatch(args):
    command = args.command
    data = read_json(args.input) if getattr(args, "input", None) else {}
    if type(data) is not dict:
        raise ValueError("input must be a JSON object")
    if command.startswith("budget-"):
        from .ledger import ResourceLedger
        ledger = ResourceLedger(args.ledger)
        if command == "budget-create":
            from .defaults import DEFAULT_LIMITS
            limits = data.get("limits", DEFAULT_LIMITS)
            if limits.get("external_credit_micros", 0) != 0:
                raise ValueError("this local CLI does not enable paid execution")
            return ledger.create_scope(args.scope, limits, parent_id=args.parent)
        if command == "budget-show":
            return ledger.snapshot(args.scope)
        return ledger.reconcile(args.request, data)
    if command == "context":
        from .context import ContextAssembler
        data["sources"] = _context_sources(data["sources"])
        return _context_result(ContextAssembler().assemble(**data))
    if command == "handoff":
        from .context import Binding, compact_handoff
        data["bindings"] = [Binding(**row) for row in data["bindings"]]
        return _context_result(compact_handoff(**data))
    if command in {"route", "bandit"}:
        from .policy import RouteEvidence, QualityPolicy, ContextualBandit, select_route
        routes = [RouteEvidence(**row) for row in data.pop("routes")]
        policy = QualityPolicy(**data.pop("policy", {}))
        if command == "route":
            return select_route(routes, policy=policy, **data)
        features = data.pop("features")
        learner = ContextualBandit.replay(data.pop("observations", []),
            feature_count=data.pop("feature_count"), policy=policy,
            **data.pop("learner_options", {}))
        return learner.propose(routes, features, **data)
    if command == "prepare":
        from .execution import run_qualified_preparation
        from .defaults import configured_budget
        from .policy import RouteEvidence, QualityPolicy
        from keel_agent.models import ReviewerConfig
        ledger, scope_id = configured_budget(args.ledger, args.scope)
        data["routes"] = [RouteEvidence(**row) for row in data["routes"]]
        data["configs"] = {key: ReviewerConfig(**row) for key, row in data["configs"].items()}
        data["policy"] = QualityPolicy(**data.get("policy", {}))
        return run_qualified_preparation(**data, ledger=ledger, scope_id=scope_id)
    if command == "review-plan":
        from .policy import plan_review
        return plan_review(**data)
    if command == "allocate":
        from .policy import allocate_batch
        return allocate_batch(**data)
    if command == "incremental":
        from .reuse import plan_incremental
        return plan_incremental(**data)
    if command.startswith("procedure-"):
        from .procedures import ProcedureRegistry, TraceStep, EvaluationCase, QualificationPolicy
        registry = ProcedureRegistry(args.registry, scope=args.scope)
        try:
            if command == "procedure-compile":
                data["steps"] = [TraceStep(**row) for row in data["steps"]]
                data["policy"] = QualificationPolicy(**data.get("policy", {}))
                return registry.compile_trace(**data)
            if command == "procedure-qualify":
                data["evaluations"] = [EvaluationCase(**row) for row in data["evaluations"]]
                return registry.qualify(**data)
            if command == "procedure-check":
                return registry.get_qualified(**data)
            return registry.revoke(**data)
        finally:
            registry.close()
    if command == "benchmark":
        from .benchmark import run_benchmark
        return run_benchmark()
    if command == "demo":
        from .demo import run_demo
        return run_demo()
    raise ValueError("unsupported command")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("budget-create", "budget-show", "budget-reconcile", "context", "handoff",
                 "route", "bandit", "prepare", "review-plan", "allocate", "incremental", "procedure-compile",
                 "procedure-qualify", "procedure-check", "procedure-revoke", "benchmark", "demo"):
        item = sub.add_parser(name)
        item.add_argument("--out", help="new private output JSON; existing files are preserved")
        if name not in {"budget-show", "benchmark", "demo"}:
            item.add_argument("--input", required=name != "budget-create")
        if name.startswith("budget-"):
            item.add_argument("--ledger", required=True)
            if name != "budget-reconcile":
                item.add_argument("--scope", required=name == "budget-create")
            else:
                item.add_argument("--request", required=True)
        if name == "budget-create":
            item.add_argument("--parent")
        if name == "prepare":
            item.add_argument("--ledger", required=True)
            item.add_argument("--scope", required=True)
        if name.startswith("procedure-"):
            item.add_argument("--registry", required=True)
            item.add_argument("--scope", default="local")
    args = parser.parse_args(argv)
    try:
        result = dispatch(args)
        if args.out:
            write_private(args.out, result)
        else:
            print(json.dumps(result, indent=2, sort_keys=True, allow_nan=False))
        return 3 if result.get("status") in {"HOLD", "HELD", "BLOCKED"} else 0
    except (ValueError, TypeError, KeyError, OSError, sqlite3.Error, OverflowError, RecursionError):
        print("keel-efficiency: invalid input, budget, state, or output destination", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
