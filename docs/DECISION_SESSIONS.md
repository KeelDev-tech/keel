# Bounded human decision sessions

Keel can now group complementary review questions into a session with a hard
**estimated human-minute budget**. It uses the same exact subset optimizer as
the mathematical core, with separate effort and value units. No paid service,
model call, network access, or extra Python dependency is required.

## Use the existing Muse review path

```python
from keel_loki.common import digest
from keel_muse.review import (
    example_snapshot, project_review, make_review_request,
    record_session_effort,
)

# Synthetic example; substitute a fresh snapshot from your authoritative host.
snapshot = example_snapshot()
report = project_review(snapshot, now=snapshot["captured_at"], session_minutes=5)
session = report["decision_session"]
print(session["selected_question_ids"])
print(session["estimated_minutes"])
print(session["conditionally_unlocked_task_ids"])

# Only after a real human reviews the exact displayed application:
request = make_review_request(
    report, application_id="app-01", question_id="review-01", response="defer",
    now=report["as_of"], expected_report_sha256=digest(report),
)
# request is unauthenticated intent, never permission to execute.

# Only when the human actually reports this time spent:
effort = record_session_effort(
    report, event_id="unique-human-effort-event-1", question_id="review-01",
    human_minutes=2.5, now=report["as_of"], expected_report_sha256=digest(report),
)
```

The existing Muse `review` operation `project_review` accepts
`session_minutes` in its arguments. Omitting it preserves the existing review
projection shape and one-question ranking. A projected session is rebuilt
during validation, so editing its selections or claimed unlocks is rejected.
Changing the packet, application, questions, or session budget also changes
the report binding; requests made against the previous report are stale.

## Selection policy and limits

- Complete task unlock weight is counted once per exact task ID. Requirements
  must all resolve, including their transitive dependencies. Two complementary
  questions can therefore beat one question with a larger immediate payoff.
- The oldest/deadline fairness policy remains visible: an affordable question
  from the existing overdue tier, or then the seven-day waiting tier, reserves
  one turn before the remaining budget is optimized. This is a reservation
  policy, not a claim that every old task is guaranteed completion.
- Ties prefer shorter estimated effort and stable question IDs. All minute
  sums are exact rational calculations over the supplied decimal numbers;
  floating-point roundoff cannot admit a bundle over its declared budget.
- If no complete task can unlock, an affordable ranked question can make
  dependency progress. The result is explicitly `PROGRESS_ONLY` with no
  invented task completion.
- The session planner permits at most **16 relevant pending questions** and
  **10,000,000 subset/task checks**. Above either bound, it returns
  `SESSION_TOO_LARGE` with an empty selection. Supply a deliberately narrower
  snapshot and retain the rest in the normal tray; no silent truncation or
  unproven heuristic is used.
- Budgets must be positive finite minutes, at most 1440. A budget too short
  for any actionable question returns `NO_AFFORDABLE_ACTIONABLE_QUESTION`.
- The hard constraint bounds planned estimated effort. It cannot guarantee how
  long a real human takes. Actual time remains unknown until explicitly reported.

## Human decision and authorization boundary

Only fact questions with the same explicit question ID, identical definition,
and authorized scopes can be reused. Similar text alone does not merge facts.
Approvals, attestations, and unaided answers stay individual, non-reusable
questions. The existing validators reject attempts to share them.

All unlocks are **conditional**: a person must supply usable answers, the host
must validate them, and all independent conditions must still hold. System
holds remain unresolved; no question can manufacture their removal. Dependent
questions are marked as conditional followups in dependency order. Rebuild
the snapshot and session after each ingested response. In particular, a fact
that changes an application or packet requires a new exact human approval.
Do not replay a batch of old approval requests against the changed packet.

Session results retain `execution_authorized=false`,
`all_authorizations_cleared=false`, and `canonical_writes=0`. They neither
submit applications nor consume or issue approval capabilities.

## Observed effort

`record_session_effort` creates a validated, exact-report-bound observation
from **human-reported** positive finite minutes. It never substitutes the
estimate, elapsed wall time, or a fabricated default. Reported time may exceed
the budget and is retained honestly. The observation contains no inferred
task completion or interview outcome.

The same operation is exposed through the existing Muse review CLI as
`record_session_effort`. Its ordinary `--out` handling writes a new observation
file; it does not append to a canonical telemetry stream. The authenticated
host must ingest and deduplicate that event before using it for learning.

The function does not persist the event. An authenticated host must durably
ingest it, deduplicate its `event_id`, and separately verify any outcomes before
joining them to Keel's outcome metrics. This adapter does not authenticate the
reporting human or establish causal improvements.

## Validation

`tests/test_decision_sessions.py` covers complementary requirements, exact
budgets, shared facts, individual attestations, dependency order, system holds,
fairness reservations, explicit size limits, deterministic choices, independent
small-subset optimization checks, stale revisions, old API compatibility, and
honest effort observations. Existing mathematical, question, and Muse review
tests continue to exercise the unchanged APIs.
