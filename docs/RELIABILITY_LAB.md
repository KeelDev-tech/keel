# Local reliability laboratory and implementation trace checking

These modules use Python's standard library and the existing Keel evaluator and
durable execution authority. They add no subscription, model download, provider
request, approval issuance, or deployment. A report's `QUALIFIED` status is scoped
to the frozen experiment and **never authorizes execution**.

## Run the synthetic demonstration

From the source root:

```bash
python3 -B -m keel_eval reliability-demo --out reliability-demo.json
python3 -B -m pytest --import-mode=importlib -p no:cacheprovider -q tests/test_reliability_lab.py tests/test_trace_conformance.py
```

The demo actually runs both Python callbacks on 24 artificial cases, three
repetitions and three evidence conditions. It is explicitly synthetic and remains
`PROPOSED`. It does not establish real model quality or production improvement.
The new modules require no third-party runtime packages; pytest is only a test
dependency. Process crash tests currently require a platform with `fork` (Linux
was tested). Native Windows execution of those tests is not qualified.

## Frozen paired trials

`keel_eval.reliability` reuses `keel_eval.evaluation.validate_dataset` and its
closed evidence rubric. The dataset must declare `split: held_out`. Each case has
an explicit cluster ID, adjudicator ID and adjudication record SHA-256. Real host
code must verify those references and that the cases were not used for tuning.
Calling a dataset “held out” does not establish its independence.

Define plain module-level trusted functions with this interface:

```python
def baseline(subject, config, seed):
    # Use a bounded local evaluator. Return exactly PASS, FAIL or ABSTAIN.
    ...

def candidate(subject, config, seed):
    ...
```

Build and run a plan through the Python API:

```python
from keel_eval.reliability import Runner, freeze_plan, run_paired
from keel_agent.io import write_private

old = Runner(baseline, baseline_config)
new = Runner(candidate, candidate_config)
plan = freeze_plan(
    dataset, baseline=old, candidate=new, adjudications=adjudications,
    repeats=3, faults=("none", "missing_evidence", "reversed_evidence"), seed=7,
)
write_private("frozen-plan.json", plan)
report = run_paired(
    plan, dataset, baseline=old, candidate=new,
    adjudication_validator=trusted_host_adjudication_validator,
)
write_private("paired-report.json", report)
```

This example depends on your actual dataset, module-level runners, configurations
and trusted host validator. It does not manufacture any of them. Omitting the
validator still runs the measurements but cannot produce `QUALIFIED`.

The validator receives private copies of the whole plan and dataset. It must
verify the pinned adjudications, retention permission, holdout separation, and
that distinct cluster IDs represent independent units. A callback returning a
constant `True` is only suitable for a labelled synthetic test. The plan digest
detects changed content relative to a saved digest; it is not a digital signature
or evidence that a human approved a newly rehashed plan.

Both runners receive fresh copies of the same subject/config and the same trial
seed. Order alternates. Labels, rationales, case IDs and provenance are withheld.
Missing-evidence trials have expected verdict `ABSTAIN` under the existing rubric;
reordering preserves the adjudicated label. The plan fixes these transformations,
repeat count, random seed and thresholds before execution.

Source-file, Python bytecode, default-argument, configuration, dataset and harness
hashes are checked. Closures are rejected: put adjustable state in the pinned
configuration. Pins are local provenance, not attestation of dependencies, global
state, model weights, hardware or a remote service. Bytecode pins are interpreter
and path specific; regenerating a plan on a different runtime is a new experiment.

The callbacks are trusted local code. This measuring harness does not sandbox
them or forcibly terminate a hung callback. Use the existing bounded local model
transport or a host worker with enforced deadlines and isolation. Never attach a
real submission handler to this evaluator.

Exceptions and invalid verdicts become `ERROR` with no exception prose retained.
All scheduled case/repeat/fault combinations must exist for both runners.
Failures, errors and abstentions remain in the denominators. Reports include
false passes, errors, abstentions, disagreement between repeats and the proportion
of case/fault pairs correct on every repeat. Missing non-PASS gold labels block
the false-pass gate instead of claiming a zero error rate.

Accuracy gain is averaged within each case and then within each declared cluster.
The one-sided Hoeffding lower bound uses **the number of independent clusters**,
not the number of repetitions. For cluster gains in `[-1, 1]` it is:

`lower = max(-1, mean_cluster_gain - sqrt(2 * log(1 / alpha) / cluster_count))`.

Its interpretation requires the host-attested independence and a preselected
experiment; optional stopping, post-hoc threshold selection and repeatedly trying
candidates are not covered. Default gates require at least 20 clusters, a
nonnegative gain lower bound, zero observed false passes, errors and instability.
Observed zero errors does **not** prove zero population risk. The bound addresses
paired accuracy gain only. Default abstention ceiling is 1 because justified
abstention is a correct result; a host may freeze a stricter ceiling.

Statuses are `BLOCKED` for failed metric gates; `PROPOSED` for a passing experiment
that lacks attestation, uses synthetic data, or is replayed; and `QUALIFIED` only
for observed nonsynthetic paired calls with all frozen gates and trusted
adjudication/independence attestation. None changes routing, budgets or approvals.

Replay a saved transcript without calling a model or runner:

```bash
python3 -B -m keel_eval reliability-replay dataset.json frozen-plan.json trials.json --out replay-report.json
```

`trials.json` is the report's `trials` array. Replays never qualify implementations.

## Failures become reviewed development cases

`import_regressions(records, dataset_id=..., adjudication_validator=...)` accepts
only the exact whitelist: case ID, fixed failure kind, expected verdict, an
explicitly sanitized closed subject, adjudicator ID, adjudication digest and
source-event digest. Extra fields such as raw logs, email envelopes or attachments
are rejected. Each record must explicitly declare `synthetic`; any synthetic
record makes the resulting dataset synthetic. Exception text is never
automatically retained. Imported plan files are fully revalidated for schema,
coverage and work bounds even if a caller recomputes their digest.

The host validator must verify the failure label and that the supplied sanitized
subject is permitted for retention. A field named `sanitized_subject` is not
proof of de-identification; this module does not claim arbitrary prose can be
reliably anonymized by regex. Imported cases always enter the **development**
split and cannot silently become an independent test set after the code learned
from them. Repeated failures are not invented or scraped into the data store.

## Real execution trace conformance

`keel_eval.trace_conformance` inspects the existing `SQLiteAuthority` used by
`DurableHostAdapter`. It compares committed event history to canonical attempt,
approval and budget rows in a consistent transaction. It also pins the source of
the durable adapter, boundary, Loki finite model and checker. These are concrete
checks of the shared approval/recovery contracts, **not** a proof that the full
Python implementation refines every transition in the abstract model.

Bind a trusted host handler with instrumentation:

```python
from security.execution import Boundary
from keel_eval.trace_conformance import TracedHandler, capture_trace, check_trace

traced = TracedHandler(authority, trusted_fixed_handler, handler_id="host-submit-v1")
boundary = Boundary(host_adapter, {"browser.submit": traced})
# The host still provides authenticated approvals, validators and real evidence.
# Existing explicitly authorized host code decides when to call boundary.execute.
trace = capture_trace(authority)
report = check_trace(trace)
```

The handler wrapper commits an entry probe before calling the fixed handler,
after requiring a matching durable `UNKNOWN` attempt and dispatch timestamp.
Duplicate probes are refused. A crash between probe commit and handler invocation
is conservatively an attempted entry; it does not establish an external effect.
All real dispatch paths must be instrumented to rely on invocation coverage.

The checker detects repeated reservation/dispatch/handler entries, missing or
mismatched approval bindings, dispatch outside approval lifetime, outcomes before
dispatch, illegal cancellation/recovery, event/row disagreement, orphan events,
dispatch-budget accounting errors and forgotten absolute 429 holds. A reduced
current budget may legitimately be below historical consumption.

Trace export omits payloads, destinations, operator identities and free-text
reasons. Join identifiers use stable SHA-256 pseudonyms; low-entropy identifiers
can still be guessed, so keep traces private. Snapshot collection uses the
authority transaction and advances its clock watermark. It does not consume
approvals or dispatch. The database owner can edit both rows and events; these
records are not tamper-proof external attestation.

Check an exported snapshot without opening a live authority:

```bash
python3 -B -m keel_eval trace-check implementation-trace.json --out conformance.json
```

`CONFORMANT` means the observed history satisfies these checks. Missing handler
probes and unresolved `UNKNOWN` counts remain visible. It does not verify real
receipt authentication, exclude hidden execution paths, authorize retry, or prove
exactly-once external side effects.

## Crash and concurrency evidence

The tests run the real `Boundary`, `DurableHostAdapter` and SQLite authority in
separate bounded child processes. They kill workers before reservation commit,
after reservation, before dispatch commit, after the durable UNKNOWN commit, and
inside a synthetic handler after an independently fsynced side-effect count.
They reopen the database and verify rollback or durable state, replay refusal,
approval consumption, evidence-gated recovery and the handler count. Four
concurrent workers also race for the same envelope; exactly one synthetic handler
is invoked. These test outcomes do not authenticate a production provider.
