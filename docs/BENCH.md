# Keel 0.11: repeated local evaluation and host rehearsal

This release builds the infrastructure needed to measure the remaining gaps.
It does not establish state-of-the-art performance. Its measured workloads are
evidence-review judgments and a synthetic source-to-browser preparation slice.
Whole-application completion, human intervention, energy cost and competitive
superiority are not measured by these workloads.

## Repeated experiments

`keel_bench.experiment` freezes a plan containing the exact dataset digest,
source inventory digest, system configurations, declared weights identities,
trial count, random seed, call budget and wall-time budget. Model calls require
explicit opt-in. Only the existing literal-loopback transport is available;
there is no paid API, remote fallback, retry, model download or submission path.

Each case visits every system before moving to the next case. The fixed seed
shuffles case order and rotates system order across trials. Every scheduled
system × trial × case appears exactly once, including unattempted, failed and
malformed responses. A 429 stops later model calls across all systems and trials.
The original model deadline must fit the remaining wall budget immediately
before transport; the harness never silently changes a model's configuration.

Limits: 2–8 systems, 1–20 repeats, at most 4,096 total scheduled rows and model
calls, at most 256 dataset cases, and a wall budget of 1–86,400 seconds. The
source/dataset/config pins must match. In-process source hashes are trusted
caller declarations; the CLI actually hashes release files before and after
runs. A changed source quarantines the result and exits 4.

The built-in `abstain_baseline` deliberately abstains on every task. It exposes
the utility cost of always refusing to answer. It is a sanity baseline, not a
competitor or evidence that Keel outperforms a capable agent. For useful model
comparisons, configure distinct installed local models as additional systems.
Different names alone do not make identical configurations independent.

## Dataset and partition checks

The dataset uses the unchanged `keel.eval.dataset.v1` contract documented in
`GROUNDING_EVALUATION.md`. The model sees only required claims and supplied
evidence, not gold labels, rationales, case IDs, tags or split metadata.

`audit-partition` detects exact claim/evidence-content overlap between a
development and a declared held-out dataset, even when IDs or ordering change.
It reports repeated held-out subjects and missing label classes. No-overlap
does not prove independence: paraphrases, common underlying people/documents,
training contamination and label correctness require separate review. The
operator owns authentic labels and the choice to designate a set held-out.
Do not tune against held-out results and keep claiming it is untouched.

## Comparison reports

`analyze_run` reconstructs metrics from the supplied dataset, independently
pinned plan and every recorded trial. It rejects changed labels, mismatched
source/configuration pins, omitted/duplicate rows, schedule reordering,
inconsistent attempted-call counts and reports that contradict a 429 hard stop.
Input hashes establish consistency, not authentication of a report's author.

Reports include false PASS rates, false FAIL rates on supported claims,
withheld supported cases (including abstentions/errors), response coverage,
all-trials correctness per task, at-least-one-correct trial per task, and
latency with explicit missing measurements. Every rate includes its numerator
and denominator; zero denominators remain null. Errors never disappear from
accuracy denominators or become intentional model abstentions.

Pairwise exact-label agreement differences are aggregated per task. The 95%
bootstrap interval resamples whole task clusters, preserving each task's
repeated trials together. Seeds and resample counts are recorded. With one task,
no interval is emitted. These are exploratory estimates: correlated or narrow
tasks can yield misleadingly small intervals. Multiple comparisons, server
caching, shared weights, task dependence and hardware conditions remain
uncontrolled. No report automatically declares a winner, competitive superiority
or production readiness.

`BASELINE_ONLY` means model calls were disabled; those model rows are errors,
not observations about model quality. `INJECTED` means a supplied test callable
was used; it is never reported as real inference. `LOCAL_LOOPBACK` means the
configured local transport was selected. Attempt counts, validated responses
and errors remain separate. A local server response does not attest which
weights were loaded or constrain the server's own downstream network behavior.

## CLI workflow

All output paths must be new files under an existing private parent. Outputs
are mode0600 and are never overwritten. A systems file is a JSON array such as:

```json
[
  {"system_id":"abstain-baseline","kind":"abstain_baseline","model_config":null,"declared_weights_sha256":null},
  {"system_id":"local-candidate","kind":"local_model","declared_weights_sha256":null,
   "model_config":{"reviewer_id":"local-candidate","backend":"ollama",
     "endpoint":"http://127.0.0.1:11434/api/chat","model":"YOUR_INSTALLED_LOCAL_MODEL",
     "timeout_seconds":60,"max_response_bytes":262144,"max_tokens":2048}}
]
```

Use the actual installed model name and existing endpoint. Licensed weights and
hardware are supplied separately. Inspect your declared held-out set first:

```sh
python3 -B -m keel_bench audit-partition --development /private/development.json --heldout /private/heldout.json --out /private/partition-audit.json
python3 -B -m keel_bench plan --dataset /private/heldout.json --systems /private/systems.json --experiment-id local-eval-1 --trials 5 --seed 41 --max-model-calls 500 --max-wall-seconds 3600 --out /private/plan.json
```

Retain the plan digest emitted by the planner independently of the plan file.
Replace `PIN_FROM_PLANNER` below with that digest. Review the actual frozen
settings before enabling model calls; do not silently replace the pin after
a mismatch.

```sh
python3 -B -m keel_bench run --dataset /private/heldout.json --plan /private/plan.json --expected-plan-sha256 PIN_FROM_PLANNER --allow-model-calls --out /private/run.json
python3 -B -m keel_bench compare --dataset /private/heldout.json --plan /private/plan.json --expected-plan-sha256 PIN_FROM_PLANNER --run /private/run.json --out /private/comparison.json
```

Exit 0 means the requested operation completed, never release approval. Exit 3
means run errors, detected partition overlap or a partial/failed host rehearsal;
the report is still saved. Exit 2 means invalid input/configuration/output.
Exit 4 quarantines a run whose source changed.

For an offline, deliberately wrong scripted responder with no model/network:

```sh
python3 -B -m keel_bench demo --out /private/synthetic-demo.json
```

The demo uses nine synthetic cases, three trials, an always-abstain baseline and
an always-PASS scripted responder. It is designed to expose false approvals,
not to manufacture a perfect model score.

See `BENCH_HOST.md` for dependency inspection and the source → scoped answer →
packet → optional real local-model review → partial rendered readback rehearsal.
Each browser trial uses a fresh private directory, loopback fixture and browser
context. It never submits. Existing no-AI, consent, approval, hold and rate-limit
controls remain necessary for actual Keel operation.

## What remains external

Supply authentic authorized records, independent labels, licensed installed
weights and suitable hardware. Run actual inference and rendered rehearsal,
then develop a representative full-application benchmark with observed final
state, explicit human decisions and real intervention measurements. Compare
against capable alternatives under controlled conditions and have another
reviewer inspect failures. Adding this harness does not complete those runs.
