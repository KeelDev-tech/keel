# Keel 0.12 temporal evidence memory and human question planning

These modules are an additive local capability laboratory. They write only to
an explicitly configured private memory home. They do not import live pipeline
state, revoke a canonical approval, fill a field, generate an answer, submit an
application, or make network/model calls. Integration must preserve existing
holds, source restrictions, no-AI rules and human approval gates.

## Temporal evidence memory

`keel_loki.temporal.TemporalMemory(home, clock=None)` creates or opens
`home/temporal.sqlite3`. Its parent must already exist. A new home is 0700 and
the database is 0600. Existing permissive directories/files, symlinks,
hard-linked database files, mismatched ownership and replaced storage are
rejected. Linux/macOS-style file permissions and a trusted OS account are
assumed; encryption at rest and authenticated remote ingestion are not included.

The default host clock returns integer Unix seconds. A trusted custom clock can
be supplied for controlled tests. `valid_from`/`valid_until` describe when a fact
applies; `recorded_at` is assigned by the store and describes when it learned the
record. Valid intervals are half-open: start inclusive, end exclusive. New writes
with a regressing host clock are rejected. Same-second events use their sequence
number for deterministic ordering. An idempotent retry retains its original
timestamp even during a clock regression.

Every write accepts an explicit idempotency key. Its exact payload can be
retried, but a different payload under the same key fails. Revision, observation,
artifact and approval-observation identities are immutable.

```python
from keel_loki.temporal import TemporalMemory

memory = TemporalMemory("/private/keel/memory")
memory.record_claim({
    "revision_id": "employment-start-v1",
    "subject_id": "applicant-1",
    "key": "employment-start",
    "value": "2020-01",
    "source": {
        "source_id": "employment-document-v1",
        "sha256": SOURCE_BYTES_SHA256,
        "classification": "personal",  # public | personal | sensitive
        "allowed_scopes": ["application-123"],
        "permitted_uses": ["application_fact"],
    },
    "scope": "application-123",
    "permitted_uses": ["application_fact"],
    "valid_from": 1577836800,
    "valid_until": None,
    "supersedes": [],
}, idempotency_key="capture-employment-start-v1")

# Pass a real upstream verification observation only when one exists.
# The caller authenticates the producer; a reviewer_id is not authentication.
memory.record_observation({
    "observation_id": "actual-review-record-1",
    "revision_id": "employment-start-v1",
    "reviewer_id": ACTUAL_REVIEWER_ID,
    "verdict": "verified",  # verified | rejected
    "source_sha256": SOURCE_BYTES_SHA256,
    "observed_at": ACTUAL_REVIEW_TIME,
}, idempotency_key="ingest-actual-review-record-1")

result = memory.resolve(
    "applicant-1", "employment-start", "application-123", "application_fact",
    valid_at=ACTUAL_VALID_TIME, known_at=ACTUAL_KNOWLEDGE_CUTOFF,
)
```

Do not substitute sample constants for authentic source records. The only
permitted-use labels are `application_fact`, `planning` and `review`; none grants
application approval or permits generated answers to an unaided exercise. A
claim's scope/use must be permitted by its source record. Cross-scope reads
return no value or revision IDs. The privileged `history()` API exposes private
records for local auditing and must not be forwarded to public logs.

Resolution returns a value only for one active revision with compatible source
use and nonconflicting recorded verification observations. Missing observations
remain `UNVERIFIED`; disagreeing facts remain `CONFLICT`; equal independent
revisions remain `AMBIGUOUS_REVISIONS`; disagreement between reviewers remains
`DISPUTED`. A late-arriving older observation does not overwrite a newer
observation from the same reviewer. These statuses describe recorded evidence,
not independently established source truth.

Corrections explicitly list their predecessors in `supersedes`. Supersession is
transitive, scoped to the same subject/key/scope, and evaluated using the
requested valid time and knowledge cutoff. A restrictive correction cannot leak
the old value through its former permitted use. Historical queries preserve what
was known before a late correction was recorded.

### Dependency and approval observations

```python
memory.register_artifact({
    "artifact_id": "packet-v1", "subject_id": "applicant-1",
    "scope": "application-123", "purpose": "application_fact",
    "sha256": PACKET_BYTES_SHA256,
    "dependencies": ["employment-start-v1"],
}, idempotency_key="observe-packet-v1")

# Optional: ingest only an existing authentic upstream decision record.
memory.register_approval({
    "approval_id": ACTUAL_DECISION_ID,
    "artifact_id": "packet-v1", "artifact_sha256": PACKET_BYTES_SHA256,
    "reviewer_id": ACTUAL_REVIEWER_ID,
    "source_record_sha256": ACTUAL_DECISION_RECORD_SHA256,
    "observed_at": ACTUAL_DECISION_TIME, "decision": "approved",
    "scope": "application-123",
}, idempotency_key=ACTUAL_DECISION_ID)

impact = memory.invalidation_projection(valid_at=ACTUAL_VALID_TIME)
```

Artifact and decision hashes are supplied bindings; this API does not open or
authenticate their original bytes. Registering an artifact can retain an
unverified dependency, which the projection marks stale. Replaced, expired,
restricted, disputed or unverified dependencies produce `STALE` and
`REAPPROVAL_REQUIRED` projections. A later rejection supersedes an earlier
observation by the same reviewer; disagreement remains visible. Even
`DEPENDENCIES_CURRENT` and `RECORDED_CURRENT` confer no execution authority.
The live integration must consume these projections and apply its existing
canonical transaction/gate logic; this module never changes those records.

`verify()` checks the deterministic event hash chain, references, canonical
payloads, sequence, time monotonicity and stored head. SQLite transactions
serialize writers; update/delete triggers protect normal application use.
`verify(expected_head_sha256=...)` can check a separately retained checkpoint.
An OS owner can rewrite both history and head; hashes do not prove truth,
authenticate identity or make this database tamper proof. Keep checkpoints in a
separate trusted location when required. Operations verify the history before
read/write; this deliberately favors auditability over throughput and is not a
large-scale database performance claim.

`demo(home)` creates synthetic claim/verification fixtures, a dependent packet,
and a late correction. It verifies the historical view and stale projection.
It records **zero actual human decisions and zero approval observations**.

## Human question planner

`keel_loki.questions.plan_questions(spec, now)` ranks user questions using a
dependency DAG. It never answers them. All timestamps are integer Unix seconds;
effort is an explicitly supplied estimate in minutes (0.001 to 1440). No
probability of a usable answer, interview or hire is fabricated.

Input schema:

```json
{
  "schema": "keel.loki.question_input.v1",
  "tasks": [{
    "task_id": "application-123", "weight": 1,
    "blocker_ids": ["geography-123"],
    "created_at": 1800000000, "deadline": null
  }],
  "blockers": [{
    "blocker_id": "geography-123", "question_id": "current-geography",
    "kind": "fact", "scope_id": "application-123", "fact_key": "geography",
    "depends_on": [], "state": "unresolved"
  }],
  "questions": [{
    "question_id": "current-geography", "kind": "fact",
    "scope_ids": ["application-123"], "prompt": "Which locations are acceptable?",
    "estimated_minutes": 1, "reuse_authorized": false
  }]
}
```

Blocker kinds are `fact`, `approval`, `attestation`, `unaided` and `system`.
Only facts have a `fact_key`; others use `null`. System blockers have
`question_id: null` and cannot become manufactured human requests. The other
kinds must reference a matching question. Approval, attestation and unaided
questions each have exactly one scope, exactly one blocker, and reuse disabled.
They always require the human's individual action. A shared factual question
requires explicit reuse authorization, the same fact key, and all target scopes.
State assertions are supplied by the upstream exporter; the planner does not
authenticate that a blocker marked resolved was actually resolved.

The DAG rejects cycles, duplicate IDs/edges, unknown references and a resolved
child with unresolved prerequisites. A question can only hypothetically resolve
its own answerable blockers. The report separately identifies affected tasks
and tasks that would have **all** dependencies resolved. Unique IDs prevent
double counting when tasks share a blocker or contain an ancestor and child.

The deterministic priority order is:

1. Fairness tier: overdue tasks, then tasks waiting at least seven days, then
   other tasks. This permits small old tasks to receive a turn.
2. Conditionally released task weight per estimated human minute.
3. Fractional dependency progress per minute, to make progress on tasks needing
   more than one answer without pretending those tasks are already unlocked.
4. Fewest unresolved dependencies, oldest affected task, then stable question ID.

These are transparent heuristics, not a learned optimum or guaranteed deadline
scheduler. The input's weights and effort estimates should be reviewed and later
compared with recorded outcomes. An overdue task remains visible even when a
different system blocker prevents completion; the report never calls partial
progress an application ready to submit.

`check_fact_reuse(answer, scope_id=..., fact_key=..., now=...)` evaluates an
existing answer's declared hash, scope, expiry and use metadata. Its exact fields
are `answer_id`, `kind`, `fact_key`, `source_sha256`, `value_sha256`, `scope_ids`,
`permitted_uses`, `reuse_authorized`, `observed_at`, `expires_at`. Only a `fact`
whose sole permitted use is `application_fact` can report
`METADATA_COMPATIBLE`; this is not a verified answer or an approval.

### Recorded outcomes and optional offline policy estimates

`evaluate_outcomes(log)` accepts rows with `event_id`, `question_id`,
`human_minutes`, `completed_task_ids`, and `interview_event_ids`. It counts
distinct task/interview IDs and reports completed tasks per recorded minute.
No recorded time means a null rate, not free work. Interview IDs are supplied
events, not inferred interviews; authenticity and causal improvement remain
unverified. Repeated event IDs are rejected.

`evaluate_policy(log, candidate_probabilities)` optionally calculates inverse
propensity scoring (IPS) and self-normalized IPS using **already logged**
probabilities. It does not learn a policy or choose actions. Each log row is:

```json
{
  "decision_id": "decision-1", "selected_action": "question-a",
  "logging_probabilities": {"question-a": 0.5, "question-b": 0.5},
  "propensity": 0.5, "reward": 1, "outcome_observed": true
}
```

The candidate is a map from decision ID to a complete distribution over the same
action set. Rewards must be observed utility in [0,1]. Missing outcomes,
impossible selected actions, altered propensities, nonnormalized distributions
or candidate mass outside logger support fail closed. The output reports
effective sample size and largest importance weight. No actual matching action
produces `NO_MATCHING_LOGGED_ACTIONS` and a null self-normalized estimate.

Logged numbers do not prove that the original policy randomized correctly or
that outcomes were independently measured. Neither confidence intervals nor a
deployment recommendation are produced. Avoid fitting and evaluating on the
same log; a production experiment must freeze its policy and evaluation split
externally. Deterministic historical logs usually lack support for evaluating
alternative actions, and must not be retrofitted with invented propensities.

`questions.demo()` demonstrates one explicitly reusable factual question and a
separate deferred application approval. All demo inputs are synthetic.

## Run focused tests

```sh
python3 -B -m pytest tests/test_loki_temporal.py tests/test_loki_questions.py -q
```

The tests exercise real SQLite persistence/reopening, concurrent writers,
idempotency, clock regression, corrections, expiry, scope restrictions,
contradictions, stale approval projections, tampering/checkpoints, DAG cycles,
no-inflation counts, fairness, forbidden reuse and logged-policy support.
