# Keel 0.12 finite control model and durable recovery laboratory

These additive modules execute local verification and an isolated recovery
journal. They do not send applications, clear canonical holds, authenticate a
human approval, retry an uncertain attempt, or authorize execution. Existing
Keel packages remain unchanged.

## What is actually verified

`keel_loki.modelcheck.check_model()` exhaustively explores a finite Python
transition system. The current model has one job, two revisions, two workers,
and fence values 0–2. Its safety checks cover:

1. Active work requires a current matching approval, revision, owner and fence,
   with neither a hold nor a rate limit.
2. An attempt starts with an approval for its exact revision.
3. Completion uses the attempt's original fence.
4. UNKNOWN remains UNKNOWN; there is no automatic retry or completion from it.
5. A recorded HTTP 429 stop cannot disappear.
6. A recorded hold cannot be cleared by the agent.

Environment actions are approval, revocation, revision change, lease expiration,
crash, hold and HTTP 429. Controller actions are lease claim, start, finish and
escalation. Invalidating active work changes it to UNKNOWN. Escalation records a
block; it does not mean a person received or resolved it.

The bounded progress check runs the documented controller policy from **every
reachable state**, with environmental changes frozen. The controller escalates a
blocked state, otherwise claims, starts and finishes. Under that specific policy
and complete local transitions, completion or escalation takes at most three
steps. This is not unrestricted temporal liveness: an unfair scheduler, repeated
external changes, a failed disk or an unavailable person can prevent production
progress. A state-limit interruption is INCOMPLETE, never a verification pass.

Five named laboratory mutants intentionally omit a control: `unapproved_start`,
`stale_completion`, `retry_unknown`, `forget_429`, and `clear_hold`. Each must
produce an executable counterexample. Mutant transitions exist only in the
model explorer and cannot change the recovery journal. Their traces are
explicitly rejected as safe-machine conformance evidence.

## Python model API and schemas

```python
from keel_loki.modelcheck import check_model, demo, replay_trace

report = check_model()                 # Actual finite BFS exploration
broken = check_model(mutant="forget_429")
rehearsal = demo()                     # Explorer, mutants and synthetic trace
observed = replay_trace(trace, expected_machine_sha256=reviewed_source_hash)
```

`check_model(*, mutant=None, max_states=50000)` accepts 1–100000 as the state
limit. Results use `schema="keel.loki.modelcheck.v1"` and one of
`VERIFIED_FINITE_MODEL`, `COUNTEREXAMPLE`, `INCOMPLETE`, or `FAIL`. They include
state and transition counts, action coverage, discovered depth, progress counts,
finite bounds, assumptions and any counterexample. The machine digest hashes the
actual `modelcheck.py` bytes. `execution_authorized`,
`production_implementation_proven`, and `human_approval_authenticated` stay false.

A trace is a closed object:

```json
{
  "schema": "keel.loki.trace.v1",
  "machine_sha256": "64 lowercase hexadecimal characters",
  "initial": {"...": "all State fields below"},
  "laboratory_mutant": null,
  "steps": [{"action": "approve", "state": {"...": "all resulting State fields"}}]
}
```

Every state has exactly these fields:

| Fields | Values |
|---|---|
| `revision` | Integer 0 or 1 |
| `approval`, `start_revision`, `start_approval` | Integer -1 (absent), 0 or 1 |
| `fence`, `start_fence`, `finish_fence` | Integers 0–2 |
| `owner` | Empty string, `a`, or `b` |
| `phase` | `IDLE`, `ACTIVE`, `UNKNOWN`, `DONE` |
| `held`, `ever_held`, `rate_limited`, `ever_rate_limited`, `ever_unknown`, `escalated` | Exact JSON booleans |

The initial state is the module's `INITIAL`. Allowed action names are `approve`,
`revoke`, `revise`, `claim_a`, `claim_b`, `expire`, `start`, `finish`, `crash`,
`hold`, `rate_429`, and `escalate`. A trace has at most 10000 steps. Each supplied
next state must equal an enabled transition result and satisfy all invariants.
The checker returns `keel.loki.trace-check.v1`, `CONFORMING` or `NONCONFORMING`,
the first invalid step, verified-step counts and observed action coverage.
Malformed schemas, machine pins or laboratory traces raise `ModelCheckError`.

Conformance checks **supplied observations**, not their authenticity or their
completeness relative to a running host. The default demo trace is synthetic.
The entire SQLite implementation has not been formally proven equivalent to the
finite abstraction. In particular, real journal fences and revisions are not
limited to the model's tiny state domain, and restart fencing is more
conservative. Do not relabel model exploration as production verification.

The recovery demo also performs a separate **measured implementation trace**:
it projects actual HMAC-checked, committed SQLite snapshots after approval,
claim, start, completion observation and a post-completion 429 stop. Its
`journal_conformance` result has schema `keel.loki.journal-conformance.v1`,
`observation_source=COMMITTED_SQLITE_SNAPSHOTS`, the projected trace, six
snapshot checkpoint hashes and the safe-machine replay result. Concrete fixture
hashes and worker IDs map to the finite model's IDs; resulting states are read
from storage rather than generated by the model. This is five observed actions
on one synthetic path. `complete_implementation_refinement_proven` stays false.

## TLA+ artifact

`formal/KeelLoki.tla` expresses the same safe transition rules and six invariants.
`formal/KeelLoki.cfg` sets `MaxFence = 2`, selects `Init`, `Next` and `Safety`, and
disables deadlock checking because terminal/blocked states are intentional.
The supplied config checks safety, not a temporal progress theorem.

Python exploration does not run TLC. Its report deliberately records TLA+
`NOT_RUN`. An operator with an already installed, appropriately licensed TLC jar
can separately run:

```sh
java -cp /operator/path/tla2tools.jar tlc2.TLC -config formal/KeelLoki.cfg formal/KeelLoki.tla
```

Retain actual TLC output and tool/source hashes before reporting a TLC result.
No Java runtime, model checker or dependency is installed by this package.

## Durable recovery journal

```python
from keel_loki.recovery import RecoveryJournal

journal = RecoveryJournal("/private/new-home", "workspace", now=100)
journal.register("job", revision_sha256, now=101)
journal.approve("job", revision_sha256, declared_approval_sha256, now=102)
lease = journal.claim("job", "worker-a", lease_seconds=30, now=103)
intent = journal.start("job", "worker-a", lease["fence"], now=104)
# No external action is performed or authorized by start().
```

Keep this controller instance alive while using its leases. **Constructing
another `RecoveryJournal` is a controller restart**, not a passive connection:
it increments the boot generation, fences every old lease and makes every
STARTED attempt UNKNOWN. An old controller's worker can no longer complete it.
This fail-closed behavior also applies when the former process is still alive.
There is no automatic detection of operating-system process identity.

For passive observation, use the distinct read-only API:

```python
reader = RecoveryJournal.open_readonly("/private/existing-home", "workspace")
snapshot = reader.snapshot()
```

The read-only view opens existing SQLite with `mode=ro` and `query_only=ON`, does
not call the mutable storage constructor, and does not change the boot epoch,
clock, events, holds or attempts. All mutation methods reject this view. Its
`snapshot()` and `reconciliation_proposal()` methods are safe to expose through
separate inspection commands. Do not implement separate claim/start CLI
commands that each create a new controller; the second invocation deliberately
invalidates the first invocation's lease.

The journal reuses Keel's private SQLite/key storage and transaction clock.
It uses `BEGIN IMMEDIATE`, FULL synchronous commits, an HMAC-bound state row and
an ordered HMAC-bound event chain. Every read checks the complete chain and its
binding to current state. Limits are 128 jobs and 4096 events; exhausting them
refuses a mutation. Files remain local to the supplied private home.

The local key proves possession by the OS account. It does not prove upstream
truth or a person's identity. Someone controlling the database **and key** can
rewrite this boundary. Restoring a complete older database/key pair cannot be
detected from that pair alone; retain an external checkpoint and pass it to
`snapshot(expected_checkpoint_sha256=...)` or reconciliation. This is not a
backup or restoration service.

### Recovery API

All mutators accept keyword `now`; omission uses the host clock. The clock must
not move backwards, including relative to authenticated journal state. IDs are
bounded nonempty text and hashes are lowercase 64-character SHA-256 strings.
A lease lasts greater than zero and at most 300 seconds. A worker's fence is an
exact positive integer and its lease must still be unexpired at start/complete.

| Method | Effect |
|---|---|
| `register(job_id, revision_sha256)` | New IDLE job; duplicate IDs rejected |
| `approve(job_id, revision_sha256, approval_sha256)` | Records a trusted-caller declaration for the exact current revision; IDLE only |
| `revoke(job_id)` | Invalidates the declaration and worker; active intent becomes UNKNOWN |
| `revise(job_id, revision_sha256)` | Invalidates old approval and worker; active intent becomes UNKNOWN |
| `claim(job_id, worker_id, lease_seconds=30)` | Fenced lease on IDLE work with no local hold/global 429 |
| `start(job_id, worker_id, fence)` | Durable STARTED intent; no send or capability issued |
| `complete(job_id, worker_id, fence, receipt_sha256)` | Stores an exact-fence completion **observation**, called RECORDED; authenticity remains unverified |
| `set_hold(job_id)` | Adds a persistent local hold; active intent becomes UNKNOWN |
| `escalate(job_id)` | Records escalation; sends no notification and clears nothing |
| `record_429()` | Persistent global stop; fences workers and changes all active intents to UNKNOWN |
| `recover_expired()` | Fences expired leases; active intent becomes UNKNOWN |
| `snapshot(expected_checkpoint_sha256=None)` | Verified read with optional independent checkpoint pin |

There are no methods to clear UNKNOWN, 429 or holds. RECORDED is not retryable.
Claiming an expired STARTED attempt is rejected; explicit recovery makes its
uncertainty durable. Approval and completion responses explicitly disclaim
human/observation authentication and execution authority.

### Recovery schemas

A snapshot has `schema="keel.loki.recovery-snapshot.v1"`, `state`,
`checkpoint_sha256`, `event_head_sha256`, `human_approval_authenticated=false`,
`canonical_writes=0`, and `execution_authorized=false`.

The state schema is `keel.loki.recovery-state.v1` with `workspace_id`,
`boot_generation`, `event_count`, `last_now`, persistent boolean `rate_limited`,
and `jobs` keyed by job ID. Each job contains:

- Identity: `job_id`, `revision_sha256`, and increasing `version`.
- Approval: nullable `approval_sha256`, nullable `approval_revision_sha256`, and
  boolean `approval_active`.
- Lease: integer `fence`, nullable `owner` and `lease_until`.
- State: `phase` (`IDLE`, `STARTED`, `UNKNOWN`, `RECORDED`), boolean `held` and
  `escalated`, nullable `unknown_reason`.
- Attempt: nullable `attempt_id`, `start_revision_sha256`,
  `start_approval_sha256`, `start_fence`, `started_at`, and `receipt_sha256`.
  `completion_fence` is nullable and records the actual fence used for a
  completion observation.

An event uses `keel.loki.recovery-event.v1` with `workspace_id`, `sequence`,
`previous_sha256`, `action`, nullable `job_id`, `occurred_at`, and `state_sha256`.
Storage adds the payload hash and local HMAC. No secret key is returned by an
API or report.

### Read-only reconciliation proposals

`reconciliation_proposal(job_id, evidence, *, expected_checkpoint_sha256,
expected_evidence_sha256, now=None)` requires **both independent pins**. The
evidence object has exactly:

```json
{
  "schema": "keel.loki.reconciliation-evidence.v1",
  "workspace_id": "workspace",
  "job_id": "job",
  "attempt_id": "attempt_identifier_from_the_journal",
  "revision_sha256": "hash_of_the_revision_at_attempt_start",
  "attempt_fence": 1,
  "observation": "CONFIRMED",
  "observed_at": 106,
  "evidence_sha256": "hash_of_the_host_supplied_supporting_evidence"
}
```

The object digest must match `expected_evidence_sha256`. Workspace, job,
attempt, start revision and original attempt fence must all match the UNKNOWN
attempt. Observations must follow attempt start, not be in the future, and be
at most 90 seconds old. `observed_at` must be an actual JSON number; null is not
a request to substitute the current clock.

`CONFIRMED` creates `REVIEW_RECORDED_CONFIRMATION`; `NOT_OBSERVED` and
`INCONCLUSIVE` create `MANUAL_INVESTIGATION`. None clears UNKNOWN or permits
retry. A negative search result does not prove that a prior action did not
occur. Proposals use `keel.loki.reconciliation-proposal.v1`, `status=REVIEW_ONLY`,
exact checkpoint/evidence hashes, and false `observation_authenticated`,
`reconciliation_authorized`, `retry_authorized`, and `execution_authorized`.
Both `journal_writes` and `canonical_writes` are zero.

`keel_loki.recovery.demo(new_home)` performs a real local SQLite close/reopen
rehearsal with synthetic declarations. Focused tests additionally fork a process,
commit STARTED, exit via `os._exit` without Python cleanup, and reopen the journal.
They also test stale workers, changed revisions, revocation, persistent 429,
unkeyed database tampering, checkpoint/evidence drift and genuine read-only
views. No external service or real submission is exercised.
