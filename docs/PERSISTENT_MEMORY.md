# Persistent evidence search and dependency checks

`keel_memory.EvidenceIndex` connects the authenticated observation store to a
private SQLite FTS5 index and the existing temporal revision/dependency engine.
It uses the standard library, requires SQLite FTS5 support, and makes no model or
network calls. It does not authenticate a producer itself, verify the truth of
text, or authorize an application action.

## Host integration

Create the observation store through `keel_observability.ObservationStore`, with
a real host verifier. Pass that store to `EvidenceIndex(home, observations)`.
The index home is a private directory under an existing, non-symlink parent.
It is permanently bound to that store's `store_id`. The host owns both stores;
untrusted plugins must not receive their paths or Python objects.

Documents use this complete schema:

```json
{
  "schema": "keel.memory.document.v1",
  "document_id": "document-1",
  "observation_id": "captured-event-1",
  "account_id": "account-1",
  "scope": "role-1",
  "source_id": "source-1",
  "claim_key": "role-description",
  "text": "The exact captured evidence text.",
  "permitted_uses": ["planning", "review"],
  "valid_from": 1790208000,
  "valid_until": null,
  "supersedes": []
}
```

Before authenticating the source observation, the trusted host must bind both
`revisions.evidence = text_digest(document["text"])` and
`revisions.memory_document = document_digest(document)`. The second digest binds
all document metadata, including account, scope, permitted uses, and replacement
relationships. Hashes alone do not authenticate anything: ingestion requires a
CURRENT, VERIFIED observation whose host proof is unexpired and whose source and
account match. Imported observations cannot satisfy that condition.

`index.ingest(document)` persists the searchable record and projects its revision
into the temporal engine. A crash between stores leaves PENDING state. Retrying
the same document or calling `index.refresh(account_id)` completes the idempotent
projection. PENDING records are held from search. Authenticated conflicting
contents under one document identity produce a persistent conflict hold.

## Search and artifacts

`index.search(query, account_id=..., scope=..., purpose="review", limit=5)` applies
account, scope and purpose filters before ranking. FTS query operators are treated
as literal words. Candidates are ranked through the existing lexical retrieval
implementation, after current provenance and temporal resolution checks. Every
returned match is rechecked against fresh document and observation state, including
revocation, conflicts and proof expiry. Results retain their assurance class and
mark text as untrusted; instructions inside retrieved text have no authority.

Search supports 10,000 stored documents, at most 1,024 matching candidates, 32
query terms, and 32 KiB of UTF-8 text per document. An overly broad query returns
QUERY_TOO_BROAD rather than silently qualifying a truncated corpus. These are
bounded implementation limits, not measured scalability guarantees. Optional
embedding generation is not installed or invoked by this module.

Register a prepared packet using `index.register_artifact` with exact keys:
`artifact_id`, `account_id`, `scope`, `purpose`, `sha256`, and `documents` (a list of
document IDs). All dependencies must currently be usable. Call
`index.artifact_status(account_id, artifact_id)` immediately before downstream
review; revoked, expired, conflicting or superseded evidence yields STALE and
`reapproval_required: true`. The content hash is a binding, not proof that the
packet itself is accurate.

Use only the index's public APIs for current provenance decisions. Its internal
`_temporal` store is a historical projection of document bindings; reading that
store directly omits the observation store's live revocation and expiry checks.
Search results are snapshots, never reusable authorization tokens. The existing
execution gateway must independently revalidate authority and revisions at use.

## Local CLI

The CLI opens an existing observation store and cannot install a verifier from
JSON or a command-line flag. It can only index evidence the host already verified.

```bash
python3 -m keel_memory --home /path/to/private-index \
  --observations /path/to/private-observations/events.sqlite3 --store-id my-store \
  ingest document.json
python3 -m keel_memory --home /path/to/private-index \
  --observations /path/to/private-observations/events.sqlite3 --store-id my-store \
  search "spreadsheet reconciliation" --account account-1 --scope role-1 --purpose review
```

Other commands are `refresh --account`, `register-artifact document.json`, and
`artifact-status --account --artifact`. An optional global `--out` writes a new
private JSON file and refuses an existing path. Avoid putting personal search
queries in shared shell history. Search and export results may contain private
evidence and should remain in the same trusted workspace.

`keel_memory.evaluation.evaluate` measures precision and recall on a pinned,
explicitly labeled held-out query set. It reports synthetic status and does not
authenticate labels or count an absent relevant label as a successful retrieval.
Regression tests exercise actual SQLite stores, authenticated synthetic host
callbacks, scope isolation, conflicts, correction, packet invalidation,
interrupted projection recovery, and revocation during ranking. They do not
establish a live provider connection or truth of applicant facts.
