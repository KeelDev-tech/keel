"""Executable composition of the efficiency components using synthetic data."""
from dataclasses import asdict
import hashlib
from pathlib import Path
import tempfile


def run_demo():
    from .benchmark import run_benchmark
    from .context import Binding, ContextAssembler, ContextSource
    from .ledger import ResourceLedger, BudgetExceeded
    from .policy import RouteEvidence, ContextualBandit, select_route, plan_review, allocate_batch
    from .procedures import ProcedureRegistry, TraceStep, EvaluationCase
    from .reuse import SingleFlightStore, build_reuse_key, plan_incremental

    def digest(text):
        return hashlib.sha256(text.encode()).hexdigest()

    pin = digest("synthetic-v1")
    sources = [ContextSource("rules", "v1", "Preserve review and approval requirements.", kind="constraint"),
               ContextSource("skill", "v1", "Extract exact source references.", kind="skill",
                             keywords=("extract",), dependencies=(Binding("rules", "v1"),)),
               ContextSource("unrelated", "v1", "Unrelated material.")]
    context = ContextAssembler().assemble(task="extract source references", sources=sources,
        required_constraints=["No external actions."], max_bytes=4096,
        current_revisions={s.source_id: s.revision for s in sources})
    routes = [RouteEvidence("small", "extract", pin, pin, pin, True, 1, 1000, 1000, 1000, "low", True, True),
              RouteEvidence("large", "extract", pin, pin, pin, True, 4, 1000, 1000, 1000, "high", True, True)]
    selection = dict(context_key="extract", risk="low", budget_units=5,
                     current_bindings={r.route_id: r.bindings() for r in routes})
    route = select_route(routes, **selection)
    learner = ContextualBandit(2)
    learner.observe(routes[0], [1., .5], observation_id="synthetic-observation",
        verified_success=True, measured_cost_units=1, evidence_certified=True,
        current_bindings=selection["current_bindings"])
    learned = learner.propose(routes, [1., .5], **selection)
    with tempfile.TemporaryDirectory(prefix="keel-efficiency-demo-") as temp:
        root = Path(temp)
        ledger = ResourceLedger(root / "resources.sqlite3")
        caps = dict(calls=2, input_tokens=100, output_tokens=100, compute_ms=1000, external_credit_micros=0)
        ledger.create_scope("session", caps)
        ledger.create_scope("task", caps, parent_id="session")
        ledger.reserve("first", "task", dict(calls=1, input_tokens=50, output_tokens=50, compute_ms=100))
        ledger.mark_dispatched("first")
        ledger.settle("first", dict(calls=1, input_tokens=20, output_tokens=10, compute_ms=3, external_credit_micros=0))
        ledger.reserve("uncertain", "task", dict(calls=1, input_tokens=50, output_tokens=50, compute_ms=100))
        ledger.mark_dispatched("uncertain")
        ledger.mark_unknown("uncertain", "synthetic interrupted callback")
        unknown_snapshot = ledger.snapshot("session")
        try:
            ledger.reserve("third", "task", {"calls": 1})
        except BudgetExceeded:
            budget_blocks = True
        else:
            budget_blocks = False
        ledger.reconcile("uncertain", dict(calls=1, input_tokens=30, output_tokens=12, compute_ms=6, external_credit_micros=0))
        final_budget = ledger.snapshot("session")
        reuse = SingleFlightStore(root / "reuse.sqlite3", operations={"extract": pin}, clock=lambda: 100.)
        key = build_reuse_key(account_id="synthetic", scope="demo", purpose="preparation", operation="extract",
            implementation_sha256=pin, model_sha256=digest("no-model"), source_sha256=pin,
            policy_sha256=pin, input_sha256=hashlib.sha256(context.encoded).hexdigest(), dependencies={"rules": pin})
        owner = reuse.claim(key, "worker-a")
        waiting = reuse.claim(key, "worker-b")
        reuse.complete(key, owner_id="worker-a", lease_token=owner["lease_token"], fence=owner["fence"],
                       value={"references": ["synthetic-source"]})
        hit = reuse.claim(key, "worker-b")
        registry = ProcedureRegistry(root / "procedures.sqlite3", clock=lambda: 100.)
        try:
            candidate = registry.compile_trace(name="extract", version="1", training_task_ids=["training-only"],
                steps=[TraceStep("extract", {"format": "references"}, ("synthetic-source",))],
                source_bindings={"implementation": pin}, expires_at=1000., success=True, verified=True)
            qualification = registry.qualify(candidate["candidate_id"],
                evaluations=[EvaluationCase(f"heldout-{i}", True) for i in range(60)],
                current_bindings={"implementation": pin}, independent_tasks=True)
        finally:
            registry.close()
    graph = {"source": {"fingerprint": pin, "needs": []},
             "packet": {"fingerprint": pin, "needs": ["source"]},
             "independent": {"fingerprint": pin, "needs": []}}
    incremental = plan_incremental(graph, graph, changed=["source"])
    packet = asdict(context)
    packet.pop("encoded")
    checks = {"unknown_usage_keeps_reserve": unknown_snapshot["reserved"]["calls"] == 1,
              "parent_cap_blocks_new_work": budget_blocks,
              "same_work_waits": waiting["status"] == "WAIT",
              "completed_work_reused": hit["status"] == "HIT",
              "mandatory_context_retained": "rules" in context.included,
              "cheapest_qualified_selected": route["selected_route_id"] == "small",
              "learned_route_shadow_only": learned["shadow_only"],
              "unchanged_branch_preserved": incremental["reuse_candidates"] == ["independent"],
              "heldout_procedure_qualified": qualification["status"] == "QUALIFIED"}
    return {"schema": "keel.efficiency.demo.v1", "status": "PASS" if all(checks.values()) else "FAIL",
            "evidence_kind": "synthetic_protocol_checks_not_model_quality", "checks": checks,
            "provider_calls": 0, "paid_services": 0, "execution_authorized": False,
            "context": packet, "routing": route, "learned_routing": learned,
            "review_plan": plan_review(mandatory_reviewers=["reviewer-a", "reviewer-b"],
                                       optional_reviewers=["reviewer-c"], budget_units=6, per_call_units=1,
                                       disagreements=20, disagreement_trials=40, evidence_certified=True, evidence_hash=pin),
            "allocation": allocate_batch([{"task_id": "task-a", "choices": [{"route_id": "small", "cost_units": 1, "value_units": 3}]},
                                          {"task_id": "task-b", "choices": [{"route_id": "large", "cost_units": 4, "value_units": 4}]}], budget_units=4),
            "unknown_budget": unknown_snapshot, "settled_budget": final_budget,
            "incremental": incremental, "procedure": qualification, "benchmark": run_benchmark()}
