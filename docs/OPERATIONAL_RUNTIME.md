# Qualified native sessions and canonical stop delivery

`keel_operational.runtime.OperationalSession` wraps the unchanged 0.13
`NativeSession` in the 0.14 shared stop ledger. It prepares fields only; there is
no submit operation. The adapter retains human approval, consent, no-AI,
deduplication holds, revision checks, native permission gates and final readback.
The tests execute the injected fixture, not a real Muse account.

## Host wiring

Create a private runtime metadata directory and a separate native-session
directory. Both must be siblings of other configured stores, never nested.
Metadata uses `operational-runtime.sqlite3` and `operational-runtime.key`.
The host key authenticates local consistency against unkeyed edits; an operator
who controls the key can alter or roll back local state.

```python
session = OperationalSession.create(
    runtime_home, native_home=native_home, ledger=shared_ledger,
    workspace_id=workspace_id, session_id=session_id, scope_id=role_id,
    qualification_provider=load_qualification_bundle,
    source_provider=current_source_inventory_hash,
    host_snapshot_provider=current_application_policy_snapshot,
    control_gate_provider=current_control_snapshot,
    contract=contract, values=values, host_approvals=existing_approvals,
    attachments=approved_attachment_bytes,
    transport_mode="HOST_NATIVE_REPORTED",
    native_gate="NATIVE_TOOL_ENFORCES_PERMISSION",
)
```

The constructor callbacks are trusted host code, not model-supplied functions.
`current_control_snapshot(binding)` accepts the exact shared attempt and must
return the control module's `keel.operational.host-snapshot.v1` record. The host
must check the current workspace, role, account, form and source before binding
its snapshot to `digest(binding)`. Reading a stale JSON file does not refresh its
`issued_at` or `expires_at`. A hash is not evidence of human authorization.

`load_qualification_bundle()` returns exactly:

```text
report, expected_report_sha256, plan, expected_plan_sha256,
manifest, tool_schemas, host_config
```

The expected hashes come from trusted host configuration. The runtime pins them
at creation and regrades the recorded observations against current source,
manifest, tool schemas, configuration and time before dispatch. Native session
expiry cannot exceed qualification expiry. An injected qualification admits only
an explicitly injected session; it cannot qualify the real native browser.
Source, schema, manifest or qualification changes require a new qualified
session, not an automatic refresh of its authority.

A later process opens the existing stores without provisioning or restarting:

```python
session = OperationalSession(
    runtime_home, workspace_id, native_home=native_home, ledger=shared_ledger,
    qualification_provider=load_qualification_bundle,
    source_provider=current_source_inventory_hash,
    host_snapshot_provider=current_application_policy_snapshot,
    control_gate_provider=current_control_snapshot,
)
proposal = session.next_request()
issued = session.next_request(dispatch=True)
# The Muse agent maps issued["native"]["request"] to its actual native tool.
# Sentinel permission remains inside that tool invocation.
observed = session.observe(normalized_observation,
                           request_sha256=issued["native"]["request_sha256"])
```

The CLI implements this successive-process file exchange; see
`python -m keel_operational --help`. Real mode accepts no permission JSON. The
`native_permission` argument exists only for injected fixture tests. Do not feed
full private host files or approval records into model context.

## What is durable

Before shared admission, metadata records a stable attempt ID and the digest of
the exact proposed native request. Admission reserves the role and account/form
resource. The token is saved privately before the shared intent is issued. The
shared intent commits before the native intent. Neither issued intent may be
returned a second time, including after an ordinary process exit and reopen.

A crash between either pair of commits leaves an unresolved association. A
subsequent `next_request` returns `WAITING_OBSERVATION` with no native request.
An exact repeated observation may finish recording a previously observed result;
it never repeats the native tool. `recover()` abandons the session. An issued
shared intent becomes UNKNOWN and locks its role and resource. A lost admission
token also prevents issuance; reservations expire under the shared ledger's
recovery rules. Recovery is not authorization to retry.

Every operation holds the shared writer barrier, including canonical stop
callbacks. Per-session file locking serializes multi-store commits. Maintenance
can therefore snapshot all registered stores without catching an intentional
intermediate commit gap. The backup's `OFFLINE_RESTORE.json` marker prevents
fresh dispatch from restored sessions. Restore needs explicit recovery and new
host qualification; copying a browser session's metadata cannot recreate its
original browser context.

## Stop propagation

An exactly bound 429 observation tightens the workspace stop even if it arrives
after source drift, expiry, recovery or a previously recorded observation.
Uncertain effects lock the role across accounts and the account/form resource.
The stop and durable outbox commit before any delivery callback is attempted.
Missing or failing canonical sinks stay visibly pending and prohibit further
wrapped dispatch. JSON input cannot acknowledge the outbox.

`coordinator_sink(live_controller)` calls the existing `Coordinator.ingest`, then
reads committed state. It uses `RATE_429` or a stable role-scoped `HOLD` event.
Do not construct a new writable Coordinator on every CLI command; writable
construction is a controller restart.

`recovery_sink(live_journal, role_to_existing_jobs)` calls the existing
`RecoveryJournal.record_429` or `set_hold`, then reads state. It never invents
jobs or approvals. Already persisted stops do not generate another mutation on
redelivery. `combined_sink(*trusted_callbacks)` requires acknowledgments from
all configured adapters. A callback to an additional canonical guard must perform
and confirm its real write before returning the exact canonical acknowledgment;
that callback remains trusted host code, not independent proof.

Only a valid normalized receipt bound to a known issued native request can claim
a native 429. Invalid or conflicting observations are conservative uncertainty,
not evidence that the real browser performed an action. `RECORDED` denotes an
observation, not a successful application submission.

## Final admission boundary

Before returning an actionable request, the wrapper rechecks qualified source
and configuration, current application policy, shared stops, delivery backlog,
and the live clock after blocking callbacks and commits. The returned deadline
is the minimum of native lease, shared intent lease and current control snapshot
expiry. An observation after that deadline cannot complete the intent.

A stop can still occur after the request leaves this process. The receiving host
must recheck the lease and current gates at actual native invocation. This wrapper
cannot revoke a tool call already invoked, stop unrelated Muse tools, supply a
Sentinel grant, authenticate an account or provide an OS sandbox. Its global stop
covers registered wrapped workers only. The status report exposes shared stops
separately from the native session's last observed status; native `READY` is never
dispatch authority.

## Verification

Tests execute all 23 fixture interactions and final readback, reopen the stores
between operations, inject both commit-gap crashes, terminate a real child process
after native commit, preserve late 429s, reject qualification drift, preserve
existing approval/consent/no-AI gates, and exercise actual Coordinator and
RecoveryJournal writes. Additional deadline tests cross qualification and control
snapshot expiry during final callbacks. No real browser or model is called.
