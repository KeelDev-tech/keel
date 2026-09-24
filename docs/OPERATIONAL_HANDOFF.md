# Keel 0.14.0 receiving-agent handoff

Status: tested integration candidate, not deployed. This release adds Muse
capability qualification, coordinated stop controls, durable native-session
execution and recovery mechanisms over the complete 0.13 reference. All 759
predecessor files remain byte-identical, including their historical audit
reports. No new paid API dependency is required by these additions.

The new interfaces belong to Keel. They do not establish that personal Muse
exposes a particular SDK, MCP server, hook, permission-receipt API or browser
debugging endpoint. Inspect and map the capabilities actually available in the
receiving account. Muse account integration, canonical live-host integration and
actual native-browser execution have not been verified by this delivery.

## Receive and verify

The complete reference is a Python-executable TXT transfer. No ZIP is required:

```sh
python3 -B Keel_0.14.0_Transfer.txt --verify-only
python3 -B Keel_0.14.0_Transfer.txt --out /existing-parent/new-keel-0.14
```

Use a new extraction directory on an authorized host. If the receiving agent
cannot execute Python, report that limitation and perform extraction on a host
that can. Never present an unexecuted command as successful.

Read `docs/OPERATIONAL.md` for the current CLI and module contracts. The Release
Evidence JSON binds the exact transfer, source inventory, regression outputs and
synthetic acceptance. Included hashes prove internal consistency; they do not
authenticate a publisher, a human reviewer, a source record or Muse itself.

## Verify and add to a reference tree

The installer checks 758 unchanged 0.13 prerequisites. Historical root
`MANIFEST.json` remains in the full transfer but is neither required nor
installed on the host. Existing files are never overwritten.

```sh
cd /existing-parent/new-keel-0.14
python3 -B tools/install_operational.py --target /path/to/clean-0.13-reference
python3 -B tools/install_operational.py --target /path/to/clean-0.13-reference --install
```

The first command only verifies. Installation is additive and idempotent;
changed prerequisites, conflicts, symlinks and digest mismatches block it.
On a modified live host, review and port the additions against that actual tree.
Do not reset live state or configuration to satisfy reference-file hashes.

Keep runtime state and generated output outside the source tree. Run the
unchanged guarded suites and the new acceptance runner on the receiving port:

```sh
python3 -B tools/run_operational_checks.py --out /existing-parent/new-checks
python3 -B tools/run_operational_acceptance.py --out /existing-parent/new-acceptance
```

Development dependencies remain in `requirements-dev.txt`. Passing reference
checks establishes the scope stated by those reports. It does not deploy Keel,
exercise the real account's native tools or authorize an application.

## Qualify the receiving Muse environment

Read `docs/OPERATIONAL_QUALIFICATION.md` and `docs/OPERATIONAL_RUNTIME.md`.

1. Inspect the tools actually exposed in this account. Record their exact names,
   input schemas and supported operations without copying credentials or
   unrelated account data. Separately establish whether Python, process
   execution and persistent files are available. Presence on a tool list is a
   declaration; exercising an authorized fixture provides stronger evidence.
2. Map only the operations required by the fixture's contract. Keel normalized
   operations and injected fixture names are not public Muse API names. Bind the
   qualification to the source, configuration, form contract and tool mapping
   that were actually exercised. Pin the qualification plan and report outside
   the model payload, and provide fresh tool schemas and host configuration at
   dispatch. Changed bindings or an expired qualification require a new run.
3. Use an authorized fixture with synthetic values and no submission action.
   Preserve the original observations and their normalized counterparts.
   Unsupported operations, missing readbacks and uncertain results remain
   visible. A reported native observation does not independently authenticate
   Muse or prove the absence of an effect.
4. Follow the durable proposal, dispatch and observation sequence. Persist
   dispatch intent before the actual tool call. An interrupted or ambiguous
   action must not be automatically replayed. Retain the current account,
   target, field, snapshot and human-approval bindings at every step.
5. Keep the native platform's own permission checks in force. The declaration
   `NATIVE_TOOL_ENFORCES_PERMISSION` delegates permission enforcement to the
   actual tool; it grants nothing. Do not fabricate an `allow` receipt or assume
   a public Sentinel permission API. Keel's per-application human approval is a
   separate requirement.

Do not use real applicant records or employer pages as substitutes for the
authorized fixture rehearsal. A filename or file size does not establish
attachment byte identity. Required attachment verification remains incomplete
unless the actual mechanism supplies the needed byte-level observation.
Injected qualification is SIMULATED and cannot satisfy real-host verification.
Observed host protocol may qualify as QUALIFIED_REPORTED; its platform,
permission and human-authentication flags remain false. Missing or unobserved
capabilities must remain UNAVAILABLE or NOT_QUALIFIED.

## Connect global controls and recover safely

Read `docs/OPERATIONAL_CONTROL.md` and `docs/OPERATIONAL_RECOVERY.md` before
connecting the new operational runtime to canonical host state.

Every participating worker must consult the same authoritative controls. A
global 429 must propagate to all relevant workers; an uncertain attempt must
prevent another worker from repeating that action. Merely recording a halt in
one new store does not prove an independently running legacy worker observes it.
Port and test that connection explicitly. Keep all existing consent, no-AI and
unaided-work rules, human approvals, dedupe holds, UNKNOWN states and 429 stops.
Install canonical sinks as trusted host callbacks. Sink acknowledgements bind
the event hash, and delivery is at least once: the sink must deduplicate each
stable event ID. JSON supplied by the model cannot acknowledge an event. The
shared writer barrier covers participating local writers; it cannot stop an
unmediated process or an already dispatched external browser action.

Recovery requires an inventory of the actual stores and files being captured.
Use the documented supported profiles and quiescence protocol; include every
authoritative component required by the configured workspace. A partial store
set is not a complete operational checkpoint. Keep an authoritative current
checkpoint outside the backup being judged. Restoring a backup cannot establish
its own freshness. Restored pending work does not become safe to dispatch merely
because its files pass hash verification.

The configured inventory can cover the stop ledger, native sessions, source
records and their historical content-addressed attachments, temporal memory,
coordinator and recovery journals, local-agent approvals and attempts, skill
workshop history and operational metadata. Explicit canonical SQLite/file
profiles cover declared opaque host records. Undeclared or missing required
files block the capture. Remote Muse, browser and server state are excluded.
Restore opens a new private offline directory; it does not start controllers.
Fence outstanding intents and revalidate live grants, tool mappings and page
state before activation.

Exercise crash recovery against a disposable fixture workspace: stop after
dispatch, restore, and show that the interrupted action cannot be repeated
automatically. Verify that revoked approvals, holds and global rate-limit state
survive. Protect private state, attachment files and integrity keys as private
operational data; none belongs in a public code transfer.

## Report measured scope

Report RECEIVED, VERIFIED, INTEGRATED, SHADOW_VALIDATED and DEPLOYED separately.
Keep reference installation distinct from integration with the real live tree.
Keep synthetic, injected and host-reported observations distinct from independent
verification. Preserve failed, missing and unavailable cases in all totals.

This delivery leaves account Muse integration and live canonical integration
NOT_VERIFIED, actual native-browser execution and real-model inference NOT_RUN,
and production deployment and execution authorization false. No benchmark here
establishes competitive superiority. No package mechanism creates authentic
source revisions, manufactures a human decision or grants submission authority.
