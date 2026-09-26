# Labelled local reviewer evaluation

`keel_eval` measures evidence-review outputs against an explicit labelled dataset.
It is additive: it does not issue approvals, change canonical state, run browser
actions, or weaken any existing gate. Python's standard library is sufficient.

The bundled nine-case dataset and transcript are **synthetic development
fixtures**. They exercise the scoring code with exact support, contradictions,
missing and conflicting evidence, incomplete claim coverage, unsupported
inference, and instructions embedded in evidence. A perfect replay score is a
test of the harness; it is **not** evidence of model quality or real inference.

From the release root, exercise the fixture with no network or model calls:

```sh
python -m keel_eval replay fixtures/grounding_eval/dataset.json fixtures/grounding_eval/replay.json --out /tmp/keel-eval-replay.json
```

Outputs are new private files (0600). An existing output path is refused. Exit 0
means the scoring operation completed without output errors, **not** that a model
meets an acceptance threshold. Exit 3 means the report contains errored cases;
exit 2 means invalid input/configuration or an unavailable output destination.
False passes and false blocks remain visible metrics, not automatic release gates.

## Dataset contract

The exact top-level keys are `schema` (`keel.eval.dataset.v1`), `dataset_id`,
`synthetic` (boolean), `split` (`development` or `held_out`), `label_source`, and
`cases`. Synthetic datasets declare `label_source: synthetic_fixture`; other
datasets declare `operator_supplied`. The harness does not authenticate labels
or establish that a declared held-out split is independent of training or tuning.

Each case has exactly `case_id`, `tags`, `expected_verdict`, `label_rationale`, and
`subject`. Gold verdicts are `PASS`, `FAIL`, or `ABSTAIN`. The subject has exactly:

- `required_claim_ids`: 1–32 unique IDs.
- `claims`: exactly those IDs, once each, as `{claim_id, text}` records.
- `evidence`: 0–32 unique `{evidence_id, text}` records.

Each prose item is at most 16,384 characters; each subject at most 256 KiB of
canonical UTF-8 JSON. There are 1–256 cases, a 2 MiB canonical document limit,
and bounded JSON depth/node counts. Inputs reject duplicate JSON keys, nonfinite
numbers, duplicate IDs, extra keys, and incomplete claim coverage. CLI input
reading uses the existing bounded local-file reader and rejects symlink paths.

The review rubric is: PASS only if all required claims are directly supported;
FAIL on an unambiguous contradiction; ABSTAIN for missing, insufficient,
ambiguous, or internally conflicting evidence when no unambiguous contradiction
establishes FAIL. Label rationales should apply this rubric and resolve expected
edge cases before a held-out run. Instructions in supplied prose are inert data.

Model requests contain only the subject, its digest, a phase identifier, and
review instructions. Case IDs, tags, labels, rationales, split, dataset ID, and the
label-bearing dataset digest are excluded. The source prose is intentionally
visible to the configured local model and should be suitable for that host.

## Replay contract and metric definitions

A replay has exactly `schema` (`keel.eval.replay.v1`), `dataset_sha256`,
`model_config`, `declared_weights_sha256` (lowercase SHA-256 or null), and
`results`. The dataset digest includes labels and rationales as well as subjects.
Each case must appear exactly once; missing, duplicate, and unknown IDs are
rejected. Replay result order can differ from dataset order.

A successful transcript row is `{case_id, assessment, latency_ms}`. An explicit
error row is `{case_id, error_code, latency_ms}`; its latency may be null if no
attempt was made. An assessment has exactly `verdict`, `covered_claim_ids`, and
`findings`, using the existing model-adapter contract. A malformed assessment is
an ERROR, never an inferred model ABSTAIN. Missing rows are not silently removed
from a denominator. Replay times are caller-supplied, not measured inference.

Reports include a confusion matrix with expected PASS/FAIL/ABSTAIN rows and
observed PASS/FAIL/ABSTAIN/ERROR columns. Every metric contains its numerator,
denominator, and fraction; a zero denominator yields null.

| Metric | Numerator | Denominator |
| --- | --- | --- |
| False pass on non-PASS labels | Observed PASS on gold FAIL or ABSTAIN | All gold FAIL or ABSTAIN cases |
| False pass among PASS predictions | Same false passes | All observed PASS cases |
| False block on PASS labels | Observed FAIL on gold PASS | All gold PASS cases |
| Withheld supported cases | Observed FAIL, ABSTAIN, or ERROR on gold PASS | All gold PASS cases |
| Abstentions | Valid observed ABSTAIN | All cases |
| Errors | Transport, protocol, explicit replay, or malformed assessment errors | All cases |
| Validated response coverage | Valid PASS, FAIL, or ABSTAIN | All cases |
| Exact label agreement | Observed verdict equals gold label | All cases |

An all-error run therefore has zero validated coverage and zero label agreement;
it cannot acquire an apparently perfect accuracy by dropping failures. A low
false-pass rate must be interpreted alongside coverage, abstentions, and the
class counts. Latency includes available errored-attempt times, reports missing
measurements, and uses nearest-rank p50/p95. Reports include per-case subject and
assessment digests, not source prose or generated findings.

## An explicitly requested local run

The operator must separately supply installed, licensed weights, a suitable
machine, and an already-running server. This package does not install/download
weights or contact a service by default. The local command uses the existing
`keel_agent.models` adapter: only literal `127.0.0.1` or `[::1]`, exact Ollama or
llama.cpp paths, direct bounded HTTP, no redirects, proxies, retries, cloud names,
tool calls, or fallback. An HTTP 429 stops subsequent cases; the report records
each remaining case as `ERROR/not_run_after_rate_limit`.

Create a private JSON configuration with all these keys, replacing the model
name with the exact already-installed name:

```json
{
  "reviewer_id": "operator-eval",
  "backend": "ollama",
  "endpoint": "http://127.0.0.1:11434/api/chat",
  "model": "REPLACE_WITH_INSTALLED_LOCAL_MODEL",
  "timeout_seconds": 60,
  "max_response_bytes": 262144,
  "max_tokens": 2048
}
```

For llama.cpp, use `backend: llama_cpp` and the exact configured loopback endpoint
ending `/v1/chat/completions`. Then explicitly request a run:

```sh
python -m keel_eval local /private/reviewed-held-out-dataset.json --config /private/local-model.json --out /private/keel-model-evaluation.json
```

Optionally supply `--weights-sha256` with an independently obtained file hash. The
report binds the full configuration and records that weight identity as an
**operator declaration**, not proof that the serving process loaded those bytes.
Loopback does not authenticate weights or constrain a server's downstream
behavior; any required server egress isolation belongs to the host deployment.

Python callers can use `evaluate_replay(dataset, replay)` or
`run_local(dataset, ReviewerConfig(...), declared_weights_sha256=None)`.
An explicitly injected transport is always reported as `INJECTED_TRANSPORT`,
with inference `NOT_RUN`, even if that callable delegates to HTTP. Tests use this
path. A normal local run is `LOOPBACK_MODEL_SERVER` / `LOOPBACK_REQUESTED`, with
attempted-call counts and exchange hashes. No mode attests model hardware,
generalization, dataset independence, or independent factual truth.

Use separately reviewed, representative held-out labels and explicit acceptance
criteria on the host to assess a real model. All reports retain
`model_quality_validated: false`, `quality_release_gate_satisfied: false`, and
`execution_authorized: false`; scoring alone cannot bypass Keel's existing human,
consent, evidence, route, or execution controls.
