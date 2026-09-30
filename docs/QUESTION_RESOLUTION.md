# Evidence-backed question resolution

QRESOLVE finds previously banked answers and supporting local records for the
input tray. It uses Python's standard library and makes no network or model
calls. Automatic factual reuse is **off by default**. Application submissions,
consent, legal statements, personal essays and judgment calls remain outside
its automatic authority.

## Run against an existing workspace

```bash
# Read-only: classify cards and report counts without logging private data.
python3 keel.py --home /path/to/workspace qresolve

# Save proposals and refresh the existing tray; default configuration cannot
# clear a queue blocker. Exact draft approval is a separate operator action.
python3 keel.py --home /path/to/workspace qresolve --live --max-cards 50
```

The command is available to the operator CLI profile. Profiles constrain the
CLI; they do not authenticate a person or isolate direct Python execution.
The tray fit threshold defaults to the canonical intake threshold,
`engines/queue_intake.py::FIT_BAR` (currently 75). Precedence is an explicit
digest `--min-fit` (or in-process `min_fit` argument), then
`KEEL_TRAY_MIN_FIT`, then `FIT_BAR`. Overrides retain the inclusive range
0–100, including fractional values; invalid or non-finite values fail closed.
Digest `--min-fit` affects that invocation only; use the environment setting
consistently across scheduled workers. An explicit lower tray override can
surface lower-fit questions for review; it does not lower the intake or READY
admission floor or authorize a queue transition.

QRESOLVE now checks `fit_policy.main_floor()` independently during planning and
again inside the sanctioned actuator. A lower visibility override cannot clear
below-floor, missing or malformed fit scores. Each application binds to the
exact current card roles, so a matching question on an excluded sibling lead
does not receive an incidental answer.

### Fit-floor default audit

`rescreen_standard.py --min-fit` also defaults to `queue_intake.FIT_BAR`;
its explicit CLI override remains available. It does not read
`KEEL_TRAY_MIN_FIT`. QRESOLVE collects the canonical tray in-process and
preserves that environment setting for its answer-actuator subprocess. Its
independent application gate derives from the same canonical intake policy.

Other 75 literals found in the trace remain unchanged in this repair:

| Location | Role |
|---|---|
| `ready_gate.FIT_FLOOR` | Compatibility alias; admission reads the current canonical floor. |
| `title_triage.FIT_OVERRIDE_THRESHOLD` | Evidence-scored triage waiver. |
| `source_yield_proposals.PROMOTE_FIT_MEDIAN` | Source promotion proposal criterion. |
| `pipeline_service`, `pipeline_doctor` | Main-floor fit diagnostics and report metadata. |
| `keel_local/supply_audit.py`, `keel_flow`, `keel_trust`, `export_flow_snapshot.py` | Fixed audit and versioned flow/trust contracts. |
| `worker-charter/assemble_charter.py`, sample data and tests | Published constraint text and fixtures. |

If the canonical intake policy itself changes, review these consumers and
their contract tests together. The scorer's APPLY band at 82
is a separate evidence-scoring rule, documented in
[SCORING_CONTRACT.md](SCORING_CONTRACT.md) and
[fit-scoring-model.md](../engines/fit-scoring-model.md).

CLI output contains counts and status only: scheduled-job logs must not contain
private questions, answer values, employers or evidence excerpts. Inspect the
private `hidden_files/input-tray.json` and `qresolve-proposals.json` files after
`--live` to review exact quotes and pointers. Trusted in-process callers can
use `qresolve.inspect()` for a full read-only report without persisting it.

## Approve an exact reviewed draft

Review the answer, evidence, question and target roles in the private proposal
file, then use its full `decision_id`:

```bash
# Revalidate without writing anything, including lock diagnostics.
python3 keel.py --home /path/to/workspace qresolve-approve --decision-id DECISION_ID

# Explicitly approve that exact draft for those exact current roles.
python3 keel.py --home /path/to/workspace qresolve-approve --decision-id DECISION_ID --live
```

Only FACT and JUDGMENT drafts can use this command. STRUCTURAL and TRENT-ONLY
cards, including protected consent and certification questions, remain refused.
Approval is revalidated while holding the queue and answer-bank locks. Changed
evidence, answers, question context, target roles or configuration invalidate the
proposal. The original bank and provenance remain unchanged; the journal records
a separate approval and `human_applied` completion. Approval never becomes
standing global permission, and CLI role labels are not human authentication.
To change an answer, use the existing explicit manual-answer workflow; this
command cannot edit or bank a quote under a new source attribution.

The digest and applier now share `KEEL_HOME`: queues live in `data/queues`,
the bank in `data/answer_bank.json`, and tray metadata in `hidden_files`.
The old hardcoded private directory layout is not searched or migrated.
Bare digest execution is read-only; `--deliver` saves the digest and advances
its delivery watermark. QRESOLVE refreshes the tray without advancing delivery.

## What can supply an answer

Banked answers must carry a nonempty provenance receipt. The existing
`answer_resolver` evaluates scope, expiry, refusal directives and every affected
employer/role. Quarantined and non-draftable entries cannot answer a card.
Conflicting applicable quotes stay held; repeating one source does not increase
its authority. Values are copied exactly, never generated or coerced from a
number into a claim about experience.

An exact bank question or a reviewed factual alias can yield a draft. A legacy
entry with unestablished scope can only be a flagged draft. Current
`confirm-answer` receipts do not automatically gain a new global machine scope.
Migration to an explicitly scoped bank entry is an operator decision.

Corpus readers also search dated workspace `memory/YYYY-MM-DD.md`, `USER.md`,
`MEMORY.md`, queue notes, telemetry, the ledger and prior tray cards. These
return exact file/pointer/excerpt references for investigation. Raw narrative
and previously filed values do not become approved answers. External memory
directories are not scanned by the CLI. A trusted in-process `Corpus` caller
may provide an explicit `memory_dir`, without granting application authority.

Entries marked provisional, unreconciled or awaiting sweep reconciliation stay
below applicant-authored evidence. They cannot become automatic answers by
also claiming own-words provenance or `approved_verbatim`. Assisted authorship
is similarly restrictive. These readers do not rewrite or reconcile a private
answer bank; an operator must review the actual source and its affected roles.

Classification runs before answer selection:

| Class | Result |
|---|---|
| FACT | Scoped quoted draft; optionally eligible for authorized reuse. |
| STRUCTURAL | Route recommendation, never an answer or automatic queue transfer. |
| JUDGMENT | Evidence for human review; never automatic application. |
| TRENT-ONLY | Human-only compatibility label; a draft requires explicitly banked `approved_verbatim: true` wording. |

Technical route failures, CAPTCHA/account gates, unknown facts, sensitive
statements and mixed blockers cannot become ordinary contact facts merely
because a bank entry resembles the text. Retrieval scores are deterministic
policy scores, not calibrated probabilities of truth. The classifier currently
recognizes ten narrow factual alias families; unknown phrasing stays human.

The supplied standing gates also constrain draft display: Alo Yoga availability,
the disputed Edmentum/Apex relationship, restricted Perplexity exercises and
OpenAI personal-completion certification cannot receive stored draft answers.
ShipBob motivation prompts and Anthropic Fellows authorship/workstream gates
require previously approved applicant wording. Fleetio's incomplete topic
descriptions cannot substitute for complete screener prompts. These are
conservative policy fixtures; this checkout does not contain the private cards.

## Optional standing authority

Only after the applicant explicitly authorizes this reuse policy, the host
operator may create `hidden_files/qresolve-config.json`:

```json
{
  "schema": "keel.qresolve.config.v1",
  "AUTO_APPLY_FACTS": true,
  "authorization_note": "Reference the applicant's explicit standing instruction here."
}
```

The code never enables the flag. Local configuration is trusted operator state,
not proof that a human signed it. Do not copy the example as implied consent.
With the flag enabled, `--live` may reuse only exact, nonlegacy scoped FACT
quotes with dated own-words provenance no older than 90 days. The request is
recomputed from current canonical data inside the applier's queue lock. An
uploaded proposal, hash, note, or confidence value cannot grant authority.

Automatic blocker changes go through `tray_answer.py --live --qresolve-request`;
explicit draft approval uses `tray_answer.py --approve-qresolve`. These modes
cannot write or overwrite the answer bank. Notes identify reused
evidence and retain its original provenance. The manual human-answer path
remains available; structural blockers are now refused there too.

The applier creates backups under the queue lock, durably records `INTENT`,
writes through `queue_io.atomic_write_json`, verifies exact blocker removals,
then records `auto_applied`. A crash or ambiguous receipt leaves a hold;
pending intents block further applications through these two modes. Inspect an
interrupted operation before attempting another application:

```bash
python3 keel.py --home /path/to/workspace qresolve-recover
python3 keel.py --home /path/to/workspace qresolve-recover --decision-id DECISION_ID --live
```

Recovery defaults to read-only inspection. The live command selects one intent
and closes it only when retained evidence proves either no queue write occurred
or the planned operation fully completed. A cancelled untouched intent can be
replanned against current evidence; a recovered completion cannot apply twice.
Partial, conflicting or damaged state stays held. Recovery does not replay
queue writes, roll back queues, delete intent history or declare leads READY.
Do not delete a journal entry to force a retry. The journal is not a cross-file
transaction or protection against a privileged workspace writer.

Cleared leads retain their existing queue/status. Receipts describe fully
unblocked leads as awaiting canonical preparation and admission. The current
operational verifier records posting evidence and does not promote READY.
Host preparation/admission still owns that transition. QRESOLVE itself does
not launch network verification. An applied answer is not a verified READY
transition, a submission, or evidence of improved throughput.

## Recurrence without hiding changed obligations

Fingerprints preserve negation, quantities, punctuation and bracketed
conditions. A finite reviewed alias table unifies known factual paraphrases;
generic fuzzy similarity never decides equivalence. Full prompts are retained
instead of being cut at the first sentence. Previously truncated card keys may
therefore change on the first upgraded digest.

Completed receipts bind question identity, exact evidence, configuration,
target roles and their post-write snapshots. Cleared blockers disappear from
the next canonical digest. A new employer, changed evidence, or an active
re-parked blocker remains visible and must be revalidated. A global
fingerprint-only "never ask again" list would conceal unresolved work and is
deliberately not used. Explicitly marked human drafts (`owner: "human"`) are
preserved only for the same full prompt and role set.

Reviewed factual aliases share a card identity within their existing family.
Notification history also binds to the current role and queue revision. A
wording-only alias can remain quiet, while a new employer, re-park timestamp,
qualifier or evidence revision creates a fresh notification. Every active
canonical obligation remains visible even when its notification is quiet.
Upgrading a legacy delivery watermark may produce one fresh notice.

## Diagnose resolution-to-supply conversion

```bash
# Counts only; private questions and evidence never enter stdout.
python3 keel.py --home /path/to/workspace qresolve-supply

# Persist bounded observations of committed READY rows, metadata only.
python3 keel.py --home /path/to/workspace qresolve-supply --live
```

The private report separates removed blocker instances from distinct affected
leads, remaining questions, current fit eligibility and static READY gates.
It refuses pending queue recovery and treats missing queues as unknown.
Resolution receipts bind the post-resolution role identity and fit policy.
Live observations can record a matching committed READY row within 24 hours
of completion. The metric is `observed_ready_within_24h`: it does not establish
an exact transition time, causality, executable readiness or submission
authority. A late first inspection cannot prove whether READY occurred earlier;
historical timing remains unknown. Observation history survives later status
changes. Existing private pipeline paths are not guessed or migrated.

## Records, scheduling and acceptance

`hidden_files/qresolve-proposals.json` holds current decisions;
`qresolve-resolutions.jsonl` records application intents and completions;
`qresolve-resolved.json` holds receipt-bound historical resolutions;
`qresolve-metrics.jsonl` reports observed draft/park/application counts. These
files contain private applicant information and must remain private. Read-only
runs print a redacted summary and write no metrics. File reads and corpus indexing
are bounded; malformed, unsafe or oversized evidence is reported as incomplete
and blocks application.

`qresolve-ready-observations.jsonl` stores bounded private observation receipts.
Dry inspection is optimistic when no existing canonical lock is available and
cannot certify a coherent live state; applications are revalidated under locks.
No command silently recovers an unrelated queue transaction during inspection
or resolution metadata writes.

`qresolve-schedule.json` retains hashed card identities, revisions and scan
progress. Live runs reserve capacity for the least recently inspected cards,
while also prioritizing new or changed work. Repeated high-ranked human-only
cards therefore cannot monopolize every batch. Dry runs inspect the next batch
without advancing progress. Previously planned active drafts remain available
and are revalidated before display or approval. Corrupt or oversized scheduling
state holds the run rather than silently resetting fairness history.
`--max-cards` bounds new planning decisions; it does not bound all corpus reads
or tray-decoration work. No measured compute or credit saving is claimed.

A host can schedule the same command every 15 minutes using its existing
scheduler. No cron entry is installed by this release. The queue lock and
canonical revalidation protect cooperating writers; they are not protection
against a privileged actor rewriting the entire workspace.

```cron
*/15 * * * * /usr/bin/python3 /path/to/keel/keel.py --home /path/to/workspace qresolve --live --max-cards 50
```

Tests cover synthetic classification, exact provenance, dry-run immutability,
scope/expiry/conflict refusal, protected classes, interrupted-write holds,
five real CLI-to-actuator applications, replay and new-scope visibility. The
private backlog, the historical incident's original rows, and a labeled set of
50 real cards were not available in this checkout. Real-card classification
accuracy, live revival rate, time-to-resolution and backlog reduction remain
host acceptance tasks. Current metrics leave READY transitions and unlocked
fit unknown rather than inventing those outcomes.
