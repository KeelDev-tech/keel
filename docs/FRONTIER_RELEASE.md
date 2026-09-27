# Keel 0.4.0: fair intake, bounded skill ancestry and better evidence

This release adds local, standard-library capabilities without paid services.
Its goal is measured progress in reliable intake and safe reuse. It does not
establish market leadership or production readiness.

## Run the integrated demonstration

```sh
python3 -B -m keel_next frontier --home /tmp/keel-frontier-new
```

Use a NEW home on Linux/WSL. The demonstration runs the actual pipeline with
socket-denied source fixtures, two artifact-bound simulation experiments and
a balanced model-response protocol benchmark. It never submits an application
or grants execution authority. Read SOURCE_SCHEDULER.md, EVOLUTION_ENGINE.md and
evaluation-frontier.md for the individual APIs and their limits.

## Useful intake under a finite budget

The previous all-source discovery method must finish its source set before
committing intake. Its default 50-request budget cannot finish a 64-board
fixture: it uses 50 requests and admits zero postings. The new scheduler
completes four independent batches of 16 boards, uses 64 requests in total,
and admits all 64 postings. This demonstrates bounded progress, not a
same-request-budget speedup. A replay admits zero duplicates. Every posting
remains parked for verification.

The controller persists fair source turns, per-source backoff, rate-limit holds
and interrupted-run status. A real SIGKILL after queue commit is detected on
restart; explicit reconciliation preserves the unknown old outcome, applies a
cooldown, and lets exact-identity deduplication prevent duplicate intake.

## Skill ancestry

`EvolutionEngine.derive(new_id, source_id, evidence_ids, additional_parents=...)`
creates a CANDIDATE with immutable parent IDs and body hashes. At most eight
direct parents and 64 total ancestry nodes are allowed. Every parent must be
usable and of the same kind/task family. Lesson applicability is intersected,
invalidators combined; incompatible conditions are rejected. Procedure state
and input requirements must match, and validator references are combined.
The child copies one source behavior; it does not synthesize or merge arbitrary
programs. An optional replacement rule still uses the closed interpreter.

Parent retirement, contradiction, expiry or a changed body blocks descendants.
Usable corrupt/expired roots and affected descendants are held on subsequent
engine transactions; use also revalidates the complete pinned ancestry. A bad
child pin does not withdraw a healthy sibling. Promotion checks require every
ancestor to be promoted in the same transaction as the child's promotion.
A dependency graph can block future use; it cannot erase already produced
external artifacts or prove semantic correctness of generated knowledge.

## Evaluation that exposes trivial policies

The benchmark executes 18 synthetic protocol cases in six families: transport,
model identity, claim coverage, findings consistency, attempted tool calls and
strict JSON. Valid, invalid and missing cases test PASS, FAIL and ABSTAIN. It
executes the actual existing response validator and two deliberately weak
constant-answer baselines, without contacting a model server.

Reports include confusion matrices, per-label/family recall, unnecessary
abstention, unsafe PASS rates and conservative simultaneous cluster bounds.
Repeated views count within their original cluster, not as new independent
evidence. Default execution makes 432 calls but has only six synthetic template
clusters. The public synthetic suite is diagnostic, never a promotion holdout.
The report does not infer that structurally valid model content is true.

## Confirmed defects repaired

- Contradiction withdrawal, retirement and rollback can still hold an existing
  artifact when application storage quotas prevent detailed audit writes.
  The response explicitly reports omitted detail. Physical disk failure remains
  a separate limitation.
- A restored held artifact can be forked to a fresh CANDIDATE with a new identity,
  new positive evidence and a newly registered unused holdout. Old qualification
  receipts are not reused; dependencies remain binding.
- Backups reject unsafe writable ancestors. Backup metadata reads are bounded.
- Raw as well as wrapped failure subjects are registered as development data,
  preventing exact relabelling into held-out qualification inputs.

## Research informing this implementation

The following informed design choices; this release does not reproduce their
systems or claim their measured results:

1. Anthropic, *Demystifying evals for AI agents* (2026): balanced tasks, actual
   outcomes, repeat reliability and isolated evaluation environments.
   https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents
2. Anthropic, *Quantifying infrastructure noise in agentic coding evals* (2026):
   record resource configuration and separate infrastructure failures from
   capability conclusions.
   https://www.anthropic.com/engineering/infrastructure-noise
3. LangGraph persistence documentation: durable checkpoints and explicit recovery.
   Keel implements its own local controller; it does not depend on a hosted
   LangGraph service.
   https://docs.langchain.com/oss/python/langgraph/persistence
4. Howard et al., *Time-uniform, nonparametric, nonasymptotic confidence sequences*
   (2021). The new diagnostic uses a conservative Hoeffding/union-bound derivation,
   not the paper's sharper algorithms; see evaluation-frontier.md for the formula.
   https://arxiv.org/abs/1810.08240

## Next evidence gates

A claim of production readiness still needs representative independently
adjudicated task streams, approved live-adapter trials, optional browser/kernel
isolation verification, resolution of private/host-dependent checks and
recovery drills on the deployment hardware. Comparative leadership needs
head-to-head tasks with equivalent resource budgets and independently reviewed
grading. Those are separate evidence requirements; synthetic passing tests do
not establish them. Daybreak Blue was unavailable and was not run.
