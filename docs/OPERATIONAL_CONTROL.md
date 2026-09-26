# Shared stop control — Keel 0.14

`keel_operational.control.StopLedger` is a local, durable coordination boundary for explicitly participating Keel writers. It adds no network client, model call, native browser call, submission operation or stop-clearing API. A trusted host must install its provider and writer barrier in every participating worker. Unrelated Muse calls and preexisting processes that do not use this integration remain outside its control.

The ledger does not replace Keel's existing approval, consent, deduplication, no-AI, source-revision or native platform permission checks. `execution_authorized` is always false in its reports. `RECORDED` means that the caller recorded an observation; it does not mean the task succeeded.

## Storage and opening

Create once with `StopLedger.create(home, workspace_id, ...)`. Open with `StopLedger(home, workspace_id, ...)`. Ordinary opening validates the current state without incrementing an epoch, recovering work, resetting a stop or rewriting a checkpoint.

The dedicated private directory contains `control.sqlite3`, `control.key` and `control.lock`. All files must be private regular files owned by the host account. The 32-byte local key authenticates the single durable state document. Commits use SQLite immediate transactions and full synchronization. Directory and file identities are pinned for each live object; unexpected replacement is rejected. There is a sequence and rolling event digest, not a separately retained event-history log.

HMAC detects unkeyed editing. It does not authenticate an upstream human or defend against the account owner who controls both state and key. Detecting rollback requires an independently retained current checkpoint. Do not delete and recreate the ledger to clear a stop.

## API and exact binding

```python
ledger = StopLedger.create(
    home, workspace_id,
    snapshot_provider=trusted_canonical_reader,
    canonical_sink=trusted_idempotent_stop_writer,
    clock=trusted_clock,
)
reservation = ledger.admit(binding, lease_seconds=30)
intent = ledger.dispatch(binding['attempt_id'], reservation['token'])
# The host's existing ActionGateway and native permission gate still apply.
check = ledger.check_dispatch(binding['attempt_id'], reservation['token'])
# Only a CURRENT final check may be considered by the wrapper for returning
# its already prepared native request. It never creates another intent.
ledger.finish(binding['attempt_id'], reservation['token'],
              status='RECORDED', receipt_sha256=observed_receipt_sha256)
```

The exact attempt object is:

```json
{
  "schema": "keel.operational.attempt.v1",
  "attempt_id": "stable-attempt-id",
  "worker_id": "installed-worker",
  "workspace_id": "workspace",
  "account_id": "account",
  "role_id": "role",
  "resource_id": "native-form-resource",
  "operation": "prepare",
  "input_sha256": "<exact request digest>",
  "source_sha256": "<release source digest>"
}
```

`admit` reserves the role and account/resource pair after checking the trusted provider. It returns the secret token once; only its hash is stored. Repeating the same attempt returns its status without a token. An attempt ID with different content is rejected. The wrapper must durably retain the token. A lost token cannot be recreated from JSON or the status view.

`dispatch` consumes a reservation once, after checking the provider again. Repeating dispatch returns `WAIT` and no new intent. `check_dispatch` checks the still-issued intent, lease, local stops and fresh provider again; it never reissues work. A `CURRENT` response includes both `lease_until` and `host_snapshot_expires_at`. The wrapper must check those deadlines after any subsequent blocking operation before exposing an actionable request.

Callbacks are supplied by trusted host code at construction, never resolved from request strings or JSON. The exact provider result is:

```json
{
  "schema": "keel.operational.host-snapshot.v1",
  "workspace_id": "workspace",
  "attempt_sha256": "<digest of exact attempt binding>",
  "source_sha256": "<same source digest>",
  "consent": true,
  "no_ai": false,
  "approval_current": true,
  "holds": [],
  "unknown_attempt": false,
  "rate_limited": false,
  "issued_at": 1800000000,
  "expires_at": 1800000060
}
```

The maximum snapshot lifetime is 90 seconds. The clock is reread after acquiring the database and barrier locks, and after invoking the provider. A clock rollback, expired observation, changed binding or blocked gate prevents admission. The provider is trusted to read the real canonical controls; these booleans do not independently prove their truth. Existing typed approvals are still validated by the action gateway.

## Stops and recovery

`finish` accepts `RECORDED`, `UNKNOWN` or `RATE_429`. An ordinary recorded result requires a receipt hash and a still-current issued lease. A late successful-looking result becomes UNKNOWN. An UNKNOWN attempt never becomes recorded or automatically retries through this API.

`recover()` expires abandoned reservations to BLOCKED because they were not dispatched. It turns expired issued intents into UNKNOWN, preserving an indefinite hold on the role across accounts and on the exact account/resource pair. Recovery is explicit; passive inspection does not write state.

A workspace-wide 429 is sticky. It prevents all new admissions, blocks existing reservations and marks issued attempts uncertain. A late 429 is still accepted from a known token after the original attempt was already recorded or became uncertain. A stop cannot preempt an external call already invoked by a different process.

`record_stop(event)` accepts the exact stop record below. UNKNOWN requires an existing issued, uncertain or previously recorded attempt with exactly matching account, role and resource. A global 429 may be reported without an associated attempt. Stable event IDs are idempotent; reusing an ID with changed contents is rejected.

```json
{
  "schema": "keel.operational.stop.v1",
  "event_id": "stable-event-id",
  "workspace_id": "workspace",
  "kind": "RATE_429",
  "attempt_id": null,
  "account_id": "account",
  "role_id": "role",
  "resource_id": "native-form-resource",
  "observation_sha256": "<observed event digest>"
}
```

## Canonical delivery

Stops, local holds and an outbox entry are committed atomically. `flush_outbox(limit=16, lease_seconds=30)` calls only the installed sink. The sink must apply the stop to the real host controls, read the resulting state and deduplicate the same event ID/hash across crashes. The response must be:

```json
{
  "schema": "keel.operational.canonical-ack.v1",
  "workspace_id": "workspace",
  "event_id": "stable-event-id",
  "event_sha256": "<digest of exact stop record>",
  "status": "APPLIED",
  "canonical_record_sha256": "<digest bound to actual persisted canonical record>"
}
```

There is no public JSON acknowledgement endpoint. A wrong acknowledgement or callback failure leaves delivery pending and preserves the stop. A crash after the canonical write but before acknowledgement causes the same event to be delivered again after its delivery lease expires. This is at-least-once delivery with an idempotent sink, not exactly-once external execution. The recorded acknowledgement establishes that the installed callback reported persistence; it is not independent authentication of an unrelated system.

The sink runs inside the shared writer barrier, outside the ledger's database transaction. This prevents a maintenance checkpoint from overlapping a participating canonical store mutation without holding a SQLite lock across the sink callback.

## Coordinated checkpoints

Wrap every participating host store mutation, including calls to older Keel modules, in `with ledger.writer():`. Nested shared writer scopes are supported. `with ledger.maintenance() as checkpoint:` takes the exclusive barrier and yields a passive ledger checkpoint; it excludes participating local writers while a recovery tool inspects and copies the declared stores. `snapshot()` is read-only and safe inside that scope. Do not call ledger mutation methods while holding the exclusive maintenance barrier.

Backup tooling must capture the original database and key together. The lock has no semantic payload and can be recreated as a new private file in a restored destination. The restored ledger must be opened as a new object. Issued intents, pending outbox deliveries, revoked approvals and stops must retain their uncertainty or restrictions. A coherent local checkpoint does not snapshot an already-running external browser operation or prove every process participates.

Reports therefore state `consistency_scope: participating_writers_only`. Restore and receiving-host enrollment are separately validated by the recovery/runtime modules.

## Verified here

The guarded tests exercise concurrent admission and dispatch, passive reopening, actual child-process exit after committed dispatch, 429 propagation across objects, exact role/resource uncertainty, independent gate rejection, slow providers, time spent waiting for maintenance, false acknowledgements, callback failure, retry after crash, storage tampering, and exclusion of actual sink writes during maintenance. Fixtures make no real canonical host writes, model calls or native browser actions.
