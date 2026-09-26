"""Real coordinator + graph + cache + committed local result receipts.

All host snapshots and graph admission decisions here are synthetic fixtures.
No test claims to authenticate a real operator, provider or approval.
"""
import json

import pytest

from keel_machine.adapters import make_coordinator_handler
from keel_machine.cache import ComputationCache
from keel_machine.common import MachineError, PrivateDB, canonical, clone, digest
from keel_machine.graph import GraphRunner, GuardDecision, Operation
from keel_muse.coordinator import Coordinator


def _upper(payload):
    return {"text": payload["arguments"]["text"].upper()}


def object_contract(properties):
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


def operations():
    text = {"type": "string", "maxLength": 128}
    return {"upper": Operation("upper", _upper,
        object_contract({"arguments": object_contract({"text": text}),
                         "config": object_contract({}), "dependencies": object_contract({})}),
        object_contract({"text": text}))}


def plan():
    return {"schema": "keel.machine.graph.v1", "run_id": "graph-run", "account_id": "account",
            "scope": "scope", "purpose": "test", "nodes": [
                {"id": "upper", "operation": "upper", "arguments": {"text": "keel"},
                 "config": {}, "needs": [], "revisions": {"profile": "a" * 64}}],
            "outputs": ["upper"], "max_compute_nodes": 1, "max_seconds": 10}


def task(graph, task_id="first", **updates):
    return {"schema": "keel.muse.task.v1", "task_id": task_id, "workspace_id": "workspace",
            "scope_id": graph["scope"], "handler_id": "machine", "account_id": graph["account_id"],
            "resource_id": "local-computation", "dependencies": {"graph_plan": digest(graph)},
            "payload": {"graph": clone(graph)}, "priority": 0, **updates}


def snapshot(document):
    return {"schema": "keel.muse.host-snapshot.v1", **{key: document[key] for key in
            ("workspace_id", "scope_id", "account_id", "resource_id", "dependencies")},
            "consent": True, "no_ai": False, "approval_current": True, "holds": [],
            "unknown_attempt": False, "rate_limited": False, "issued_at": 100, "expires_at": 160}


def source(document, *, revision=None, identifier="source", previous=None):
    return {"schema": "keel.muse.event.v1", "event_id": identifier, "workspace_id": "workspace",
            "scope_id": document["scope_id"], "kind": "SOURCE", "payload": {
                "source_id": "graph_plan", "revision_sha256": revision or document["dependencies"]["graph_plan"],
                "expected_previous_sha256": previous}}


class ResultWriter:
    """Test host implementation commits the canonical report before replying."""
    def __init__(self, path):
        self.db = PrivateDB(path)
        self.calls = 0
        with self.db.transaction() as db:
            db.execute("CREATE TABLE reports(digest TEXT PRIMARY KEY, report BLOB NOT NULL)")

    def __call__(self, report):
        self.calls += 1
        checksum = digest(report)
        with self.db.transaction() as db:
            db.execute("INSERT OR IGNORE INTO reports VALUES(?,?)", (checksum, canonical(report)))
        return checksum

    def reports(self):
        with self.db.transaction() as db:
            return {row["digest"]: json.loads(row["report"]) for row in db.execute("SELECT * FROM reports")}


def setup(tmp_path, *, guard_allowed=True, provider=snapshot, writer_mode=None):
    machine = tmp_path / "machine"
    machine.mkdir(mode=0o700)
    cache = ComputationCache(machine / "cache.sqlite3", clock=lambda: 100)
    admissions = []
    def guard(request, now):
        admissions.append(clone(request))
        return GuardDecision(guard_allowed, digest(request), now + 30)
    runner = GraphRunner(cache, operations(), guard, clock=lambda: 100)
    writer = ResultWriter(machine / "reports.sqlite3")
    def persist(report):
        receipt = writer(report)
        if writer_mode == "uncertain":
            raise OSError("synthetic reply loss after commit")
        if writer_mode == "bad_receipt":
            return "0" * 64
        return receipt
    handler = make_coordinator_handler(runner, persist)
    coordinator = Coordinator(tmp_path / "coordinator", "workspace", {"machine": handler}, provider,
                              clock=lambda: 100)
    return coordinator, runner, writer, admissions, handler


def enqueue(coordinator, document):
    coordinator.ingest(source(document, identifier="source:" + document["task_id"]))
    coordinator.enqueue(document)


def test_real_coordinator_persists_report_receipt_and_reuses_graph_cache(tmp_path):
    coordinator, runner, writer, admissions, _ = setup(tmp_path)
    graph = plan()
    first_task = task(graph)
    enqueue(coordinator, first_task)
    first = coordinator.worker_once("worker")
    assert first["status"] == "RECORDED"
    assert first["outcome"]["status"] == "RECORDED"
    receipt = first["outcome"]["receipt_sha256"]
    report = writer.reports()[receipt]
    assert receipt == digest(report)
    assert report["outputs"]["upper"] == {"text": "KEEL"}
    assert report["computed_nodes"] == 1 and report["cache_hits"] == 0
    assert report["execution_authorized"] is False
    # A recorded task is never invoked again, even after an explicit wake.
    coordinator.ingest({"schema": "keel.muse.event.v1", "event_id": "wake", "workspace_id": "workspace",
                        "scope_id": graph["scope"], "kind": "WAKE", "payload": {}})
    assert coordinator.worker_once("worker")["handler_calls_attempted"] == 0
    assert writer.calls == 1
    # A distinct admitted task may reuse inert values, with fresh graph guards.
    coordinator.enqueue(task(graph, task_id="second"))
    second = coordinator.worker_once("worker")
    cached = writer.reports()[second["outcome"]["receipt_sha256"]]
    assert cached["computed_nodes"] == 0 and cached["cache_hits"] == 1
    assert writer.calls == 2
    phases = [r["phase"] for r in admissions]
    assert phases.count("before") == phases.count("after") == phases.count("return") == 2
    assert len({r["nonce"] for r in admissions}) == len(admissions)


@pytest.mark.parametrize("change", ["scope", "account", "stale_plan", "missing_pin", "extra_payload"])
def test_adapter_binding_mismatch_blocks_before_runner_or_writer(tmp_path, change):
    coordinator, _, writer, admissions, _ = setup(tmp_path)
    graph = plan()
    document = task(graph)
    if change == "scope":
        document["scope_id"] = "other-scope"
    elif change == "account":
        document["account_id"] = "other-account"
    elif change == "stale_plan":
        document["payload"]["graph"]["nodes"][0]["arguments"]["text"] = "changed after pin"
    elif change == "missing_pin":
        document["dependencies"] = {"other": "b" * 64}
    else:
        document["payload"]["approval"] = True
    if change == "missing_pin":
        event = source(task(graph))
        event["payload"].update(source_id="other", revision_sha256="b" * 64)
        coordinator.ingest(event)
        coordinator.enqueue(document)
    else:
        enqueue(coordinator, document)
    result = coordinator.worker_once("worker")
    assert result["outcome"] == {"status": "BLOCKED", "receipt_sha256": None}
    assert writer.calls == 0 and admissions == []
    assert coordinator.snapshot()["state"]["tasks"]["first"]["reason"] == "callback_observation_blocked"


def test_stale_source_pin_is_stopped_by_existing_coordinator(tmp_path):
    coordinator, _, writer, admissions, _ = setup(tmp_path)
    document = task(plan())
    enqueue(coordinator, document)
    coordinator.ingest(source(document, revision="b" * 64, identifier="new-source",
                              previous=document["dependencies"]["graph_plan"]))
    assert coordinator.worker_once("worker")["handler_calls_attempted"] == 0
    assert writer.calls == 0 and admissions == []


def test_held_graph_returns_blocked_observation_without_result_receipt(tmp_path):
    coordinator, _, writer, admissions, _ = setup(tmp_path, guard_allowed=False)
    enqueue(coordinator, task(plan()))
    result = coordinator.worker_once("worker")
    assert result["outcome"] == {"status": "BLOCKED", "receipt_sha256": None}
    assert writer.calls == 0 and len(admissions) == 1
    # Existing coordinator records the BLOCKED observation; that is not a
    # successfully completed graph or a persisted graph-result receipt.
    assert result["status"] == "RECORDED"
    assert result["execution_authorized"] is False


@pytest.mark.parametrize("gate,value", [("approval_current", False), ("consent", False),
    ("no_ai", True), ("unknown_attempt", True), ("holds", ["operator-hold"]), ("rate_limited", True)])
def test_existing_admission_gates_still_prevent_graph_and_writer(tmp_path, gate, value):
    def provider(document):
        return {**snapshot(document), gate: value}
    coordinator, _, writer, admissions, _ = setup(tmp_path, provider=provider)
    enqueue(coordinator, task(plan()))
    result = coordinator.worker_once("worker")
    assert result["handler_calls_attempted"] == 0
    assert writer.calls == 0 and admissions == []


@pytest.mark.parametrize("mode", ["uncertain", "bad_receipt"])
def test_uncertain_or_wrong_writer_receipt_preserves_unknown_and_never_retries(tmp_path, mode):
    coordinator, _, writer, _, handler = setup(tmp_path, writer_mode=mode)
    enqueue(coordinator, task(plan()))
    result = coordinator.worker_once("worker")
    assert result["status"] == "UNKNOWN"
    assert writer.calls == 1 and len(writer.reports()) == 1
    assert coordinator.snapshot()["state"]["resources"]["account:account"]["uncertain"] is True
    restarted = Coordinator(tmp_path / "coordinator", "workspace", {"machine": handler}, snapshot, clock=lambda: 100)
    restarted.enqueue(task(plan(), task_id="second"))
    assert restarted.worker_once("restarted")["handler_calls_attempted"] == 0
    assert writer.calls == 1


def test_writer_exception_is_not_swallowed_as_blocked(tmp_path):
    _, runner, _, _, _ = setup(tmp_path)
    def uncertain(report):
        raise MachineError("synthetic_writer_uncertain")
    handler = make_coordinator_handler(runner, uncertain)
    context = {"schema": "keel.muse.callback-context.v1", "task": task(plan()), "execution_authorized": False}
    with pytest.raises(MachineError, match="synthetic_writer_uncertain"):
        handler(context)


def test_persistence_callback_receives_an_isolated_report(tmp_path, monkeypatch):
    _, runner, _, _, _ = setup(tmp_path)
    original = runner.run(plan())
    before = clone(original)
    monkeypatch.setattr(runner, "run", lambda graph: original)
    def mutate(report):
        report["outputs"].clear()
        return digest(report)
    handler = make_coordinator_handler(runner, mutate)
    context = {"schema": "keel.muse.callback-context.v1", "task": task(plan()), "execution_authorized": False}
    with pytest.raises(MachineError, match="receipt_mismatch"):
        handler(context)
    assert original == before


@pytest.mark.parametrize("field,value", [("status", "SUBMITTED"), ("plan_sha256", "0" * 64),
    ("execution_authorized", True), ("external_actions", 1), ("outputs", {}), ("nodes", {})])
def test_malformed_or_misbound_runner_report_is_never_persisted(tmp_path, monkeypatch, field, value):
    coordinator, runner, writer, _, _ = setup(tmp_path)
    report = runner.run(plan())
    report[field] = value
    monkeypatch.setattr(runner, "run", lambda graph: report)
    enqueue(coordinator, task(plan()))
    assert coordinator.worker_once("worker")["outcome"] == {"status": "BLOCKED", "receipt_sha256": None}
    assert writer.calls == 0


def test_spoofed_context_cannot_enable_execution(tmp_path):
    _, _, writer, admissions, handler = setup(tmp_path)
    result = handler({"schema": "keel.muse.callback-context.v1", "task": task(plan()), "execution_authorized": True})
    assert result == {"status": "BLOCKED", "receipt_sha256": None}
    assert writer.calls == 0 and admissions == []


def test_trusted_per_run_dependency_mode_accepts_exact_run_key(tmp_path):
    coordinator, runner, writer, _, _ = setup(tmp_path)
    coordinator._handlers['machine'] = make_coordinator_handler(runner, writer, per_run_dependency=True)
    graph = plan()
    document = task(graph)
    event = source(document)
    key = 'graph_plan:' + graph['run_id']
    document['dependencies'] = {key: digest(graph)}
    event['payload']['source_id'] = key
    coordinator.ingest(event)
    coordinator.enqueue(document)
    result = coordinator.worker_once('worker')
    assert result['outcome']['status'] == 'RECORDED'
    assert writer.calls == 1


@pytest.mark.parametrize('per_run_dependency', [False, True])
def test_dependency_mode_is_host_configuration_not_inferred_from_task(tmp_path, per_run_dependency):
    coordinator, runner, writer, _, _ = setup(tmp_path)
    coordinator._handlers['machine'] = make_coordinator_handler(runner, writer,
                                                               per_run_dependency=per_run_dependency)
    graph = plan()
    document = task(graph)
    event = source(document)
    key = 'graph_plan' if per_run_dependency else 'graph_plan:' + graph['run_id']
    document['dependencies'] = {key: digest(graph)}
    event['payload']['source_id'] = key
    coordinator.ingest(event)
    coordinator.enqueue(document)
    result = coordinator.worker_once('worker')
    assert result['outcome'] == {'status': 'BLOCKED', 'receipt_sha256': None}
    assert writer.calls == 0


def test_dependency_mode_requires_boolean(tmp_path):
    _, runner, writer, _, _ = setup(tmp_path)
    with pytest.raises(MachineError, match='coordinator_dependency_mode_invalid'):
        make_coordinator_handler(runner, writer, per_run_dependency='from-json')
