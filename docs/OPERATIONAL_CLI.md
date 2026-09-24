# Keel 0.14 private host CLI

Run these commands from the extracted or installed Keel code directory. Python
must be able to import `keel_operational` and `tools`. Runtime files, operator
configuration, requests, evidence and output reports belong outside that code
directory. Every output path must be new; existing files are never replaced.

The CLI does not call Muse, a model, a browser or a canonical sink. It persists
one native request for the receiving host to execute through its actual native
tool. It does not create genuine human approval records. All current evidence,
consent, no-AI, per-field approval, shared hold and native permission requirements
still apply.

## Command and result format

Every command uses this shape:

```sh
python -m keel_operational runtime \
  --host-config /private/keel/runtime-config.json \
  --input /private/keel/request.json \
  --out /private/keel/report-001.json
```

`/private/keel` is an example operator-selected private directory, not a directory
the package creates automatically. Use an actual absolute path whose parent
exists. Runtime store directories are created privately with mode `0700`; reports
have mode `0600`. Keep backed-up private keys private too.

The request has exactly two keys:

```json
{"operation":"status","arguments":{}}
```

Paths, providers, workspace identity, clock, qualification pins and authority
cannot be selected in `arguments`. Optional `expected_running_source_sha256` is
only a comparison against the CLI's actual inventory; it cannot replace that
inventory. Host configuration is supplied separately and checked for changes
during the invocation. This separation assumes a trusted host installation: it
does not stop the filesystem owner from editing either document.

Standard output contains only the command, status and exit code. The new report
contains the detailed result in `.result`, the source inventory pin and explicit
non-authorizing flags. Do not treat the envelope or `OBSERVED` as task success.

| Exit | Meaning |
|---|---|
| `0` | The operation produced a recorded or ready result; inspect `.result`. |
| `2` | Invalid input, unsafe path, missing configuration or another error. |
| `3` | Blocked, unknown, partial, unavailable, unqualified or waiting. |
| `4` | Source or operator configuration changed; result is quarantined. |

When one operation produces an input for another, save the inner object as a new
private file, not the whole envelope. This example prints its canonical SHA-256
pin for the operator to retain in configuration:

```sh
python -c 'import sys; from keel_loki.common import load_json,atomic_json,digest; r=load_json(sys.argv[1]); assert r["result_valid_for_source"] is True; v=r["result"]; atomic_json(sys.argv[2],v); print(digest(v))' \
  /private/keel/freeze-report.json /private/keel/qualification-plan.json
```

A hash proves consistency of bytes. It does not authenticate Muse, a human
approval, the capture adapter or a canonical system.

## Shared stop ledger

The `control` configuration is exact:

```json
{
  "schema":"keel.operational.control-cli.v1",
  "workspace_id":"keel-host",
  "control_home":"/private/keel/control"
}
```

Use `create` once, then `status`, `recover` or `pending`, all with empty arguments.
Normal opens do not restart a controller or clear state. `recover` tightens expired
issued attempts to `UNKNOWN`; it never retries them. `pending` exports undelivered
stop observations and returns `PARTIAL` while any remain.

This generic CLI has no acknowledgement, hold-clear, retry, direct-admission or
dynamic-sink operation. Canonical delivery requires a trusted long-lived host
controller using the adapters in `runtime.py`; the CLI reports delivery as
`NOT_ATTEMPTED`. Every participating writer must use the same ledger and write
barrier. Unwrapped existing workers are not covered by that barrier.

## Qualify the actual host against a synthetic fixture

The host must first map the actual native tools and capture their actual schemas.
`fixture.*` names from `qualification.fixture_inputs()` are injected test names,
not Muse API names. Do not relabel injected observations as native observations.

All qualification configurations contain these common fields:

```json
{
  "schema":"keel.operational.qualification-cli.v1",
  "workspace_id":"keel-host",
  "manifest":"/private/keel/native-manifest.json",
  "tool_schemas":"/private/keel/native-tool-schemas.json",
  "host_config":"/private/keel/qualification-host.json"
}
```

The `host_config` file uses the qualification module's exact host schema:

```json
{
  "schema":"keel.operational.qualification-host.v1",
  "host_id":"keel-host",
  "adapter_version":"host-adapter-v1",
  "runtime_fingerprint_sha256":"REPLACE_WITH_ACTUAL_HOST_FINGERPRINT_SHA256",
  "fixture_origin":"http://127.0.0.1:8765",
  "fixture_account_id":"fixture-account",
  "fixture_authorized":true,
  "transport_mode":"HOST_NATIVE_REPORTED",
  "native_gate":"NATIVE_TOOL_ENFORCES_PERMISSION"
}
```

The fingerprint placeholder is intentionally invalid: replace it with the real
host's retained fingerprint. `fixture_authorized` is the operator's authorization
for this synthetic fixture, never application or submission approval. Whether a
personal Muse browser can reach a local fixture origin must be checked on that
host; use an actual authorized reachable origin if it cannot.

The exact additional configuration fields and request arguments vary by operation:

| Operation | Add these configuration fields | `arguments` |
|---|---|---|
| `freeze` | `fixture` template path, `ttl` integer, `required_operations` list | `{}` |
| `export_fixture` | `plan` path, `expected_plan_sha256` pin, `fixture_home` new directory | `{}` |
| `record` | `native_request` path, `raw_observation` path, `normalized_observation` path, `captured_at` actual capture timestamp | `{}` |
| `grade` | `plan` path, `expected_plan_sha256` pin | `{"records":[…]}` |
| `verify` | `plan` path, `expected_plan_sha256`, `report` path, `expected_report_sha256`, `required_operations` list, `allow_injected` boolean | `{}` |

These are exact keys: use a separate configuration document for each operation.
For example, a complete `freeze` configuration is:

```json
{
  "schema":"keel.operational.qualification-cli.v1",
  "workspace_id":"keel-host",
  "manifest":"/private/keel/native-manifest.json",
  "tool_schemas":"/private/keel/native-tool-schemas.json",
  "host_config":"/private/keel/qualification-host.json",
  "fixture":"/private/keel/synthetic-fixture-template.json",
  "ttl":3600,
  "required_operations":["accessibility_snapshot","set_text","select_option","choose_radio","set_checkbox","set_attestation","attach_file"]
}
```

`freeze` requires an explicitly synthetic fixture matching the host's authorized
origin and account. It rebinds that fixture to the actual host clock and creates
only synthetic fixture approvals. It rejects a real application template. Fixture
approval and host snapshots currently expire after 300 seconds, so complete the
rehearsal within that window. The plan TTL does not extend a fixture approval.

Save the returned plan and its pin, then use `export_fixture`. Export writes a
local accessible HTML fixture plus the actual approved attachment bytes. It does
not start a server or open a browser. The host serves that fixture at the pinned
origin through its authorized tools. A visible file digest is a fixture page
observation, not proof that arbitrary employer pages expose attachment hashes.

For personal Muse's model-level native tools, use the serial JSON handshake in
`docs/MUSE_SESSION.md` with this pinned synthetic fixture. A trusted adapter may
instead use `qualification.run(..., capture=...)` in-process. This CLI deliberately
does not load a callback named in JSON or invent a browser SDK.

For every issued native request, retain original tool-output bytes before
normalization. `record` reads the original and normalized observations from
separate host-selected files and derives the tool name from the actual manifest.
Keep each resulting `.result` record in order. Pass that list to `grade`, then
retain the report and canonical report pin. `verify` regrades it using the current
source, host configuration, manifest, schemas, operation requirements and clock.
Use `allow_injected:false` for native host admission.

Empty observations remain `UNAVAILABLE`; incomplete observations remain visible.
`SIMULATED` is not native qualification. `QUALIFIED_REPORTED` means the retained
observations conform; the capture/normalization relationship is still a trusted
adapter assumption, not independent platform attestation.

## Durable qualified runtime

Use one exact runtime configuration throughout a session:

```json
{
  "schema":"keel.operational.runtime-cli.v1",
  "workspace_id":"keel-host",
  "runtime_home":"/private/keel/runtime-application-001",
  "native_home":"/private/keel/native-application-001",
  "control_home":"/private/keel/control",
  "session_id":"application-001-session",
  "scope_id":"application-001",
  "transport_mode":"HOST_NATIVE_REPORTED",
  "native_gate":"NATIVE_TOOL_ENFORCES_PERMISSION",
  "lease_seconds":30,
  "expires_at":null,
  "application":"/private/keel/application-001-preparation.json",
  "host_approvals":"/private/keel/application-001-approval-observations.json",
  "host_snapshot":"/private/keel/application-001-current-host.json",
  "control_gate":"/private/keel/application-001-current-gate.json",
  "qualification":{
    "manifest":"/private/keel/native-manifest.json",
    "tool_schemas":"/private/keel/native-tool-schemas.json",
    "host_config":"/private/keel/qualification-host.json",
    "plan":"/private/keel/qualification-plan.json",
    "expected_plan_sha256":"REPLACE_WITH_RETAINED_PLAN_SHA256",
    "report":"/private/keel/qualification-report.json",
    "expected_report_sha256":"REPLACE_WITH_RETAINED_REPORT_SHA256"
  }
}
```

The application file has exactly `contract`, `values` and `attachments`, using the
existing 0.12/0.13 preparation schemas. The approval and host-snapshot files must
come from the trusted host's existing approval/evidence workflow; a model must not
invent them. Synthetic approvals are appropriate only for a synthetic fixture.

The shared gate file has this exact shape:

```json
{
  "schema":"keel.operational.cli-gate.v1",
  "workspace_id":"keel-host",
  "account_id":"actual-bound-account",
  "role_id":"application-001",
  "resource_id":"actual-bound-form",
  "source_sha256":"REPLACE_WITH_CURRENT_OPERATIONAL_INVENTORY_SHA256",
  "consent":true,
  "no_ai":false,
  "approval_current":true,
  "holds":[],
  "unknown_attempt":false,
  "rate_limited":false,
  "issued_at":0,
  "expires_at":0
}
```

This is a format example, not valid permission: zero timestamps are expired and
the placeholder pin is invalid. The host must supply observed scope, policy
values and timestamps. The CLI reloads this file for every admission check and
preserves its timestamps; the maximum lifetime is 90 seconds. It binds only an
exact workspace/account/role/resource/source match to the proposed attempt. It
does not turn a wildcard or old snapshot into current authority.

The trusted host can read the actual source pin with:

```sh
python -c 'from tools.operational_inventory import inventory; print(inventory()["sha256"])'
```

Use the following sequence, with a new report file every time:

1. `create` with `{}` validates the current qualification and creates both new
   session stores under the shared write barrier.
2. `next` with `{}` produces `PROPOSED` and `.result.native.request`.
3. `next` with `{}` and operator flag `--dispatch` persists issuance. Only a
   result with status `ISSUED` and `.result.native.dispatch_issued_once:true`
   carries a request to invoke. Retain its request hash before invocation.
4. The host rechecks its controls at actual invocation and calls its real mapped
   native tool, which enforces native permission. A Keel issuance does not grant
   Sentinel permission. Actual native permission JSON is not accepted here.
5. `observe` accepts `{"response":NORMALIZED_TOOL_RESULT,"request_sha256":"ISSUED_REQUEST_SHA256"}`.
   Interpret the returned status before requesting another step.
6. If interrupted, inspect `status` and use explicit `recover` as appropriate.
   An unresolved issued action becomes `UNKNOWN`; it is not automatically replayed.

Repeated `next --dispatch` while an observation is pending returns waiting with
no request. `status` and ordinary opening do not clear or recover a session.
The generic CLI cannot deliver pending canonical stops. The trusted canonical
sink must finish that separate integration, or further admission stays blocked.

For offline tests only, `transport_mode:"INJECTED"` and `native_gate:null` may use
fixture permission observations in `next` arguments. That path never establishes
real host qualification or actual native permission.

## Inventoried backup and offline restore

`checkpoint` uses this configuration:

```json
{
  "schema":"keel.operational.recovery-cli.v1",
  "workspace_id":"keel-host",
  "inventory":"/private/keel/runtime-inventory.json",
  "control_home":"/private/keel/control"
}
```

The exact inventory format and supported store profiles are documented in
`OPERATIONAL_RECOVERY.md`. It must include exactly one control store, every
configured required store and at least one canonical store. Every store home
must be separate and private. The inventory source pin must match the actual
running operational source. External services are explicitly outside the cut.

Save `.result` from `checkpoint` as an independently retained checkpoint. For
`snapshot`, add `backup_home` (a new directory) and `expected_checkpoint` (that
checkpoint file path) to the configuration. Both operations use empty arguments.
They acquire the exclusive maintenance barrier internally; do not wrap them in
a shared writer barrier.

Retain the snapshot's `manifest_sha256` separately. `restore` uses a different,
exact configuration and empty arguments:

```json
{
  "schema":"keel.operational.recovery-cli.v1",
  "workspace_id":"keel-host",
  "backup_home":"/private/keel/backup-001",
  "destination":"/private/keel/restored-001",
  "authoritative_checkpoint":"/private/keel/independently-retained-current-checkpoint.json",
  "expected_manifest_sha256":"REPLACE_WITH_RETAINED_BACKUP_MANIFEST_SHA256"
}
```

The checkpoint path must be outside the backup. Location alone does not prove
freshness: the operator must independently know that the retained checkpoint is
current. An old backup's own checkpoint cannot rule out rollback. Restore creates
a new offline destination, writes `OFFLINE_RESTORE.json`, preserves holds and
unresolved intents, and starts no controller. Revalidate the host and follow the
recovery procedure before any later execution.
