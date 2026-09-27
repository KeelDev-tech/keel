# Keel credit efficiency — 0.5.0 candidate

This release implements local resource governance and efficiency primitives with Python 3.11+ and the standard library. It adds no paid service, API key requirement, model download, paid fallback, or automatic application submission.

## Start without a model

From the extracted source directory:

```bash
python -m keel_efficiency demo
python -m keel_efficiency benchmark
python -m keel_efficiency context --input fixtures/efficiency-context.json
python -m keel_efficiency review-plan --input fixtures/efficiency-review.json
```

The demo executes the real ledger, context selector, router, bandit, reuse store, graph planner and procedure registry on explicitly synthetic data. Its quality records are fixtures. The benchmark is a replay, not a live model comparison or proof of production savings.

## Set one shared budget

Create an operator-owned private directory outside the source tree, then create a scope once:

```bash
mkdir -m 700 /tmp/keel-budget-example
python -m keel_efficiency budget-create --ledger /tmp/keel-budget-example/resources.sqlite3 --scope session-001 --input fixtures/efficiency-budget.json
python -m keel_efficiency budget-show --ledger /tmp/keel-budget-example/resources.sqlite3 --scope session-001
```

Use a persistent operator-owned directory instead of /tmp for real operation. Scope configuration is immutable. Repeating the same creation is idempotent; changing its limits is rejected. Child scopes created with `--parent session-001` charge every ancestor atomically. Use one common root for all workers and separate child IDs for tasks or explicitly planned daily windows. Creating an independent root creates an independent allowance; it does not share a global cap. Dates never silently reset counters.

The local agent worker accepts `--budget-ledger PATH --budget-scope ID`. Without explicit configuration, LocalAgent and native Loki routing use a cumulative allowance beside their local state: 64 calls, 2,000,000 input tokens, 524,288 output tokens, 3,600,000 client elapsed milliseconds and zero external credit units. It does not reset at restart. Separate homes have separate defaults. For shared enforcement, pass the same ledger/root or configure both environment variables:

```bash
export KEEL_BUDGET_LEDGER=/tmp/keel-budget-example/resources.sqlite3
export KEEL_BUDGET_SCOPE=session-001
```

The environment boundary covers Keel's exported loopback transport, including local evaluation and lab calls. `keel_eval local` requires `--budget-ledger` and `--budget-scope`; `keel_loki lab/route --allow-model-calls` require them too. Explicit host-supplied ledger objects take precedence. Incomplete or empty configuration fails before dispatch. Low-level compatibility calls without any budget configuration remain explicitly ungoverned; this is not an OS-wide spending firewall. A custom transport is trusted host code and can bypass Python controls if it deliberately avoids the boundary.

## Durable accounting

`ResourceLedger` reserves an integer resource vector in one SQLite transaction, then grants exactly one dispatch. Lifecycle: RESERVED → DISPATCHED → SETTLED or UNKNOWN. A cancelled request releases budget only before dispatch. Missing token counters, invalid usage and interrupted calls retain unresolved reservations. Reconciliation can fill unknown dimensions; it cannot rewrite previously recorded usage. A crash after dispatch cannot refund the reservation automatically.

Actual usage above an estimate is recorded truthfully and locks the scope and its ancestors against further dispatch. Reservations are admission estimates, not server-side tokenizer or physical-compute limits. The transport separately enforces the requested output cap and existing network deadline. Local servers may continue work after the client disconnects.

Usage fields:

| Field | Meaning |
| --- | --- |
| calls | Dispatched local requests; failed responses count |
| input_tokens / output_tokens | Backend-reported, unverified counts; missing means unknown |
| cached_input_tokens / reasoning_output_tokens | Informational subsets when reported; never charged twice |
| compute_ms | Client elapsed wall time, not GPU time or energy |
| external_credit_micros | Provider-defined millionths of credits; no guessed token conversion |

Loopback adapters record zero external API credit consumption for the local path, with billing unattested. They cannot observe a local server's downstream paid calls. Host egress controls are still needed for strict offline operation. No ChatGPT/Muse billing adapter or account-credit control is claimed.

`budget-reconcile --ledger PATH --request ID --input usage.json` accepts known resource dimensions. Only an operator with authoritative evidence should reconcile; estimates are not receipts. Ledger files and their directories must remain operator-owned. SQLite is local shared storage, not a distributed consensus service or network-filesystem deployment.

## Capability APIs

| Module | Working capability |
| --- | --- |
| ledger.py | Hierarchical atomic reservations, settlement, recovery, unknown holds |
| usage.py / transport.py | Ollama and llama.cpp usage adapters, bound request admission, nested-call fencing |
| policy.py | Cheapest qualified local route, finite-family quality bounds, shadow LinUCB, mandatory review planning, bounded multiple-choice knapsack |
| execution.py | Actual governed low-risk preparation through the qualified router; no approval capability |
| context.py | Revision-bound skill/persona/evidence selection and structured handoffs under exact serialized byte limits |
| reuse.py | Registered pure-work single-flight leases, exact artifact reuse, bounded retention, incremental DAG planning |
| procedures.py | Data-only trace compilation, fixed held-out qualification, revocation and expiry |
| benchmark.py | Matched-cohort replay with host validation and resources per verified successful task |

Use `python -m keel_efficiency --help` for planning, registry, budget and demo commands. API signatures and JSON contracts are in module docstrings. All plans return execution_authorized=false. Real authorization is rechecked by existing Keel gates.

## How adaptation is bounded

Routing requires current model/source/policy fingerprints and independently supplied, fixed held-out task outcomes. Its finite-family Hoeffding bounds require the declared IID assumptions; the code cannot authenticate those assumptions. LinUCB uses bounded features, ridge regression and a Cholesky solve; replay reconstructs its observations. Learned choices stay in shadow mode. A host must qualify a new policy before routing with it. Batch allocation optimizes caller-assigned integer utility under declared costs; actual dispatch still reserves measured resource dimensions.

The review planner always retains the mandatory two-or-more reviewer roster and budgets both review phases. Additional reviewers depend on certified empirical disagreement and available budget. It never edits an existing signed roster or weakens an existing review. A changed roster requires a new bound review subject. There is no claim that more reviewers guarantee greater accuracy.

Required constraints and dependency closures are indivisible context. If they do not fit, assembly holds. Optional selection uses deterministic lexical relevance, not a trained semantic retriever. UTF-8 byte token allowances are conservative heuristics, not exact tokenizer outputs. Handoffs retain caller-supplied concise findings, constraints and revision references without automatic lossy summarization.

Only trusted registered pure preparation/analysis/test operations may use single-flight caching. Exact keys bind account, scope, implementation, model settings, source, policy, inputs and dependency revisions. Hits are inert artifacts, never approvals. Lease expiry can overlap a stalled pure computation; fencing prevents a stale worker from publishing over its replacement. Unchanged DAG nodes are reuse candidates and still need a valid current artifact. Semantic similarity never reuses an authorization.

Procedures contain data-only steps and evidence references. They do not execute generated code. Qualification policy is frozen when the candidate is compiled. Training and held-out task IDs cannot overlap within the registry scope; one fixed evaluation trial is allowed. An exact one-sided binomial error bound and any unsafe outcome govern qualification. Multiple candidate selection needs a separately planned statistical error budget; per-candidate bounds do not establish portfolio-wide guarantees. Source/version changes, revocation, expiry, or observed clock regression block reuse.

## Validation and release boundaries

Read the accompanying handoff for exact final test counts and inherited baseline failures. Synthetic protocol tests establish behavior under fixtures; live model quality, real credit savings, distributed deployment and production readiness are not established by them. External approvals and submission restrictions remain unchanged.

Primary format references checked during implementation:
- https://github.com/ollama/ollama/blob/main/docs/api/usage.mdx
- https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md

Research basis for optional learned routing and compute allocation:
- https://arxiv.org/abs/2508.21141
- https://arxiv.org/abs/2604.14853
