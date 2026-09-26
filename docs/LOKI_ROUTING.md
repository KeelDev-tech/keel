# Local review routing and frozen risk selection

This addition implements bounded local review escalation and selection from a
predeclared finite family of score thresholds. Existing assurance metrics remain
unchanged. These modules produce recommendations and measurements; neither can
approve or submit an application, clear a hold, edit policy, or certify a model.
The implementations use the Python standard library and existing Keel modules.
No model, browser, service account or paid API is installed or required by demos.

## Routing API

```python
from keel_loki.routing import make_policy, digest, run_route

policy = make_policy(
    policy_id="review-v1", source_sha256=actual_source_inventory_sha256,
    small_config=installed_small_reviewer_config,
    strong_config=installed_strong_reviewer_config,
    max_model_calls=2, max_wall_seconds=180,
)
report = run_route(
    subject, policy, expected_policy_sha256=reviewed_policy_sha256,
    source_sha256=actual_source_inventory_sha256,
    permissions=authoritative_permission_snapshot,
    state_path="/absolute/private/existing-parent/route-state.json",
    allow_model_calls=False,
)
```

The independent reviewed policy digest must match `digest(policy)`. The caller
must compute the source inventory hash from the actual source and retain one
canonical state path for the whole policy session. The Python API checks these
declared pins; it does not authenticate their origin or discover live holds.
`ReviewerConfig` objects or their exact dictionaries are accepted. Configurations
retain Keel's literal-loopback addresses, installed-name restrictions, bounded
responses and deadlines. No cloud fallback, download, shell or tool execution is
implemented. "Small" and "strong" name stages; their relative quality is unproven.

Subject shape is the existing evidence-review contract: `required_claim_ids`,
`claims` with `claim_id`/`text`, and `evidence` with `evidence_id`/`text`.
The permissions dictionary has exactly:

| Key | Meaning |
|---|---|
| `ai_allowed` | Boolean from the existing trusted no-AI/unaided rule gate |
| `consent_for_model` | Boolean from the trusted model-consent gate |
| `holds` | List of current canonical hold IDs, including unresolved attempts |
| `permission_revision_sha256` | Revision digest for that permission snapshot |

Any hold or denied permission stops before a state write or model request.
Missing evidence produces deterministic ABSTAIN. Disabled/unconfigured models
produce a no-model ABSTAIN. Otherwise the small model reviews the original
evidence; a valid ABSTAIN alone escalates to the strong model. PASS, FAIL and ERROR
terminate the route. The stronger model receives the original subject, not the
first model's findings, and cannot promote those findings into source evidence.
Malformed responses, including attempts to add execution authority, are errors.
No score or natural-language request may choose an unpinned route.

The private regular state file records cumulative attempted calls, active elapsed
time, an in-flight reservation and a sticky rate-limit flag. Its parent must
already exist. Every path component is opened without following symlinks; the
file is held under a nonblocking exclusive advisory lock. Hard links and group/
world-readable files are refused. Each request reserves and fsyncs its attempt
before transport. HTTP 429 is persisted immediately and stops every later stage
and invocation sharing that state. An ambiguous transport error or interrupted
request keeps an unknown-attempt hold. Corrupt/truncated JSON, changed policy
pins, lock contention and unsafe files fail closed. There is no reset API.

An operator who deletes/replaces the state or switches paths can evade its
history; this module cannot prevent an administrator from modifying local files.
Use one authoritative path and do not interpret a new path as authorized reset.
The lock is Linux/POSIX advisory locking and creation uses `/proc/self/fd` for
descriptor-anchored paths compatible with the unchanged guarded test runner.
Idle time between invocations is not charged. Each unchanged model deadline must
fit the remaining cumulative active-time budget, including a final check before
transport. The standard transport enforces its deadline; injected callbacks are
trusted synchronous test code and cannot be forcibly preempted by this API.

The report lists actual visited stages and uses `NO_MODEL` for zero attempted
calls, `INJECTED` for a supplied test transport, and `LOCAL` for the existing
literal-loopback transport. No-model stage names remain explicit in `records`.
Every report has `execution_authorized=false`. A local endpoint is not an
attestation of installed weights, their license, or its downstream network use.
The module does not remove host inference, real browser or authentic-data gates.

## Calibration API and mathematical scope

`make_plan(config, *, source_sha256, thresholds, risk_limit, delta,
calibration_id, calibration_sha256, test_sha256)` freezes a JSON configuration,
source inventory, both dataset digests and 1–64 ascending unique thresholds.
`config` must name/pin the actual model, prompt, retrieval and score function used
to collect observations. A changed effective configuration requires new data.

`fit(calibration, test, plan, *, expected_plan_sha256, config, source_sha256)`
validates both frozen partitions, then selects using calibration observations
only. Test labels never choose a threshold. `evaluate(calibration, test, plan,
fitted, *, expected_plan_sha256, expected_fit_sha256, config, source_sha256)`
recomputes the fit and rejects altered summaries before scoring the pinned test
set. `invalidate(fitted, *, config, source_sha256)` reports stale bindings;
`PINS_MATCH` is consistency, not certification or continued distribution validity.

Each `keel.loki.calibration-data.v1` dataset has exactly `schema`, `dataset_id`,
`split` (`calibration` or `test`), `synthetic`, `label_source`, `config_sha256`,
`source_sha256` and `rows`. At most 2,000 rows and the inherited JSON byte/shape
limits apply. Real data requires the declaration `label_source="human_supplied"`;
fixtures use `synthetic_fixture`. Mixing these categories is rejected.

Each row contains exactly `case_id`, `group_id`, `subject` (the existing review
subject), `ranking_score` (finite 0–1), `observed_verdict` (PASS/FAIL/ABSTAIN/ERROR),
`expected_verdict` (PASS/FAIL/ABSTAIN), `observation_sha256`,
`label_revision_sha256` and `reviewer_id`. Repeated cases, groups, observation
hashes or model-visible subjects are rejected within a partition. Matching
case/group/observation identities or subjects across the partitions are rejected.
Renaming IDs and reordering claim/evidence records do not hide exact overlap.
Semantic overlap or actual statistical independence cannot be inferred from IDs.
Use one independently sampled unit per group, not multiple trials of one task.

The score is a caller-observed ranking feature, NOT a calibrated probability.
Adding a `confidence` field is rejected by the closed schema. Observation hashes,
reviewer names, labels and scores remain caller observations: this API does not
authenticate who labelled a case or establish truth. It cannot verify that a
threshold family was truly declared before anyone saw labels.

For each predeclared threshold, select rows with observed PASS and score at least
that threshold. Let n be selected cases and k be those whose human label is not
PASS. Compute the one-sided Clopper–Pearson upper bound U solving
`P[Binomial(n,U) <= k] = delta / number_of_thresholds`. For n=0 or k=n, U=1.
For zero errors, U=`1 - (delta / number_of_thresholds) ** (1/n)`. General bounds
use log-sum-exp binomial tails and conservative-endpoint bisection.

Under independent IID cases, reliable labels, a frozen score function and a
predeclared finite threshold family, the Bonferroni adjustment gives simultaneous
upper bounds with family error probability at most delta. Select maximum observed
coverage among thresholds whose upper bound is at most `risk_limit`; ties use
the lower threshold. If no threshold qualifies, report `INSUFFICIENT_EVIDENCE`.
This is finite-family binomial risk bounding, NOT conformal risk control, an
anytime-valid theorem, a per-person guarantee, or proof against arbitrary shift.

The calibrated risk denominator is selected PASS recommendations. Reports also
show unsupported PASS per all tasks and coverage; abstentions/errors remain in
the coverage denominator. Empty selection has no empirical-risk estimate.
Held-out evaluation is descriptive and can expose poor generalization even when
calibration selected a threshold. Synthetic and real labels remain distinct, and
all reports retain `deployment_validated=false` and `execution_authorized=false`.

Both modules expose `demo()` returning JSON-safe synthetic rehearsal reports.
Routing's demo uses temporary private state and injected responses; calibration's
demo performs arithmetic only. Neither is evidence of real model capability.
