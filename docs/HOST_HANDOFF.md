# Qualify the existing host before a live trial

Keel 0.6.2 adds a non-migrating host check and an isolated productivity rehearsal.
They use Python 3.11+ and the standard library, without paid services. Neither
command deploys a private executor, authenticates provider receipts, submits an
application, or grants approval.

Version 0.6.3 adds [canonical trial advice and process-crash qualification](EVIDENCE_LOOP.md).
Preflight can inspect requested targets, request caps and deadlines; the default
values remain unchanged.

## Rehearse the public controller

From the reviewed checkout, select a **new** output directory:

```bash
python3 tools/run_productivity_rehearsal.py --out /tmp/keel-rehearsal-001
```

The tool creates synthetic data in that directory and exercises the actual
public discovery, verification, budget, trial and replay code. Board responses
are injected fixtures; socket operations are denied. Its report distinguishes
synthetic fetches from real network calls and checks that replay spends no new
allowance, a budget refusal prevents dispatch, and existing synthetic approval
and uncertainty holds survive. An existing output directory is refused.

This checks integration behavior on this machine. It does not measure real
board availability, model quality, production throughput or credit savings.

## Inspect the real host without repairing it

Keep the code checkout separate from the private workspace. On the actual host:

```bash
python3 keel.py --home /path/to/private-workspace host-preflight \
  --budget-ledger /path/to/private-workspace/data/resources.sqlite3 \
  --budget-scope public-pipeline-window-001
```

The command reads the existing public workspace documents, runtime bindings,
retained productivity history and shared budget. It reports counts and reason
codes without copying applicant documents, source names or filesystem paths.
It does not create an allowance, migrate history, repair state, contact a board,
or acquire a queue writer lock. SQLite may access or create its WAL coordination
sidecars; read-only means no application data, accounting or schema mutation,
not zero filesystem activity.

Exit code `0` means the observed local checks passed. Exit code `1` means a
local prerequisite is blocked; `2` indicates an invocation error. A local pass
is not permission to execute. The output separately preserves unknown host
qualifications, including worker shutdown, coherent backup, cooperating private
writers and provider authentication. These reads are not a coherent snapshot
across all stores; conditions may change immediately after inspection. Live
commands must recheck their own gates and reserve the actual shared allowance.

An absent, unrelated or old-schema budget ledger is never initialized or
migrated by inspection. Have the host operator validate the existing ledger
and its backup before any explicit writable migration. Do not replace it with
a fresh ledger or scope to get past a refusal. The same read-only ledger opening
is used for productivity status, trial reports, comparisons and previews.
Those existing commands retain their own documented lock-file behavior.

## Host operator handoff

1. Confirm the reviewed code version on the host. Stop older workers before
   any history migration. Preserve the workspace and resource ledger together
   using the host's coherent backup procedure. The Muse recovery/coordinator
   backup helper is **not** a full public-workspace backup.
2. Run the isolated rehearsal and `host-preflight`. Retain their reports locally.
   Resolve observed blockers using authoritative records; do not rewrite holds,
   approvals, uncertain requests or replay history to obtain a passing report.
3. Use the preview and bounded live-trial commands in
   [SUSTAINED_OPERATION.md](SUSTAINED_OPERATION.md). Start with at most three
   cycles and an existing operator-created allowance. Inspect the canonical
   report before another cohort. Stop on uncertain effects or unexplained
   accounting. Do not reset an exhausted allowance automatically.
4. Use a new trial ID when the runtime build changes. Existing trial reports
   remain evidence about their recorded build; the old cohort must not be
   restarted under new code or relabeled as a new measurement.
5. Keep receipt projection inactive until the host's provider validator,
   account/attempt binding, paths, lock cooperation and telemetry sink are
   qualified. `receipt-review` remains an offline review command with no
   provider authentication or write switch.

Return the code revision, sanitized check statuses, canonical trial report and
any unknowns. Never send applicant data, credentials, raw receipts or host keys
to the public repository. A production improvement requires evidence from that
host; an offline rehearsal alone cannot establish it.

## Recovery fixes included

A shared budget can lock after a trial has already finished. Replaying that
completed trial now preserves its original report instead of inserting a new
historical stop. A real overage on the trial's own request still stops work.

Receipt projection retains a known committed queue outcome if its later
telemetry acknowledgment cannot acquire a lock or read the queue. The durable
outbox remains pending for replay. If acknowledgment writing has begun and its
outcome is uncertain, the result remains `UNKNOWN`; no repeated submission is
authorized.
