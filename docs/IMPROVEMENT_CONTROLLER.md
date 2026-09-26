# Local statistical improvement controller

`keel_learning` implements the next statistical layer without paid APIs, model
calls, automatic applications, or schedule writes. It can propose a policy only;
existing permission, review, and execution gates remain authoritative.

## What is implemented

- `ExperimentRegistry`: persistent SQLite preregistration, a family confidence
  budget that survives restart, immutable plans, locally randomized decisions,
  immutable mature outcomes, actual human minutes, drift pins, and sequential
  evaluation. No retrospective action/propensity import endpoint is provided.
- `bounded_confidence_sequence`: a transparent Hoeffding/time-union sequence.
- `reliability_matrix`: empirical task-by-agent error, abstention, coverage,
  latency, and correction-cost cells. It reuses
  `keel_loki.calibration.binomial_upper`, rather than trusting model confidence.
- `propose_route`: builds the existing `keel_loki.routing.make_policy` format
  from admissible measured cells. It invokes no model and changes no route.
- `source_budget_proposal`: reuses `engines.source_feedback.propose`, preserves
  its source permissions/evidence/cooldown holds, and can preview an authenticated
  evaluated source policy. There is no schedule writer.

Run `python3 -B -m keel_learning capabilities` or `... demo`. The demo is marked
synthetic and cannot qualify a production proposal. Python APIs carry the host
callback contract; the CLI deliberately cannot manufacture trusted host proofs.

## Experiment contract and use

Construct a plan with schema `keel.learning.experiment.v1` and these fields:

| Field | Meaning |
|---|---|
| `experiment_id` | Unique preregistration ID; never overwrite or refit it |
| `bindings` | Exact `source_sha256`, `config_sha256`, `model_sha256`, `calibration_sha256` |
| `actions` | 2–32 named, permitted actions; source IDs for source experiments |
| `strata` | Frozen context buckets; each contains `logging`, `candidate`, `baseline`, `predictions` maps over every action |
| `objective` | Operator-defined bounded reward with fixed horizon and unit semantics |
| `predictor_frozen_at` | Must precede registration; host also verifies training history |
| `predictor_training_sha256` | Pin for the pre-experiment training inputs |
| `delta` | Error budget spent irreversibly from this registry's family budget |
| `min_samples`, `min_ess`, `min_improvement` | Preregistered adequacy and improvement requirements |
| `synthetic` | Synthetic plans are always held from production proposals |

All policy probabilities sum to exactly one as finite decimal rationals; their
common denominator must be at most 10^12. Predictions and rewards are in [0,1].
The predictor table is fixed before this experiment, not fitted against its
outcomes. This implementation intentionally chooses that defensible option over
claiming cross-fitting without managing disjoint training data.

The integration sequence is:

1. Create one canonical, host-private `ExperimentRegistry(absolute_db_path,
   validator=trusted_host_callback)`. The containing directory must be owner-controlled mode 0700; the DB and SQLite sidecars must be owner-controlled regular mode 0600 files. DB and parent inode changes are rejected.
2. `register(plan, proof=host_proof)` returns its SHA-256 pin. The callback must
   attest preregistration, predictor history, reward horizon, and eligible units.
3. `decide(..., expected_pin=pin, decision_id=..., unit_id=..., stratum=...,
   current_bindings=..., proof=...)` samples the frozen logging distribution,
   durably stores all selection probabilities, and returns the selected action.
4. The host independently gates and performs the approved action, if permitted.
   The returned decision is **not** permission to act. If action is not completed,
   keep the pending record; do not selectively delete failed assignments.
5. `record_outcome(..., reward=..., human_minutes=..., executed_action=...,
   observation_sha256=..., current_bindings=..., proof=...)` verifies the assigned
   action was followed and records an immutable, mature, authenticated outcome.
   Missing/unknown effort is rejected; measured zero is allowed.
6. `evaluate(..., expected_pin=pin, current_bindings=..., proof=...)` recomputes
   the report. Incomplete outcomes hold the entire proposal. Statistics use only
   the complete prefix in original assignment order, never the faster later
   outcomes. Changed bindings and failed host attestations block promotion.

No production caller may override randomness. Deterministic `synthetic_draw` is
available only in explicitly synthetic plans. The DB's chronological insertion
order enforces that a recorded decision precedes its outcome; this alone does
not prove when a person actually learned an outcome. The host must verify that.
Evaluation attestations bind the exact read snapshot hash, observation hashes, current bindings, and evaluation timestamp; the same snapshot is then evaluated. Matrix attestations bind trial hashes, current bindings, and timestamp; source allocation additionally binds the budget and proposal timestamp. The host must reject expired/replayed proofs and check current revocations. All callback `required_assertions` are part of its contract. A callback that
returns `True` for arbitrary user JSON provides no authentication.

Repeated unit/cluster IDs are rejected within an experiment. A repeated receipt
hash holds evaluation. The host must supply honest cluster identities; renaming
a company/person does not make their observations independent. The local DB is
trusted state, not a tamper-proof ledger. Protect it against other processes and
use one canonical instance. Creating fresh registries to reset spent alpha
invalidates any claimed global error control. SQLite locking handles concurrent
inserts, not hostile local filesystem mutation or distributed consensus.

## Estimator and uncertainty

For context stratum x, action a, logged probability p, reward r, fixed reward
prediction m, and target policy pi, each record contributes

    DR(pi) = sum_b pi(b|x) m(x,b) + pi(a|x)/p(a|x) * [r - m(x,a)].

The evaluator reports candidate and baseline means, and their **paired**
difference. It does not clip individual DR contributions or importance weights:
that would change the estimator. A DR estimate can legitimately fall outside
[0,1] in finite samples. Lack of action overlap yields HOLD, not a model-only
causal claim. Weights above 10,000 are held. Effective sample size is reported as
(sum w)^2/sum(w^2) separately for both policies; this is a weight-concentration
diagnostic, not proof of independence or a count of independent samples.

For D = DR(candidate)-DR(baseline), define the preregistered bound

    W = max_{stratum,action} |candidate-baseline| / logging_probability.

Then D lies in [-1-W, 1+W]. At every n, the implementation allocates
`delta_n = delta/[n(n+1)]` and uses the two-sided Hoeffding radius

    (upper-lower) * sqrt(log(2*n*(n+1)/delta)/(2*n)).

Since sum_n 1/[n(n+1)] = 1, union bounding all n gives simultaneous coverage.
The conditional Hoeffding/martingale argument requires bounded adapted
observations and correct logging randomization. The covered target is the
**running average of conditional policy reward differences**. Under constant
conditional means it also covers a fixed policy-value difference. Repeated
looks and optional stopping are allowed under these assumptions. It is a
conservative union-bound sequence, not the tighter mixture or empirical
Bernstein method from recent papers. The docstring and report name say so.

Known propensities plus correct action execution make the DR term unbiased
without requiring correct reward predictions, under consistency and no
unlogged/outcome-dependent enrollment. An unmeasured choice that overrides the
random action breaks that claim. Doubly robust mathematics does not repair
fabricated probabilities, selective missing outcomes, changing reward horizons,
unlogged decisions, interfering units, or labels seen during predictor fitting.
Fixed tables, action checks, complete-prefix evaluation, and host attestations
reduce those risks; they cannot independently prove all external assumptions.

Human minutes are measured separately. No causal reward-per-minute ratio or
future distribution-shift guarantee is claimed. A positive interval alone is
insufficient: sample/ESS, overlap, complete outcomes, current pins, nonsynthetic
provenance, and host attestations must pass before `PROPOSAL_ONLY` is returned.

## Agent/task matrix and routing

`matrix_plan` freezes an explicit agent configuration/model identity roster,
task family, source and calibration pins, risk limit, minimum coverage, minimum
trials, and delta. `reliability_matrix` consumes labeled trials with exact pins,
unit IDs, observation/label hashes, observed PASS/FAIL/ABSTAIN/ERROR, expected
PASS/FAIL, measured latency, and human correction minutes. It requires a host
validator to attest frozen plans, authentic held-out labels, complete ordered
trial streams, stable populations, and independent units within each cell.

For every frozen cell, half its family-adjusted alpha controls answered-case
error and half controls coverage. It allocates alpha over the answered-count
index for error and total-count index for coverage, using 1/[n(n+1)]. The
existing exact one-sided binomial bound supplies each endpoint. This needs IID
units within a cell and a fixed agent/abstention rule. Pairing the same units
across different agents is allowed: the family union bound does not assume
agents are independent. It does not prove their errors are uncorrelated.

Runtime errors reduce coverage, abstentions are counted, and no answered trials
means error upper=1. Drift and inadequate/error-prone evidence hold the route.
Among eligible cells, routing prioritizes measured human correction minutes,
then latency, then risk bound. These cost tie-breakers are descriptive, not
confidence bounds. The route uses the existing Loki loopback configuration,
permissions and budget path when separately reviewed and installed by the host.
Matrix plans also need a host-wide multiple-experiment alpha budget if the host
registers more than one matrix; this module's individual matrix delta does not
cover an unlimited search across newly invented families.

## Source feedback adapter

Source allocation requires an authentic registry evaluation, the exact plan
pin, exact action/source IDs, stratum, and a host assertion that the experimental
unit really matches an allocated minute. Otherwise it returns HOLD and zero
minutes. It preserves source-feedback permission, maturity, cooldown, missing
measurement and attribution checks. A passing report multiplies candidate
probabilities by the budget, rounds down, respects each source cap, and leaves
the remainder unallocated. Rounding/capping changes the finite allocation, so
it is explicitly a preview and does not claim that exact allocation inherits a
causal guarantee. Existing schedules and agent permissions are untouched.

## Verification and references

Tests include analytical DR values, incorrect predictors with correct logged
propensities, missing probability entries, no overlap, future-frozen predictors,
immutable outcomes, cluster repetition, unknown human effort, invalid numbers,
randomness overrides, restart/alpha persistence, drift, late-outcome selection,
all-failure data, matrix abstention/error bounds and local route generation.
The optional-stopping simulation is a seeded regression check; the mathematical
coverage argument is the stated Hoeffding/union-bound proof, not the simulation.

Primary references checked during implementation:

- Dudik, Langford & Li (ICML 2011), [Doubly Robust Policy Evaluation and Learning](https://icml.cc/2011/papers/554_icmlpaper.pdf).
- Howard, Ramdas, McAuliffe & Sekhon (2021), [Time-uniform, nonparametric, nonasymptotic confidence sequences](https://arxiv.org/abs/1810.08240).
- Howard et al., [research implementation of confidence sequences](https://github.com/gostevehoward/confseq). This module does not import it or claim to implement its tighter boundaries.
