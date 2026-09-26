# Canonical posting identity index

The local SQLite index prevents duplicate discovery while preserving distinct
opportunities. It uses the existing `posting_identity.identity` provider parser
and the Python standard library. No service, credential, browser or model call
is needed. No identity verdict authorizes an application or certifies a receipt.

## Matching rules

- Exact Greenhouse, Lever, Lever EU and Ashby provider/board/posting IDs match
  across their recognized URL forms, including `/apply` and Greenhouse embed
  URLs. Board and posting ID case are preserved; provider regions stay separate.
- On unsupported providers, conservative canonical posting URL equality can
  match. Query IDs, meaningful query parameters, repeated parameter order, URL
  parameters, path case and URL fragments are preserved. This avoids merging
  SPA jobs whose IDs live after `#`. Explicit tracking parameters are discarded. A malformed URL never becomes an identity.
- Name/title similarity is advisory. Source and Sourcegraph with the same title
  cannot hard-match. Two postings at the same company with distinct job IDs also
  remain distinct. Enumerators, staging triage and emission prechecks follow the
  same rule; internal duplicate `role_id` checks still apply.
- Candidate fields include `ats_url`, `application_url`, `posting_url`,
  `confirmation_url`, `url`, `job_url`, `jobUrl`, and `absolute_url`. Multiple
  conflicting exact provider IDs in one candidate/row are ambiguous. A known
  exact ID takes precedence over a generic URL shared with another posting.
  For unsupported providers, dedicated posting URL fields take precedence over
  application/ATS endpoints; conflicting unsupported fallback URLs are advisory.
- Generic home, careers, apply and confirmation destinations are not posting
  identity. An unrecognized `confirmation_url` alone is never treated as the
  posting URL. Recognized provider posting IDs are allowed in that field; this
  establishes posting identity, not application acceptance.

## Configure the sources explicitly

Without a registry, the index covers only the established submitted ledger and
standard queue beneath `KEEL_HOME`. Missing files are unknown, not empty. To
cover additional queues, put `config/identity-sources.json` under that workspace:

```json
{
  "sources": [
    {"source_id": "submitted-ledger", "path": "/absolute/keel/data/application-ledger.json", "kind": "ledger"},
    {"source_id": "standard", "path": "/absolute/keel/data/queues/standard-queue.json", "kind": "queue"},
    {"source_id": "western-approved", "path": "/absolute/keel/data/queues/western-queue.json", "kind": "queue"}
  ]
}
```

The operator must approve this local configuration. There is no directory glob
that silently adds backups, historical snapshots or unapproved queues. Source
IDs and paths must be unique. Staging role-ID checks use these selected queues
plus the target standard queue for local idempotence; unconfigured historical
`*-queue.json` files cannot suppress re-admission. The staging audit records
identity coverage and missing/malformed role-ID sources. Queue envelopes may use one of `rows`, `entries`,
`items`, or `leads`, or be a top-level list. Multiple envelopes in one object,
duplicate JSON keys and malformed identity fields are rejected. Ledger rows
participate only when their status is exactly `SUBMITTED`.

```bash
python3 engines/dedupe_index.py --home /absolute/keel refresh
python3 engines/dedupe_index.py --home /absolute/keel status
python3 engines/dedupe_index.py --home /absolute/keel check --url https://jobs.lever.co/example/posting-id
```

`status` and `check` do not refresh or repair missing state. The established
`dedupe_gate` callers use `get_index`, which refreshes incomplete/expired/changed
projections automatically. An unreadable/corrupt database remains an explicit
hold; it is not silently overwritten, since it may contain reviewed aliases.

## Freshness, uncertainty and limits

Refresh captures bounded source bytes and atomically replaces the projection.
Each revision hashes the source contents and source configuration. Replay of
identical bytes keeps the revision stable; refresh updates the observation time.
Every individual lookup hashes the configured source files before and after
lookup. `check_batch` uses
one SQLite read transaction and checks all source hashes both before and after
its lookups. A source change or expiry during that batch downgrades every result
to advisory uncertainty. A timestamp-only cache cannot hide changed bytes.

Missing, malformed, expired, changed or unresolved coverage never yields a
confident `fresh`. A source row without a usable identity makes coverage
incomplete. A positive exact match to a validated available source can still
report `duplicate` with `coverage_complete=false`; other incomplete results are
`suspect`. Expired/changed/corrupt projections cannot establish a positive match.
`fresh` is limited to the explicitly listed source snapshot and its revision;
it does not prove universal novelty or authorize future actions. Explicit
`ledger_rows`/`queue_entries` callers supply their own snapshot and own its age.

The compatibility `filter_batch` return name `fresh` means retained for intake;
advisory rows are retained with `dedupe_advisory` evidence. Only exact duplicates
are withheld. Batch keys are registered after validation succeeds in staging,
so an invalid early row cannot suppress a later valid row with the same posting.

Limits: 64 sources, 100,000 aggregate rows, 64 MiB per file, 128 MiB aggregate
input, 32 KiB per row, 256 MiB database, 10,000 candidates per indexed batch,
300-second default snapshot age, at most one day configurable age. Individual
lookups hash input bytes; use the batch API for large discovery batches. A source
file that changes while being read, symlink or hardlink source is rejected.
New database files use mode 0600 on POSIX. Existing databases and SQLite
sidecars must be private, operator-owned regular files; the immediate parent
must be operator-owned and not writable by other users.

Keep the workspace in an operator-controlled directory. SQLite/source hashes
are consistency evidence, not tamper-proof authentication against an attacker
who can rewrite the local database, configuration or source files. Source files
are not locked together; results are revision-scoped observations, not a global
linearizable snapshot across independent writers. The host must recheck relevant
identity and authorization when performing any later consequential action.

## Human-reviewed aliases

No name-based company alias can merge all future jobs. A review can approve only
an exact pair of posting/URL identities. Alias pairs do not propagate transitively
and apply only under the reviewed source configuration. Accept/revoke requests
are retained in the `alias_reviews` table; the current pair state lives in
`aliases`.

```python
from dedupe_index import DedupeIndex, Source

index = DedupeIndex(
    "/absolute/keel/data/identity-index.sqlite3",
    [Source("west", "/absolute/keel/data/queues/western-queue.json")],
    authenticate_review=host_verify_review,
)
receipt = index.review_alias(
    original_url, reposted_url,
    evidence_ref="operator-review:review-id",
    credential=authenticated_review_credential,
    action="accept",  # Explicit authenticated action="revoke" removes the pair.
)
```

The host must supply `host_verify_review(request, credential)`. It must verify
operator authority, expiry, replay protection and binding to the complete request
(action, two identities, evidence reference, index path and source configuration
revision), returning the authenticated operator ID string. Missing callbacks,
boolean approval flags and non-string authentication results are refused. There
is no bundled claim of real operator authentication and no alias CLI bypass.

## Validation and scale exercise

`tests/test_identity_index.py` covers false merges, exact matches, multiple queue
sources, stale/corrupt/missing state, same-length source edits with preserved
mtime, replay, query and path case, incomplete coverage, reviewed aliases and
revocation, batch consistency, staging and intake call paths.

```bash
PYTHONPATH=engines python3 -B tests/benchmark_identity_index.py --output /tmp/identity-benchmark.json
```

The scale exercise builds 10,000 and 100,000 synthetic postings, checks 1,000
known identities per batch, and verifies that a distinct posting remains fresh.
It reports real elapsed time and file sizes for that host. It is not a live
accuracy study, provider authentication test, or guarantee for another machine.
