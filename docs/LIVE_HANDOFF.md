# Receiving agent: Keel 0.9 integration candidate

This release connects the existing source producers to a private operator review surface and a capture-to-review proof report. It supplies code and measured local validation. It does not establish receipt by another agent, installation into the user's live tree, genuine evidence for a live role, actual local inference, rendered application preparation or deployment.

The latest user-supplied live status remains 0.7 integrated and shadow-validated, with 0 of 837 leads exporting any of the seven source families. Confirm subsequent host changes directly before claiming a new live status.

## Transfer and inspect

The preferred transfer is the **single complete TXT attachment**, `Keel_0.9.0_Transfer.txt`. It contains its decoder and the combined reference tree: the preserved 0.7 baseline, additive 0.8 source producers, the preserved Workbench extension and additive 0.9 integration files. This is a full reference delivery; the earlier 0.8 TXT was only a patch.

Save the attachment intact. Do not copy only its visible preview or truncated message text. From a directory outside the live tree:

```sh
python3 -B Keel_0.9.0_Transfer.txt --verify-only
python3 -B Keel_0.9.0_Transfer.txt --out /existing-parent/new-keel-0.9-reference
```

The output directory must not exist. The decoder verifies the contained sizes, paths and hashes before extraction. Hash agreement establishes delivery integrity, not cryptographic publisher identity. `live-base-manifest.json` records the 528 preserved predecessor files, including their historical evidence. `live-release-files.json` lists the additive release inputs, and `keel_live/PATCH_MANIFEST.json` binds the additive installation to its base-file hashes. Review the measured evidence in `audit/live/validation.json`.

Do not overwrite the live tree with this reference. Identify which additive extensions are already present, compare file hashes, and port only the missing/new files through the receiving host's normal review process. Preserve the host's real configuration, secrets, state, source histories and existing modifications. A conflicting local file needs an explicit reviewed merge; it must not be reset merely to match a reference hash.

The included installer first verifies the exact base and identifies missing versus identical additions:

```sh
python3 /existing-parent/new-keel-0.9-reference/tools/install_live.py --target /actual/keel/tree
```

After reviewing those hashes and the proposed additions, an authorized host integration can run:

```sh
python3 /existing-parent/new-keel-0.9-reference/tools/install_live.py --target /actual/keel/tree --install
```

The installer verifies all 465 original source/evidence payload files. The historical 0.7 ZIP's extra `MANIFEST.json` remains preserved in this full reference, but is neither required nor installed because the original 0.7 TXT omitted it. The installer creates missing additive files exclusively, skips identical existing files and refuses differences. A strict-baseline rejection can reflect legitimate host configuration changes: do not reset those changes. In that case, validate against a separate clean reference, then have the receiving agent review and port the additive modules to the actual tree with the live tests. The installer starts no service, creates no source evidence and downloads no runtime dependencies.

The regression inventory excludes only that historical ZIP manifest; it verifies every installed source, test, configuration, static asset, and listed supporting document. A full reference rebuild also requires the original historical manifest and should run from the extracted reference tree. A host's own packaging manifest is left unchanged.

## Verify in isolation

Read `docs/LIVE_INTEGRATION.md`, then run the inherited release checks and the new integration tests using the receiving host's established test environment:

```sh
python3 tools/run_live_checks.py --out /new/private/live-regressions
python3 -m pytest tests/test_live_*.py -q
python3 tools/rehearse_live.py --out /new/private/live-rehearsal
python3 -m keel_live demo --home /new/private/live-preview
```

The added runtime uses Python 3.11+ standard-library components and no paid service. The inherited attachment path requires supported Linux atomic publication. The test environment may need the same existing development dependencies as the reference suite. Use a separate fixture directory for each command; the tools refuse an existing rehearsal/demo destination.

Open the private preview URL printed by `demo`. Review source diagnostics, request a packet explicitly, inspect its full material, and exercise explicit approval/rejection and revocation using the synthetic fixture. An approved source packet alone remains blocked until the synthetic rehearsal or real host supplies matching canonical dependencies and original assurance/trust evidence. Verify visible rendering and interaction on the target host; a local HTTP test or Python test is not rendered browser evidence.

The delivered validation report is authoritative for this build's measured tests. Historical evidence inside preserved 0.7/0.8/Workbench files retains its original scope and date; do not relabel it as new 0.9 live validation. The proof and rehearsal explicitly leave actual model inference, rendered browser preparation and final form readback `NOT_RUN`.

`tools/run_live_checks.py` wraps the unchanged guarded regression runners and binds their output hashes to the entire explicit release inventory before and after testing. `tools/live_inventory.py` verifies predecessor preservation and the additive allowlist. Run the separate acceptance wrapper to measure the synthetic rehearsal, actual loopback HTTP behavior, JavaScript syntax and optional rendered UI, binding every report to the same unchanged inventory:

```sh
python3 tools/run_live_acceptance.py --out /new/private/live-acceptance
```

Node.js and the free Playwright development package plus Chromium are needed only for rendered UI checks, not application runtime. The wrapper records an unavailable browser as `UNVERIFIED`; an actual browser test failure blocks release. It never downloads dependencies automatically. A receiving agent that rebuilds a transfer must use these measured reports:

```sh
python3 tools/build_live_release.py --checks /absolute/private/live-regressions --rehearsal /absolute/private/live-acceptance/rehearsal.json --http /absolute/private/live-acceptance/http.json --browser /absolute/private/live-acceptance/browser.json --out /new/private/keel-delivery
```

The builder requires passing guarded checks, rehearsal and HTTP evidence and a matching `release_source_sha256` on every integration report. The browser report must retain its actual measured status, including an unperformed or blocked result. A successful build does not upgrade that status. After a source change, rerun affected validation and bind it to the new complete inventory; do not merely change an old report's hash to make it match.

## Integrate the first real role

1. Identify a real current role and exact canonical application identity. Supply a complete fresh body with `flow`, `assurance` and `trust`, retaining genuine absence as `null`.
2. Establish the operator through the host's existing authentication. Create a private host configuration with exact workspace/action/scopes and only their actual review permissions. Every role included in the body requires an explicit read scope; no wildcard or inferred grant is supported.
3. Initialize or use the correctly identified source store, then explicitly register fresh canonical scopes. This creates no evidence or requests.
4. Wire the six actual producer outputs to `HostSourceConnector.capture_event`. Authenticate each producer at the host, preserve original observations, bind the exact flow hash and use component generation compare-and-swap. Real attachments must be read from the host-selected document root.
5. Start `python3 -m keel_live serve --config /absolute/private/host-config.json --body /absolute/private/current-body.json`. The server automatically rereads the trusted body file. Keep that export current through genuine canonical observations; the interface cannot renew its timestamps.
6. Have the authorized operator review the exact packet and record their actual decision. Protect the printed session URL as a delegated capability. A token or configured actor string is not independent proof of identity.
7. Consume the sidecar's seven revisions through the real canonical exporter, with full workspace/role/application/action matching and the exact flow digest. Rebuild the actual packet dependency hash and existing assurance/trust bindings through the established host path. Do not fabricate passed envelopes or edit hashes to hide changed material.
8. Run the new proof report against that current body and actual attachment root. Retain named blockers. One qualified real role is the first capture-to-review milestone; ten varied current roles are the next. Neither milestone grants submission authority.

CLI commands and exact contracts are in `LIVE_INTEGRATION.md`. Each successful export/proof command can still report blocked roles; read the report rather than treating exit 0 as readiness.

## New files and interfaces

- `keel_live/`: source connector, immutable principal and review service, Workbench surface, local server/UI, proof report, protected configuration and CLI.
- `tests/test_live_*.py`: additive integration and regression checks.
- `tools/make_live_demo.py` and `tools/rehearse_live.py`: explicitly synthetic fixture generation and rehearsal.
- `docs/LIVE_INTEGRATION.md`, this handoff and the release's measured inventory/validation artifacts.

The supported material action is `PREPARE`. HTTP can inspect and explicitly request, decide or revoke source reviews. It cannot ingest arbitrary producer events, replace the host's canonical snapshot, call a model, automate a browser or submit an application. Producer ingestion and body updates remain trusted in-process/CLI host operations.

Existing consent and policy holds, prior rejection/revocation, dedupe decisions, unknown attempt handling, 429 protections, original evidence/assurance/trust checks and execution controls remain unchanged in their predecessor files. No new result substitutes for those controls. `execution_authorized` remains false throughout this integration.

## Report host outcomes separately

Record **RECEIVED**, **VERIFIED**, **INTEGRATED**, **SHADOW_VALIDATED** and **DEPLOYED** independently, with the actual evidence for each. Include real source coverage before/after, current qualified-role counts and unresolved diagnostic categories. Record actual model and rendered-browser checks only after they occur on the target host. Do not report synthetic applicants or synthetic approvals as real workflow completion.

The next production engineering work is the host-specific mapping of actual source producers and canonical exporter bindings, followed by independently measured local inference and rendered preparation. Those integrations need the receiving machine's real schema, records, accounts and runtime; this transfer contains no substitute data.
