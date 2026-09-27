# Measured public pipeline work

`productivity-once` connects Keel's existing supply report, fair source scheduler,
posting verifier, telemetry outbox and resource ledger. One invocation performs
at most one bounded public-read stage. It uses the existing workspace and real
queue writers. It requires Python 3.11+ and the standard library.

## Inspect before running

Use an initialized private workspace outside the source checkout:

```bash
python3 keel.py --home /path/to/private-workspace productivity-status
python3 keel.py --home /path/to/private-workspace productivity-once
```

Both commands inspect local state without public network reads. The second
command is a plan unless `--live` is supplied. The report distinguishes posting
presence, unanswered questions, holds and cooldowns; it never treats a queue
status as evidence of an application submission.

The operator and discovery CLI roles may run a cycle. Other review roles may
inspect status. These role names constrain CLI actions; they are not OS identities
or a sandbox against arbitrary Python code.

## Create one explicit budget

```bash
python3 -m keel_efficiency budget-create \
  --ledger /path/to/private-workspace/data/resources.sqlite3 \
  --scope public-pipeline-window-001 \
  --input fixtures/productivity-budget.json
```

The example allows 64 reader dispatches and 300,000 milliseconds of cumulative
client elapsed time. It enables no model tokens or paid-credit allowance. Review
these limits for your workload before creating the scope. Scope configuration
is immutable and restarting a process does not reset it. Reusing this ledger
and scope shares admission accounting with cooperating workers; child scopes
can share an existing parent allowance.

## Run a bounded cycle

```bash
python3 keel.py --home /path/to/private-workspace productivity-once \
  --live --run-id cycle-001 \
  --budget-ledger /path/to/private-workspace/data/resources.sqlite3 \
  --budget-scope public-pipeline-window-001 \
  --max-requests 8 --timeout 30 \
  --max-boards 2 --max-new 50 --verify-limit 100 \
  --target-verified 20 --backlog-limit 200
```

Register permitted boards with `source-add` first. Discovery honors the optional
`--title` and `--location` filters. These filters do not claim qualification or
change fit policy. A separate invocation may use a new run ID after inspecting
the previous result. Reusing the same run ID with the same settings replays its
receipt; changing the settings under that ID is rejected.

The scheduler works through actionable verification before adding new intake.
It pauses intake when fresh verified supply is sufficient, the backlog is full,
or remaining work depends on human input or cooldowns. Existing source fairness,
per-source backoff and host rate limits continue to apply. Repeated zero-progress
work receives a bounded cooldown. Current row state and authority are checked by
the existing commit gates; a historical scheduling result grants no authority.

`--target-verified` counts fresh posting-presence observations. Reaching the
target does not establish form completeness, eligibility, applicant approval,
or completed applications. Preparation still requires explicit role and material
selection through the existing preparation interface. An observation must match
the current exact posting identity; changing the target invalidates its old
presence evidence.

## Read the measurements correctly

| Stage | Recorded progress | Recorded resources |
| --- | --- | --- |
| Discovery | New deduplicated postings actually written to the queue | All reader dispatches and client elapsed milliseconds |
| Verification | Live posting-presence observations successfully committed | All reader dispatches and client elapsed milliseconds |

The verifier's `committed_verdicts` is populated only after successful per-file
writes. Observations lost to conflicts or failed writes cannot increase the
completion denominator. Failed work still contributes its known resource usage.
The two stage units are reported separately; they are not interchangeable
application completions or success probabilities. Zero-output and incomplete
measurements must not be presented as a successful efficiency ratio.

Reader dispatches exclude HTTP redirect hops and socket retries. The transport
retains its own bounds and rate-limit controls. Elapsed time is client wall time
for the measured work interval, excluding startup/history validation and final
receipt/accounting writes; it is not total command runtime, GPU usage, CPU time
or energy. Reservations govern admission; cooperative
deadlines do not forcibly stop arbitrary host callbacks. Usage exceeding a
reservation is recorded and locks the resource scope against further dispatch.

The controller invokes no model. It cannot read ChatGPT or Muse account billing,
and it does not invent a token-to-credit exchange rate. Actual credits and
credits per completion remain unavailable. A zero reserved credit allowance is
not evidence of zero unobserved billing. Unknown billing dimensions remain
unknown in the resource ledger even when the local work receipt is complete.

## Recovery and boundaries

The history under `data/productivity` records the selected work before
effects, then stores its result before settling resource usage. A durable result
can be settled after a restart without redoing the work. An interrupted run
without a durable result requires reconciliation; changing run IDs does not
authorize an automatic retry or refund its reservation. Run history is retained
to preserve replay protection. Version 0.6.1 uses indexed SQLite storage instead
of the 0.6.0 JSON journal's 256-run / 2 MiB lifetime limit. Reads are bounded while
old run IDs remain retained. Disk use grows with history; storage failure holds
new work. Changing the budget scope does not discard replay identities. See
[migration and bounded trials](SUSTAINED_OPERATION.md).

Inspect the run and its authoritative local stores before acknowledging an
uncertain recorded result. The operator-only command is:

```bash
python3 keel.py --home /path/to/private-workspace productivity-recover \
  --run-id cycle-001 \
  --budget-ledger /path/to/private-workspace/data/resources.sqlite3 \
  --budget-scope public-pipeline-window-001
```

A recorded result can finish settlement without another read. An explicit
acknowledgment of a recorded UNKNOWN result preserves its original receipt,
unknown progress and known usage; it permits later new cycles after local
reconciliation. Interrupted source scheduling retains its existing recovery
cooldown. If the ledger proves dispatch permission was never granted, recovery
cancels the intent and releases only its unused reservation. That run ID remains
terminal and cannot dispatch on replay. A dispatched intent without a durable
result stays held with its reservation; the command does not manufacture a
result or release uncertain consumption.

Budget scopes may roll over explicitly for a new accounting window. Supplying a
ledger and scope filters efficiency metrics to that accounting namespace;
unfiltered status lists the namespaces represented. Retained requests are checked
against the ledger before dispatch, so replacing or rolling back its database
cannot silently reset the recorded allowance. Returned hold statuses appear in
the JSON `status` field even when the CLI exits cleanly. Validation, capacity and
ledger-integrity failures instead emit error JSON on stderr with exit code 2.
Callers must check both the exit code and the result status.

Verification outbox recovery republishes stable event IDs and acknowledges
committed events. It does not guess application outcomes or clear unresolved
submission attempts. The private executor's ledger-to-queue reconciliation and
live Muse billing adapters remain outside this public integration.

Use one workspace per process. Legacy queue locks, event sinks and host cooldowns
bind to `KEEL_HOME`; the CLI sets it before loading the controller. Embedding code
must configure that environment before importing workspace-bound engines.

No daemon or cron job is installed. No applicant answers, attestations, approvals,
fit thresholds or application outcomes are inferred. No external submission is
exposed by this controller. Deployment on the live host and measured production
improvement require evidence from that host.
