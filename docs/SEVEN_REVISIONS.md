# Seven source revisions: close the exporter gap first

The receiving agent reported 837 leads blocked because policy, form, answers,
attachments, target, approval, and route revisions are absent. This package does
not contain that live database or its schema. Those reported counts are not a
reproduced production result. `keel_agent.revisions` supplies a strict normalized
adapter contract and read-only exporter; the receiving host must explicitly map
its actual records into it. No paid service or model call is involved.

## What this does

`export_revisions(snapshot, *, attachment_root, now)` returns content-bound SHA-256
revisions, per-component diagnostics, and grouped missing-source categories. It
never writes to the canonical store, invents a missing record, refreshes an old
record's observation time, grants approval, creates assurance/trust envelopes, or
performs an application action. Outputs omit answer values and attachment bytes.

`envelope_inputs_ready=true` means the seven source records are structurally valid,
current, and consistently bound. It is **not application readiness**: a form can
still have unanswered required questions, policy can still prohibit an action,
and existing holds may still apply. `source_authenticity_verified` and
`execution_authorized` are always false. The host must authenticate the source
reader and approval actor, validate facts/form semantics, and conjunctively apply
existing assurance, trust, workflow, consent, dedupe, 429, and other restrictions.

## Exact normalized contract

Top-level object:

```json
{
  "schema": "keel.revision_sources.v1",
  "workspace_id": "workspace-opaque-id",
  "snapshot": {
    "source_ref": "export:canonical-snapshot",
    "source_version": "actual-snapshot-version",
    "observed_at": "2026-09-18T12:00:00+00:00",
    "expires_at": "2026-09-18T12:01:30+00:00"
  },
  "roles": [
    {
      "role_id": "canonical-role-id",
      "application_id": "canonical-application-id",
      "action": "submit",
      "sources": {}
    }
  ]
}
```

The dates above illustrate schema only. They must not be copied into a live export.
Every present source descriptor has `source_ref`, `source_version`, `observed_at`,
`expires_at`, and `record`. The reference/version must identify the actual record;
the times must come from its real capture and configured validity policy, not from
the exporter's current clock. The `snapshot` descriptor likewise identifies an
actual canonical export. Optional descriptor `revoked=true` blocks the component.
All timestamps require timezone offsets. Opaque IDs may contain letters, digits,
`_ . : / @ + -`, up to 256 characters.

| Component | Required actual `record` shape | Authoritative source to map |
|---|---|---|
| `policy` | `policy_id`, nonempty `rules` object | Active policy/hold/consent configuration version |
| `form` | `form_id`, nonempty `fields` array with distinct `field_id` values | Captured form definition and version, including required flags/options |
| `answers` | `fields` object keyed by field ID | Exact candidate answer values and supporting references |
| `attachments` | `files` array of `{path, purpose}` records | Approved document manifest and actual local regular files |
| `target` | `canonical_posting_url`, `role_id`, `application_id` | Verified role/application identity |
| `route` | `account_id`, `action`, `destination`, `transport` | Bound destination/account/action configuration |
| `approval` | Detailed binding below | Actual recorded human approval and authority reference |

Additional record fields participate in the material hash. Include every field
that can change the decision, not just the required structural keys. Do not remove
restrictions during normalization. Shared policy/form records may be read once
and reused by the host across roles; revisions remain bound to each role scope.
Do not generate 837 artificial source versions for one actual shared record.

An explicitly empty attachment manifest is permitted only as an actual source
record `{ "files": [], "none_required": true }`. A missing manifest does not
mean no attachments are required. File paths must be relative to the explicit
attachment root. On POSIX every root/path component is opened without following
symlinks. Only regular files are accepted, with maximum 16 MiB per file, 64 MiB
total, and 32 files per role. Bytes and size are hashed, with metadata stability
checked during reads. Supplied digests never substitute for reading the bytes.
These controls establish bytes observed; they do not prevent a privileged host
from changing storage afterward. Execution must recheck or use immutable staged
copies of the exact approved bytes.

## Approval is the seventh revision, with no circular hash

First export the six nonapproval source records. Use their `revisions` map to
present the exact proposed materials to the existing approval interface. **Do not
have a model fill in the approval record.** When a real authorized decision exists,
map its record as follows:

```json
{
  "approval_id": "actual-approval-id",
  "actor_id": "actual-human-id",
  "authority_record_ref": "authority:actual-record-id",
  "decision": "APPROVE",
  "scope": {
    "workspace_id": "workspace-opaque-id",
    "role_id": "canonical-role-id",
    "application_id": "canonical-application-id",
    "action": "submit"
  },
  "component_revisions": {
    "policy": "actual-six-component-digest",
    "form": "actual-six-component-digest",
    "answers": "actual-six-component-digest",
    "attachments": "actual-six-component-digest",
    "target": "actual-six-component-digest",
    "route": "actual-six-component-digest"
  },
  "approved_at": "actual-aware-ISO-timestamp",
  "expires_at": "actual-aware-ISO-timestamp",
  "revoked": false
}
```

The placeholders are documentation, not valid approval evidence. The approval
descriptor must have been observed after its recorded decision. An approval binds
exactly the six nonapproval revisions; adding its own digest is rejected. Any
material, source version, scope, destination, account, or file-byte change
invalidates reuse. Expired, revoked, rejected, future-dated, or unbound approvals
remain blocked. Authority record references are opaque provenance, not proof of
identity: the host still must verify the actual actor and authority.

Observation and descriptor expiry timestamps are deliberately excluded from a
component's material digest. A genuinely reobserved unchanged record can retain
its review identity, but timestamps and revocation are independently enforced on
every export. Source-version changes invalidate identity. Approval's own decision
time and expiry are part of the approval record and therefore material.

## Avoid turning one integration gap into hundreds of user requests

Missing `sources.<component>` defaults to `source_not_exported`, owned by
`export_adapter`, responsibility `system`. A missing exporter column is not
evidence that the candidate must answer a question or grant consent.

The host can explicitly distinguish:

```json
{"absence": {"kind": "SYSTEM_NOT_EXPORTED"}}
```

or a genuinely missing source record:

```json
{"absence": {"kind": "SOURCE_RECORD_MISSING"}}
```

Only an observed approval-source absence with a real decision request reference
can be assigned to the human:

```json
{
  "source_ref": "approval-lookup:actual-reference",
  "source_version": "actual-version",
  "observed_at": "actual-aware-ISO-timestamp",
  "expires_at": "actual-aware-ISO-timestamp",
  "absence": {
    "kind": "HUMAN_DECISION_REQUIRED",
    "decision_request_ref": "actual-request-id"
  }
}
```

`root_causes` groups by component, diagnostic reason, source reference, and
responsibility. For 837 synthetic leads with all seven mappings absent, it reports
seven system-owned missing-source categories, each affecting 837 leads, and zero
human requests. This is diagnostic deduplication, not a claim that seven proven
production root causes have been investigated.

`scope_count` counts distinct role/application/action tuples and
`unique_role_count` counts distinct role IDs. Diagnostic groups likewise distinguish
`affected_scopes` from `affected_roles`. The compatibility `role_count`,
`ready_role_count`, and `blocked_role_count` fields count exported scope rows.

## Receiving-host integration sequence

1. Inspect the actual exporter and canonical table/file schemas. Write an explicit
   field mapping for each row above. Preserve current source observations and
   existing restrictions. Mark unmapped fields missing; do not infer lookalikes.
2. Read shared sources once in a consistent snapshot. Supply real role IDs,
   application IDs, versions, source references, records, and attachment files.
3. Run this exporter in shadow mode. Resolve system-owned source gaps in the
   exporter before creating human work. A true missing answer or approval remains
   a separate, evidenced decision in the established review workflow.
4. Feed complete revisions plus authenticated original records into the existing
   host envelope builders. This module supplies no invented envelope. Run all
   preexisting gates conjunctively; readiness from one branch cannot replace
   another branch's result.
5. Revalidate and recompute immediately before execution. Report before/after
   source coverage, diagnostic categories, authenticator results, unchanged holds,
   and actual canonical write/network/model-call counts. Never claim that the
   source package alone repaired the reported live export.

## Reproducible synthetic demonstration

```sh
python3 tools/make_revision_demo.py --out /tmp/keel-revision-demo
python3 -m pytest tests/test_agent_revisions.py -q
```

The demo writes fixtures and reports only to the requested local directory. It
creates 837 synthetic incomplete roles and one complete synthetic example with
actual local attachment bytes. Its synthetic approval is a test fixture, never a
live permission. Runtime implementation uses Python's standard library; pytest is
an optional free development dependency for the tests.
