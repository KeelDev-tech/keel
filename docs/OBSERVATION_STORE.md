# Authenticated local observations

`keel_observability` persists measured observations in a local SQLite database.
It supplies evidence to source-feedback proposals and persistent memory indexing.
It never submits applications, approves actions, changes source schedules, or
calls a network service. It uses the Python standard library only.

## Trust boundary

An imported document is **UNVERIFIED**, regardless of its wording or filenames.
The CLI has no authenticated-import switch. A trusted host explicitly invokes
`store.ingest(event, authenticate=True, context=host_context)` and installs a
Python verifier. The verifier receives `(canonical_event_bytes, context, now)`
and must return `Verification` with the exact event digest, account, producer,
allowed event kinds, principal, verification time, expiry, and assurance.
Exceptions, absent callbacks, bad scopes, changed bytes, expired grants, and
future verification times fail before writing the event. Authentication context
and credentials are never persisted. Verification metadata is recorded.

Assurance levels are distinct:

| Assurance | What the host must establish |
| --- | --- |
| `authenticated_producer` | A configured producer authenticated these exact bytes. This does not prove the factual claims. |
| `provider_verified` | Independently validated provider evidence and the exact application/attempt binding. |
| `human_reported` | An authenticated human explicitly reported the actual measured minutes. |
| `host_measured` | Trusted host instrumentation measured the value or checked the stated coverage. |

Effort ingestion requires assurance matching its measurement method. Cohort
submission/outcome credit requires `provider_verified`. Complete telemetry and
follow-up windows require `host_measured` or `provider_verified`. HMAC signatures
alone cannot establish either human presence or a provider's acceptance.

Verification expires. Hosts must refresh exact observations with a fresh valid
proof when continued use is appropriate, or replace/revoke them. Refreshing a
child requires its exact ancestors to remain current. There is no automatic
renewal based on a stored identity label.

## Local producer authentication without a paid identity service

`auth.py` provides `ProducerKey`, `HMACProducerVerifier`, and `sign_context`.
The host owns a Python policy mapping of key IDs to account/producer bindings,
principals, allowed event kinds, activation/expiry, and revocation status. Keys
must contain at least 32 random bytes. Generate secrets on the trusted host,
keep them in private host storage, and never include them in events, examples,
logs, source archives, or worker-accessible configuration.

```python
import secrets
import time
from keel_observability import (
    HMACProducerVerifier, ObservationStore, ProducerKey, sign_context,
)

# Example creates a key in memory; provision/persist it privately on the host.
key = ProducerKey(
    key_id="worker-key-v1", account_id="account-A", producer_id="collector",
    subject="collector-service", secret=secrets.token_bytes(32),
    allowed_kinds=("source", "opportunity", "evidence", "measurement"),
)
verifier = HMACProducerVerifier(store_id="keel-workspace", keys={key.key_id: key})
store = ObservationStore("/private/keel/observations.sqlite",
                         store_id="keel-workspace", verifier=verifier)
now = int(time.time())
context = sign_context(event, key=key, store_id=store.store_id,
                       issued_at=now, expires_at=now + 60,
                       nonce=secrets.token_hex(16))
result = store.ingest(event, authenticate=True, context=context)
```

The signed context binds the full canonical event digest, store, account,
producer, key ID, nonce, issue time, and expiry. MAC comparison is constant time.
The default context lifetime limit is 300 seconds; a host can configure at most
3,600 seconds. Verification expiry is the context expiry. Replays of the same
accepted event are idempotent; the nonce is not an action-authorization token.

Rotate or revoke keys by replacing the immutable verifier policy snapshot.
Revocation immediately rejects new ingestion under that key. Previously stored
observations retain their recorded verification until expiry or an explicit
observation revocation. For immediate historical invalidation, the host must
emit signed revocation observations. Possession of a producer key grants only
its configured observation scope and no application execution permission.

## JSON event schema

The schema name is `keel.observation.v1`. Every field below is required. Unknown
fields are rejected. Canonical bytes use UTF-8, sorted keys, compact separators,
and finite JSON numbers. One event is limited to 65,536 bytes.

```json
{
  "schema": "keel.observation.v1",
  "event_id": "document-2",
  "account_id": "account-A",
  "producer_id": "collector",
  "kind": "evidence",
  "occurred_at": "2026-09-24T12:00:00Z",
  "lineage": {
    "source_id": "S",
    "opportunity_id": "O",
    "application_id": null,
    "attempt_id": null
  },
  "revisions": {
    "evidence": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  },
  "payload": {
    "evidence_ref": "local:document-2"
  }
}
```

The example digest is a placeholder, not a valid digest for any supplied text.
Memory ingestion must match `revisions.evidence` to the exact UTF-8 document
bytes. Other named revision hashes can bind forms, answers, policies, adapter
code, receipt payloads, and review snapshots.

- IDs match `[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}`.
- `occurred_at` is an ISO 8601 timestamp with an explicit timezone, not in the future.
- Lineage contains exactly the four displayed fields. Values are IDs or null;
  an absent ancestor cannot be followed by a present descendant.
- `revisions` contains at most 32 names matching `[a-z][a-z0-9_]{0,63}`, each
  mapped to a lowercase 64-character SHA-256 hex digest.
- Evidence references are nonempty strings of at most 2,048 characters. They
  are provenance pointers, never URLs that the store fetches.

Each payload has **exactly** the following fields:

| Kind | Lineage | Payload fields and constraints |
| --- | --- | --- |
| `source` | Source only | `name`, `evidence_ref` |
| `opportunity` | Source + opportunity | `qualified`: boolean or null; `evidence_ref` |
| `application` | Source + opportunity + application | `submitted_at`: aware timestamp or null; `fit_score`: finite 0–100 or null; `evidence_ref` |
| `attempt` | All four IDs | `evidence_ref` |
| `evidence` | Any nonempty exact lineage | `evidence_ref`; bind document bytes using `revisions.evidence` |
| `effort` | Any nonempty exact lineage | `human_minutes`: finite 0–1,000,000, not boolean; `measurement`: `human_reported` or `host_measured`; `task_ids`: up to 128 unique IDs; `evidence_ref` |
| `outcome` | All four IDs | `outcome`, `evidence_ref`, `provider_receipt_id`, `receipt_sha256`. The last two are either both null or both populated. |
| `window` | Any nonempty exact lineage | `window_start`, `window_end`, `observed_through`, `complete`: boolean, `evidence_ref`. Start < end; observed-through cannot exceed event time; complete requires observed-through ≥ end. Maximum window 180 days. |
| `measurement` | Exactly the target entity's lineage | `target_event_id`, `target_sha256`, `previous_event_id`, `previous_sha256`, `values`, `evidence_ref`; see below. |
| `revocation` | All lineage fields null | `target_event_id`, `target_sha256`, `reason`; target must exist in the same account with exactly that digest. |

Outcome labels are `SUBMISSION_CONFIRMED`, `ACKNOWLEDGMENT`, `INTERVIEW`, `OFFER`,
`REJECTION`, `ASSESSMENT`, `INFO_REQUEST`, and `OTHER`. A label does not confer
assurance. A submission timestamp in an application declaration alone does not
establish confirmed submission.

## Identity, updates, conflicts, and revocation

Source → opportunity → application → attempt declarations register immutable
identities. Authenticated children require current authenticated ancestors in the
same account, matching every supplied ancestor ID. No employer-name guessing or
cross-account joining occurs. Parent declarations must precede child events.

Stable event IDs are required. Same-ID/same-bytes retries do not double count.
All payload variants are retained. Unverified variants cannot replace or taint
a trusted selection; a later authenticated variant can become the selection.
Two authenticated payloads for one event ID create a permanent conflict hold.
Re-registering an entity under a new event ID also creates a conflict hold.
Review and a new appropriately scoped observation are required to resolve the
underlying facts; an import cannot silently overwrite them.

Use `evidence` for new document revisions under existing lineage. Publish a new
evidence event ID with the new content hash, and explicitly revoke obsolete
observations when needed. Index supersession itself does not authenticate text.

Use `measurement` to complete or update unknown opportunity/application facts
without re-registering identity:

```json
{
  "target_event_id": "opportunity-declaration",
  "target_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "previous_event_id": null,
  "previous_sha256": null,
  "values": {"qualified": true},
  "evidence_ref": "local:qualification-review"
}
```

For an application, `values` must contain both `submitted_at` and `fit_score`.
For an opportunity it must contain only `qualified`. The target digest and
lineage must match the current original declaration. First measurement uses a
null predecessor pair. Later measurements pin the exact current measurement ID
and digest, providing atomic compare-and-swap ordering. Stale updates fail and
cannot overwrite a concurrent measurement. Each event is a complete independent
measurement snapshot; predecessor pins order writes and are not inherited facts.
There is no recursive predecessor traversal. Revoking an obsolete measurement
does not retract a newer independently verified snapshot. A held/revoked/expired
current measurement causes source-feedback HOLD instead of fallback allocation.

Provider-verified receipt IDs bind content and lineage. Repeated IDs under new
event IDs cannot multiply credit. Conflicting content/lineage creates holds.
An exact acknowledgment can gain provider acceptance through a new event ID;
that upgrade preserves receipt content and lineage and requires a current prior
binding. Producer-only acknowledgments do not consume a provider binding.

Revocations are authenticated exact-digest events. They immediately invalidate
the target and affect descendants when inspected. Imported revocations do not
change trust. Replay never removes conflict or revocation holds.

## Public APIs and existing-system adapters

```python
store = ObservationStore.create(path, store_id="workspace-id")
store = ObservationStore(path, store_id="workspace-id", verifier=trusted_callback)
row = store.ingest(event)  # Unverified import, even with a verifier installed.
row = store.ingest(event, authenticate=True, context=trusted_context)
row_or_none = store.inspect("account-A", "event-id")
rows = store.events("account-A", after_sequence=0, limit=1000)
page = store.export("account-A", after_sequence=0, limit=1000)
```

Rows contain `sequence`, `event`, `digest`, `trust`, `status`, `holds`, and
`verification`. Trust is `VERIFIED` or `UNVERIFIED`; status is `CURRENT`, `HELD`,
or `UNVERIFIED`. A `CURRENT` authenticated producer claim is not automatically a
provider-verified fact. Measurement rows also report `measurement_current`.

Pagination is by last direct change, including authentication upgrades and
revocation. It is a current projection, not a complete change-log export.
**Consumers must re-inspect an event before use:** ancestor changes and expiry
can invalidate a descendant without changing that descendant's cursor. Account
filtering prevents accidental cross-account joins; the trusted host must enforce
user access to the shared database. This module is not a remote access-control
service.

`keel_observability.adapters` integrates with existing code:

- `session_effort_event` calls the existing review projection validator and
  `record_session_effort`, checks exact equality, and preserves actual minutes
  including budget overruns. Shared time is attributed once; no per-role split
  is inferred. Authentication remains a host responsibility.
- `receipt_event` calls existing receipt normalization and claim grading. Role,
  application, and attempt IDs must exactly match supplied lineage. Source ID is
  explicit. Imported `verified` flags cannot establish provider acceptance.
- `source_feedback_snapshot` builds the existing `keel.source_feedback.v1`
  input and runs its actual validator. It uses authenticated current identities,
  the current measurement snapshot, measured effort, provider-confirmed outcomes,
  and complete observed windows. It accepts explicit host-reviewed source caps,
  permissions, and cooldown facts; it does not invent those settings. Missing
  data, unattributed shared time, held observations, and incomplete windows set
  `telemetry_complete=false` in the exported document itself. Passing that
  document to `propose` with a positive budget still produces HOLD. Unknown
  minutes remain null, unknown fit remains unknown, and silence is not rejection.
- `reconcile_receipt_observations` re-reads the existing `ReceiptStore`, detects
  later conflicts, missing receipts, or changed payloads, and proposes exact
  revocations. With a trusted `context_factory(event)` it applies them through
  normal authenticated ingestion. Without that callback it is read-only. Pass
  `receipt_store=` to `source_feedback_snapshot` to force HOLD while receipt
  reconciliation is pending. The host must run this integration before reports
  if it needs immediate retraction after a source receipt changes. Detached
  stored observations cannot discover changes in an external file by themselves.

Snapshots are limited to 10,000 account events. Exceeding that bound is explicit;
there is no silently truncated allocation. Policies and synthetic fixtures are
not real telemetry. The store does not demonstrate improved hiring outcomes.

## CLI

Use a private directory owned by the host user. Ancestors must be owned by the
current user or root and cannot be group/world-writable unless sticky. Symlink
ancestors, linked database files, nonprivate storage, and inode replacement are
rejected. Existing files are never overwritten by `init`.

```bash
mkdir -m 700 keel-observations
python3 -m keel_observability --db keel-observations/events.sqlite --store-id local init
python3 -m keel_observability --db keel-observations/events.sqlite --store-id local import event.json
python3 -m keel_observability --db keel-observations/events.sqlite --store-id local inspect --account account-A --event-id document-2
python3 -m keel_observability --db keel-observations/events.sqlite --store-id local export --account account-A --limit 1000
```

Every command emits JSON. Export includes `next_sequence`, `page_full`, and
`execution_authorized=false`. Imports reject duplicate JSON keys, nonfinite
numbers, extra fields, and oversized input. SQLite uses immediate transactions,
full synchronization, and an exclusive initial file create. Concurrent replay,
restart, and account separation are covered by regression tests.

The store protects consistency between cooperating trusted host processes. It
cannot protect against a hostile process with the same OS privileges, privileged
filesystem access, database rollback, or a dishonest configured verifier.
Linux storage behavior is tested; native Windows ACL behavior is not qualified.
There is no network listener, paid dependency, or implicit production cutover.
