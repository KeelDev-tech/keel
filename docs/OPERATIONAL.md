# Keel 0.14 operational integration candidate

This additive release connects the 0.13 native preparation session to shared stop
state, a pinned host qualification report, and an explicitly inventoried recovery
checkpoint. It uses Python's standard library at runtime and introduces no paid
API or model requirement. Muse's own service availability and allowances remain
outside this package. All 759 reference files are preserved byte for byte.

## Runtime path

The trusted host freezes a synthetic qualification plan against actual tool names
and schemas, its adapter/runtime fingerprint, and this release's source digest.
It captures the original tool responses separately from normalized observations.
Qualification recomputes the entire request/receipt sequence and final field
readback. Missing verbs, missing attachment digests, expired plans and changed
source or tool schemas do not qualify. Injected tests remain `SIMULATED`.

`OperationalSession` wraps the existing `NativeSession`. Each request is assigned
a durable attempt, reserved in `StopLedger`, and consumed once at dispatch. The
wrapper rechecks qualification, source, host policy, approval state and deadlines
at the action boundary. It persists uncertain outcomes instead of replaying them.
Existing per-application and per-field human approval controls remain in force.

A 429 stops the shared workspace; UNKNOWN holds both its role and its resource.
Stops survive process reopen. The durable outbox forwards events to a trusted
canonical sink with stable event IDs. Delivery is at least once: that sink must
deduplicate, and acknowledgement occurs only through the configured callback.
No acknowledgement supplied through model JSON can clear delivery obligations.

This guarantee covers registered workers using the shared wrapper. An already
invoked remote action cannot be recalled. The host must enforce the returned
deadline and native permission gate at actual invocation. Legacy workers,
unrelated Muse tools and external browser state require their own host wiring.

## Recovery path

The operator names every required store and at least one canonical store in an
explicit inventory. Recovery takes the participating-writer maintenance lock,
captures logical SQLite state and private keys, copies attachment objects, and
checks the source state again before publishing. All configured stores are
required; unsupported or unexpected files cause a failure.

Restore requires the independently retained current checkpoint and manifest
digest, creates a new private directory, verifies restored state, and writes an
offline marker. It starts no controllers and replays no actions. UNKNOWN, 429 and
revocation history remain present. The operator must reconcile external effects
before activating any restored environment. The checkpoint cannot prove that
the operator omitted no store or that no unmanaged writer exists.

## Receiving-host workflow

1. Verify and restore the TXT transfer; follow `docs/OPERATIONAL_HANDOFF.md` for
   additive installation and source-bound regression commands.
2. Define actual trusted Muse tool mappings and schemas. Keep native permissions
   enabled. Personal Muse and Muse Code integrations are separate host surfaces;
   this package invents no permission, hook or Sentinel receipt API.
3. Freeze a fresh synthetic qualification plan. Export its browser fixture with
   `qualification export_fixture` through the JSON CLI. The output includes
   `fixture.html`, exact approved attachment bytes and a hash manifest.
4. Serve that fixture only through a host-supported, operator-authorized route
   matching the plan's origin. The exporter starts no server and publishes
   nothing. Visible scope text and attachment hash readouts support accessibility
   tools without requiring agent-executed page JavaScript. Page-owned WebCrypto
   reports `UNKNOWN` when unavailable. This fixture capability does not establish
   attachment-hash availability on employer pages.
5. Capture actual native tool observations, grade and independently pin the
   qualification report. The result is `QUALIFIED_REPORTED`, not authenticated
   platform execution. A trusted host capture/normalization adapter is required.
6. Route participating workers through `OperationalSession`, connect the shared
   ledger's canonical sink to the real store, and exercise stop propagation.
7. Inventory actual runtime stores, retain the checkpoint independently, and
   rehearse an offline restore before enabling the deployment.

See `OPERATIONAL_QUALIFICATION.md`, `OPERATIONAL_CONTROL.md`,
`OPERATIONAL_RUNTIME.md`, and `OPERATIONAL_RECOVERY.md` for exact Python APIs and
limitations. `OPERATIONAL_CLI.md` provides complete operator configurations and
JSON requests; `python -m keel_operational --help` describes the command interface.
Host configuration and output directories must be private and outside the source
tree. The generic CLI exposes pending outbox events; canonical delivery requires
a trusted long-lived host callback, rather than dynamically loading supplied code.

## What the included evidence establishes

The regression and acceptance runners use the unchanged offline test guard,
private SQLite/files and synthetic observations. They test complete preparation,
qualification drift, deadline races, multiworker stop state, uncertain recovery,
canonical delivery contracts and offline restoration. Release evidence binds the
actual test outputs and acceptance checks to one source inventory. The transfer
verifier independently checks payload hashes before writing any files.

This is an integration candidate. Actual Muse tools, real local inference, live
canonical integration, production deployment, competitive superiority and the
authenticity of user white-paper claims are not established by these tests.
