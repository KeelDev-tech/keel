# Explicit source approval workflow

`keel_sources.decisions` creates local review requests and records explicit
operator decisions. It never submits an application, calls a model, sends a
message, authenticates a person, or grants execution permission. The trusted host
must authenticate its operator and resolve the actual authority record before
calling the decision API. An `actor_id` or `authority_record_ref` string does not
prove identity or authority; every output retains
`source_authenticity_verified: false` and `execution_authorized: false`.

## Required sequence

1. Register the exact workspace, role, application, and action scope in
   `SourceStore`. Capture the six actual policy, form, answer, attachment, target,
   and route sources. Missing source records do not create human requests.
2. Explicitly call `prepare_request(store, scope, expires_at=...)`. It checks all
   six records, their freshness, attachment bytes, and cross-record semantic
   constraints before creating anything. A request expires within 24 hours. Its
   expiry is independent of current source freshness: a genuine later
   observation can refresh an unchanged source while review is in progress.
3. Read the entire returned `review_payload`, the separately displayed
   `source_observations`, and `review_sha256`. The private packet includes all
   six source records and material metadata, exact answers and form definitions,
   policy rules, target and route bindings, and attachment names, manifests,
   sizes, and SHA-256 hashes. It is not a redacted summary. Attachments themselves
   remain in the private attachment store and must be inspected when needed.
4. Explicitly call `decide_request` with the request ID, `APPROVE` or `REJECT`,
   actual operator identity and authority reference, the exact reviewed digest,
   and an expiry. Approval expiry cannot exceed the request expiry or the current
   expiry of any of the six sources.
5. Export using the existing producer/export commands. An approval revision is
   one input to existing assurance, trust, hold, consent, and execution checks.
   Their requirements still apply.

```python
from keel_sources.decisions import (
    prepare_request, get_request, decide_request, revoke_request,
)

# Only after six real sources have been captured; no automatic decision follows.
request = prepare_request(store, scope, expires_at=request_expiry)
packet = get_request(store, request["request_id"])

# Explicit trusted-host operator invocation, after reviewing the complete packet:
result = decide_request(
    store,
    request["request_id"],
    decision=operator_decision,
    actor_id=authenticated_operator_reference,
    authority_record_ref=actual_authority_record_reference,
    reviewed_sha256=operator_reviewed_digest,
    expires_at=decision_expiry,
)
```

These variables must come from real host observations and an explicit operator
decision. The example supplies no approval or authority values. The CLI exposes
the same operations as `approval-request`, `approval-show`, `approval-decide`,
and `approval-revoke`; use `python -m keel_sources <command> --help` for arguments.
Treat review-packet output as sensitive application data.

## Binding and concurrency

The request, decision, and revocation records are immutable SQLite rows; database
triggers reject updates and deletes. Each decision is made in one `BEGIN
IMMEDIATE` transaction. Inside it, Keel rereads source records and actual
attachment bytes, reruns semantic and freshness checks, recomputes all six
revision hashes and the full material review packet, and compares them to the
reviewed request. The pending approval generation must still match. The decision
row, approval source version, and audit events commit together or roll back
together. Concurrent decisions on one request cannot both succeed.

Source `observed_at` and `expires_at` are displayed separately from the stable
review digest. Only these two descriptor fields are excluded from the material
packet. All record fields remain bound, including any expiry inside a record.
Actual source versions, references, form/answer values, policy, destination,
account, attachment bytes, and other material changes invalidate review. Fresh
observations of identical source content/version can advance the six source
generations without discarding a valid material review; both generation sets are
retained for audit. Freshness is nevertheless checked at decision time.

The local filesystem and database belong to the trusted host. These controls do
not authenticate upstream facts, prove source completeness, or protect against
the operating-system owner rewriting data. Attachment bytes must still be
checked again during the existing fresh execution preflight.

## Holds, renewal, and revocation

An explicit pending request creates the only `HUMAN_DECISION_REQUIRED` descriptor
produced here. Without a real request, an absent approval remains
`SOURCE_RECORD_MISSING`. Unready prerequisites cause no pending request and no
approval write. A request alone grants nothing.

Each request accepts one decision. Repeating a decision or attempting to flip
`REJECT` to `APPROVE` (or vice versa) fails. A rejection remains a hold for those
same six revisions; the workflow has no automatic reconsideration path.

An explicit new request is allowed after material changes or after an earlier
approval expires or is revoked. It is also possible after an unanswered request
expires. This always creates a new immutable request and requires a new explicit
decision. No source version needs to be fabricated merely to renew an expired
approval. An unchanged, still-valid approval or pending request cannot be
silently replaced.

`revoke_request(store, request_id, actor_id=..., reason=...)` applies to the
current approved request. It appends an immutable revocation and a new approval
source version with `revoked: true`. It preserves the original decision, original
approval time, and original expiry. Its fresh descriptor time records observation
of the revocation; it does not extend the decision. Revocation is allowed after
approval expiry and cannot overwrite a newer request or approval. It does not
undo any past external action.

`get_request` returns the workflow state, `is_current`, immutable decision data,
and any revocation. Historical requests displaced by a newer source request are
marked `SUPERSEDED`. An unanswered request with changed material is `STALE`;
unready current source prerequisites, including expired source observations,
produce `BLOCKED`. Both have `request_actionable: false`. These are read-only
observations: they do not modify the original request or source descriptor.
`get_request` does not create tables, advance the durable clock, or write database
bytes. `APPROVED` describes the recorded decision; it is never a
claim that an application is currently eligible to execute. The legacy export
schema requires an `approved_at` field even for a stored `REJECT`; the explicit
`decision` field controls its meaning, and `decided_at` records the same actual
decision time.
