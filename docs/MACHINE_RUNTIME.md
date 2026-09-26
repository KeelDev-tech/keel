# Durable local machine workflows

Keel can now run admitted pure computation graphs directly from its CLI. This
connects the existing Coordinator, graph runner, persistent computation cache
and a concrete durable result writer. It needs only Python's standard library
on a POSIX host with `fcntl` locking. No network, model, browser, paid service,
shell command or dynamically imported task handler is registered.

## Run a real local workflow

Choose a new directory under your home directory, outside the source tree:

```sh
python3 -B -m keel_machine init --home "$HOME/keel-machine" --workspace-id local-machine --account-id local-owner
python3 -B -m keel_machine submit --home "$HOME/keel-machine" --plan sample_data/machine-plan.example.json
python3 -B -m keel_machine work --home "$HOME/keel-machine" --max-tasks 16
python3 -B -m keel_machine status --home "$HOME/keel-machine"
python3 -B -m keel_machine result --home "$HOME/keel-machine" --run-id document-compare-1
```

The example contains synthetic text. You can replace it with your own bounded
input text and submit it under a new run ID. The example account ID must match
the account chosen at initialization. Input content and revision hashes remain
local assertions; this workflow does not authenticate their external truth.

`submit` records a plan durably. `work` processes a bounded batch and exits; it
is not an installed daemon. Invoking `work` again handles pending plans. Reusing
an existing run ID with identical plan bytes is idempotent; changing its plan
is rejected. A new run ID can reuse unchanged node computations after fresh
admission checks. Changes to inputs, declared revisions or implementation pins
invalidate affected cache entries.

Stop a run with a persistent hold:

```sh
python3 -B -m keel_machine hold --home "$HOME/keel-machine" --run-id document-compare-1 --reason owner_hold
```

Holds are sticky and no CLI operation clears them. Holding a completed run keeps
its historical receipt, but `result` withholds its report. Running a new job ID
is a new owner-controlled request, not clearance of an earlier hold or unknown
outcome. Existing uncertain Coordinator resources remain locked across restart.

## Concrete lifecycle and admission

`LocalGraphRuntime.create(home, workspace_id, account_id)` provisions new private
state. `LocalGraphRuntime(home)` opens existing complete state passively.
Methods are `submit(plan)`, `work(max_tasks=16)`, `status()`, `result(run_id)` and
`hold(run_id, reason='owner_hold')`.

Only `work` obtains an exclusive, nonblocking OS file lock and constructs a
writable Coordinator. Another `work` call returns a busy error without fencing
that controller. Status, result, hold and submit do not restart the controller.
Each work session is an explicit Coordinator restart; its existing generation,
lease and resource fencing rules apply. An interrupted STARTED task becomes
UNKNOWN when the next worker session starts. No automatic retry or uncertainty
clearance is provided.

Submitted plans are an outbox. Work publishes each exact per-run plan revision
and enqueues its task using idempotent Coordinator interfaces. A crash between
these two durable boundaries can safely resume publication/enqueue. The trusted
adapter uses `per_run_dependency=True`, binding to exactly
`graph_plan:<run_id>`; its default mode remains backward compatible.

The graph guard checks the stored account, scope, purpose, exact plan hash and
node revision bindings, the sticky owner hold, and the live Coordinator lease
and generation. The final graph check observes all stored node bindings and
hold state in one runtime transaction. The result writer rechecks these local
conditions before committing, and returns the report digest only after commit.

A recorded report alone does not settle an uncertain Coordinator outcome. If a
write commits but its reply is lost, the task becomes UNKNOWN and `result` does
not expose it as completed. A result is released only when its stored bytes,
stored digest and completed Coordinator receipt all agree. A handler BLOCKED
observation may have outer Coordinator status RECORDED; the runtime translates
that into a held result rather than completed computation.

## Bounds and trust boundary

| Resource | Limit |
| --- | --- |
| Runs retained per runtime | 64 lifetime; no silent deletion |
| Worker sessions per runtime | 512 lifetime |
| Tasks processed per work call | 1–64, default 16 |
| Graph JSON inside Coordinator task | Existing 8 KiB payload cap |
| Run ID | 110 ASCII identifier characters |
| Graph output report | Existing 256 KiB canonical JSON bound |
| Computation cache | 1,024 entries / 16 MiB logical content |
| Registered callbacks | Fixed builtins only |

A full runtime holds work with a capacity error. Keep the existing state for
inspection; this bounded runtime does not implement archival compaction. These
are logical record/content limits, not a physical SQLite disk quota. The
Coordinator has its own bounded journal and event tables. Deleting databases
or keys is not supported recovery; incomplete state fails closed.

The local OS owner and administrator control this boundary. Account IDs are
local namespaces, not authenticated human identities. All runtime results retain
`execution_authorized=false`, `external_actions=0` and
`paid_services_required=false`. Coordinator consent and approval booleans
represent admission to this inert local computation namespace only. They never
enter the separate external-effect `SQLiteAuthority` or confer application,
publishing or account-management permission.

Pure operation registration is fixed in reviewed source. Declared revisions
are pins, not automatic filesystem watchers or verified source provenance. The
runtime can revoke a plan through its hold interface; it cannot detect changes
to an external document unless new content/revisions are supplied. Graph
execution deadlines remain cooperative. The existing worker isolation boundary
is required for untrusted arbitrary code, which this runtime does not load.

## Verification

`tests/test_machine_runtime.py` uses standard-library `unittest` and real private
SQLite stores. It exercises repeat computation reuse, changed branches, multiple
plans in one scope, passive access, duplicate rejection, final-check revocation,
report-commit races, lost replies, interrupted intents, stale generations,
exclusive worker locking, capacities, clock rollback and receipt corruption.
Test fixtures do not contact external services or certify live source truth.
