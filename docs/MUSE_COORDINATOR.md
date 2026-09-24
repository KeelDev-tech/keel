# Muse host coordinator: Keel 0.13

`keel_muse.coordinator` is a durable host adapter around existing Keel work. It
schedules registered callbacks; it does not implement another reasoning engine,
a shell runner, a browser driver or a submission mechanism. A caller-provided
callback result is an observation, never approval or proof of an external effect.

## Controller and client lifecycle

```python
from keel_muse.coordinator import Coordinator

coordinator = Coordinator(
    "/private/existing-parent/new-coordinator", "workspace",
    handlers={"review": trusted_review_callback},
    snapshot_provider=read_current_authoritative_snapshot,
    max_running=2, max_pending=128, max_calls=100,
)
coordinator.ingest(source_event)
coordinator.enqueue(task)
result = coordinator.worker_once("worker-a", lease_seconds=60)
```

Construct one long-lived writable controller and keep it alive. Concurrent
workers may call `worker_once` on that controller. **Constructing another writable
controller is an explicit restart:** it advances the controller generation,
fences old leases, returns LEASED work without an intent to READY, and preserves
STARTED work as UNKNOWN. Resources associated with uncertain effects remain
held. An old controller cannot acquire more work; late callback results cannot
complete its fenced tasks. A late HTTP 429 observation still adds the global stop.

Read-only clients must use:

```python
reader = Coordinator.open_readonly("/private/coordinator", "workspace")
snapshot = reader.snapshot()
```

This uses SQLite `mode=ro` and `query_only=ON`, performs no mutable constructor or
epoch transition, and rejects mutation methods. `inspect_home(home, workspace_id)`
is the equivalent convenience function. Do not instantiate a writable controller
for each CLI inspection, request or dashboard refresh.

Handlers, snapshot providers and the clock are trusted in-process configuration.
Task JSON chooses an already registered handler ID; it cannot import code,
select executable paths, supply a callback or install a new tool. The host must
keep callbacks and credentials outside model-controlled interfaces. This Python
boundary is not an OS sandbox and cannot stop an independently privileged process
from bypassing the broker.

## Events and source revision ordering

`ingest(event)` accepts this exact envelope:

```json
{
  "schema": "keel.muse.event.v1",
  "event_id": "producer-event-17",
  "workspace_id": "workspace",
  "scope_id": "role-application-scope",
  "kind": "SOURCE",
  "payload": {
    "source_id": "answers",
    "revision_sha256": "64 lowercase hexadecimal characters",
    "expected_previous_sha256": null
  }
}
```

The producer identity, role/application/account scope and source observation must
already be authenticated and normalized by the existing host source boundary.
A source digest here is a dependency pin; it is not proof of source truth.

| Kind | Exact payload | Effect |
|---|---|---|
| `SOURCE` | `source_id`, `revision_sha256`, `expected_previous_sha256` | Compare-and-swap the registered revision and invalidate dependent work |
| `WAKE` | `{}` | Reconsider blocked/ready tasks in the scope without inventing source evidence |
| `HOLD` | `reason` | Add a persistent local scope hold; there is no clear operation |
| `RATE_429` | `{}` | Persist a global stop across every scope and worker |

A first source record requires `expected_previous_sha256=null`. Subsequent
records require the exact current revision. Out-of-order producer events are
rejected as `source_cursor_conflict`, not silently applied. Source scope is part
of the dependency identity, so another role's source cannot satisfy this role.

Events are stored with stable canonical payload hashes and HMAC-bound receipts.
An identical event ID and payload returns the identical original receipt without
reapplying it; an identical ID with changed content conflicts. Preserve the
producer event ID across delivery retries. This provides deduplicated **local
transactional ingestion**, not exactly-once delivery by an external system.

A changed dependency blocks queued work and fences leased work. If an intent
already started, it becomes UNKNOWN and keeps its account/browser resource held.
The old task's dependency pins are never rewritten to the new revision; a new
reviewed task must be constructed. Matching source arrivals can wake previously
missing-evidence work. Repeated wake events coalesce into one pending task
recheck; the source events themselves are retained, not discarded. UNKNOWN and
RECORDED tasks are never revived by wakeups.

## Tasks, fairness and resource limits

`enqueue(task)` accepts exactly:

```json
{
  "schema": "keel.muse.task.v1",
  "task_id": "review-role-17-revision-4",
  "workspace_id": "workspace",
  "scope_id": "role-application-scope",
  "handler_id": "review",
  "account_id": "candidate-account",
  "resource_id": "browser-session-or-exclusive-resource",
  "dependencies": {"answers": "64 lowercase hexadecimal characters"},
  "payload": {},
  "priority": 0
}
```

Dependencies have 1–64 source pins. Payloads are JSON objects of at most 8192
canonical bytes. IDs are bounded text; priority is an exact integer 0–10. Never
put credentials or executable instructions in task payloads.

Reusing a task ID with identical content returns the existing task; changed
content conflicts. Defaults allow two active callbacks, 128 pending tasks and
100 callback reservations. Configurable bounds are 1–16 active workers,
1–128 pending tasks and 1–10000 reservations. There are also hard limits of
512 retained tasks, 4096 received events and 10000 journal entries, plus the
existing bounded canonical-JSON limit. Hitting a limit refuses more work.
Reopening with different persisted limits or handler IDs is rejected; it is not
a budget reset mechanism.

Both `account:<account_id>` and `browser:<resource_id>` receive exclusive,
fenced leases. Separate accounts can proceed concurrently only when their
browser/resource identities are also different. Leases last 1–3600 seconds;
callbacks must implement their own appropriate deadline. The coordinator does
not forcibly terminate arbitrary Python callbacks or claim OS containment.

Scheduling first favors accounts with fewer prior claims, then effective
priority, then enqueue order. Effective priority gains one point per 30 seconds
of waiting, capped at ten. This prevents permanent fixed-priority starvation
under the stated bounded queue and advancing-clock assumptions. A blocked
account/resource is never made eligible merely to achieve fairness.

`max_calls` bounds **callback reservations**, not inferred model calls or tokens.
A callback may invoke an existing multi-stage Keel reviewer; its actual model
budgets and sticky 429 protections remain the responsibility of the existing
routing/runtime gate. Reserve-before-effect is conservative: a crash or a final
preflight block may consume a reservation even when the handler was never
entered. A new home must not be used to evade persisted budgets or stops.

## Fresh host admission and callback outcomes

The configured provider receives the task document and returns exactly:

```json
{
  "schema": "keel.muse.host-snapshot.v1",
  "workspace_id": "workspace",
  "scope_id": "role-application-scope",
  "account_id": "candidate-account",
  "resource_id": "browser-session-or-exclusive-resource",
  "dependencies": {"answers": "64 lowercase hexadecimal characters"},
  "consent": true,
  "no_ai": false,
  "approval_current": true,
  "holds": [],
  "unknown_attempt": false,
  "rate_limited": false,
  "issued_at": 100,
  "expires_at": 160
}
```

All flags are exact booleans. Scope, account, resource and dependencies must
match the task. The observation must be currently valid and its whole lifetime
must not exceed 90 seconds. Missing approval, unaided/no-AI restrictions, consent
failure, holds or uncertainty block the callback. A valid scoped host observation
of 429 persists the global stop before blocking.

The broker claims a task, reads current host state, commits a STARTED intent and
budget reservation, reads host state again, checks the durable source/lease
watermark, and commits callback admission. Transaction admission samples the
clock again after acquiring the database lock. After the admission commit and
final committed-state read, the broker checks the live clock, lease, fence and
host observation immediately before invoking the registered handler. Slow lock
acquisition, commit or state reads cannot silently extend an expired approval
observation. An expired boundary after STARTED remains UNKNOWN with resources
held, even if this controller observed zero callback calls. No SQLite
transaction is held across an arbitrary callback. Therefore
the actual effect adapter **must recheck its authoritative Keel gateway at the
effect boundary**. These observations are not a transaction with an employer's
website; they cannot eliminate every external-state race.

The callback context is `keel.muse.callback-context.v1`, containing `task`,
`intent_id`, `worker_id`, `fence`, `generation`, the checked `snapshot`, its
`snapshot_sha256`, and `execution_authorized=false`. The context is evidence for
existing host checks, not a capability to bypass them.

A callback returns exactly:

```json
{"status":"RECORDED","receipt_sha256":"64 lowercase hexadecimal characters"}
```

Supported statuses are:

| Callback status | Receipt | Recorded behavior |
|---|---|---|
| `RECORDED` | Required SHA-256 | Store a terminal completion observation; no external authenticity claim |
| `BLOCKED` | `null` | Store a terminal observation that the invoked handler blocked; no automatic rescheduling |
| `UNKNOWN` | `null` | Preserve uncertainty and resource holds |
| `RATE_429` | `null` | Preserve uncertainty, persist the global stop, and fence other active work |

An exception or malformed response becomes UNKNOWN with no private exception
text in the result. A response after lease expiry or invalidation cannot complete
the task. A stale worker's 429 is still recorded as a conservative stop.

Distinguish **pre-callback BLOCKED** from a handler's BLOCKED observation. A host
preflight block has zero handler calls and may be reconsidered after a genuine
source/WAKE event. Once the callback has run, a BLOCKED response is terminal
RECORDED processing with reason `callback_observation_blocked`; a new reviewed
work item is required for another invocation. This avoids turning an ambiguous
callback outcome into a blind retry.

An IDLE result has `handler_calls_attempted=0`. Missing evidence, saturated
resources and persistent gates do not invoke the snapshot provider or handler
when no task can be claimed. The broker does not invent model-call counts for
opaque callbacks.

## Existing Keel adapters

`local_agent_handler(agent, worker_id, transport=None)` delegates to the existing
`LocalAgent.worker_once` REVIEW worker. It does not duplicate reasoning. Because
that API selects its own next queued review rather than accepting a specific
broker task ID, the broker task must cover the **whole agent workspace queue**:

- `workspace_id` equals `agent.state.workspace_id`.
- `scope_id` and `account_id` equal `local-agent:<workspace_id>`.
- `resource_id` equals `local-agent:` plus `digest(str(agent.home))`.

A role-specific broker grant cannot be used to wake an arbitrary queue through
this adapter. The LocalAgent still applies its own per-job snapshot, evidence,
roster and review checks. A completed review can itself contain HOLD; its hash
is a processing receipt, never submission permission. Route all such queue
workers through the same trusted coordination resource if using this adapter.

`recovery_guarded_provider(base_provider, journal, job_bindings,
revision_source_id=...)` intersects a host snapshot with an **already open**
`RecoveryJournal` or its read-only view. The trusted `job_bindings` map broker
scope IDs to journal job IDs; the selected dependency must match the journal's
current revision. Revoked approval, local holds, STARTED/RECORDED attempts,
UNKNOWN and persistent 429 can only restrict the provider's result. The adapter
creates no approvals, restarts, reconciliations or recovery writes.

## Storage, integrity and backup contract

The private home contains `coordinator.sqlite3` and `coordinator.sqlite3.key`.
The implementation reuses existing `LocalState` private-file checks, SQLite
transactions, synchronous FULL commits and monotonic host-clock checks. New
`muse_state`, `muse_journal` and `muse_inbox` tables live in that database.

`muse_state` contains the HMAC-bound current control state. Mutations verify the
current state and latest journal binding; `snapshot()` additionally verifies the
complete journal chain and all inbox receipts. The host key proves possession
by the OS account, not human identity or resistance to an owner who replaces
both database and key. A complete historical rollback needs an independently
retained checkpoint to detect.

The read-only snapshot schema is `keel.muse.coordinator-snapshot.v1`, with
`workspace_id`, `state`, `checkpoint_sha256`, `event_head_sha256`, `inbox_sha256`,
and `execution_authorized=false`. Its checkpoint is:

```text
digest({state, event_head_sha256, inbox_sha256})
```

The state schema is `keel.muse.coordinator-state.v1` and includes:
`workspace_id`, `limits`, `generation`, `sequence`, `last_now`, `rate_limited`,
`calls_reserved`, `tasks`, `sources`, `holds`, `resources`, `account_served`,
`enqueue_sequence`, `events_received`, and `wakeups_coalesced`.

Each task row stores its original `document`, `enqueue_sequence`, `enqueued_at`,
`status`, `reason`, `owner`, `fence`, `lease_until`, `intent_id`,
`snapshot_sha256`, `outcome`, and `wake_pending`. Resource rows contain `fence`,
`task_id`, `worker_id`, `lease_until`, and persistent `uncertain`. Task states are
READY, BLOCKED, LEASED, STARTED, RECORDED and UNKNOWN.

Use SQLite's consistent backup mechanism and preserve the matching key. Do not
copy only a live WAL-mode database file. Obtain read-only checkpoints before and
after backup, require equality and verify the destination through the read-only
API before accepting it. Retain the expected checkpoint outside the backup;
`snapshot(expected_checkpoint_sha256=...)` rejects drift. Restore to a new
private destination and explicitly open a new controller epoch only when ready
to resume. This preserves 429, uncertainty, holds and budget counters.

## Measured rehearsal

`demo(new_home)` executes nine checks using real local SQLite and injected
callbacks: missing-evidence idle, event deduplication, source-triggered work,
concurrent worker exclusion, persisted STARTED intent, restart to UNKNOWN,
uncertain 429, durable global stop, and read-only epoch preservation. Its
interruption is an explicitly injected `BaseException`; separate regression
tests fork a process and use `os._exit` after the committed STARTED intent.

The tests also cover independent resource locks, useful concurrency across
unrelated resources, source CAS, late/stale 429, source correction between
preflight and callback, revocation/no-AI/consent gates, queue backpressure,
account fairness, persistent budgets, tamper detection and both existing Keel
adapters. No network, real model, browser, employer submission or OS-security
validation is claimed by this rehearsal.
