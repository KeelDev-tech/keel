# Bounded operation and measured trials

Keel 0.6.1 extends the public productivity controller with indexed replay
history, fixed trial cohorts and an exact-attempt receipt projection interface.
It requires Python 3.11+ and the standard library. It does not require a paid
service, start a daemon or submit applications.

## Prepare the existing host

For the non-migrating `host-preflight` command, isolated rehearsal and a concrete
operator handoff, see [HOST_HANDOFF.md](HOST_HANDOFF.md).

Keep code separate from private workspace data. Back up the workspace and its
resource ledger together using a coherent snapshot while writers are stopped.
Confirm the host's queue writers cooperate with the same queue lock before
enabling any new writer. Do not copy personal data into the public checkout.
Stop workers running an older release before migration; do not run different
history formats against the same workspace or downgrade in place afterward.

Inspect the actual workspace and current allowance:

```bash
python3 keel.py --home /path/to/private-workspace productivity-status \
  --budget-ledger /path/to/private-workspace/data/resources.sqlite3 \
  --budget-scope public-pipeline-window-001
```

An existing resource ledger and operator-created scope are required for live
work. The commands never create a replacement allowance or automatically renew
an exhausted budget. See [the budget setup guide](PRODUCTIVITY.md).

## Run a fixed cohort

Preview first; without `--live` no public reads or trial history writes occur:

```bash
python3 keel.py --home /path/to/private-workspace productivity-trial \
  --trial-id pilot-001 --label candidate --max-cycles 3 \
  --budget-ledger /path/to/private-workspace/data/resources.sqlite3 \
  --budget-scope public-pipeline-window-001 \
  --max-requests 4 --timeout 20 --target-verified 20
```

Add `--live` to execute that bounded plan. The cohort has at most 100 cycles,
with a fixed identity and deterministic run IDs. Each cycle is subject to the
same shared resource allowance. Idle, held or uncertain work stops the runner;
there is no polling loop that spends requests waiting for cooldowns to end.

Reusing the trial ID resumes or replays the same declared cohort. Changing its
configuration or runtime build requires a new trial ID. Do not delete its
history to force a restart. Process termination does not authorize a repeated
dispatch or release an unknown reservation.

Inspect the evidence without making public reads:

```bash
python3 keel.py --home /path/to/private-workspace productivity-trial-report \
  --trial-id pilot-001 \
  --budget-ledger /path/to/private-workspace/data/resources.sqlite3 \
  --budget-scope public-pipeline-window-001

python3 keel.py --home /path/to/private-workspace productivity-compare \
  --baseline baseline-001 --candidate pilot-001 \
  --budget-ledger /path/to/private-workspace/data/resources.sqlite3 \
  --budget-scope public-pipeline-window-001
```

The baseline must be a real recorded cohort, not an estimated performance number.
Reports read the canonical cycle receipts and budget records. Labels such as
`baseline` and `candidate` are operator annotations, not evidence of quality.
The initial workload and runtime files are fingerprinted automatically. Do not
reset a live queue to manufacture a matching baseline.
The workload fingerprint covers queue documents, the application ledger and
source registry. It does not hold scheduler history, cooldowns, host contention
or external board contents constant.

Discovery and verification have different output units. Compare requests and
measured elapsed time per deduplicated posting added or committed live-presence
observation separately. Missing receipts, unknown progress, mismatched workloads
or inconsistent accounting cannot establish improvement. Even matching local
snapshots do not freeze external board contents: live comparisons are
observational, not proof of causation or market superiority. Credits remain
unobserved unless authoritative billing evidence exists elsewhere.

Reports use `measurement_status`: `COMPLETE` means all declared measurements
are present, `STOPPED` identifies a receipt-backed early stop, and `INCONCLUSIVE`
preserves missing or uncertain evidence. A budget refusal without a canonical
work receipt is inconclusive even if its local stop reason is known. These are
measurement states, not application-completion states. Check the command's exit
code as well as these fields.

## Preserve replay history

Indexed history replaces the 0.6.0 lifetime limit of 256 runs. Old identities and
receipts remain available for replay; they are not evicted when a new budget
window opens. Existing JSON history is migrated on a live operation. Status and
trial-reporting reads do not perform that migration.

The retained store is `data/productivity/history.sqlite3`, paired with its
`history.json` identity manifest. Migration preserves the original journal's
bytes in `journal.v1.json` and replaces `journal.json` with a history reference
that older workers reject. A missing database is an error, not permission to
reimport the old journal and forget newer work. Incomplete migration can resume
on an explicit live operation; read-only commands do not repair it implicitly.

History still consumes disk space. Storage exhaustion or corruption stops work;
there is no silent deletion or reset. Keep history and the resource ledger in
the same recovery plan. Ledger checkpoints detect replacement or rollback
relative to retained history, including divergent branches reusing an event
sequence. They do not defend against a privileged actor rewriting both stores
or restoring the entire workspace to an older coherent backup.

Uncertain dispatched work remains held. Use `productivity-recover` only after
examining the authoritative records. A successful recovery acknowledgment does
not rewrite original unknown progress or manufacture billing measurements.

## Project authenticated receipts

The public CLI provides an offline review path:

```bash
python3 keel.py --home /path/to/private-workspace receipt-review \
  --role-id synthetic-role --attempt-id synthetic-attempt \
  --receipts data/inbox/receipt-observations.json
```

The receipt store must already exist inside the workspace. This command has no
provider authentication adapter, so it cannot certify a receipt or update an
application outcome. Receipt JSON, email text and stored `verified` flags do not
grant authority.

A qualified host can call `receipt_projection.reconcile(..., live=True,
validators=...)` with application-owned Python validators implementing the
existing `ProviderValidation` contract. Each validator must authenticate the
provider evidence, account/recipient and acceptance of the exact application
and attempt. No validator is loaded from a CLI flag, JSON or an import string.

Projection requires matching queue, ledger, terminal intent and provider
evidence. Duplicate identities, conflicting receipts, a changed posting,
protected states or unresolved attempts remain held. The queue transition and
its publication marker are one atomic write; telemetry delivery uses a stable
event ID and can resume after failure. Approval, verification, lease and intent
records are not rewritten, and no retry is authorized.

Before activating this adapter, qualify the real host's paths, lock ordering,
ledger writer, receipt authentication and telemetry sink. Synthetic tests of the
public contract do not establish that the private executor is integrated.

## Accept or stop the trial

Keep a trial bounded until its records show useful output, stable recovery and
resource use within the shared allowance. Investigate any duplicate dispatch,
unexpected gate change, unexplained missing receipt or unknown commit outcome
before another live cohort. Compare against a recorded baseline where the
workload and measurement scope permit it; otherwise report the measurements
without claiming a gain.

This release provides the host-side code and checks. A live deployment and a
measured production improvement require evidence from that host.

## Reproduce the storage check

```bash
PYTHONPATH=. python3 tests/benchmark_productivity_history.py
```

The default gate runs 10,000 actual controller cycles with an empty synthetic
registry and no network or model calls. It checks retention of every run ID,
replay of the oldest ID without new dispatch, at most one cached run record per
cycle, pages of at most 100 receipts, and no historical request lookups during
new cycles. It reports disk growth and observed runtime without a hardware
speed threshold. This is a retained-history test, not a job-application
throughput benchmark. Migration, crash and receipt-projection regressions run
through the normal unittest suite.
