# Computation graphs inside Muse coordination

`keel_machine.adapters.make_coordinator_handler` connects a host-configured
`GraphRunner` to the existing durable `keel_muse.coordinator.Coordinator`. Graphs
perform registered pure local computations. They cannot select Python callbacks,
import code, submit applications, acquire approval or call paid services through
their task JSON. The adapter adds no service dependency.

The existing coordinator still owns canonical host admission, source pins,
leases, resource exclusivity, budgets, holds, 429 stops and uncertain outcomes.
The graph runner independently checks its current evidence admission, including
when it reuses cached values. A cached answer is not cached authorization.

## Host registration and task binding

Install the handler in trusted embedding code:

```python
from keel_machine.adapters import make_coordinator_handler
from keel_machine.common import digest
from keel_muse.coordinator import Coordinator

handlers = {"machine": make_coordinator_handler(runner, record_result)}
coordinator = Coordinator(
    coordinator_home, workspace_id, handlers, canonical_host_snapshot,
)

task = {
    "schema": "keel.muse.task.v1",
    "task_id": "analysis-1",
    "workspace_id": workspace_id,
    "scope_id": plan["scope"],
    "handler_id": "machine",
    "account_id": plan["account_id"],
    "resource_id": "local-analysis-worker",
    "dependencies": {"graph_plan": digest(plan)},
    "payload": {"graph": plan},
    "priority": 0,
}
coordinator.enqueue(task)
```

`runner`, `record_result`, the workspace and `canonical_host_snapshot` are host
dependencies in this registration example, not implied live integrations. The
host must publish its authentic current `graph_plan` source revision into the
coordinator through the existing SOURCE event interface. Enqueueing a task does
not establish that revision, approve its admission or release a hold.

The handler requires an exact graph payload, a valid graph plan, matching task
`account_id` and `scope_id`, and `task.dependencies["graph_plan"] == digest(plan)`.
Changed graph content with an old pin, a changed scope, or a missing plan pin
returns `BLOCKED` before calling the runner or writer. Additional task dependency
pins remain available to the coordinator. The trusted constructor option
`per_run_dependency=True` instead requires exactly
`task.dependencies["graph_plan:" + plan["run_id"]] == digest(plan)`. This
keeps multiple runs in one scope independent. The mode is host configuration;
task JSON cannot select it. The default remains `graph_plan`. The concrete
local runtime uses this opt-in mode; see `MACHINE_RUNTIME.md`. Graph node evidence revisions remain
the runner's independently checked responsibility.

The coordinator's existing 8 KiB task-payload limit applies even though a graph
used directly can be larger. The adapter does not relax it. Use a smaller graph
that fits this boundary; task JSON cannot import a plan from an arbitrary path.

## Durable result callback

Supply `record_result(report)` in trusted host code. It must:

1. Persist the entire canonical report durably in an operator-controlled store.
2. Complete the durable commit before returning.
3. Return `keel_machine.common.digest(report)` as a lowercase SHA-256 string.

The callback receives an isolated JSON clone. The adapter compares its returned
receipt with the digest computed before calling it. A correct receipt records a
completed computation observation; it does not certify external facts or confer
action rights. The writer is a trusted persistence boundary: a returned digest
by itself is not independent proof that a dishonest writer actually committed.

No writer runs for a `HELD` graph, malformed report, mismatched plan/report pin,
or incomplete node/output set. A complete report must retain
`execution_authorized=false`, `external_actions=0`, and
`paid_services_required=false`.

Writer exceptions, uncertain commits and wrong receipts propagate to the
coordinator's existing `UNKNOWN` handling. This includes a commit that succeeded
before its reply was lost. The adapter neither retries nor fabricates a receipt.
The coordinator preserves the uncertain resource hold across restart. Its
existing host recovery procedures are required before further use of that
resource; a cached graph result does not release the hold.

## Interpret the two status levels correctly

| Graph/result condition | Handler outcome | Existing coordinator meaning |
| --- | --- | --- |
| Completed graph and matching durable writer receipt | `RECORDED` with receipt | Completed computation report was recorded |
| Held graph or rejected graph binding | `BLOCKED`, null receipt | The blocked callback observation was recorded |
| Writer failure, uncertain commit or bad receipt | Exception becomes `UNKNOWN` | Resource remains held; no automatic retry |

The existing coordinator may report outer task `status=RECORDED` for a handler's
`BLOCKED` outcome because it successfully recorded that blocked observation.
Inspect `result["outcome"]["status"]` and the receipt; outer task `RECORDED` alone
does not mean the graph completed. This established coordinator behavior is
preserved. If admission stops the callback beforehand, no graph runs at all.

## Verification and remaining host boundaries

`tests/test_machine_coordinator.py` exercises the real coordinator, its private
SQLite state, a real computation cache, registered pure operations, and a result
writer that commits reports to private SQLite before replying. It checks repeat
task suppression, fresh admission on cache hits, graph/source/scope mismatches,
all existing host gates, blocked graphs, isolated report copies, incorrect
receipts, and lost replies after real commits with restart-preserved holds.

The tests use synthetic host snapshots and admission decisions. They do not
authenticate a real operator or provider. The trusted host remains responsible
for authentic current snapshots, graph evidence admission, pure bounded function
registration, durable result storage and recovery. Graph deadlines are
cooperative; arbitrary Python must use the existing worker isolation boundary.
