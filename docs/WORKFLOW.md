# Keel 0.6 workflow controls

This release combines the two distinct 0.5 review candidates and implements
operating patterns from Trent Wade's other research and projects. It is an
offline, standard-library implementation with local SQLite persistence. No new
paid service, model download, provider account or external database is required.

## Implemented connections

| Prior work | Implementation | What is actually enforced |
|---|---|---|
| Secondary Adversarial AI Protocol v2 | `keel_workflow/reviews.py` | Immutable phase-A commitments precede any phase-B disclosure. Every expected reviewer commits before sealing. |
| SentryTest | `verification.py` | Evidence-aware results, expected-check coverage, dependency root findings, immutable snapshot validation and explicit cross-build comparisons. |
| MUSE | `integration.py` | Deterministic gates intersect assurance and trust, bind the exact application payload, project only relevant claim/evidence dependencies into review, and preserve external-action boundaries. |
| Wade Art Forge | `delivery.py` | Exact private attachment snapshots, scoped approval, destination/account binding, durable idempotency, no blind retries or fallback. |
| Purpose Publish | `recovery.py` and `demo.py` | Exact restored-object inventory, integrity/authorization/workflow checks and a real isolated SQLite backup/restore rehearsal. |

The files are implementation adaptations of these methods. They are not evidence
of improved reasoning accuracy, independent model behavior or live acceptance.
Historical projects, their operational data and their organizational identities
remain separate.

## Run the complete local example

```bash
python3 -B -m keel_workflow demo --out /tmp/keel-workflow-demo-new
```

Use a new output directory. The example prepares synthetic inputs, exercises the
actual reducers, persists blind reviews, records a clearly synthetic approval,
simulates delivery, checks restart/idempotency and uncertain outcomes, then backs
up and restores the SQLite stores. Evidence files record observed results.
`report.json` is the result, not a claim about the live pipeline. All reviewer
judgments in the demo are labeled fixtures; no model performed a real review.

Run the full existing and new regression suites:

```bash
python3 -B tools/run_release_checks.py --out /tmp/keel-workflow-checks-new
```

Free test dependencies are pinned in `requirements-dev.txt`. The guarded runner
disables network operations, ambient pytest plugins and writes outside the
fixture/report paths. It is a regression guard, not an OS security sandbox.

## Combined candidate contract

`evaluate_candidate(bundle, flow_export=..., assurance_export=...,
trust_export=..., now=...)` requires all three exports. The trust envelope must
contain the identical original flow snapshot. Assurance is evaluated against
that original snapshot; trust evaluates its own derived view. The role must pass
both views and both must be current. Existing dedupe, consent, policy, unanswered
question and uncertain-attempt holds cannot be cleared by a reviewer consensus.

The bundle's `content.application` has exactly `role_id`, `destination`,
`account_id`, `answers`, `claim_values`, and `attachments`. Its canonical digest
must equal the assurance action's payload digest. Every declared claim value
matches the corresponding assurance value hash. Every attachment is copied and
hashed, associated with a current trust artifact, and included in the role's
artifact dependency closure. `context_revisions` derives answer, fact, evidence
and policy hashes from the supplied inputs; changed inputs require a new bundle.

An artifact association is a trusted-host assertion. The code does not read a
PDF semantically, authenticate qualifications, or discover omitted claims. The
host must enumerate all material claims and validate the destination against the
real posting. It must supply complete, fresh canonical state and enforce these
same checks at its own live execution boundary.

The projected blind task omits prior assurance opinions and human judgments.
It includes the exact proposed application, relevant facts, the necessary
evidence/attachment lineage, and hashes of the full context. The projection does
not grant permission to disclose personal data to a model provider. The host
must apply its workspace and processor permissions before any such request.

## Persisted blind reviews

`ReviewStore(path)` creates a private durable database. The lifecycle is:

1. `create_round` binds a nonempty claim inventory, exact subject hash, at least
   two canonical reviewer IDs and an expiration of at most 900 seconds.
2. `phase_a` exposes only the task and that reviewer's own commitment status.
   `commit` stores a single immutable judgment and its digest for each reviewer.
3. `seal` requires every expected commitment. `phase_b` then exposes the sealed
   judgments and records each reviewer's access.
4. `audit` requires that reviewer's phase-B access. `evaluate` requires complete
   phase-A and phase-B PASS judgments, coverage and no findings to report
   `READY_FOR_HUMAN_REVIEW`. Disagreement, FAIL and ABSTAIN remain blockers.
5. `invalidate` irreversibly retires a round. Subject changes, expiry and clock
   rollback also block reuse. A new round cannot reuse an existing round ID.

IDs are supplied by the trusted host. Local file owners can bypass the API or
read SQLite directly. The host must authenticate identities, isolate reviewer
contexts and protect files; this code cannot prove outside-model blindness or
statistical independence. Commit hashes provide local consistency, not a public
timestamp authority or immutable storage against the file owner.

The underlying flow/assurance exports retain their **90-second freshness limit**.
A 900-second review expiration does not extend that window. Refreshing bound
context requires a new round. The live host must account for reviewer latency;
this release does not weaken freshness checks to make a slow review pass.

## Controlled local delivery simulation

`DeliveryStore` preserves exact bundle bytes and separate approval/attempt
metadata. `approve` binds the bundle, approver reference, authority context and
expiry. `revoke` blocks subsequent admission. The simulator supports only
`SIMULATE_SUBMISSION` and the built-in `SyntheticAdapter`.

Reservations are committed as `UNKNOWN` before completion. A crash therefore
cannot leave a reusable unrecorded reservation. Workspace plus role is the
duplicate scope across changed accounts, destinations, bundles and idempotency
keys. Matching retries return the existing attempt without another effect.
`UNKNOWN` and `SIMULATED_CONFIRMED` prevent fresh reservations for that role.
Only trusted reconciliation with an evidence reference can establish
`NOT_SENT`; absence of a receipt is insufficient. Terminal records cannot be
rewritten. Confirmed synthetic receipts bind the exact attempt, document,
workspace, role, destination, account and action.

`simulate_candidate` recomputes both branches and the blind subject immediately
before calling the simulator. It then checks the approved bundle, current input
revisions, account and authority reference. It performs no browser operation,
network request, real submission or actual consent decision. `submitted` and
`execution_authorized` remain false even after `SIMULATED_CONFIRMED`.

The delivery event outbox is workflow metadata for a trusted host to bridge into
its existing canonical logger. It is not a second source of application truth.
Real submission adapters, authenticated approval issuers, canonical event
consumption and provider receipt verification remain integration work.

## Verification and recovery

`aggregate_run(spec, observations)` requires a nonempty expected check graph,
exact run/build/scope matching and evidence references for PASS or FAIL.
Missing evidence becomes UNVERIFIED. A tool error stays TOOL_ERROR. Dependent
checks become BLOCKED and share the originating root finding; one failing
dependency does not manufacture multiple independent defects.

`compare_runs` rebuilds both snapshots before comparing them. Disappearing
failures are resolved only by an observed PASS; missing/blocked checks remain
unresolved. Builds must match by default. `allow_build_change=True` permits an
explicit comparison across builds only when the workflow scope and expected
check graph match. Both original snapshots and hashes are retained; causation
is not inferred from a changed result.

`evaluate_recovery` requires a nonempty expected object manifest, complete
observed inventory, exact object kinds/versions/digests, inventory evidence,
and all authorization/integrity/synthetic-workflow checks passing. The QA scope
binds both inventories. Healthy infrastructure with missing application objects
is NOT_READY. The demo performs an actual isolated local restoration; the
general evaluator receives trusted-host inventory and observations. No cloud
database recovery or real-account authorization test is implied.

## CLI integration

`python3 -B -m keel_workflow --help` lists all commands. `candidate` and
`review-create` consume a JSON file with `bundle`, `flow_export`,
`assurance_export`, `trust_export`. Bundle attachment values are relative file
paths inside that input file's directory. Absolute/escaping/symlink paths are
rejected. The sample builder `tools/make_workflow_demo.py` documents the Python
contract without any real applicant data.

Review commands are `review-create`, `review-task --phase A|B`, `review-commit`,
`review-seal`, `review-audit`, `review-status`, and `review-invalidate`.
`simulation-approve` and `simulate` explicitly concern local simulation.
`verify`, `compare`, and `recovery` evaluate supplied evidence; `compare` has an
explicit `--allow-build-change` option. `--now` defaults to the current aware
clock; fixed historical times are for synthetic fixtures only.

Exit 0 means the command completed with its documented positive local state,
not external-action authority. Exit 3 indicates a hold/incomplete result,
including UNKNOWN/NOT_SENT simulation outcomes. Exit 2 indicates invalid input
or an operational error. JSON outputs use exclusive creation and private 0600
permissions. Database files are private, persistent local regular files;
`:memory:` and symlinks are rejected. Protect parent directories and backups.

## Storage and remaining limits

SQLite transactions cover local state transitions. This is not distributed
exactly-once delivery and makes no guarantee for network filesystems, malicious
file owners or a host that bypasses the API. Backup and restore checks must be
repeated against the receiving environment. No self-modifying policies,
autonomous live deployment, new scheduled jobs or remote agents are installed.
State-of-the-art performance and scientific efficacy have not been measured.
