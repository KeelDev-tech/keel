# Durable native-tool sessions

`keel_muse.session.NativeSession` bridges a specific practical gap: an agent can
see its native browser tools, while a Python process may have no supported way
to call them. Keel emits bounded JSON preparation requests, the receiving agent
uses an actually available native tool, and Keel consumes a normalized report
of that tool's actual output. Keel does not launch a browser, run page JavaScript,
connect to CDP, discover an unofficial SDK, make model calls or submit a form.

The delivered demonstration uses `InjectedFixture`, an in-memory executor. Its
23 requests cover 11 typed preparation actions and 12 accessibility snapshots.
Passing that demonstration is not evidence of an actual Muse browser run or an
account integration. Both capabilities still need verification on the host.

## Establish the trusted host mapping

Inspect the tools actually available in the receiving agent/account. Record the
actual native tool names, input schemas, output schemas, account binding and
supported operations. Use `capabilities.validate_manifest`'s
`keel.muse.adapter-manifest.v1` schema. Its operation names are Keel protocol
verbs; the `native_tool_names` values must identify genuine available tools.
`fixture.*` names are only test data. The tool must support binding an action to
the exact current accessibility target and snapshot. If that binding, an
applicable typed verb, Python execution, private storage, or native permission
enforcement is missing, stop with `HOST_MAPPING_REQUIRED` and name the actual
missing capability. A declared configured manifest does not prove availability.

No public Muse Sentinel permission-receipt API was verified for this delivery.
Ordinary native-tool sessions therefore use:

```json
{"native_gate":"NATIVE_TOOL_ENFORCES_PERMISSION"}
```

This is a declaration that permission enforcement is delegated to the actual
mapped native tool. It grants no permission. Invoke that tool normally so its
platform permission UI can pause or deny the operation. Do not manufacture an
`allow` receipt, infer permission from a JSON field, bypass a prompt, or claim
that Keel authenticated Sentinel. Host-mode sessions reject `native_permission`
JSON. Fixture permission records are accepted only in explicitly `INJECTED`
sessions; they cannot upgrade fixture observations to native provenance.

The Keel gate is separate. A trusted host must load current canonical policy
and authentic human application approvals. JSON paths and hashes are not human
authentication. Do not let the model construct its own approval records,
canonical consent state, source hash, clock, workspace binding or tool mapping.
The CLI separates these privileged inputs into host-controlled flags/files.

## API and sequence

Create a new private session directory under an existing canonical parent:

```python
session = NativeSession.create(
    home, workspace_id=trusted_workspace,
    source_sha256=current_release_inventory_sha256,
    manifest=trusted_manifest, contract=contract, values=proposed_values,
    host_approvals=trusted_human_approvals,
    host_snapshot=fresh_canonical_host_snapshot,
    attachments=approved_base64_attachments,
    transport_mode="HOST_NATIVE_REPORTED",
    native_gate="NATIVE_TOOL_ENFORCES_PERMISSION",
    now=trusted_clock_seconds,
)
```

`contract`, `values`, approvals and canonical host snapshot retain the 0.12
typed form/gateway schemas. Attachment keys must match the active attachment
fields exactly; decoded bytes must match the approved size and SHA-256. The
session pins the complete contract, seven revisions, fields, plan, values,
approvals, attachments, manifest, workspace and current source fingerprint.
Optional `lease_seconds` is an integer from 1 through 300, default 30.
`expires_at` must be after creation, within one hour, and no later than the
human plan's validity limit. State configuration is bounded to 3 MiB.
The optional `clock` callable on `create` and the constructor is trusted host
code; its default reads the actual system time. Injected tests supply a
deterministic fixture clock explicitly. Request JSON cannot supply a clock.

1. Call `next_request(source_sha256=..., host_snapshot=..., now=...,
   dispatch=False)`. It persists a proposal and returns `PROPOSED`, the
   `request`, and `request_sha256`. `dispatch_issued_once` is false. Review and
   map the request; do not invoke an effect from a proposal.
2. Refresh actual host policy, source fingerprint and time. Call the same
   method with `dispatch=True`. Keel rechecks approvals, holds, no-AI rules,
   consent, source, revisions, current accessibility and the native-gate
   declaration. It durably commits `ISSUED` before returning the request with
   `dispatch_issued_once=true`, `native_tool_name`, and `lease_until`.
3. Invoke the mapped actual native tool once, using the exact account, form,
   target, operation and typed value. Its existing permission enforcement
   controls the call. Check the returned deadline immediately before invoking.
   Preserve and normalize the genuine tool output. Do not replace missing
   accessibility values with planned values or use an agent's recollection as
   a browser observation.
4. Call `observe(response, request_sha256=..., source_sha256=...,
   host_snapshot=..., now=...)` with fresh trusted state and time. An action
   response must use the exact `keel.muse.native-receipt.v1` schema and match
   request ID/hash, operation, snapshot ID/revision and target. A snapshot uses
   `keel.muse.accessibility.v1`; the outer request hash binds it to the issued
   snapshot request. The schemas and provenance requirements are implemented in
   `keel_muse/browser.py` and described in `docs/MUSE_BROWSER.md`.
5. Continue only when the returned session state allows another proposal. A
   fresh complete snapshot precedes each effect and follows the last effect.
   There is no submit action and no generic click, script or shell operation.

The CLI exposes the sequence with one command:

```sh
python3 -B -m keel_muse session --input REQUEST.json --home SESSION_HOME \
  --workspace-id WORKSPACE_ID --out NEW_RESULT.json
```

The input envelope is `{"operation":"create","arguments":{...}}`; supported
operation values are `create`, `next`, `observe`, `status` and `recover`.
Creation arguments contain `contract`, `values` and optional `attachments`,
`lease_seconds`, `expires_at`. Use the trusted flags `--manifest`, `--approvals`,
`--host-snapshot`, `--transport-mode` and `--native-gate` for privileged host
inputs. `next` uses `--dispatch` only for the issue step; ordinary native mode
has no permission JSON. `observe` arguments contain `response` and
`request_sha256`. Refresh `--host-snapshot` for each next/observe command.
`status` and `recover` have empty arguments. See the command's `--help` for the
exact available flags. Current source identity and actual time are computed by
the CLI, never accepted from request JSON. Request/output files contain personal
form data and should remain in private host storage. Every output path must be
new.

## Interruption, deadlines and no replay

Opening `NativeSession(home, workspace_id)` and reading `status()` perform no
writes and do not increment a controller epoch. This makes successive CLI
processes usable. An ordinary process exit between commands does not imply a
lost controller. After issue, repeated `next` calls return
`WAITING_OBSERVATION` with a hash and deadline; they do not return an actionable
request. Retrying the command cannot redispatch the consumed intent.

The deadline is bounded by the session, configured lease, current canonical
policy and fresh gateway capability. Effect deadlines also fit within the
accessibility snapshot's expiry and ten-second freshness window; injected
permission expiry also bounds the fixture lease. The trusted live clock is
sampled after transaction acquisition and after commit, so lock/fsync delays
cannot return an already stale actionable request. A consumed effect intent
that expires during commit becomes UNKNOWN and its request is withheld. Keel can
check admission and observation times; it cannot preempt a native tool or stop
an already paused platform operation. The host must enforce the request's
scope/deadline at dispatch and handle platform pause, denial and human takeover.
When that guarantee is unavailable, the mapping is incomplete. An expired
issued effect, late receipt, mismatched response, unknown outcome, denial,
permission pause reported as `ask`, takeover or 429 stops the session without
automatic retry. Never invoke an already issued request after its deadline.

Call `recover(now=...)` only when explicitly abandoning the old controller.
An issued effect becomes terminal `UNKNOWN`, even if its output was lost
before reaching the agent. Do not assume a missing receipt means no effect.
Unissued proposals and read-only snapshot requests can be abandoned for a new
snapshot nonce. Recovery does not resume a terminal session or reconcile an
uncertain effect. The tests include an actual child process terminating after
the committed effect intent, followed by an independent reopen and recovery.

An identical accepted observation is idempotent; its second delivery does not
advance the cursor or repeat an effect. A conflicting observation for a consumed
request hash halts. An unaccepted or malformed receipt cannot authorize the next
action. After an error the trusted host must reconcile actual page state and
record canonical UNKNOWN/429/hold information before any other work. The report
includes a `halt_event`; `global_host_hold_written=false` explicitly records
that this file protocol has not modified the upstream canonical store. Creating
a new session is not a way to bypass those holds.

An exact bound 429 receipt is retained even if it arrives after the lease,
explicit recovery, changed source, or an earlier accepted observation. Its
bounded issued-request identity can tighten the local stop and global hold
proposal; it cannot authorize work or upgrade an observation. An unbound or
mis-scoped response cannot claim a verified native 429.

## Meaning of the evidence

`SIMULATED` means an injected run. `PREPARED_REPORTED` means a host-reported
native sequence reached exact reported readback; this does not independently
verify a browser or account integration. `PARTIAL` preserves missing native
attachment byte hashes; metadata alone cannot prove uploaded bytes. Unverified
normal field provenance and changed/ambiguous accessibility state block the
protocol. Planned values, source-document values and fixture observations never
become native accessibility evidence by changing a status label.

Reports keep `execution_authorized=false`, `submission_authorized=false`,
`human_approval_authenticated=false`, `native_permission_authenticated=false`,
`native_browser_independently_verified=false`, and account integration
`NOT_VERIFIED`. These are separate from the narrow, consumed preparation intent.
Storage uses a private 0700 directory, 0600 database and key, SQLite transactions,
and a keyed MAC over the current state. It rejects symlinks, hardlinked state,
changed storage during an operation and unkeyed edits. The OS account/key holder
controls this boundary; the MAC does not independently detect restoring an old
database/key pair, authenticate upstream truth or protect against a malicious
host owner. Do not present a local integrity check as stronger evidence.
