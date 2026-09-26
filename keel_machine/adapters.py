"""Trusted-host adapter from Muse coordination to pure local computation graphs.

Task JSON supplies data and revision pins, never Python callbacks or authority.
The coordinator retains its existing admission/lease/uncertainty gates. Graph
admission is independently rechecked by the runner, including cache hits.
"""
from .common import MachineError, clone, digest, require
from .graph import GraphRunner, validate_plan


def _blocked():
    return {"status": "BLOCKED", "receipt_sha256": None}


def make_coordinator_handler(runner, record_result, *, per_run_dependency=False):
    """Return a real ``Coordinator`` callback for a preconfigured ``GraphRunner``.

    ``per_run_dependency=True`` is trusted host configuration selecting the exact
    ``graph_plan:<run_id>`` source key, so runs can share a scope without
    replacing each other. Task JSON cannot choose this mode. The default
    preserves the existing ``graph_plan`` binding.

    ``record_result(cloned_report)`` is a trusted local persistence callback. It
    must return the canonical report's SHA-256 only after durable commit. Its
    exceptions, uncertain commits and incorrect receipts propagate; the existing
    coordinator then records UNKNOWN and preserves its resource hold. This
    adapter never retries a writer or fabricates a persisted receipt.
    """
    require(type(per_run_dependency) is bool, "coordinator_dependency_mode_invalid")
    require(isinstance(runner, GraphRunner), "coordinator_graph_runner_required")
    require(callable(record_result), "coordinator_result_writer_required")

    def invoke(context):
        # All deterministic binding failures occur before any computation or
        # report persistence. They are blocked observations, not successes.
        try:
            context = clone(context)
            require(type(context) is dict and context.get("schema") == "keel.muse.callback-context.v1",
                    "coordinator_context_invalid")
            require(context.get("execution_authorized") is False, "coordinator_authority_invalid")
            task = context.get("task")
            require(type(task) is dict and task.get("schema") == "keel.muse.task.v1", "coordinator_task_invalid")
            payload = task.get("payload")
            require(type(payload) is dict and set(payload) == {"graph"}, "coordinator_graph_payload_invalid")
            plan, nodes, _ = validate_plan(payload["graph"])
            plan_digest = digest(plan)
            require(task.get("account_id") == plan["account_id"] and task.get("scope_id") == plan["scope"],
                    "coordinator_graph_scope_mismatch")
            dependencies = task.get("dependencies")
            dependency_key = "graph_plan:" + plan["run_id"] if per_run_dependency else "graph_plan"
            require(type(dependencies) is dict and dependencies.get(dependency_key) == plan_digest,
                    "coordinator_graph_revision_mismatch")
        except MachineError:
            return _blocked()

        # The runner owns fresh evidence admission for its pure operations. A
        # validated coordinator snapshot cannot substitute for those checks.
        try:
            report = clone(runner.run(clone(plan)))
            require(type(report) is dict and report.get("schema") == "keel.machine.graph-result.v1",
                    "coordinator_graph_result_invalid")
            require(report.get("plan_sha256") == plan_digest and report.get("run_id") == plan["run_id"],
                    "coordinator_graph_result_binding_invalid")
            require(report.get("execution_authorized") is False and report.get("paid_services_required") is False
                    and type(report.get("external_actions")) is int and report["external_actions"] == 0,
                    "coordinator_graph_result_authority_invalid")
            require(report.get("status") in ("COMPLETED", "HELD"), "coordinator_graph_result_status_invalid")
            if report["status"] == "HELD":
                return _blocked()
            require(type(report.get("nodes")) is dict and set(report["nodes"]) == set(nodes)
                    and all(type(value) is dict and value.get("status") in ("COMPUTED", "CACHED")
                            for value in report["nodes"].values()), "coordinator_graph_nodes_incomplete")
            require(type(report.get("outputs")) is dict and set(report["outputs"]) == set(plan["outputs"]),
                    "coordinator_graph_outputs_incomplete")
        except MachineError:
            return _blocked()

        # Deliberately outside the blocked-result exception handler: after the
        # writer is called, failure may mean it committed but lost its reply.
        expected_receipt = digest(report)
        receipt = record_result(clone(report))
        require(type(receipt) is str and receipt == expected_receipt,
                "coordinator_persisted_report_receipt_mismatch")
        return {"status": "RECORDED", "receipt_sha256": expected_receipt}

    return invoke
