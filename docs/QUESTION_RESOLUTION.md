# Evidence-backed question resolution

QRESOLVE finds previously banked answers and supporting local records for the
input tray. It uses Python's standard library and makes no network or model
calls. Automatic factual reuse is **off by default**. Application submissions,
consent, legal statements, personal essays and judgment calls remain outside
its automatic authority.

## Run against an existing workspace

```bash
# Read-only: classify cards and inspect exact evidence pointers.
python3 keel.py --home /path/to/workspace qresolve

# Save proposals and refresh the existing tray; default configuration cannot
# clear a queue blocker. Existing approve/edit flow remains available.
python3 keel.py --home /path/to/workspace qresolve --live --max-cards 50
```

The command is available to the operator CLI profile. Profiles constrain the
CLI; they do not authenticate a person or isolate direct Python execution.
The fit threshold defaults to 60 and can be set with `KEEL_TRAY_MIN_FIT` in
the inclusive range 0–100. Digest `--min-fit` affects that invocation only;
use the environment setting consistently across scheduled workers.

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

All blocker changes go through `tray_answer.py --live --qresolve-request`.
This mode cannot write or overwrite the answer bank. Notes identify reused
evidence and retain its original provenance. The manual human-answer path
remains available; structural blockers are now refused there too.

The applier creates backups under the queue lock, durably records `INTENT`,
writes through `queue_io.atomic_write_json`, verifies exact blocker removals,
then records `auto_applied`. A crash or ambiguous receipt leaves a hold;
pending intents block further automated applications. Do not delete an intent
to force a retry: reconcile the retained backup, queues and evidence on the
host. No automatic reconciliation or cross-file transaction is claimed.

Cleared leads retain their existing queue/status and become eligible for the
canonical `verify_retry` worker where its genuine-blocker check permits it.
QRESOLVE itself does not launch network verification. An applied answer is not
a verified READY transition, a submission, or evidence of improved throughput.

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

## Records, scheduling and acceptance

`hidden_files/qresolve-proposals.json` holds current decisions;
`qresolve-resolutions.jsonl` records application intents and completions;
`qresolve-resolved.json` holds receipt-bound historical resolutions;
`qresolve-metrics.jsonl` reports observed draft/park/application counts. These
files contain private applicant information and must remain private. Read-only
runs print their report and write no metrics. File reads and corpus indexing
are bounded; malformed, unsafe or oversized evidence is reported as incomplete
and blocks application.

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
