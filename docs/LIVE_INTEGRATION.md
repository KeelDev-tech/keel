# Keel 0.9: connect source capture to operator review

Keel 0.9 adds a local integration layer around the existing source producers and Workbench. Trusted host code can capture normalized events, export the corresponding source revisions, display an exact review packet, record a scoped operator decision, and evaluate capture-to-review qualification against the supplied flow, assurance and trust exports.

The package is an integration candidate. It has not populated the receiving host's live leads or deployed a service. The last live report supplied by the user confirmed 0.7 integration and reported 0 of 837 leads carrying the seven real source families. Local 0.8, Workbench and 0.9 artifacts do not establish that those extensions are installed on that host.

## Runtime and boundaries

The added runtime uses Python 3.11+ and the standard library, including SQLite and the local HTTP server. It requires no paid API, subscription, remote identity provider or hosted database. The inherited attachment publication path requires a supported Linux filesystem and `renameat2(RENAME_NOREPLACE)`. Development tests use the existing test dependencies. Actual model inference and rendered browser operation have separate host dependencies; this extension neither installs nor exercises them.

| Layer | New behavior | Boundary |
|---|---|---|
| `keel_live.connector` | Checks producer allowlists, exact scope, fresh flow hash and source generation before capture | An in-process host integration, not an authenticated public ingestion endpoint |
| `keel_live.review` | Limits reads and explicit review actions to an immutable host principal and exact scopes | The host authenticates the operator; actor strings are not identity proof |
| `keel_live.surface` | Connects current source records and decisions to Workbench and the review interface | Canonical snapshots come from a trusted provider, never browser upload |
| `keel_live.proof` | Rechecks source semantics, dependency binding and the existing assurance/trust reducers | Capture-to-review checks only; no rendered preparation, readback or execution authority |
| `keel_live.server` | Serves the private UI and API on `127.0.0.1` | A session bearer token delegates the configured principal's limited authority |

Existing consent, hold, source expiry, attempt-history, approval, assurance, trust and workflow rules remain in their original packages. Every result has `execution_authorized:false`. The new HTTP surface has no application submission, arbitrary source capture, model execution or browser automation endpoint.

## Preview the interface

From the combined source tree, choose an output directory that does not already exist:

```sh
python3 -m keel_live demo --home /new/private/fixture
```

Open the private `/review#token=...` URL printed by the process. The UI presents synthetic captured sources, an explicit request/decision flow and the existing Workbench. The fixture does not create a real human decision. Approving a synthetic source packet does not manufacture assurance/trust artifacts or canonical dependencies, so that alone does not pass the full proof report. Stop the server with Ctrl-C.

The deterministic rehearsal separately constructs clearly marked synthetic assurance and trust fixtures to test the positive path:

```sh
python3 tools/rehearse_live.py --out /new/private/live-rehearsal
```

Read the resulting `live-rehearsal.json` and `synthetic-capture-review-proof.json`. Synthetic successes never count toward operational milestones. The rehearsal is not a rendered-browser test.

## Configure a real host

The host configuration is private startup input. Keep it in an operator-owned regular file with no group/other permissions and no symlink or hardlink. The configuration loader validates those properties and checks that the file did not change during reading. `home` and `attachment_source_root` must be absolute paths. Use a new source-store home or the correctly identified existing 0.8 source store, never an unrelated database.

The following is a **placeholder configuration**, not a record of an actual account, role, authority or grant. Replace each `REPLACE_...` value using the real host. The source store's workspace, the canonical role/application identity and the operator's grant must agree exactly.

```json
{
  "schema": "keel.live_host.v1",
  "home": "/absolute/private/REPLACE_SOURCE_STORE",
  "workspace_id": "REPLACE_WORKSPACE_ID",
  "action": "PREPARE",
  "producer_components": {
    "REPLACE_POLICY_PRODUCER": ["policy"],
    "REPLACE_FORM_PRODUCER": ["form"],
    "REPLACE_ANSWER_PRODUCER": ["answers"],
    "REPLACE_ATTACHMENT_PRODUCER": ["attachments"],
    "REPLACE_TARGET_PRODUCER": ["target"],
    "REPLACE_ROUTE_PRODUCER": ["route"]
  },
  "attachment_source_root": "/absolute/private/REPLACE_ACTUAL_DOCUMENT_ROOT",
  "operator": {
    "actor_id": "REPLACE_AUTHENTICATED_OPERATOR_ID",
    "authority_record_ref": "REPLACE_EXISTING_AUTHORITY_REFERENCE",
    "allowed_actions": ["review:read", "review:request", "review:decide", "review:revoke"],
    "allowed_scopes": [
      {
        "workspace_id": "REPLACE_WORKSPACE_ID",
        "role_id": "REPLACE_ACTUAL_ROLE_ID",
        "application_id": "REPLACE_EXACT_CANONICAL_APPLICATION_ID",
        "action": "PREPARE"
      }
    ]
  }
}
```

Grant only the review operations the actual operator is authorized to perform. `allowed_actions` contains review operations; the material scope's `action` is `PREPARE`. This release accepts no other material action. Producer allowlists cannot contain `approval`: approval records come from an explicit review decision.

The configured principal must have `review:read` permission for **every role and application scope in the supplied body**, because Workbench exposes the complete canonical projection. There are no wildcard grants and no ad hoc browser filtering that changes authorization. When a fresh export adds a role outside the startup grant, access fails closed until the host updates the real grant and restarts the service. Do not remove arbitrary roles from a canonical export without the host rebuilding a valid, consistent export.

The CLI creates a principal from this protected configuration; it does not authenticate a human login. The host must establish the operator and authority before writing the configuration or constructing a `Principal`. The printed session URL is a privileged bearer capability for those specific review operations and scopes. Anyone holding it can exercise that delegated authority. Keep it within the authenticated operator's session; do not send it to a model, add it to release files or put it in public logs.

## Supply a current canonical body

The `--body` file is a JSON object with exactly three fields:

| Field | Content |
|---|---|
| `flow` | The actual complete canonical flow export, including complete attempt history and original observation times |
| `assurance` | Its existing assurance envelope, or `null` when genuinely absent |
| `trust` | Its existing trust export, or `null` when genuinely absent |

An assurance envelope must bind the exact supplied flow digest. A trust export must contain that same flow and workspace. Missing envelopes remain missing. Keel 0.9 does not build replacements, revise approvals or repair digest conflicts to make a report pass.

```sh
python3 -m keel_live init --config /absolute/private/host-config.json
python3 -m keel_live register --config /absolute/private/host-config.json --body /absolute/private/current-body.json
python3 -m keel_live serve --config /absolute/private/host-config.json --body /absolute/private/current-body.json
```

`init` explicitly initializes the source store. `register` registers exact current scopes and creates no source records or pending requests. Other operational commands require the store's existing identity marker.

The server rereads the `--body` file through its trusted `body_provider` on each refresh/request. The canonical producer must publish a complete, internally consistent body, preferably by an atomic file replacement. Reading the file does **not** renew any canonical or source observation timestamp. Unavailable or malformed input is an error. An unchanged stale export can remain visible as a diagnostic; review mutations require current complete canonical flow no more than 90 seconds old. A refresh button cannot establish a new observation. The source snapshot's wrapper clock likewise cannot extend individual source validity.

The body provider is host code, not an HTTP client parameter. Browser replacement of `/api/v1/snapshot` is disabled in this surface. The original standalone Workbench remains unchanged.

## Connect actual producers

The producer boundary accepts actual normalized source descriptors using the 0.8 contracts in `SOURCE_CAPTURE.md`. It does not scrape a live ATS, resolve unsupported form controls, infer unknown policies, verify applicant claims from labels, or create answers. Map the actual host producers to these contracts before sending events.

Each `keel.live_source_event.v1` has exactly:

| Field | Binding |
|---|---|
| `schema` | `keel.live_source_event.v1` |
| `producer_id` | A host-authenticated producer selected from protected configuration |
| `scope` | Exact `workspace_id`, `role_id`, `application_id`, `action` |
| `component` | One of policy, form, answers, attachments, target or route |
| `descriptor` | The actual 0.8 descriptor, preserving its source reference/version, observation/expiry and record |
| `expected_generation` | Current stored generation for that component; zero only when absent |
| `flow_export_sha256` | `keel_flow.common.digest` of the complete current canonical flow |

An embedding adapter can call the public API as follows. Variables prefixed `actual_` must come from actual host records; this sketch provides no example applicant data or fabricated approval:

```python
from keel_flow.common import digest
from keel_live.connector import HostSourceConnector

connector = HostSourceConnector(
    store,
    producer_components=actual_producer_allowlist,
    action="PREPARE",
    attachment_source_root=actual_document_root,
)
connector.register_flow(actual_body["flow"])
event = {
    "schema": "keel.live_source_event.v1",
    "producer_id": actual_authenticated_producer_id,
    "scope": actual_scope,
    "component": actual_component,
    "descriptor": actual_descriptor,
    "expected_generation": actual_current_generation,
    "flow_export_sha256": digest(actual_body["flow"]),
}
capture = connector.capture_event(event, flow=actual_body["flow"])
```

The host must authenticate the producer **before** selecting the configured identity. Merely naming an allowlisted producer does not prove origin. No event can supply the host attachment root. For attachments, the connector stages actual bytes into the private content-addressed store and then records the resulting descriptor. The target's canonical posting URL must match the current lead's posting URL exactly.

Capture uses generation compare-and-swap inside the source transaction. A replay or stale generation is rejected; there is no silent retry-success or exactly-once claim. If another capture wins, reread current material and reconcile before sending a genuinely current event. A large attachment operation must still satisfy canonical freshness before the record commits. A failed capture can leave an unreferenced verified content object, but it cannot publish a partially captured source record or authorize a decision.

The equivalent host-only CLI consumes an actual event file:

```sh
python3 -m keel_live capture --config /absolute/private/host-config.json --body /absolute/private/current-body.json --event /absolute/private/actual-event.json --out /absolute/private/new-capture-result.json
```

## Review exact material

The review UI shows the actual source descriptors, semantic gaps, attachment manifest and current request. Inspection creates no request. The operator explicitly requests review after the six source families pass the required checks. They inspect the exact packet and explicitly choose `APPROVE` or `REJECT`. The decision binds the displayed `review_sha256`, exact scope, six material revisions and attachment bytes. Revocation is a separate explicit operation.

The in-process API is `ReviewService(store, transaction_guard=..., transaction_source_guard=...)` with `inspect`, `get`, `request`, `decide` and `revoke`. Each method takes the separately established immutable `Principal` and exact scope. JSON payloads cannot override actor or authority. The live surface rechecks the captured canonical context and target/route bindings inside the source transaction, after acquiring the write lock and before commit. A standalone embedding must supply its own trusted guards when decisions depend on a current external flow; the underlying source store alone cannot establish that external freshness or binding.

The private HTTP API exposes:

| Method and path | Purpose |
|---|---|
| `GET /api/live/v1/review` | Current scoped overview and flow digest |
| `POST /api/live/v1/review/show` | Inspect current source material and any actual request |
| `POST /api/live/v1/review/request` | Explicitly create a review request |
| `POST /api/live/v1/review/decide` | Explicit `APPROVE` or `REJECT` for the exact packet |
| `POST /api/live/v1/review/revoke` | Explicit withdrawal with a reason |
| `GET /api/live/v1/proof` | Current capture-to-review qualification report |

All review POSTs carry exact `scope` and the current `flow_sha256`. Mutations also require `confirmed:true`; this records an explicit UI command, not independent proof a human clicked it. A request also carries `expires_at`; a decision carries `request_id`, `decision`, `reviewed_sha256`, `expires_at`; a revocation carries `request_id`, `reason`. Refresh after a conflict. Rejection and revocation remain recorded across restart; expired, changed or revoked material cannot keep its old approval valid.

The session bearer token is required for private API access. Existing Workbench local transport controls include exact Host/Origin checks, bounded requests and client count, local binding and private no-store responses. This server is a local operator surface, not a multiuser network deployment.

## Bind the sidecar in the real exporter

```sh
python3 -m keel_live export --config /absolute/private/host-config.json --body /absolute/private/current-body.json --out /absolute/private/new-workbench-export.json
python3 -m keel_live proof --config /absolute/private/host-config.json --body /absolute/private/current-body.json --out /absolute/private/new-proof.json
```

The Python entry point is `HostSourceConnector.export_workbench(flow=..., assurance=..., trust=..., synthetic=False)` or `export_workbench(store, body, action="PREPARE", synthetic=False)`. It returns `source_export` and `workbench_snapshot`. The former retains the strict source semantics and seven named revision columns; the latter connects the same normalized source snapshot to the existing Workbench reducers.

Join the source export by **workspace + role + application + action** and bind it to `flow_export_sha256`. Check `current_flow_verified` and exact identity before consuming columns. Stale diagnostic exports have null joined columns and false producer completion. Noncurrent stored scopes are excluded from the current Workbench snapshot and counted separately.

The host's canonical exporter must deliberately consume the valid sidecar revisions into the actual lead's `dependencies` and rebuild the real packet's `packet_dependency_hash`. It must also produce correctly bound original assurance and trust material through the existing evidence/review path. This extension never mutates the flow, recomputes a passing assurance envelope or updates a trust artifact to hide a mismatch. Capturing and approving sources therefore leaves an existing unbound packet visibly blocked until the canonical host completes that work.

Do not use `producer_inputs_complete`, the Workbench's older source inventory or a successful command exit as a substitute for the new proof report and existing execution gates. CLI exit 0 means the command completed, including when a proof report says `BLOCKED`; inspect its explicit status and counts. Invalid input/runtime errors exit 2. `--out` writes a new private JSON file and refuses an existing destination.

## Interpret qualification

```python
from keel_live.proof import build_proof

report = build_proof(
    actual_workbench_snapshot,
    workspace_id=store.workspace_id,
    synthetic=False,
    action="PREPARE",
    attachment_root=store.attachment_root,
    now=store.clock(),
)
```

The report rechecks actual attachment files, the six-source semantic contract, all seven current revisions, exact packet dependency hashes, canonical posting and transport, current flow, pipeline activity and the existing assurance/trust reducers. Missing, stale, rejected, revoked and mismatched inputs retain distinct blockers. Snapshot or scope contract violations raise an error instead of reporting a passed role.

`CAPTURE_TO_REVIEW_CHECKS_PASSED` means at least one role in the supplied snapshot passed those checks; inspect each `roles[]` result and counts for coverage. It does not mean every role passed. Synthetic counts are separate from `declared_operational_capture_review_checks_passed`. The one-role and ten-role milestones count distinct current roles in that supplied snapshot, not historical test runs. Operational labeling remains a host assertion; explicit synthetic source markers cannot count as operational.

`source_authenticity_verified`, `operator_authenticity_verified`, `factual_support_verified` and `canonical_packet_content_verified` remain false. This diagnostic proves consistency of the supplied material and gates, not source truth, human identity, final packet contents or an externally rendered form. Local inference, rendered browser, rendered preparation and form readback remain `NOT_RUN`. The first host milestone is one genuinely sourced role qualified through the actual existing gates, followed by ten varied roles and separately evidenced runtime/preparation checks.

## Validation and handoff

The release's measured checks are recorded in `audit/live/validation.json`; use its actual counts and status rather than counts copied from older handoffs. `docs/LIVE_HANDOFF.md` describes transfer, isolated verification and host integration. Preserve source-store identity, immutable source histories and attachment objects together when backing up. Live configurations, stores, applicant records, body exports and session tokens belong outside the release tree.
