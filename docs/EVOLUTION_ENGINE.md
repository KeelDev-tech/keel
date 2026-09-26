# Artifact-bound evolution (0.3.1)

The local engine records evidence, quarantines proposed lessons/procedures, executes paired repeated trials, and admits only a closed rule interpreter. It uses Python and SQLite without a paid service. Linux/WSL is required for private filesystem checks, file locking and worker resource limits.

## Run

From the extracted local package, choose a NEW directory under a private parent:

```bash
python3 -B -m keel_evolve workflow --home /tmp/keel-workflow-new
python3 -B -m keel_evolve faultlab
```

`demo --home ...` remains supported. The workflow exercises the actual discovery, verification and outbox pipeline with synthetic fixtures, records its result, proposes an explicit missing-evidence rule, runs repeated paired trials, executes that rule in simulation, and withdraws it after contradictory evidence. It does not train a model or infer arbitrary new code. The fault lab exercises controlled 401/429/timeout/deadline, event-directory sync failure, duplicate delivery and process-crash recovery. It performs no live service actions.

## Qualification contract

Register a held-out dataset BEFORE proposing its candidate. `freeze` returns a plan and the only supported baseline/candidate runners. `qualify` executes the complete hashed artifact body through the fixed interpreter in a bounded subprocess. Arbitrary runner callbacks, unsupported procedure steps and caller-declared pass counts are rejected. The baseline is a fixed PASS policy; this is not a competitive benchmark against another agent system.

A rule has exactly `path`, `equals`, `on_match`, and `otherwise`. Paths have 1–8 components; verdicts are PASS, FAIL or ABSTAIN. Missing paths abstain. Procedures must have exactly one `evaluate_rule` step. Qualification proves this narrow behavior on its tested distribution, not lesson prose, shell commands, browser behavior or general intelligence.

Promotion requires a nonsynthetic dataset, trusted host adjudication/independence attestation, at least 20 independent clusters, at least three executed repeats, positive conservative gain, no paired regressions and zero allowed false passes/errors/instability. Synthetic or unattested results cannot promote; the internal trial receipt can admit simulation only. `production_qualified` and `execution_authorized` remain false.

Exact subject hashes prevent duplicate subjects, development/holdout overlap, and relabelled reuse. A holdout is consumed by one candidate/plan binding, including failed attempts. Failure scenarios containing `fixture.subject` mark that subject as development. Semantic similarity, undisclosed prior exposure and false host attestations are outside this mechanism; the trusted host must enforce those independently.

## Retrieval and execution

Retrieval checks exact typed applicability, declared invalidators, linked evidence, body integrity, lifecycle and expiry. Default/max TTL is 24 hours. Reproposal does not renew expiry. Action contracts carry simulation status, full replay bindings, expiry and required state. Validation re-reads current lifecycle and rejects held, retired, expired or changed artifacts; callers must supply fresh target, evidence, authority revision and state.

`execute_review` performs only side-effect-free rule interpretation, with the final lifecycle check and interpretation serialized against withdrawal. It grants no browser, shell, network or financial authority. External action executors must provide their own fresh action gate; a returned contract status alone is never authorization.

## Limits and recovery

All ten engine data tables have a 4,096-row limit. Aggregate logical stored values are limited to 16 MiB; SQLite page count is capped at 16,384 (64 MiB with the default 4 KiB pages). Quota failures roll back the transaction. These bounds exclude temporary journal files and Python parent-process overhead; they are not a universal filesystem quota.

One evaluation worker per engine home is allowed. Each worker receives 3 CPU seconds, 512 MiB address space, 2 MiB file size, 64 file descriptors, no core dumps and an 8-second parent wall deadline. Input/output and receipts are size bounded. Unsupported platforms fail closed. This fixed-code worker is not a sandbox for arbitrary hostile code; the host code and same OS user remain trusted.

Contradictory/drift/runtime-failure feedback referencing same-family failed or unknown evidence immediately holds an artifact. `rollback` selects only an older, still-active promoted artifact of the same kind/family; it cannot revive a held, retired or expired version. This is rollback selection, not a deployment manager.

`backup` makes a private consistent SQLite snapshot with a digest. `restore` verifies it, creates a new private home, checks integrity and holds EVERY restored artifact for requalification. Legacy promotions also migrate to held. The backup is not encrypted and has no external anti-rollback anchor. No existing home is overwritten.

## Validation scope

Regression tests reproduce six released defects before the fix and pass after it. Additional tests cover actual repeats, binding, contamination, lifecycle, quotas, worker limits/failure, pure execution, backup/restore and real local pipeline/fault adapters. Release evidence reports the full matrix and pre-existing unavailable/private/live/host-dependent checks separately. No competitive-superiority, live-browser or production-readiness claim is established.
