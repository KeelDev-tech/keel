# Keel 0.17.2 intake verdict — 2026-09-19

## Transfer identity (independently verified)

- Transfer file: `~/workspace/user/files/Keel_0.17.2_Transfer.txt`
- SHA-256 independently computed on 2026-09-19:
  `c7428f135cb733bddd825477a706b136e2eef1bdd6041c9aa735b798fb6ee30d`
  — exactly matches the handoff claim.
- `--verify-only` passed: 819 delivered files, publisher `NOT_AUTHENTICATED`,
  `execution_authorized=false`, `production_deployed=false`.
- Extracted ONLY to `~/workspace/keel-transfers/keel-0.17.2` (819 files);
  `--verify-tree` passed on all 819.
- Handoff metadata: integration version `0.17.2-integration.1`, upstream
  `VERSION` `0.2.0`, source inventory SHA-256
  `4919b0c693a52db739005af1bf173dae46d8a8e6aa824f478181eb14b2ef9f31`
  (818 source files + `MANIFEST.json` = 819 delivered).
- Diff against the 0.17.1 extraction confirms the claimed delta:
  **8 added, 14 modified, 0 removed** (plus manifest bookkeeping).
- The live tree (`~/workspace/keel`, `VERSION` `0.2.0`), the 0.17.1 extraction,
  queues, schedulers, and providers were untouched throughout.

## Package claims, separated from verified evidence

From the handoff and evidence files (all remain package claims unless marked
verified below):

- Four-suite result claimed: **4,945 passed, 13 skipped, 0 failures, 0 errors**;
  coverage `INCOMPLETE`.
- Ordinary acceptance: 13/13 checks passed (synthetic; source authenticity not
  verified). Evidence file independently parsed: `acceptance_status: PASS`,
  `tests: 4958`, `skipped: 13`, consistent with the handoff arithmetic
  (4945 + 13 = 4958).
- Ordinary incident campaign: `NOT_REPRODUCED`, one completed run, zero external
  network calls, zero browser/model calls, zero human decisions.
- Existing six-case storage fault campaign: 6/6; development-host storage
  observations: 9/9.
- `publisher_authenticated=false`, `live_canonical_integration=NOT_VERIFIED`,
  `account_muse_integration=NOT_VERIFIED`, `actual_native_browser=NOT_RUN`,
  `real_local_model_inference=NOT_RUN`.
- Production readiness: `BLOCKED`. Original intermittent SQLite incident:
  `UNRESOLVED`. No new root-cause fix is claimed; no live Muse, native-browser,
  model, deployment, physical-power-loss, or business-action qualification exists.

## Independent validation performed

Environment: this VM, Python 3.12.3, pytest 9.1.1, run in the isolated
0.17.2 extraction only — no live-tree involvement.

| Test file | Result | Runtime |
|---|---|---|
| `tests/test_incident_store.py` + `tests/test_incident_wiring.py` | 28 passed | 1.39 s |
| `tests/test_incident_adversarial.py` | 33 passed | 3.26 s |
| `tests/test_acceptance_campaign.py` | 55 passed | 2.50 s |
| `tests/test_integration_release.py` (modified builder/packaging tests) | 79 passed | 9.74 s |
| **Total** | **195 passed, 0 failed, 0 skipped** | — |

The new test files exercise the incident recorder, its adversarial hardening,
the campaign orchestration (real `os.fork()` child-kill and timeout-kill paths
included), and the release builder's new mandatory incident-campaign evidence
gate. All green. This validates the package's internal consistency — it is
synthetic evidence and does not qualify the receiving host, per the package's
own stated limits.

## Complete change inventory and classification

### Added (8) — REJECT, incident-capture package

- `keel_integration/incident.py` — bounded, hash-chained, value-free incident
  recorder. Private to the candidate's SQLite/canonical-storage diagnostic
  pipeline; uses SQLite diagnostic vocabulary without touching business state.
- `keel_integration/acceptance_campaign.py` — ordinary-acceptance campaign
  runner (fresh isolated subprocess per run, audit-hook child guard, grading,
  artifact validation). Part of the same SQLite qualification story.
- `tools/capture_host_incident.py` — CLI for the campaign (fixed worker,
  read-only re-verification mode).
- `tests/test_acceptance_campaign.py`, `tests/test_incident_adversarial.py`,
  `tests/test_incident_store.py`, `tests/test_incident_wiring.py` — package
  tests (verified passing above).
- `docs/INTEGRATION_0172.md` — release notes for the package.

### Modified (14) — classification

1. `keel_integration/__init__.py` — version string `0.17.1` → `0.17.2`.
   Packaging only. **Reject.**
2. `keel_composition/canonical.py` — one-line `diagnostic_sink` pass-through in
   `fixture_environment`. Incident-package wiring; live tree has no
   `keel_composition` module. **Reject (no live counterpart).**
3. `keel_composition/controller.py` — same pass-through. **Reject.**
4. `keel_integration/demo.py` — `diagnostic_sink` pass-throughs in
   `fixture_environment` / `run_demo`. **Reject.**
5. `keel_operator/host.py` — optional trusted `diagnostic_sink` constructor arg,
   forwarded to `CanonicalPublisher`. Incident-package wiring; no live
   `keel_operator`. **Reject.**
6. `keel_operator/commission.py` — (a) fd-anchored open for `commission.lock`,
   (b) `diagnostic_sink` pass-through in `fixture_environment`. No live
   counterpart module; see fd-anchor analysis below. **Reject.**
7. `keel_operator/intake.py` — fd-anchored open for the intake upload temp
   file. No live counterpart module. **Reject.**
8. `keel_sources/capture.py` — fd-anchored open for `pending-*.tmp`:
   `os.open(pending, ..., dir_fd=objects)` →
   `os.open(Path('/proc/self/fd') / str(objects) / pending, ..., dir_fd=objects)`.
   This is the one module that exists in the live tree. Live
   `~/workspace/keel/keel_sources/capture.py` is byte-identical to the 0.17.1
   transfer version (trailing-whitespace-normalized diff is empty), i.e. the
   silent-sweep landed nothing here. See analysis below. **Reject.**
9. `tools/build_integration_release.py` — makes `--acceptance` and the new
   `--incident-campaign` evidence mandatory; regrades campaign artifacts.
   Release-packaging only. **Reject.**
10. `tests/test_integration_release.py` — builder tests for the new gate,
    including the synthetic campaign fixture (verified: 79 passed). Packaging
    tests. **Reject.**
11. `tools/test_guard/sitecustomize.py` — allowlists the new worker script,
    docstring update. Test-harness packaging. **Reject.**
12. `docs/INTEGRATION_HOST.md`, `docs/INTEGRATION_PORTS.md` — documents the
    new package and its builder requirements. Docs only. **Reject.**
13. `release-files.json`, `MANIFEST.json` — manifest bookkeeping, version bump
    to `0.17.2-integration.1`. Packaging only. **Reject.**

### The one potentially portable idea, investigated and rejected

The three `os.open` changes convert relative `dir_fd`-anchored opens to
absolute `/proc/self/fd/<fd>/name` paths while keeping `dir_fd`. Analysis:

- Semantics are identical: same inode, same atomic
  `O_CREAT | O_EXCL | O_NOFOLLOW` semantics, same directory-handle anchoring.
  The old form has no TOCTOU symlink race the new form fixes.
- The change's purpose is visible in the package itself: the
  `acceptance_campaign._child_guard` audit hook notes "The open event omits
  dir_fd, so relative write opens are ambiguous and rejected" — the absolute
  path makes the *package's own child guard* able to verify containment. The
  new campaign test `test_ordinary_attachment_capture_uses_owned_anchored_write_open`
  pins the path form (`startswith('/proc/self/fd/')`), i.e. it tests the
  convention, not a contract.
- No live-tree contract failure was reproduced or observed at the live
  `capture.py` open site (line 425); the 2026-09-19 silent-defect sweep did
  not flag these call sites. Per the standing standard, porting by analogy
  without a reproduced live defect is not permitted.
- The other two sites (`commission.py`, `intake.py`) have no live counterpart
  modules at all.

**Decision: nothing is ported.** No backups were needed (live tree unchanged),
no focused live-tree tests were added, no mutation checks were required
(nothing to revert), and no live-tree SHA-256 changed.

## Conclusion

Keel 0.17.2 is an internally consistent, independently test-verified packaging
of the ordinary-incident-capture and acceptance-campaign feature for the
candidate's own SQLite/canonical-storage qualification story. It adds no
measured value to the live system: the incident recorder, campaign runner,
acceptance workload, `diagnostic_sink` wiring, and builder gates have no
corresponding modules or storage substrate in the live tree (live pipeline
has no SQLite — established at the 0.17.1 intake), and the one
superficially generic hardening idea (fd-anchored opens) fixes no live defect.

**Port decision: zero files ported. Verdict: REJECT the release as a live-tree
change; retain the extraction and this memo as reference.**
