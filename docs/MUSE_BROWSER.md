# Native accessibility adapter for the Keel host

Keel 0.13 supplies an executable normalization protocol and a tested injected executor. It does **not** claim that the protocol names below are real Muse tool names or that an official Muse SDK exists.

Meta's September 8, 2026 explanation describes a controlled browser interface, an external permission authority named Sentinel, and user takeover. Its public description does not provide the concrete tool schema needed to connect this package. A host must inspect its actual available tools and map supported operations. Native permission remains separate from application approval. [Meta's Muse security architecture](https://research.meta.ai/blog/security-and-safety-for-ai-agents-our-approach-with-muse).

## What this code executes

`keel_muse.browser.NativeAdapter` compiles the existing complete 0.12 typed form plan, calls an explicitly supplied native transport, checks each returned accessibility snapshot, consumes exactly one approved action, validates the receipt, and obtains final readback. The frozen fixture has 12 fields, 11 active actions and one inactive conditional field. Its complete rehearsal makes 23 injected transport calls: one snapshot before each action and one final snapshot.

The module contains no browser launcher, page JavaScript, CDP, arbitrary shell, generic click, arbitrary navigation or submission operation. Tool names in the manifest are descriptive strings; this module never executes those strings. A native mapping that cannot safely express a field must declare the corresponding verb unavailable and stop.

A Python callback does not automatically gain access to agent-only tools. When Muse's tools are available only as agent turns, use the durable JSON handshake in `keel_muse.session` and its `MUSE_SESSION.md` instructions. The callback API is for a trusted host that actually exposes a callable bridge. Both paths use the same normalization records and preserve the same authority boundaries.

## Capability probe

```python
from keel_muse.capabilities import probe_host
report = probe_host()
```

The probe observes the running Python interpreter and executable filesystem path. It does not launch a subprocess, invoke a model, open a browser or call native tools. Python's presence does not prove process spawning is allowed. Playwright presence is reported separately and is not a native-adapter requirement.

Passing a validated manifest changes the native mapping status to `HOST_DECLARED`; it never changes it to a live tested or authenticated capability. The report distinguishes `PROBED` current interpreter information, `FILESYSTEM_OBSERVED` files, `HOST_DECLARED` mappings, and `UNAVAILABLE` capabilities.

## Host-callable API

```python
adapter = NativeAdapter(
    manifest,
    transport=actual_host_normalized_transport,
    host_snapshot_provider=load_current_keel_policy,
    native_permission_provider=check_actual_native_permission,
    record_halt=persist_unknown_rate_limit_or_takeover,
    clock=trusted_epoch_seconds,
    transport_mode="HOST_NATIVE_REPORTED",
)
report = adapter.prepare(
    captured_contract, proposed_values, real_host_human_approvals,
    attachments=approved_attachment_base64_by_field,
)
```

These providers are trusted-host facilities and must not be exposed as model tools. Keel cannot authenticate their authors merely because returned JSON has the correct keys. The host must load current canonical policy and actual human approvals. Do not fabricate an `allow` permission record because an application has human approval; conversely, a native permission grant cannot supply an application approval.

Each adapter instance performs at most one preparation session. It never retries an action or carries a capability across a process restart. On uncertainty, rate limiting, takeover, changed form scope or failed readback, it revokes the local capability and invokes the halt callback. The report records whether that callback returned, while leaving actual storage durability unverified. The real host must persist holds and reload them before another session.

## Normalized transport contract

`transport(request)` receives `keel.muse.native-request.v1` with:

- A unique request ID and one allowlisted operation.
- Exact origin, account, form ID and complete form-contract hash.
- For mutations, the snapshot ID/revision, one captured target reference and the existing 0.12 typed approved action.
- For upload only, the approved attachment's canonical base64 bytes. Bytes are size/hash checked before any native call.

The embedded callback interface accepts `keel.muse.native-permission.v1`: a permission ID, request hash, decision, issue/expiry times and revocation flag. Only a current `allow` for the exact request proceeds. Its deadline is checked again after any slow canonical-state refresh and immediately before the transport call. This interface is usable only if that host actually has an authoritative permission provider; no public Muse API for producing such a receipt has been verified. **Never invent an allow record to make this callback work.**

For ordinary Muse agent-tool turns, the durable session handshake instead uses the explicit host declaration `NATIVE_TOOL_ENFORCES_PERMISSION`. That declaration is not permission. It means the real native tool still consults its existing platform permission gate during the actual invocation; pending or denied native permission stops the workflow. Application approvals remain separately required. See `MUSE_SESSION.md` for proposal, persisted dispatch intent, normalized observation and uncertain-effect recovery.

`accessibility_snapshot` must return `keel.muse.accessibility.v1`. It includes a fresh snapshot ID and increasing revision, observation/expiry times, origin/account/form/contract identity, all seven pinned revisions, exact complete field descriptors, native target references, active/editable states and actual reported values. It also explicitly reports unexpected controls, human takeover, submission observation and provenance. Missing field descriptors or unsupported conditions stay blocked; they must not be invented from the desired plan.

The host must enforce snapshot-to-action binding with its actual tool semantics. A declared guarantee alone is not proof. If the real tools cannot bind an action to the current exact reference and scope, set `snapshot_action_binding` false and use a human workflow.

A mutation returns `keel.muse.native-receipt.v1`, binding the exact request ID/hash, operation, snapshot ID/revision and target reference. Recognized statuses are `applied`, `denied`, `ask`, `unknown`, `rate_limited` and `human_takeover`. Only `applied` continues; an error after dispatch remains uncertain and must not be replayed automatically.

Normal field values require accessibility-observation provenance. Values copied from host source records or marked unavailable cannot become verified text readback merely by matching the plan. Attachment metadata without a reported native content hash yields `PARTIAL`, including when the local source file's hash is known. A local upload-source hash is not evidence that the browser received those bytes.

## Status interpretation

- `SIMULATED`: the explicitly injected executor reached matching complete reported values.
- `PREPARED_REPORTED`: a configured host transport reported complete matching readback; execution and permission authenticity remain unverified by this module.
- `PARTIAL`: other matching readback exists, but attachment content or another observation remains unverified.
- `BLOCKED`: a required gate or observation failed.
- `UNKNOWN`: a dispatched action lacks an accepted matching applied receipt. Stop and reconcile without replay.

No result is named browser `PASS`. `native_browser_independently_verified`, `native_permission_authenticated`, `human_approval_authenticated`, `execution_authorized` and `submission_authorized` remain false. Successful complete data comparison cannot grant application submission authority.

## Offline rehearsal

```python
from keel_muse.browser import demo
report = demo("/absolute/existing-parent/new-native-run")
```

This creates a private report for an actual in-memory fixture executor. It supplies synthetic application approvals and synthetic native permission observations explicitly for testing. It performs zero actual Muse browser actions, model calls or external requests. These fixture records must never be copied into authentic application authority.
