# Receiving agent: Keel Workbench 0.1.0

This is an additive extension to the supplied Keel 0.7.0-review.1 release.
It supplies a local operator UI, HTTP/OpenAPI integration, Python SDK,
JSON-line bridge, existing LocalState adapter and five review workflows.
Read `WORKBENCH.md` first. Do not run an old release script expecting it to
include the new extension files.

## Transfer

The single TXT attachment contains the whole source tree and the extractor.
Save the full attachment as `Keel_Workbench_0.1.0_Transfer.txt`.

```sh
python3 -B Keel_Workbench_0.1.0_Transfer.txt --verify-only
python3 -B Keel_Workbench_0.1.0_Transfer.txt --out /existing-parent/new-keel-workbench
```

The output directory must not exist. Do not extract over the live repository.
The decoder checks all sizes, paths and SHA-256 hashes before extracting.
Integrity checks do not establish cryptographic publisher authenticity.

## Review and integrate

1. Verify the base-file hashes in `workbench-base-manifest.json`. All 465 source
   and evidence files from the supplied 0.7 transfer are preserved byte-for-byte.
2. Run both unchanged guarded suites with `tools/run_release_checks.py`.
3. Run `tools/check_workbench_http.py` for actual local server/SDK behavior.
4. Start `python3 -B -m keel_workbench serve --demo`; open its private URL.
5. Check the visible interface at desktop and narrow widths. Search and filter
   opportunities; open a role; run a selected-role source plan and material
   review; run a two-minute brief and synthetic replay; compare the default twin
   (300s baseline vs 200s scenario runway; 1200s vs 1800s first refill).
6. Download a result and a snapshot. Reimport that snapshot. Try an invalid
   workspace ID and confirm that the current workspace is preserved. Refresh
   using the printed terminal URL. Test keyboard focus and the role dialog.
7. Export an authentic snapshot from the existing canonical adapter. Wire
   `snapshot_from_agent(existing_state)` or `adapt-body` to the receiver's
   actual host. Supply actual source records and the host attachment root.
8. Start a separate operational server with explicit workspace ID; do not relabel
   the synthetic demonstration as live applicant data.

No live repository, deployment target or recipient transport was configured
here. Receipt, integration and live operation must be established by the
receiving host. This handoff is not evidence that Muse received the code.

## Verification boundary

`audit/workbench-validation.json` records the new tests and actual loopback HTTP
checks. Historical `audit/selfhost-*` files belong to unchanged Keel 0.7; do not
present them as new Workbench browser evidence. The current remote browser
failed local navigation with `ERR_BLOCKED_BY_CLIENT`, so rendered visual and
interactive browser checks are explicitly **UNVERIFIED**.

This extension grants no browser authority, no consent, no source authenticity,
no source freshness, and no submission authorization. JSON uploads replace
only the Workbench process's copy. Runtime state, imported snapshots, session
tokens and real applicant data must not be added to the release allowlist.

## Files to integrate

- `keel_workbench/`: application service, reducers, API, SDK, bridge, CLI and UI.
- `WORKBENCH.md`, this handoff and `workbench-base-manifest.json`.
- `tests/test_workbench_*.py` and `tools/check_workbench_http.py`.
- `tools/build_workbench_transfer.py` and `workbench-release-files.json`.

Keep all changes additive unless a reviewed change to the underlying Keel
contracts is separately intended. Preserve the existing guardian, review,
consent, attempt-history and canonical-write boundaries.
