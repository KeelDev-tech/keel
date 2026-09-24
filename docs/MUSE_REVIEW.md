# Keel private review desk

The review desk projects an adapter-supplied snapshot into exact questions,
application and packet revisions, changes, evidence references, holds, a timeline,
and observed measurements. It writes a standalone HTML file with local search,
status filters, keyboard-accessible tabs, and review-request downloads. It does
not call Muse, a model, an API, a browser service, or a canonical application host.
No server, JavaScript package, remote asset, account, or subscription is required
to open the generated HTML in a modern browser.

Every downloaded decision is an **unauthenticated review request**. Selecting
“Request approval” cannot approve an application. Keel's authenticated host must
ingest the request, re-read current authoritative records and consent, authenticate
the human, show the exact scope, and record a fresh decision through its existing
approval workflow. Existing holds continue to dominate.

## Python API

```python
from pathlib import Path
from keel_loki.common import digest
from keel_muse.review import (
    example_snapshot, project_review, make_review_request,
    validate_review_request,
)
from keel_muse.dashboard import render_dashboard, write_dashboard, demo

snapshot = example_snapshot()  # Explicitly synthetic fixture; replace with adapter records.
report = project_review(
    snapshot,
    now=snapshot["captured_at"],
    expected_snapshot_sha256=digest(snapshot),
)
html = render_dashboard(report)
# The parent directory must exist. Existing files and symlink targets are refused.
output = write_dashboard(Path("/private/existing/directory/review.html"), report)

request = make_review_request(
    report,
    application_id="app-01",
    question_id="review-01",
    response="approve",
    now=report["as_of"],
    expected_report_sha256=digest(report),
)
check = validate_review_request(
    request,
    report,  # Host must provide a fresh report from current authoritative input.
    expected_current_report_sha256=digest(report),
    now=report["as_of"],
)
assert check["status"] == "BINDINGS_MATCH"
assert check["execution_authorized"] is False

# Writes review.html and review-summary.json to an existing, empty private directory.
summary = demo("/private/existing/demo-directory")
assert summary["synthetic"] is True
assert summary["browser_render_verified"] is False
```

`render_dashboard()` and request creation recompute the supplied projection before
using it; callers cannot replace missing measurements with fabricated success.
Hash pins bind the supplied bytes. They do not authenticate source truth, model
quality, the adapter, or human identity.

The importer checks request schema and exact values, including its entire current
report hash, application revision, packet artifact revision, displayed packet
hash, question hash, response scope, and blockers. It also reprojects each allowed
response scope at ingestion time: crossing an evidence expiry or deadline makes
an old request invalid. Elapsed seconds alone do not invalidate a request. A new
report has a new binding and requires a new request. Import validation still
grants no authority and cannot determine whether the caller supplied authentic
upstream records.

## Explicit adapter input

`example_snapshot()` is an executable, complete schema example. The input is
strict JSON; extra keys, duplicate IDs, future observations, invalid hashes,
nonfinite metrics, and ambiguous booleans are rejected. Timestamps are integer
Unix seconds. Hashes are lowercase SHA-256 hex digests. Nullable timestamps are
explicitly unknown. No fields are inferred from a role title or model output.

The top-level `keel.muse.review-snapshot.v1` object has these exact fields:

| Field | Meaning |
| --- | --- |
| `schema` | `keel.muse.review-snapshot.v1` |
| `snapshot_id` | Stable adapter snapshot ID |
| `source_sha256` | Caller-supplied upstream export binding |
| `captured_at` | Capture timestamp, at or before projection time |
| `telemetry_complete` | Caller assertion that the event capture is complete |
| `applications` | Application records described below |
| `events` | Observed event records described below |

Each application contains exactly:

```text
application_id, role, employer, created_at, deadline,
application_revision_sha256, packet, previous_packet,
holds, questions, evidence, approval_observation
```

`deadline` may be null; reaching a supplied deadline creates `DEADLINE_PASSED`.
`holds` is a list of `{code, since}` with nullable `since`; every supplied hold
remains present. No local interaction removes it.

`packet` and `previous_packet` are null or:

```json
{
  "revision_sha256": "<actual packet artifact revision hash>",
  "fields": [
    {"field_id": "motivation", "label": "Why this role?", "value": "Exact text",
     "required": true, "evidence_ids": ["employment"]}
  ],
  "attachments": [
    {"attachment_id": "resume", "name": "Resume.pdf", "sha256": "<actual attachment hash>"}
  ]
}
```

Field values may be null. Empty required values block preparation; nonempty values
need fresh, verified, explicitly scoped evidence permitted for `application_fact`.
The displayed packet hash separately binds every projected field and attachment
record. Deltas compare stable field and attachment IDs, preserve before/after
records, and expose additions, changes, and removals. Changed content under the
same supplied artifact revision creates a blocker. Without a previous packet,
comparison is explicitly unavailable. A packet's presence does not prove that an
adapter supplied every field from the original form.

Questions use the existing Loki question scheduler schema:

```json
{
  "question_id": "review-01",
  "kind": "approval",
  "scope_ids": ["app-01"],
  "prompt": "The exact question shown by the authoritative source",
  "estimated_minutes": 2,
  "reuse_authorized": false
}
```

Kinds are `fact`, `approval`, `attestation`, and `unaided`. Only factual responses
with explicit `reuse_authorized: true` may cover multiple declared application
IDs. Every scoped application must include the identical question definition.
Approval, attestation, and unaided responses have one application scope and cannot
be reused. The interface leaves responses empty; unaided questions receive no
generated text. Ranking remains conditional on an honest usable answer, using
the existing age/deadline fairness and blocker logic; it predicts no success rate.

Evidence records contain exactly:

```text
evidence_id, source_id, source_revision_sha256, label, reference,
kind, status, scope_ids, permitted_uses, observed_at, valid_until, binding
```

`reference` is nullable text and is never a clickable URL. `kind` is `source`,
`receipt`, or `review`. `status` is the caller's `verified`, `unverified`, or
`rejected` observation. `scope_ids` is nonempty. `permitted_uses` is a nonempty
subset of `application_fact`, `planning`, `review`. Unknown observation time,
expiry, missing permission, or absent scope cannot become verified evidence.
`valid_until` is nullable, otherwise later than a known `observed_at`.
`binding` is null or `{application_revision_sha256, packet_revision_sha256}`;
submission receipt observations require both hashes to match the current packet.

`approval_observation` is null or exactly:

```text
application_id, application_revision_sha256, packet_revision_sha256,
packet_projection_sha256, observed_at, decision, source_record_sha256
```

`decision` is `approved` or `rejected`. A mismatched application, artifact or
displayed packet makes it `STALE`. Matching observations are labeled as imported
history and do not authenticate or confer approval. A different application's
approval never applies to this one.

Events contain exactly:

```text
event_id, application_id, at, kind, verification,
source_record_sha256, latency_ms, detail
```

Kinds are `discovered`, `prepared`, `model_call`, `human_intervention`,
`submission_observed`, `blocked`, `note`. `verification` is `verified`,
`unverified`, or `unknown`; `PASS` is not a verification value. `at` and
`source_record_sha256` may be null. Numeric `latency_ms` must be finite, nonnegative,
and at most one day; null means unknown. Events require an existing application.
An evidence-backed event needs `verified` plus a source revision matching a fresh,
verified, scoped record permitted for review. Receipt events additionally require
the receipt kind and exact application/packet binding. These checks establish
consistency among caller-supplied records, not independent source authentication.

Input limits: 8 MiB canonical JSON, 512 applications, 4096 events; per application,
256 evidence records, 64 holds, 64 questions; per packet, 256 fields and 32
attachments. The existing question scheduler additionally permits at most 2048
total questions/blockers and a 2-million-character scheduler input. Oversized
projections fail closed; adapters should partition exports into bounded snapshots.

## Request output

The `keel.muse.review-request.v1` output contains exactly:

```text
schema, kind, created_at, report_sha256, snapshot_sha256,
application_id, application_revision_sha256, packet_revision_sha256,
packet_projection_sha256, question_id, question_sha256, question_kind,
scope_ids, response, blocking_causes, requires_authenticated_host_ingestion,
human_identity_authenticated, execution_authorized, canonical_writes,
network_requests
```

`kind` is `review_request`; approval-question responses are only `approve`,
`reject`, or `defer`. Other responses are nonempty text up to 16000 characters.
The packet hashes may be null when a fact question concerns a missing packet.
Authority flags are fixed false, ingestion required is true, writes and requests
are zero. Adding `approved: true`, changing any binding or discarding a blocker
causes validation to reject the request. The response itself is unauthenticated
user intent; the host must capture a real human decision rather than trusting a
download's contents.

## Measurements and visible uncertainty

| Measurement | Calculation and missing-data rule |
| --- | --- |
| Queue age | Projection time minus supplied creation time; age distribution excludes applications with a verified receipt observation; empty denominator yields null |
| Blocked causes | Distinct applications per retained or derived cause; one application may have multiple causes |
| Model calls / human interventions | Count evidence-backed records; show the total as unknown unless capture is declared complete and every reported event is backed; retain known, reported and unverified counts |
| Model latency p50 / p95 | Linear interpolation over sorted, finite supplied latencies for evidence-backed model-call events; show sample count and missing/unverified count; no samples yields null |
| Submission observations | Count applications with current bound receipt evidence; show unverified observations and unknown outcomes separately |
| Success rate | Always null: this schema does not provide a verified attempt denominator and complete outcome window |

The HTML labels these as snapshot observations. It exposes capture time and age,
unknown dates, missing latency and incomplete telemetry. No receipt, benchmark,
model use, network call, or human intervention is invented to fill an empty cell.

## Local interface and artifact handling

The interface supports text search, status filtering, exact questions, empty
response fields, revision-bound request downloads, packet comparison, evidence
inspection, and a dated timeline with unknown timestamps sorted last. Tabs work
with arrow keys, Home and End; controls have labels, focus states and live status
messages. The layout adapts to mobile widths and respects reduced motion.

Untrusted values enter the DOM through `textContent`. Embedded JSON escapes
script-closing markup and Unicode line separators. Hash-pinned static script/style
and restrictive CSP deny network connections, forms, frames, objects and remote
assets. Evidence references remain text, including `javascript:` or hostile HTML.
Downloads use a local Blob and contain a typed JSON request. There is no persistent
browser storage, shell execution, arbitrary HTML, source navigation, submission,
or approval endpoint.

Generated HTML contains the supplied application data in clear text. It is a
private local artifact, not encrypted storage. `write_dashboard()` publishes a
new mode-0600 file atomically through a held directory descriptor, refuses
overwrite and symlink redirection, and returns the exact output hash and byte
count. Keep the file and downloaded requests on the intended private host.

`demo()` only claims HTML generation. Browser rendering and interactive fixture
tests must be reported separately with the actual browser result; a local review
desk rehearsal does not validate Muse, a rendered application form, a model,
source ingestion, authenticated approvals, or production deployment.

Focused validation:

```bash
python -B -m pytest -q -p no:cacheprovider tests/test_muse_review.py tests/test_muse_dashboard.py
```
