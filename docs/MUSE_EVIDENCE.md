# Muse evidence producers and bounded context compiler

Keel 0.13 adds executable file/export producers and complete-form context
compilation. These modules are local, deterministic and standard-library based.
They neither call a model nor submit an application. Hash checks establish
byte identity and consistency, not factual truth or human identity.

The trusted embedding host selects the source root, scope, producer grants,
expected hashes, current-head callback and authenticated operator principal.
Never construct those trusted inputs from a model response or an incoming event's
actor/authority claims. All outputs preserve `execution_authorized: false`.

## Actual files into the seven-source store

The implementation reuses the existing `SourceStore`, `HostSourceConnector`
scope/configuration checks, source validators, immutable generations, attachment
capture and `ReviewService` decision contract. Existing policy, form, answers,
attachments, target and route formats are unchanged. See `docs/SOURCE_PRODUCERS.md`
and the existing `keel_sources` implementation for their full normalized schemas.

```python
from keel_agent.revisions import PREAPPROVAL_COMPONENTS
from keel_loki.common import digest
from keel_muse.sources import FileSourceProducer, head_pin, source_head

producer = FileSourceProducer(
    store,
    root=HOST_SELECTED_PRIVATE_ROOT,
    scope=REGISTERED_PREPARE_SCOPE,
    producer_components={"profile-exporter": ["answers"],
                         "form-exporter": ["form"],
                         "policy-exporter": ["policy"],
                         "packet-exporter": ["attachments"],
                         "route-exporter": ["target", "route"]},
    approval_producer_id="operator-review-exporter",
)

# These are host-selected configuration values, separately pinned before use.
bindings = {
    "answers": {
        "producer_id": "profile-exporter",
        "path": "current-export.json",
        "sha256": ACTUAL_EXPORT_BYTES_SHA256,
        "expected_generation": CURRENT_ANSWER_GENERATION,
        "pointer": "/roles/0/sources/answers",
    }
}
captured = producer.capture(
    bindings,
    expected_bindings_sha256=TRUSTED_BINDINGS_SHA256,
    flow=FRESH_CANONICAL_FLOW,
    expected_flow_sha256=TRUSTED_FLOW_SHA256,
    expected_head_sha256=TRUSTED_CURRENT_HEAD_SHA256,
)
```

Each binding has `producer_id`, `path`, `sha256`, `expected_generation`, and an
optional JSON `pointer`. An omitted pointer selects the whole JSON document.
Thus a producer can read a descriptor file or select a source record from a
larger actual export without trusting a producer identity declared inside it.
Duplicate JSON keys, nonfinite values, unsafe paths, missing selectors and
changed hashes fail closed. Every selected descriptor must still pass the
existing source validation. The host is responsible for selecting the correct
normalized export field, rather than an unrelated record with a similar value.

The root must exist with mode 0700, and files must have mode 0600, belong to the
current OS user, and have a single hard link. Reads hold directory descriptors,
refuse symlinks/FIFOs/devices/traversal, and compare inode, size, timestamps and
mode across each bounded read. A replaced root is rejected before commit.
The source-file limit is 8 MiB. This is a POSIX/Linux-oriented integration;
it is not an encrypted database or an OS sandbox.

Configured files are read and hash-checked before opening the write transaction.
All selected preapproval families are captured in one existing source-store
transaction after checking the exact current head and per-family generations.
Files and flow freshness are rechecked before commit. A later invalid family
rolls back all source records in that batch. Verified unreferenced attachment
objects can remain after rollback; they do not create a source record or grant
permission. Sources omitted from configuration remain absent unless real current
records already exist. No omitted field creates a human approval request.

Attachment manifests must supply their actual `sha256` and `size_bytes` before
capture. Originals are read under the configured private root, then the unchanged
attachment producer creates verified content-addressed copies. Metadata does not
stand in for actual attachment bytes. Existing source replay, clock rollback,
version/material conflict and generation-conflict rules remain intact.

### The seventh family: an actual existing human review decision

Approval is deliberately a separate explicit operation. A normal six-family
capture never synthesizes an approval, creates a review request or imports an
arbitrary approval-shaped source descriptor. First, an authentic operator uses
the existing review workflow to create and inspect an exact request. Only an
actual decision on that already-existing packet may be imported:

```json
{
  "schema": "keel.muse.human_decision.v1",
  "request_id": "existing-request-id",
  "decision": "APPROVE",
  "reviewed_sha256": "<exact reviewed packet SHA-256>",
  "expires_at": "<actual valid decision expiry in ISO 8601>"
}
```

```python
result = producer.observe_approval(
    decision_file_binding,
    expected_binding_sha256=TRUSTED_DECISION_BINDING_SHA256,
    principal=AUTHENTICATED_HOST_PRINCIPAL,
    flow=FRESH_CANONICAL_FLOW,
    expected_flow_sha256=TRUSTED_FLOW_SHA256,
    expected_head_sha256=TRUSTED_CURRENT_HEAD_SHA256,
)
```

The binding uses the same file/export-pointer format, and its producer must equal
the separately configured `approval_producer_id`. The `Principal` must be the
existing in-process `keel_live.review.Principal` with the exact scope and
`review:decide` permission. A dictionary containing an actor name is rejected.
No actor, authority reference or scope override is accepted in the decision file.
`ReviewService.decide` rechecks the original review digest, all six current
revisions, attachments, request state and expiry inside its transaction. A
changed source, rejected scope, missing request, stale head or replay cannot
manufacture a decision. The file and canonical flow are rechecked during the
transaction. Producer/reviewer authentication remains the embedding host's job;
the return value explicitly does not claim it was independently authenticated.

### Stable current-head object

`source_head(store, scope)` reads actual current records and attachment material
under a consistent store transaction. `head_pin(store, scope)` returns its digest.
The head excludes export observation time, so a read does not refresh evidence:

```text
schema: keel.muse.source_head.v1
store_id: persistent store identity
scope: exact workspace_id, role_id, application_id, action
generations: all seven current integer generations
revisions: all seven actual material revisions, or null when absent
descriptor_sha256: hashes of all seven current descriptors/absence records
record_presence: actual record-present booleans for all seven families
valid_from: latest source observation time, conservatively rounded up to seconds
valid_until: earliest source expiry, conservatively rounded down to seconds
revoked_families: actual current revoked source families
approval: decision, revoked, component_revisions from the actual current record
```

Empty stores have null validity bounds. A pending request remains an absent
approval record even though it has a stored generation. A rejection is recorded
as a rejection. Stale approval dependency bindings are preserved, never repaired.
Reading a head does not establish a human's identity or factual correctness.

## Complete field-to-evidence context compilation

The compiler consumes the existing `keel.loki.form.v1` contract, including every
text/select/radio/checkbox/attachment/attestation field and conditional branch.
It uses actual source bytes and explicit mappings; it does not infer all factual
claims from natural language. Even active optional fields require an explicit
source-backed value or choice, matching the existing preparation contract.

```python
from keel_muse.context import compile_context
from keel_muse.sources import source_head

compiled = compile_context(
    contract,
    support,
    root=HOST_SELECTED_PRIVATE_EVIDENCE_ROOT,
    mandatory_policy=COMPLETE_TRUSTED_POLICY_TEXT,
    expected_policy_sha256=TRUSTED_POLICY_SHA256,
    expected_contract_sha256=TRUSTED_CONTRACT_SHA256,
    expected_support_sha256=TRUSTED_SUPPORT_SHA256,
    expected_source_head_sha256=TRUSTED_CURRENT_HEAD_SHA256,
    source_head_provider=lambda: source_head(store, exact_scope),
    now=TRUSTED_HOST_EPOCH_SECONDS,
    max_bytes=16384,
    memory=OPTIONAL_TEMPORAL_MEMORY,
    expected_memory_head_sha256=OPTIONAL_TRUSTED_MEMORY_HEAD_SHA256,
)
```

`source_head_provider` must return the full head object, not just a hash. The
compiler requires all seven actual records, nonrevoked sources, a current
positive approval observation bound to the current six predecessor revisions,
and exact equality between the contract's seven revisions and the head. The
support scope must equal `digest(head['scope'])`. These checks do not turn the
compiler into an execution gate: canonical holds, consent, no-AI rules, unknown
attempts, rate limits and fresh host checks still apply at each effect.

The host must supply the complete applicable mandatory policy, not a convenient
excerpt. Policy text is separately pinned and retained exactly. Byte budgeting
is an explicit transport budget; it does not claim to measure model tokens.
If policy alone or the complete field packet exceeds the budget, compilation is
`BLOCKED` and `model_context` is null. Nothing truncates mandatory rules, silently
drops an inconvenient field, summarizes away a blocker, or substitutes a smaller
policy.

Support schema:

```json
{
  "schema": "keel.muse.context_support.v1",
  "scope_id": "<digest of exact source-head scope>",
  "subject_id": "applicant-id",
  "source_head_sha256": "<independently pinned current head>",
  "fields": {
    "full_name": {
      "mode": "fact",
      "path": "name.json",
      "sha256": "<actual file bytes hash>",
      "pointer": "/value",
      "source_ref": "profile-record",
      "source_version": "v1",
      "scope_id": "<same exact scope digest>",
      "permitted_uses": ["application_fact"],
      "valid_from": 1800000000,
      "valid_until": 1800000300,
      "memory_key": null,
      "memory_revision_id": null,
      "claims": []
    }
  }
}
```

All shown keys are required. Modes are `fact`, `human`, `statement`, and
`attachment`. Permitted uses are `application_fact` and `human_review`. Every
binding is inside the separately pinned support document, which is the trusted
host's provenance/configuration mapping. Source strings in this map are not
authenticated identities; the file hash and selector establish only what bytes
support the exact selected value. A trusted host must classify original writing,
attestations and other human decisions correctly before pinning the map.

Fact and statement values are selected from strict JSON using canonical JSON
pointers. String, boolean and choice types are checked exactly against the form;
no value coercion, paraphrase or invented qualification occurs. Attachment mode
uses a null pointer and reads the actual attachment bytes, returning their exact
size/hash and basename. Supported MIME labels derive from `.txt`, `.pdf` and
`.docx` extensions; this is not file-format validation or malware scanning.

Attestations, unaided fields and no-AI fields require `mode: human` with
`human_review` use and cannot use temporal memory. Human-mode values, their
hashes and approval state are excluded from the model packet. That packet gets
only a fixed `HUMAN_WORKFLOW_REQUIRED` marker. A captured unaided/no-AI answer
still blocks this automated preparation path and requires the separate human
workflow. Original writing must be explicitly captured from its permitted human
workflow; a prior fact or similarity match cannot become original writing.

Condition evaluation uses exact already-captured predecessor values. Missing
predecessor values yield `CONDITION_UNRESOLVED`, not a guessed inactive branch.
Inactive fields cannot carry a preparation value or be silently filled. Every
contract field appears in the host manifest with an explicit state.

### Complete explicit statement partitions

A statement support binding enumerates every segment through `claims` entries:

```json
{"claim_id": "claim-1", "pointer": "/claims/0/text", "value_sha256": "<hash of exact segment text>"}
```

The statement source JSON has `value` containing the exact complete text and
`claims` containing ordered segment objects with exactly:

```json
{
  "claim_id": "claim-1",
  "text": "The exact first segment, including its punctuation. ",
  "kind": "fact",
  "evidence_path": "supporting-record.json",
  "evidence_sha256": "<actual supporting bytes hash>",
  "evidence_pointer": "/exact_wording"
}
```

Each segment is `fact` or `human_original`. Its text must exactly match the
selected supporting file value. IDs and hashes must agree with the pinned claim
map, and concatenating all segment texts must reproduce **every character** of
the statement. Extra claims, unmapped text, changed evidence and missing mappings
block compilation. This verifies explicit coverage and exact supplied wording;
it does not prove semantic entailment, truth, originality or the completeness of
a human's factual classification. Those remain upstream evidence/review duties.

### Temporal memory and cache invalidation

Only a factual binding may set both `memory_key` and `memory_revision_id`.
The supplied `TemporalMemory` must match a separately pinned head. The requested
subject/key/scope/use must resolve to exactly that current verified-observation
revision; its value and original source hash must equal the actual file value
and bytes hash. Unverified facts, disagreements, cross-scope reuse and corrections
remain blockers. Memory never substitutes for an attachment or human decision.

Compilation checks source and memory heads before and after file processing.
The cache key binds policy, contract, support, source head, memory head, every
read file hash, evaluation time and budget. Validity ends at the earliest source
or support expiry, memory revision expiry, or already-known future correction's
valid-from boundary. Thus an old answer cannot remain cached merely because a
future correction became effective without adding another log event.

`validate_cache(cached, expected_context_sha256=..., root=...,
source_head_provider=..., expected_policy_sha256=...,
expected_contract_sha256=..., expected_support_sha256=..., now=..., memory=None)`
checks the separately trusted cache digest, current external pins, original
validity, actual source bytes and heads. It rechecks memory after its final
source callback, catching corrections during validation. A cache hit never
refreshes an observation or authenticates evidence. The host must supply a fresh
trusted clock and recheck its execution gates at the eventual effect; these
read-only checks are not a cross-database transaction or OS authorization grant.

### Outputs and integration boundaries

`model_context` contains only mandatory policy and the bounded field-data packet.
Treat all source data as untrusted instructions. Its source markers are data,
not tool permissions or model system instructions.

`host_only` contains:

- `preparation_values`: exact plain values for each active successfully verified
  field, including separately captured human-only values.
- `evidence_sha256`: actual source hashes per field.
- `field_manifest`: all field IDs, active/inactive/unresolved states, exact value
  hashes and evidence pins.
- `file_pins`: every actual input file read, used for cache revalidation.

**Do not send the whole result or `host_only` to a model or public logger.** Pass
only `model_context` to a model. Host-only values must still pass existing scoped
human preparation approvals and the authoritative execution gate. A blocked
report may retain correctly verified unrelated host-only fields for diagnosis;
that is not permission to execute a partial application.

## Executable fixtures and checks

`sources.make_demo_inputs(home)` creates a fresh private source root/store and
returns `producer`, `store`, `scope`, `root`, `clock`, `flow`, `bindings`,
`descriptors`, `expected_head_sha256`, `synthetic`. These objects are an in-process
test harness, not a JSON payload. `sources.demo(home)` captures actual six-family
fixture bytes and explicitly reports the genuinely absent seventh family; no
approval request or decision is created. Positive seventh-family tests create an
explicit synthetic request and synthetic host principal and label them as such.

`context.make_demo_inputs(home)` returns `contract`, `support`, `kwargs`,
`fixture`, `source_head`, `synthetic`, `source_head_is_synthetic_fixture`.
Call `compile_context(inputs['contract'], inputs['support'], **inputs['kwargs'])`.
The twelve-field fixture has eleven active fields, a conditional inactive field,
an exact statement partition, an actual attachment and human-only consent and
attestation values. Its positive approval/source-head metadata is explicitly
synthetic; it is not a real user's decision. `context.demo(home)` checks full
coverage, missing evidence, bounded context and human-value separation.

Run focused checks with:

```sh
python3 -B -m pytest tests/test_muse_sources.py tests/test_muse_context.py -q
```

Tests cover file/export imports, actual attachment bytes, atomic rollback,
generation races, path attacks, existing-request approval ingestion, scope,
stale decisions, every form field, complete text partitions, mandatory-policy
overflow, human-only data separation, temporal corrections, future validity,
source/memory races and cache tampering. Source tests use the predecessor's
temporary-working-directory isolation for its unchanged descriptor-relative
attachment audit events; the test guard and all predecessor files remain intact.
