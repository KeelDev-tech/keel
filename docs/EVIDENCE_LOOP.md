# Diagnose measured work and qualify crash recovery

Keel 0.6.3 connects canonical trial evidence to a conservative follow-up proposal
and adds process-crash qualification for the actual public controller. Both run
locally with Python's standard library. No model, paid service, external
submission or expanded approval authority is required.

## Ask what the recorded work supports

On the host that retains the original workspace and shared resource ledger:

```bash
python3 keel.py --home /path/to/private-workspace productivity-advice \
  --trial-id pilot-001 --max-cycles 3 \
  --budget-ledger /path/to/private-workspace/data/resources.sqlite3 \
  --budget-scope public-pipeline-window-001
```

The advisor reads the immutable trial plan, canonical receipts, their accounting
bindings and current host observations. Uploaded reports and claimed successes
are not inputs. It distinguishes measured discovery and verification output,
incomplete evidence, recovery holds, idle reasons, cooldowns, budget limits and
zero measured yield. A zero-yield observation does not identify its root cause:
the public receipt does not retain enough per-source detail to blame duplicate
leads, a particular board or a filter.

The output uses three states:

| State | Meaning |
|---|---|
| `PROPOSAL` | Current observations support considering another bounded cohort. |
| `NO_SPEND` | The observed state calls for waiting, reviewing existing work or investigating evidence. |
| `HOLD` | Missing, inconsistent, changed or blocked evidence prevents a usable proposal. |

Exit code `0` means a proposal or no-spend diagnosis was produced; `1` means
HOLD, and `2` means invalid invocation. None of these grants permission to act.
The advisor does not reserve allowance, start a trial, poll a board, modify a
queue, clear a hold or renew a budget.

## Fit the next cohort within the observed allowance

The proposal preserves the prior work settings, filters and transport. It only
reduces the requested number of cycles to fit the remaining allowance. For
each resource, available allowance is the minimum across the selected scope and
all ancestors. The cycle count is bounded by:

```text
min(requested cycles,
    floor(available calls / calls reserved per cycle),
    floor(available compute_ms / compute_ms reserved per cycle))
```

The wall-time reservation is `ceil(timeout * 1000)`, matching the actual
controller. This is a bound on proposed reservations. Cooperative deadlines can
overrun, and the real ledger still records overages and blocks further work.
It is not CPU/GPU metering or proof of external credit savings.

Numeric settings and hashes of the exact private filters appear in the
proposal; labels, paths and filter text do not. A qualified host can retrieve
the original configuration from its canonical plan and verify `options_sha256`
before creating a separately authorized trial with a new ID. An injected test
transport remains injected and requires the same trusted in-process adapter;
it cannot be converted into public HTTPS work by accepting the proposal.

The advisor repeats bounded observations of the plan, receipts, selected
controller build files, preflight and ledger checkpoint. A detected change
discards the proposal. This does not establish a coherent cross-store snapshot,
whole-repository integrity, remote host authenticity or protection against a
privileged actor rewriting every store. The live command must recheck admission
and reserve allowance. Matching proposal settings do not establish a causal
comparison with an earlier trial: boards and workloads can change.

## Check the limits you actually intend to use

`host-preflight` now accepts the same targets and reservation limits as the
public controller:

```bash
python3 keel.py --home /path/to/private-workspace host-preflight \
  --budget-ledger /path/to/private-workspace/data/resources.sqlite3 \
  --budget-scope public-pipeline-window-001 \
  --target-verified 20 --backlog-limit 200 --max-requests 4 --timeout 20
```

`requested_cycle_estimate` and `requested_cycle_fits_observed_budget` describe
these values. The earlier `default_cycle_*` fields still describe the original
8-request/30-second defaults. Preflight does not validate every trial filter or
qualify the private executor. Read-only SQLite may use WAL coordination files.

## Kill and restart the real controller

Choose a new output directory under an existing real parent:

```bash
python3 tools/run_productivity_faultlab.py --out /tmp/keel-faultlab-001
```

The tool uses isolated Python processes, fixed synthetic inputs, denied socket
operations and real SIGKILL cuts. Fresh processes inspect the retained records
and replay each case. Fault injection is confined to the tool; production
modules have no kill switches.

| Durable state at the cut | Required restart behavior |
|---|---|
| Reservation exists; intent absent | Reuse that reservation for the same run ID without double charging. |
| Intent exists; dispatch absent | Explicit recovery cancels the request and releases the unused reservation. |
| Queue committed; result absent | Hold the uncertain run and keep its reservation; no second dispatch. |
| Result recorded; accounting unfinished | Replay completes accounting without another fetch. |
| Accounting recorded; history completion absent | Replay preserves accounting and completes history once. |

The report records actual child termination, retained state, durable synthetic
fetch counts and protected-row checks. Unknown billing remains unknown even
when measured public-call usage has been accounted for. This qualifies the
tested process-crash boundaries, not power-loss durability, arbitrary filesystem
failure, malicious host behavior or production throughput.

Run the normal tests and the fault tool after changes to the controller,
history or resource ledger. Use [HOST_HANDOFF.md](HOST_HANDOFF.md) for deployment
and [SUSTAINED_OPERATION.md](SUSTAINED_OPERATION.md) for bounded live trials.
Actual host performance and provider receipt authentication remain separate
qualification steps.
