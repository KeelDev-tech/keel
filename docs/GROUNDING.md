# Keel 0.10: source and packet content binding

This additive, dependency-free Python package connects existing trust claims to
actual local files, preserves the existing scoped answer resolver, and checks
the full declared packet content. It does not run models, issue approvals,
change canonical records, or execute an application. The separate `keel_eval`
package measures labelled reviewer responses and only calls a local model when
the operator explicitly invokes its `local` command.

## Trust boundary

The canonical host supplies the trust export, form context, approved answer
bank, and independently obtained approved packet-manifest digest. These are
trusted host inputs; this package does not authenticate people or source
publishers. Do not accept them from an uploaded document, model output or
untrusted HTTP request. A successful hash comparison means identical bytes;
it does not establish source truth or that approved prose follows logically
from those bytes. All reports retain `execution_authorized: false`.

No-AI, unaided work, consent, attestation, holds, unknown attempts, rate-limit
protection and all earlier authority checks remain applicable. Content checks
are an additional requirement. They cannot clear an earlier hold.

## Evidence contract

`verify_grounding(document, bindings, root=..., now=...)` re-runs the existing
`keel_trust.evidence.evaluate` reducer. `document` is its unchanged schema.
`source.content_hash` must now be the SHA-256 of actual file bytes, not a hash
of a description. The bindings object has exactly these fields:

```json
{
  "schema": "keel.grounding.bindings.v1",
  "trust_snapshot_sha256": "canonical digest of the complete trust document",
  "sources": [{
    "source_id": "profile", "revision": "v1", "path": "profile.json",
    "media_type": "application/json",
    "selectors": [{"selector_id": "skill", "kind": "json_pointer", "pointer": "/skill"}]
  }],
  "claims": [{
    "claim_id": "skill-claim", "revision": "v1", "value": "Example value",
    "selections": [{"source_id": "profile", "selector_id": "skill"}]
  }]
}
```

Source and claim coverage must be exact and unique. Every evidence source for
a claim must supply an exact selected value matching that claim's `value_hash`.
The value is compared as canonical JSON, preserving distinctions such as false,
zero, strings and null. Revisions, expiry, conflicts, approved wording, scope,
and transitive artifact invalidation are retained from the trust graph.

Supported source formats are strict UTF-8 JSON, UTF-8 text and binary identity.
Text selectors use `{selector_id, kind:"utf8_bytes", start, end}` with exact
half-open byte offsets. JSON selectors use strict JSON pointers. Duplicate
keys, malformed escapes, nonfinite values, byte offsets splitting UTF-8,
traversal, symlinks, hardlinks, nonregular files and concurrent file/path
replacement are rejected. Text has no whitespace or Unicode normalization.
Binary sources have no selectors and cannot provide semantic claim support.
Files are bounded to 2 MiB in this adapter, selected material is bounded, and
bundles have source/claim/file count and aggregate byte limits.

The API reports exact source-value binding, not natural-language entailment.
PDF/DOCX/OCR semantics and rendered pages are outside this version. Convert to
a reviewed text/JSON representation upstream while retaining the original
attachment digest; a conversion itself is not proof of source truth.

## Scoped answers

`resolve_grounded_answer(records, context, answer_bindings, document, bindings,
root=..., now=...)` first invokes the existing `keel_local.answers.resolve_answer`.
It then requires a JSON selector containing the entire selected answer record,
matching its canonical hash, source reference and verification reference.

Each answer binding is exactly `{record_hash, source_id, selector_id, claim_id,
claim_revision}`. Facts and experience require a grounded claim with the same
value, candidate, kind, revision and evidence source. The trusted question
adapter must supply `context.claim_predicate`, matching the claim predicate.
The claim's allowed scopes must include the canonical digest of
`{candidate_id, employer_id, posting_id}` from context (`posting_id` is null
when absent). This prevents an equal answer value from being reused against an
unrelated question or applicant.

Consent, attestation and essay records instead require null claim references.
Their original explicit authorization rules still apply. No factual claim
authorizes a personal decision. A recorded false consent stays false.
Missing or changed records return `NEEDS_USER`.

## Packet contract

`verify_packet(document, bindings, packet, evidence_root=..., packet_root=...,
now=..., expected_packet_sha256=...)` freshly resolves evidence. The expected
canonical packet-manifest digest is a mandatory, separate trusted host input;
obtain it from the existing authenticated review context. Do not calculate it
from an untrusted incoming manifest and treat that as an approval.

The manifest fields are exactly `schema:"keel.grounding.packet.v1"`,
`trust_snapshot_sha256`, `workspace_id`, `subject_id`, `scope`, `artifacts`,
`attachments`. Every artifact entry has exactly `artifact_id`, `revision`,
`path`, `sha256`, `format`, `fields`. Selected artifacts must include their
dependencies and match the approved trust graph, workspace, subject and scope.

Two full-content formats are supported:

* `text_lines`: `fields` is empty. Bytes equal the artifact's approved statement
  wording joined by LF, with a final LF; zero statements means empty bytes.
* `json_fields`: `fields` maps each unique `field_id` to a unique
  `statement_index`, covering every statement exactly once. Actual strict JSON
  is exactly the object mapping those field IDs to approved wording. Extra or
  missing fields, duplicate JSON keys, or altered values block verification.

Attachments are `{attachment_id, path, sha256}` and receive byte identity checks
only. Their membership and field mappings are pinned by the independently
supplied manifest hash; authentication of that approval remains the host's
responsibility. The verifier reads only declared files. A downstream consumer
must consume that same declaration and never append arbitrary directory files.

These are observations at verification time, not reusable dispatch tokens or
atomic multi-file snapshots. A live executor must recheck existing authority,
freshness and all exact hashes immediately before use, or consume the exact
immutable approved content-addressed bytes. Never infer permission from a
stored `VERIFIED` result.

## Running locally

For the existing 0.9 Workbench path, call
`keel_grounding.integration.build_grounded_proof(workbench, bindings, packet,
expected_packet_sha256=..., workspace_id=..., synthetic=..., action="PREPARE",
evidence_root=..., packet_root=..., attachment_root=..., now=...)`.
It derives evidence solely from the Workbench's canonical trust export, freshly
runs the existing capture/review proof, and matches each role's artifact and
packet dependency binding. It retains the original proof verbatim. A role can
reach `CONTENT_REVIEW_CHECKS_PASSED` only when both the old gates and the new
content checks pass; the report still grants no execution authority.

```sh
python3 -B -m keel_grounding demo --home /existing-parent/new-synthetic-demo
python3 -B -m keel_grounding evidence --document trust.json --bindings bindings.json --evidence-root /private/evidence --out /private/new-evidence-report.json
python3 -B -m keel_grounding packet --document trust.json --bindings bindings.json --evidence-root /private/evidence --packet packet.json --packet-root /private/packet --expected-packet-sha256 TRUSTED_DIGEST --out /private/new-packet-report.json
python3 -B -m keel_grounding answer --document trust.json --bindings bindings.json --evidence-root /private/evidence --records answers.json --context context.json --answer-bindings answer-bindings.json --out /private/new-answer-report.json
```

The synthetic demo uses a fixed synthetic clock and never becomes current live
evidence. Operational CLI commands always use the actual UTC clock and refuse
to overwrite output files. Reports omit source passages; a resolved answer
report necessarily contains the chosen answer, so keep reports private.

See `GROUNDING_EVALUATION.md` for replay and explicit local model evaluation.
No paid API, model download, browser installation or external service is needed
for the verification package. Actual inference requires suitable local hardware
and an independently chosen, licensed model installation.
