# Process-crash qualification and batch commits

Run from the extracted local profile on Linux with Python 3.11+:

```sh
python3 -B -m keel_next qualify --home /tmp/keel-new-crash-qualification
```

The home must be new under an existing owner-controlled parent. This command
creates synthetic state and kills four child processes at fixed checkpoints.
It does not load user plans, plugins, credentials, models, or remote services.

Checks cover queued work before dispatch, uncertain work before and after a
local report is committed, and event replay after a durable three-record prefix.
Uncertain work remains UNKNOWN with its report withheld. Repeated restarts do
not automatically retry it. An existing qualification home is never overwritten.
Exit 0 means all scenario checks passed; exit 3 means a qualification failed.
A host/input exception is blocked with exit 2.

These are process-crash tests, not power-loss, kernel-crash, cross-machine,
security-isolation, or deployment certification. Runtime operation remains
local and bounded; this command does not authorize external actions.

## Durable event batches

The default pipeline outbox uses explicit event IDs in batches of at most 128
records and 2 MiB of normalized encoded JSON. One history scan and file fsync
serve a batch; parent-directory synchronization precedes acknowledgement.
Legacy and batch writers share the existing cooperative file lock. Conflicts
or malformed history fail closed. Receipts preserve original timestamps on
replay. Custom logger callbacks retain the single-event interface.

A failed write may leave a complete prefix: replay reuses it and appends missing
records. A torn line is held for investigation rather than automatically erased.
Batching does not claim multi-record transaction atomicity or universal
exactly-once execution. Encoded byte limits are not a process RSS limit.
