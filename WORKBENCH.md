# Keel Workbench 0.1.0

An open-source operator interface and integration layer for **Keel 0.7**.
It adds a local dashboard, HTTP API, Python SDK, JSON-line bridge, and five
review workflows. The original 0.7 source and controls remain intact.

## Start in one command

From the extracted project directory, with Python 3.11 or later:

```sh
python3 -B -m keel_workbench serve --demo
```

Open the **private session URL printed in your terminal**. It contains an
ephemeral token in the URL fragment. The dashboard removes the fragment after
reading it and retains the token in page memory only. Reloading the page means
opening the terminal link again or entering the same session token.

No runtime dependency install, paid API, model, hosted database, or account is
needed. Stop with Ctrl-C. A different local port can be chosen with `--port`.
The default is 8765. The address is always `127.0.0.1`; `localhost` is deliberately
not an alias. The token, exact Host, and same Origin are checked independently.

The nine demo opportunities and their display labels are synthetic. Its clock
is fixed at **2026-09-18 12:00 UTC**. The demo has 5 nominal READY roles, 1 passing
the base flow checks, and 0 passing all Workbench review checks. All 63 missing
source records appear as 7 system repair groups, not 63 applicant questions.
Those values are calculated by the actual bundled Keel reducers.

## Interface

| View | Working controls and output |
| --- | --- |
| Overview | Derived pipeline counts, current blockers, review brief shortcut and supply estimate |
| Opportunities | Search by role/company/ID; filter Operations, Sales, Technology or General; inspect exact role blockers |
| Evidence | Seven source families, packet/revision mismatches, grouped adapter tasks and artifact status |
| Workflows | Choose a workflow and scope; run checks; inspect and download its JSON result |
| Twin lab | Change refill delay, consumption multiplier, a source rate hold or a packet binding; compare baseline and scenario |
| Integrations | Import/export workspace JSON, download OpenAPI, inspect session run history |

The interface contains review and simulation actions. Application submission,
browser operation, applicant consent, approval issuance and canonical queue
updates stay with the authorized Keel host. A passing review count is diagnostic
and never execution authority.

## Specialized workflows

| Workflow ID | Result | Scope |
| --- | --- | --- |
| `source-repair` | Deduplicated missing/invalid source and packet-binding tasks, with actual system/human responsibility | All roles, or selected IDs |
| `material-review` | Selected role checks, material holds, workspace artifact/claim graph | All roles, or selected IDs |
| `daily-brief` | Applicant decision groups within a 0–240 minute budget, adapter tasks and existing operating holds | Whole workspace |
| `incident-replay` | Three synthetic regressions: misleading READY count, incomplete export, changed packet dependency | Bundled fixtures |
| `twin-scenario` | Before/after runway, refill timing, discovery budget and base readiness | Whole workspace |

Material review exposes the recorded evidence graph; it does not rewrite a
resume. The daily brief never invents answers. Incident replay tests bundled
fixtures, not the imported live pipeline. The twin is a conditional flow/material
model: its forecasts are uncalibrated, it has no automatic live synchronization,
and it does not predict hiring success. Assurance and seven-source qualification
are separate from the twin's base-ready counts.

## Bring in operational data

Keel Workbench consumes `keel.workbench.snapshot.v1`, an envelope containing:

```json
{
  "schema_version": 1,
  "schema": "keel.workbench.snapshot.v1",
  "workspace_id": "YOUR_EXISTING_WORKSPACE",
  "synthetic": false,
  "flow": {},
  "assurance": null,
  "trust": null,
  "labels": [],
  "revision_sources": null
}
```

The empty flow above shows the shape only; it is not a valid flow export. Use
the actual Keel export and existing contracts. `assurance` and `trust` may be
null, in which case the interface exposes their absence and cannot qualify a
role. The trust export and assurance export must bind the exact flow digest.
Labels contain `role_id`, `company`, `title`, and `lane`; they never change
scoring or authority. Source records use the existing `keel.revision_sources.v1`
contract from `keel_agent/revisions.py`.

Wrap an existing 0.7 snapshot body (exactly `flow`, `assurance`, `trust`):

```sh
python3 -B -m keel_workbench adapt-body \
  --body /private/export/body.json \
  --workspace YOUR_EXISTING_WORKSPACE \
  --out /private/export/workbench.json

python3 -B -m keel_workbench serve \
  --snapshot /private/export/workbench.json \
  --workspace YOUR_EXISTING_WORKSPACE
```

Optional `adapt-body` inputs: `--labels labels.json` and
`--revision-sources revision-sources.json`. When the source export contains
attachment files, the host must supply `serve --attachment-root /private/files`.
Paths in the snapshot cannot choose or change that root. Existing Keel source
checks read actual attachment bytes within that root and reject traversal and
symlinks. Neither the manifest nor a model's assertion is accepted as proof of
file contents. Workbench reads the referenced files; it does not upload them.

Operational mode always uses the host UTC clock. Stale/incomplete snapshots stay
unverified; importing does not refresh their timestamps. Operational and synthetic
imports cannot mix within one server process. The dashboard refreshes derived
checks every 30 seconds, but source updates require an explicit new import.
Inputs are bounded to 8 MiB and 2,000 roles. Operational use on larger workloads
and multi-user use have not been performance-qualified.

Every qualified role must pass flow, assurance, trust/material checks, all seven
current source checks, **and** exact source-revision equality with the packet's
dependencies. Structural source validity does not authenticate origin. The host
must still authenticate observations, reviews and approvals.

Keel 0.7 can retain material review across a refreshed lead observation. The
legacy flow board's `lead_sha256` check is stricter in that case. Workbench shows
`HOST_SCOPE_CHECK_REQUIRED` and leaves review qualification false; it neither
rewrites the assurance export nor substitutes itself for the host's stable-scope
evaluator. This release does not surface host runtime reviewer commitments.

## Integrate with the existing agent

If your canonical adapter already has an open `LocalState`, use its verified
snapshot API directly:

```python
from keel_workbench.integrations import snapshot_from_agent
from keel_agent.io import write_private

# `state` is your existing host-owned keel_agent.state.LocalState instance.
document = snapshot_from_agent(state)
write_private("/private/export/workbench.json", document)
```

The adapter calls `state.latest_snapshot` to verify the stored signature, digest,
workspace and expiry. It does not initialize a database or key, sign a replacement
envelope, enqueue a job, or create a source observation. Labels and authentic
revision sources can be passed as optional arguments. The receiver owns the
canonical adapter and its updates. There is no automatic polling connection.

## Python SDK and HTTP

Use the endpoint and token from the terminal link. Keep the token out of source
control. In this example `session_token` is supplied by your local caller:

```python
from uuid import uuid4
from keel_workbench.client import Client

client = Client("http://127.0.0.1:8765", session_token)
overview = client.overview()
report = client.run({
    "request_id": str(uuid4()),
    "workflow_id": "source-repair",
    "snapshot_sha256": overview["snapshot_sha256"],
    "role_ids": [],
    "options": {}
})
print(report["result"]["tasks"])
```

The standard-library SDK disables redirects and proxy environment settings and
only accepts `http://127.0.0.1:PORT`. Local session tokens are never forwarded
to another destination. `ClientError.status` distinguishes HTTP errors.

| Endpoint | Method | Contract |
| --- | --- | --- |
| `/health` | GET | Service/version only; no token needed |
| `/api/v1/overview` | GET | Current derived review projection |
| `/api/v1/snapshot` | GET | Complete imported session snapshot |
| `/api/v1/snapshot` | POST | `{previous_sha256, snapshot}`; compare-and-swap session import |
| `/api/v1/workflows` | GET | Workflow catalog |
| `/api/v1/run` | POST | `{request_id, workflow_id, snapshot_sha256, role_ids, options}` |
| `/api/v1/history` | GET | Last 100 successful request summaries in this process |
| `/openapi.json` | GET | OpenAPI 3.1.1 document |

Data endpoints require `Authorization: Bearer TOKEN`. POST uses UTF-8
`application/json` with Content-Length. Cross-origin requests, chunked bodies,
compressed bodies, query parameters and duplicate JSON keys are rejected.
The server is bounded to eight concurrent connections with five-second socket
timeouts. It is a local desktop server, **not an Internet-facing deployment
gateway**. Public hosting, shared identity, authorization roles and durable
multi-user state would require a separate production service.

Error JSON is `{ "error": { "code": "…", "message": "…" } }`. HTTP 400 means
invalid input; 401 missing/invalid token; 403 origin/host rejection; 409 stale
snapshot, conflicting request ID or expired/context-changed cached report;
413 oversized body; 415 unsupported media/encoding. A saturated server responds
503 with an empty body. Refresh the overview and submit a **new request ID** when
context changes. Do not automatically keep retrying a rejected command.

Identical request IDs with identical bodies return the original dated result
within 90 seconds, only while role/source validity is unchanged. History and
request deduplication are bounded to the last 100 successful runs and are lost
on restart. These are review operations; this is not durable execution
idempotency. Download reports if they need to survive the session.

Full nested flow, assurance, trust and revision-source contracts remain defined
and checked by their existing Keel modules. OpenAPI describes the Workbench
envelope and workflow option variants; its generic response objects are not
intended to generate a fully typed SDK for every Keel report.

## JSON-line bridge and CLI reports

For agents that can launch a local process:

```sh
python3 -B -m keel_workbench bridge --demo
```

Write one JSON object per line on stdin:

```json
{"id":"inspect-1","method":"GET","path":"/api/v1/overview","body":null}
```

Receive `{id, status, data}` or `{id, status, error}` on stdout. POST request
bodies use the same contracts as HTTP. This is a Keel-specific JSON-line protocol,
not MCP. EOF exits normally; malformed/oversized JSON stops with exit code 2 and
a stderr explanation. Valid JSON with an invalid command returns a structured
error and the process can continue. No logs are mixed into stdout.

For operational input replace `--demo` with `--snapshot … --workspace …` and,
when required, `--attachment-root …`. There are no model tokens or remote endpoint
settings in this extension.

Other commands:

```sh
python3 -B -m keel_workbench demo-export --out /new/path/demo.json
python3 -B -m keel_workbench run --demo --request request.json --out /new/path/result.json
```

`request.json` has the same five fields as `/api/v1/run`; obtain the snapshot
digest via the bridge/HTTP overview or `keel_trust.common.digest(make_demo())` for
the synthetic fixture. Output files are created privately with mode 0600 and
never overwrite existing files.

## Verification and handoff

Install the pinned free test tools only if your environment lacks them:

```sh
python3 -m pip install -r requirements-dev.txt
python3 -B tools/run_release_checks.py --out /new/path/workbench-checks
python3 -B tools/check_workbench_http.py --out /new/path/workbench-http.json
node --check keel_workbench/static/app.js
```

The release runner preserves both existing test guards. The HTTP fixture is a
separate explicit loopback-only check; the guarded regression suite has no network
access. Node is optional for syntax verification; it is not needed at runtime.
See `docs/WORKBENCH_HANDOFF.md` and `audit/workbench-validation.json` for the
actual release evidence and known verification limit.

The rendered browser interface could not be exercised in this environment: the
available remote browser rejects loopback navigation with `ERR_BLOCKED_BY_CLIENT`.
Actual server/SDK HTTP integration and workflow tests do run. On the receiving
host, open the demo and verify search/filter/detail, every workflow, JSON
download/import, scenario comparison, mobile layout, and keyboard navigation
before calling the UI production-verified.

## Contributing and release boundaries

This extension uses the existing Apache 2.0 license in `LICENSE`. Workbench
code is isolated in `keel_workbench/`; integration tests start with
`tests/test_workbench_`; no original source file is replaced. Keep new provider
adapters behind these typed input/output contracts. Bring authentic data through
the host-owned adapter, keep missing evidence explicit, and test invalid/stale
inputs as well as successful paths.

Start future work with the receiving host adapter and rendered UI validation.
Then consider packaging for a persistent local service, richer typed API
responses, and authenticated reviewer-commitment display. A multi-user service,
new third-party provider integrations, marketplace publication, live model runs
and automatic application submission are outside this release's verified scope.

Preserve Keel's existing controls: fit floor 75; exact applicant consent;
disputed Stripe WhatsApp YES quarantined; unknown attempts held; authorized
MAIN-CHAT live browser lane; no launch/steer/close/confirm-close authorization
from this package; stop on 429; no outreach, spending or references; guardian
floor 5 / check 30 seconds / spawn throttle 600 seconds and existing
`--live --async --limit 200` behavior. Workbench changes none of them.

To rebuild the one-file transfer, use `tools/build_workbench_transfer.py`, not
the historical 0.7 release builder. The explicit extension allowlist excludes
runtime snapshots, tokens, reports from real work and credentials.
