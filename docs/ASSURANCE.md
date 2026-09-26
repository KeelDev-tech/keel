# Keel Cognitive Assurance Layer

Release: 0.5.0-review.1. Research-to-code integration; not a production safety
certification. All supported runtime components use Python's standard library.

## Research provenance and deliberate refinements

This implementation draws on the supplied 17-page *Artificial Intelligence as
a Fourth Cognitive Domain: A GAN-Inspired Framework for Externalized
Meta-Cognition and Reliability Preconditions* (July 2026 file), the one-page
*AI-Driven Fraud & Cognitive Risk* brief, the *Workflow Assessment: AI
Integration & Risk Evaluation* template, and the September 18 Keel mathematics
research. No claim is made that every supplied document is original empirical
research, peer-reviewed, published by IEEE, or a completed experiment. A filename
containing IEEE is not evidence of publication. The cognitive-domain paper
proposes an experiment; its hypotheses are not measured Keel results.

| Research concept | Implemented mechanism | Deliberate boundary |
|---|---|---|
| ECV critique loop, paper III–IV | Version/content-bound review records; dissent and abstention hold; maximum three iteration indices 0–2 | No GAN training or convergence guarantee; the next iteration is host-scheduled |
| Human override primacy | Explicit rejection blocks; scoped unexpired approval observation required for higher-risk actions | No override can supply absent facts or bypass canonical authority |
| Secondary adversarial validator | At least one reviewer pair differing in method, family and independence group | Registry metadata is an assertion, not statistical independence or a real model invocation |
| Source Grounding Index | Exact claim-to-scoped-fact hash match with current necessary dependencies | Traceability is not proof of semantic truth or complete extraction |
| Reliability/consensus score | Hard conjunctive gates and visible reasons | No weighted score purchases permission or outweighs contradictory evidence |
| Confidence calibration | Brier loss, equal-width ECE, reliability bins, coverage and Wilson intervals | Brier is not a pure calibration measure; no self-reported confidence becomes calibrated automatically |
| ECS | Explicitly versioned descriptive Keel adaptation | Paper's composite is not fully specified; these equal weights/components are engineering choices, not fitted research results |
| ACR | Ground-reference-backed wrong→correct fraction and harmful correct→wrong fraction | This is stricter than mere revision uptake; no causal effect inferred |
| Fraudulent digital twins | Minimal scoped fact projection; identity/field/value drift, contradictory canonical facts and cross-target reuse detection | Not a psychological model, impersonation detector or population profiling system |
| Accountability/sensitivity/complexity template | Five-dimensional risk routing, adding irreversibility and external impact | Risk scores come from trusted policy owners, not self-assigned model confidence |
| Dependency and discrete optimization research | Necessary-dependency DAG and exact bounded reviewer-subset selection | Not full formal verification, general proof search, neural policy learning or a distributed scheduler |

The main paper's ECS describes accuracy, calibration and hallucination
resistance. `keel.ecs.descriptive.v1` instead combines grounding, a supplied
calibration component and grounded correction rate for engineering telemetry.
It is intentionally not presented as that original scientific scale. Preserve
the individual components and method version when comparing experiments.

## Placement and compatibility

`keel_flow.board.build(snapshot, now=..., assurance=envelope)` first applies
the existing readiness, fit, hold and attempt-history gates. It verifies that
the envelope binds the exact snapshot digest and source revision, then binds
each action to the role's identity, lead bytes-as-canonical-JSON digest, and
packet dependency hash. Final diagnostic eligibility is:

`base_executable AND assurance_checks_passed`

Neither term grants execution authority. Outputs retain
`execution_authorized: false`. Held, duplicate, low-fit, stale and
unresolved-attempt records cannot become eligible through assurance. Missing
coverage blocks the uncovered role when an envelope is supplied. Unassured
future release members force an UNKNOWN forecast rather than inflated supply.
Global stale/partial exports suppress actionable discovery proposals.

When `assurance=None`, backwards-compatible diagnostic behavior remains, but
assurance is explicitly NOT_CONFIGURED and `assurance_qualified_ready=null`.
Deployment policy must require the envelope at the live integration boundary;
running an old CLI without it is NOT an assurance-enforced execution path.
The supplied code does not contain or modify the private live executor.

## Trust boundary

The layer consumes observations from the existing canonical stores. It does
not create a competing identity database, consent ledger, receipt authority,
queue, or scheduler. No canonical records are mutated.

The host must establish ALL of the following outside this reference evaluator:

1. Authenticate the exporter, review issuers and authorization/approval issuers.
2. Obtain current source revisions from authoritative stores, not model text.
3. Hash actual payload/attachment bytes and extract every material claim; verify
   that the exported claim inventory completely represents that payload.
4. Reconcile target, applicant, form, purpose and destination with the canonical
   application identity. In this schema `proposal.target` is exactly `role_id`.
5. Establish factual support and review validity, not merely accept a source URL.
6. Assign conservative risk and genuine reviewer metadata. Two personas on the
   same underlying model are not automatically independent witnesses.
7. Recheck the same bound versions under the existing launch lock immediately
   before an independently authorized side effect, then use existing canonical
   attempt ownership, UNKNOWN reconciliation and trusted provider receipts.

A fabricated JSON export can fabricate all supplied observations. Digests
detect mismatch against supplied bytes; they do not authenticate a publisher,
decrypt or verify source content, prevent same-principal rewriting, or prove
that a statement is true. Free-form claims omitted by an upstream extractor
cannot be detected by this hash-only projection. These adapter limitations are
release blockers for live enforcement, not hidden PASS conditions.

## Envelope contract (schema version 1)

Top-level fields are exact; missing/unknown keys are errors:

| Field | Meaning |
|---|---|
| schema_version | Integer 1; booleans are not integers |
| source_revision | Exact canonical export revision |
| export_sha256 | Canonical JSON digest of the complete `keel_flow` snapshot |
| observed_at | Aware ISO timestamp; age in [0, 90] seconds |
| complete | Explicit boolean; false blocks all actions |
| evidence | Necessary-dependency nodes, up to 4,096 |
| facts | Up to 4,096 scoped fact projections |
| reviewers | Up to 32 registry observations |
| actions | 1–256 unique role/application actions, at most 4,096 total claims |

The entire envelope is bounded to 8 MiB, depth 40, finite JSON values. Duplicate
JSON keys, duplicate identities, dangling graph references and cycles are errors.
An invalid contract raises ValueError; the CLI exits 2. Well-formed evidence
that is missing, stale, revoked, contradictory or incomplete yields visible
holds instead of an exception or permission.

### Evidence nodes

Exact fields: `id, revision, expected_revision, status, observed_at,
expires_at, parents`. Status is CURRENT, REVOKED or UNKNOWN. Every parent is
necessary. Validity requires CURRENT, matching revision, observation at or
before evaluation, evaluation strictly before expiry, and every parent valid.
`observed_at < expires_at` is required. Cycles cannot establish their own support.

The graph implementation validates all nodes, including disconnected ones,
using iterative topological traversal. Bounds: 4,096 nodes, 16,384 edges, depth
1,024. `evaluate_lineage(nodes, roots, now=...)` accepts up to 128 selected roots
for full ancestor-reason explanations and returns invalid IDs for the entire
graph. The core uses the all-graph validity set. `affected(nodes, changed_ids)`
returns the changed IDs and necessary-dependent closure, without mutating state.
Alternative sufficient proof paths are NOT modeled; use deliberate reevaluation
rather than pretending one necessary parent can silently substitute for another.

### Facts and claims

Fact fields: `fact_id, subject, field, value_sha256, purposes, targets,
evidence_roots`. Scope lists contain exact IDs, not wildcard permissions.
Claim fields: `claim_id, field, value_sha256, fact_id, evidence_roots`.

`proposal` contains `subject, purpose, target, payload_sha256, claims`. Each
claim must match its referenced fact's subject, field and value digest;
purpose/target must be admitted; source roots must match exactly and remain
current. Contradictory current scoped facts cause a hold, even if the proposal
selects only the favorable fact. Conflicting same-field values in one proposal
also hold. Zero claims cannot obtain a vacuous grounding score of 1.
Claim-to-fact matching is exact, not natural-language entailment.

Hashes can reveal low-entropy values through guessing. Minimize exports, keep
them private, apply access controls, and use opaque canonical IDs where suitable.
Do not publish applicant exports, review records or telemetry with the source.

### Action, receipts and review binding

Action fields: `role_id, application_id, lead_sha256, packet_dependency_hash,
proposal, risk, authority, human_review, iteration, reviews`.

Risk dimensions are integer 1–5: accountability, sensitivity, complexity,
irreversibility, external_impact. Any dimension >=4 is HUMAN_LED. Otherwise
any >=3 or external_impact >1 is HUMAN_REVIEW_REQUIRED. Remaining actions are
BOUNDED_LOCAL_CANDIDATE. This routing never permits external execution itself.

Authority and human review are null or exact objects:
`status, receipt_ref, scope_sha256, observed_at, expires_at, revision`.
Authority statuses: GRANTED/DENIED/UNKNOWN. Human statuses:
APPROVE/REJECT/PENDING. Use `proposal_scope(action)` to derive the bound scope
over the role/application, lead/packet, complete proposal, risk and policy
revision. It is a digest helper, not a receipt issuer. All actions require a
current GRANTED observation; non-low-risk actions additionally require a
matching current APPROVE observation. Any supplied human REJECT blocks, even
when the action would otherwise be low risk.

Use `review_subject(envelope, action)` for review binding. It covers the shared
evidence, facts, reviewer registry, source/export revision, observation/complete
state, and all action fields except review outputs. Editing evidence, risk,
payload, authority or iteration invalidates old reviews. Conservative full
snapshot binding may require re-review after unrelated export changes; a
future narrower proof projection must prove completeness before relaxing this.

Review fields: `reviewer_id, subject_sha256, verdict, confidence,
covered_claim_ids, evidence_roots, observed_at, expires_at, findings`.
Verdicts PASS/FAIL/ABSTAIN; confidence is null or finite [0,1]. Each review must
cover every claim and necessary evidence root. A review is stale after 900
seconds or at its explicit expiry, whichever comes first. Extra unresolved
findings, FAIL, ABSTAIN, incomplete coverage and dissent all hold. A reviewer
cannot make up an unregistered ID or submit multiple votes under one ID.

At least one valid reviewer pair must differ on method, family AND
independence_group after Unicode, case and whitespace normalization. Cosmetic
labels cannot create diversity. Checking these as separate marginal counts is insufficient:
a crossed group of correlated reviewers could satisfy each count without a
genuinely diverse pair. Even the pair constraint only checks supplied metadata;
it does not measure error correlation. Confidence spread is diagnostic only.

Iterations 0,1,2 are within budget. At 3 or above, require human escalation.
There is no hidden automatic recursive model loop or clock-reset bypass.

## Measurement definitions

`evaluate_predictions` accepts unique `{id, probability, outcome, abstained?}`
records; outcome is bool or null. Unknown labels remain counted but cannot
become successes or negatives. Brier/ECE use all labeled predictions, including
abstentions. Selective accuracy uses answered labeled predictions. Coverage,
unknown outcomes, bin counts and Wilson intervals accompany scores.

`Brier = mean((p-y)^2)`. ECE is the count-weighted average absolute difference
between mean probability and observed positive rate in equal-width bins.
ECE depends on binning/sample size. Brier also reflects discrimination and
outcome uncertainty, so lower Brier alone is not evidence of better calibration.
See the [official calibration documentation](https://scikit-learn.org/stable/modules/calibration.html).

`evaluate_challenges` accepts unique `{id, challenged, revised, before_correct,
after_correct, ground_truth_ref}` records. Only challenged records with both
labels and a ground-reference pointer enter the eligible set:

- ACR = wrong→correct / initially wrong eligible challenges.
- Harmful revision rate = correct→wrong / initially correct eligible challenges.
- Correction precision = wrong→correct / eligible revisions, including ineffective
  revisions in the denominator. Missing-reference and missing-label exclusions
  are visible; zero denominators yield null.

The ECS-v1 engineering composite is the explicitly reported positive weighted
mean of grounding, calibration quality and ACR; default weights are equal.
The metrics CLI uses `1 - ECE` as calibration quality. If any component is
missing, ECS is null, not a reweighted optimistic score. ECS is descriptive,
unvalidated, and cannot release a hold or select production thresholds.

Binary split-conformal sets use score `1 - probability(true class)` and rank
`ceil((n+1)*(1-alpha))`. If the rank exceeds n, both labels remain possible.
Unknown labels cannot be used for calibration. Training, calibration and test
IDs must not overlap; set outputs request abstention unless singleton. This
requires a fixed predictor and exchangeability for marginal coverage, neither
of which is established by the helper. It gives no per-case truth or conditional
coverage guarantee. See the [author tutorial](https://arxiv.org/abs/2107.07511).

The included synthetic examples demonstrate arithmetic and contract behavior,
not calibrated Keel predictions or successful user outcomes. Chronological
held-out validation, semantic deduplication, delayed outcome maturation and
independent labeling remain necessary for real learning.

## Review planning

`select_reviewers` enumerates all subsets of at most 16 supplied reviewers.
Require full capability coverage, an available diverse pair, cost within a
hard budget and modeled parallel latency within a hard limit. Among feasible
subsets, minimize cost, then latency, then IDs for reproducibility. Return
INFEASIBLE rather than weakening a constraint. Costs/capabilities/latencies
are host-supplied; parallel execution is assumed, not implemented or measured.
The planner chooses a proposed team; it never spawns agents or calls a service.

## Evaluation and live promotion

Run `python3 -B tools/run_release_checks.py --out /new/report/directory` with
the pinned free test requirements. The guard isolates runtime data, disables
ambient pytest plugins, and denies network APIs for the main suite. This is
not an operating-system sandbox or evidence that model outputs are correct.

Before enabling live enforcement: authenticated exports and issuers; actual
payload/attachment extraction; trustworthy reviewer adapters; canonical
pre-dispatch checks under lock; fault-injection recovery; and chronological
shadow data comparing useful completion, error, abstention, latency, operator
effort and cost. Require no authority/truthfulness regression. Test a strong
single-reviewer baseline against added review at equal workloads and budgets.
Do not claim state-of-the-art from a passing unit-test count.

The [NIST Generative AI Profile](https://www.nist.gov/publications/artificial-intelligence-risk-management-framework-generative-artificial-intelligence)
is an external risk-management reference, not certification of this code.

## Deliberate non-features

No live application submission, browser takeover, host model installation,
provider credential access, biometric profiling, autonomous consent inference,
fraud-scoring of real people, protected-service bypass, formal verification,
causal superiority result, novelty/patent opinion or paid fallback is included.
Existing source-rate limits, consent quarantine and canonical UNKNOWN holds
remain controlling.
