# Host conformance qualification

Keel 0.14 qualifies a **pinned host normalization contract** against retained
observations. It does not authenticate Muse, Sentinel, a browser or a person.
`QUALIFIED_REPORTED` means the host-reported observations passed deterministic
checks. `SIMULATED` means an injected fixture passed; this cannot qualify a real
host. Both preserve `execution_authorized=false` and `submission_authorized=false`.

## Host-owned configuration

The trusted host supplies the exact 0.13 adapter manifest, current native tool
schemas and a qualification configuration:

```json
{
  "schema": "keel.operational.qualification-host.v1",
  "host_id": "operator-chosen-host-id",
  "adapter_version": "operator-pinned-version",
  "runtime_fingerprint_sha256": "64-lowercase-hex-digest-of-actual-runtime-configuration",
  "fixture_origin": "http://127.0.0.1:8765",
  "fixture_account_id": "synthetic_candidate",
  "fixture_authorized": true,
  "transport_mode": "HOST_NATIVE_REPORTED",
  "native_gate": "NATIVE_TOOL_ENFORCES_PERMISSION"
}
```

This is illustrative configuration, not a usable hash or a native Muse API.
Tool schemas are an exact dictionary from the actual mapped native tool names
to their current nonempty schema objects. No names are guessed. The injected
demo uses names starting `fixture.`; these are not Muse tool names.

The configuration, source fingerprint, clock, normalization adapter, and pinned
expected report/plan hashes must come from trusted host code or operator-owned
configuration. Accepting model-supplied JSON as this authority defeats the
boundary. Host configuration declares an authorized **synthetic fixture** origin
and account, never permission to prepare or submit a real application. The
underlying ActionGateway still checks fixture consent, policy, no-AI rules,
approvals, holds, revisions and expiry. Native tools enforce their actual native
permission gate. There is no fabricated Sentinel receipt or public Sentinel API.

## Workflow and API

1. `freeze_plan(fixture, manifest=..., tool_schemas=..., host_config=...,
   source_sha256=..., now=..., ttl=3600, required_operations=None)` freezes the
   approved synthetic fixture, complete expected action sequence, required verbs,
   host/source/schema pins and expiration. Default required verbs are all declared
   manifest operations. The maximum qualification lifetime is 24 hours; this
   never extends the much shorter approvals, leases or snapshots used during a run.
2. On a host with a trusted Python capture bridge, `run(home, plan,
   expected_plan_sha256=..., manifest=..., tool_schemas=..., host_config=...,
   source_sha256=..., capture=..., now=..., clock=...)` drives one existing
   durable NativeSession. The capture callback receives one bounded request and
   returns exactly `raw` original bytes, `normalized` observation JSON and the
   actual `native_tool_name`. The clock is sampled before and after capture;
   no automatic replay occurs on interruption. No callback means `UNAVAILABLE`.
3. Personal Muse may instead drive the existing serializable NativeSession
   propose/dispatch/observe handshake with its actual native tools. Capture each
   observation using `record_observation(request, raw_bytes, normalized,
   captured_at=..., native_tool_name=...)`. Pass the retained ordered records to
   `grade(plan, records, expected_plan_sha256=..., manifest=..., tool_schemas=...,
   host_config=..., source_sha256=..., now=...)`. No Python-callable Muse API is
   assumed. Fixture qualification does not require an already-qualified host,
   but must retain all other shared stop, policy and native permission controls.
4. Pin the accepted report hash in trusted host configuration. Before dispatch,
   call `verify(report, expected_report_sha256=..., plan=...,
   expected_plan_sha256=..., manifest=..., tool_schemas=..., host_config=...,
   source_sha256=..., now=..., required_operations=...)` using **current** host
   values. `admissible=true` only passes the conformance prerequisite. It grants
   no tool permission or application approval. Injected evidence is rejected by
   default; `allow_injected=true` is solely for a fixture execution path.

`fixture_inputs()` and `demo(home)` are executable examples; their observations
are injected local state, not rendered browser results. Callback capture is a
trusted host integration facility, not a model-configurable arbitrary tool.

## What is checked

Every normalized request is reconstructed from the exact approved plan. Unique
request IDs, actual native tool mapping, snapshot/target/contract bindings,
operation, approved value and attachment bytes must match. Each snapshot must
be fresh, scoped to the exact fixture/account/revisions, complete and free of
unexpected controls or submission. Snapshot revisions must advance. Every
action receipt must match its issued request, and the target must be active and
editable. The final readback checks every field and attachment hash.

Each capability is reported as `TESTED`, `OBSERVED_UNVERIFIED`, `UNEXERCISED` or
`UNAVAILABLE`. A complete exact final readback and all required verbs are
necessary for qualification. Missing final observations, metadata-only uploads,
absent required verbs, native denial, mismatching values or expired evidence
cannot become success. Report grades and authority flags are recalculated from
records; a supplied `PASS` or forged report status is not trusted.

Manifest, tool schema, runtime configuration, adapter version or source changes
invalidate the qualification. Expiration also requires a fresh run. Persisted
source/report hashes detect changes only relative to externally trusted current
pins; they do not defeat a host owner replacing all code, data and pins together.

## Evidence and limits

Each record retains original bytes as canonical base64 with their SHA-256, plus
separate normalized JSON and its SHA-256. These are intentionally distinct.
The raw-to-normalized relationship remains a trusted-adapter claim; a digest is
not proof of execution or of a correct normalizer. Qualification needs real
host fixture observations before any Muse account compatibility can be claimed.

Each capture is written to an exclusive `observation-NNN.json` before it is
interpreted. After a capture crash, the durable session retains the issued
intent. Recovery must not automatically replay it. Records may contain fixture
values and original tool output; protect them as private host data. The fixture
is bounded to 64 actions / 129 observations; original output is at most 32 KiB
per observation, and larger observations fail closed. No real model inference,
browser launch, network call or production submission is performed by the demo.

This tests the declared preparation protocol. It does not establish semantic
truth, independently authenticated human consent, competitive superiority,
all-site browser compatibility, or that host-global stop propagation works.
Those remain separate controls and tests in the operational runtime.
