# Keel 0.8 source capture

`keel_sources.capture` captures supplied source records and copies actual attachment
bytes. It does not discover a job page, scrape a form, make a model call, manufacture
a policy, confirm a profile fact, or approve an application. A host producer must
map its actual records to this contract without dropping restrictions.

An existing source can be incomplete or unsupported. Preserve that truth upstream;
do not fill fields with defaults to make a revision appear ready. Empty allowlists,
missing source timestamps, absent attachment requirements, unknown constraints,
and unsupported schemas fail closed.

## Source envelope and scope

All six sources use the existing 0.7 descriptor:

```json
{
  "source_ref": "host:actual-record-id",
  "source_version": "actual-immutable-version",
  "observed_at": "2026-09-18T12:00:00+00:00",
  "expires_at": "2026-09-18T12:10:00+00:00",
  "revoked": false,
  "record": {}
}
```

The timestamps above illustrate syntax, not a permissible freshness update.
Retain the original observation time, version, reference, expiry and revocation.
`scope` has exactly `workspace_id`, `role_id`, `application_id`, and `action`.
Source and scope identifiers follow the 0.7 token format; URLs are exact HTTPS
destinations without credentials or fragments.

`validate_source(component, descriptor, *, scope, attachment_root, now,
allow_inactive=False)` returns a defensive copy of the original supported JSON
descriptor. It raises `SourceError` with a fixed diagnostic code on invalid input.
The storage layer may set `allow_inactive=True` to retain expired or revoked
evidence. This does not refresh or remove those attributes; readiness checks still
reject them. Future observations remain invalid even for storage.

The record schemas below are intentionally narrow. Extra record fields are blocked,
not discarded. Each record may have a descriptive `metadata` object, which must
never be used to hide an unimplemented restriction. Add a supported schema and
tests before mapping additional form or policy rules.

## Actual record schemas

| Source | Required record data |
| --- | --- |
| `policy` | `policy_id`; `rules` with nonempty `allowed_actions`, `allowed_accounts`, `allowed_destinations`; boolean `requires_human_approval`; `holds` list. No unknown rule keys are accepted. |
| `form` | `form_id`; nonempty `fields`; explicit `required_attachment_purposes` list, including an actual recorded empty list when none are required. |
| `answers` | `fields` mapping field IDs to strings or booleans; `provenance` mapping exactly the same field IDs to origin records below. |
| `attachments` | `files` list. A genuinely empty manifest must explicitly contain `none_required: true`. Each captured item has `path`, `purpose`, `sha256`, `size_bytes`. |
| `target` | `role_id`, `application_id`, `canonical_posting_url`, `application_url`, boolean `verified`, and the actual `verification_ref`. |
| `route` | `role_id`, `application_id`, `action`, `account_id`, `transport`, `destination`, `target_source_ref`, `target_source_version`. |

A form field has `field_id`, a nonempty `label`, `kind` (`text`, `select`, or
`checkbox`), and boolean `required`. A select field additionally has a nonempty
list of unique string `options`; other kinds cannot have options. Optional
`assistance_allowed` is a boolean; missing or false requires a human-origin answer.
Optional `answer_class` is `fact`, `statement`, or `attestation`; missing means
`fact`. Attestations always require a human-origin answer. Unknown field constraints
such as an unimplemented `max_length` block instead of being ignored.

An answer provenance record has exactly:

```json
{
  "source_ref": "host:original-answer-or-fact",
  "source_version": "v17",
  "origin": "verified_profile",
  "observed_at": "2026-09-18T11:59:00+00:00",
  "evidence_refs": ["host:actual-supporting-record"]
}
```

Allowed origins are `human`, `verified_profile`, and `generated_draft`. Profile
answers require at least one evidence reference. A generated draft cannot have
factual evidence references or answer a fact/attestation field; only explicitly
assisted statement fields permit it. Human provenance can have an empty
`evidence_refs` list when the original human answer itself is the source. Origin
observations cannot postdate the enclosing answer source observation.

These are host assertions. This module checks provenance structure and binding;
it does **not** dereference evidence records, compare an answer to underlying
documents, or authenticate a human. A model can place a factual assertion inside
a statement, so an allowed draft still needs the host's truth review. Results
explicitly set `factual_support_verified: false` and
`source_authenticity_verified: false`. The producer/host must resolve supporting
evidence and enforce its existing truth and identity gates before execution.

## Cross-record review checks

`validate_semantics(sources, *, scope, attachment_root, now)` returns
`ready_for_approval`, issue codes, and `execution_authorized: false`. It checks:

- Every source exists, is fresh, is not revoked, and has a supported record shape.
- Policy action/account/destination allowlists match exactly; any active hold
  blocks. This capture contract requires human approval even if a supplied policy
  claims otherwise. It never removes existing holds or consent rules.
- Route scope, target source reference/version, and application URL match the
  actual target record. An unverified target blocks.
- Required answers exist, text is nonempty, select values are listed options,
  and checkbox values are booleans. A required checkbox must be checked.
- Unknown answers, absent/mismatched provenance, unsupported generated claims,
  and nonhuman answers to human-only fields block.
- Required attachment purposes exist, and actual bytes match recorded hash/size.

`ready_for_approval` means the six supplied records satisfy this review contract.
It is not evidence authenticity, factual truth, an assurance envelope, an approval,
a claim that a browser observed these values, or authorization to submit. The
approval workflow binds its decision to the exact six content revisions.

## Attachment capture and durable publication

```python
captured = ingest_attachments(
    supplied_descriptor,
    source_root="/private/incoming",
    attachment_root="/private/keel/attachments",
    scope=scope,
    now=explicit_utc_time,
)
```

Input manifest items require `path` relative to `source_root` and `purpose`.
Optional supplied hash/size must match. The capturer preserves the envelope and
file metadata, records the original relative path as `source_path`, and replaces
`path` with `objects/<actual-sha256>`. It computes `sha256` and `size_bytes` from
bytes actually read. Optional file data includes `filename`, `mime_type`, and
descriptive `metadata`. Declared metadata is preserved, not independently verified.

The implementation opens each directory and file without following symlinks,
rejects nonregular files and hardlinks, checks file identity/size/times before
and after reading, and enforces the 0.7 limits: 32 files, 16 MiB per file, 64 MiB
total. Duplicate content in one manifest is rejected because 0.7 requires distinct
attachment paths. Supply the document once with its intended purpose.

The target root and object directory are private (0700); new object files are
0600. All inputs are validated before copying. A complete temporary file is
flushed, then published with Linux `renameat2(RENAME_NOREPLACE)`, followed by a
directory flush. Existing objects are never overwritten, including corrupt
objects under a correct-looking filename. Identical existing bytes can be reused
after validation. A process crash before publication may leave a temporary file;
it cannot leave a partial authoritative object and does not prevent retry.

Attachment publication requires Linux with `renameat2` and filesystem support
for `RENAME_NOREPLACE`. Unsupported hosts receive
`atomic_attachment_publication_unavailable`; there is no overwrite fallback.
The parent of `attachment_root` must already exist. A private root can be created
by the capturer, but an existing permissive root is rejected rather than silently
changing its permissions. Capture never changes source files.

The fixture helper in `tests/test_sources_capture.py` contains a complete example
using synthetic names, `.invalid` URLs, and real local fixture bytes. It is test
data only and must never be imported as evidence for a live role.
