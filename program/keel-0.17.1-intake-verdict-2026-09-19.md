# Keel 0.17.1 Intake Verdict — 2026-09-19

**Request:** Trent: "read all and find value" (evidence-first evaluation of the 0.17.1 delta; integrate only small, tested additions that provide missing value to the live system).

**Verdict: two micro-ports landed; no wholesale adoption; the rest of the candidate's machinery is correctly scoped to its own SQLite host model and does not transfer.**

---

## 1. Provenance and verification (observed 2026-09-19 ~05:36–05:40 PDT)

- Transfer files (`Keel_0.17.1_Transfer.txt` and the `__1` copy): identical, SHA-256 `5340bc6a…6069c32`.
- Handoff: SHA-256 `5972b052…0d4b9df1a3`. Test evidence: `e5720e9b…ac80e09e2`. Release evidence: `3ed03851…cc705bf05a`.
- `--verify-only` passed: 811 files, publisher NOT authenticated, `execution_authorized=false`, `production_deployed=false`.
- Extracted ONLY to `~/workspace/keel-transfers/keel-0.17.1`; independent tree verification passed for all 811 files. No live deployment, no source replacement.
- Delta vs 0.17.0 confirmed by `diff -rq`: exactly **12 added, 12 modified** files (plus a stray `.pytest_cache` in the 0.17.0 tree — a test artifact, not a source difference).

## 2. Evidence posture (supplied claims, not independently reproduced)

- Version `0.17.1-integration.1`, status `INTEGRATION_CANDIDATE`, production readiness `BLOCKED`.
- Publisher authenticated: false. Execution authorized: false. Submission authorized: false. Live host qualified: false.
- Original SQLite readonly incident: **UNRESOLVED** (by design — the candidate explicitly refuses to close it on passing tests).
- Supplied full suite: 4,821 total / 4,808 passed / 13 skipped / 0 failed / 0 errors (claim, not re-run here).
- Separate restored focused tests: 270/270 (claim, not additive).

## 3. Independent validation performed

- Re-ran the five new storage test files on this machine (pytest 9.1.1, SQLite 3.45.1): **124 passed, 0 failures, 74.87s**. Synthetic — no production-readiness claim.
- Deep-read review covered all 24 changed/added files with full diffs against 0.17.0 (read-only; nothing executed).

## 4. What 0.17.1 actually fixes

A distinct, demonstrated defect — NOT the original readonly incident:

- **Old behavior:** `CanonicalPublisher.publish()` ran `_save` (sequence-incrementing write), then `db.commit()` inside the `_connection` body. The old `_connection` identity check ran only *after* the body yielded — i.e. after the body's own commit. Worse, the old check compared key-file **bytes**, not key-file **identity**. So a same-bytes inode swap of the key file between `_save` and commit passed the check entirely, and a chmod/swap committed sequence 1 *then* errored.
- **New behavior:** `_commit()` calls `_check_storage()` — parent dir identity + private-mode, DB and key-file identity (`_private()`: 0600, owned, nlink==1) plus key bytes, sidecars (`-journal/-wal/-shm`) private if present — **before** the irreversible `db.commit()`, while the source lock is held. Regression preserves sequence 0.
- **Recovery taxonomy** (`StorageOperation`): `COMMITTED_UNCONFIRMED` (commit returned, later step failed — write landed, durability unconfirmed, must not replay); `UNKNOWN` (error *around* commit — deliberately refuses to infer even if rollback later succeeds); `ROLLED_BACK`; `NO_WRITE_ATTEMPTED`. Neither UNKNOWN nor COMMITTED_UNCONFIRMED authorizes replay. Cleanup failures are recorded separately and can never replace the primary exception.
- **Diagnostics:** bounded (16 KiB), value-free (category + numeric codes only, paths SHA-256 hashed, no messages/SQL/keys/payloads/paths).

Honest engineering: the handoff states passing fault tests do not resolve the original incident, and `build_integration_release.py` hard-codes `PRODUCTION_READINESS='BLOCKED'`.

## 5. Ported to the live pipeline (2026-09-19)

The live pipeline uses atomic JSON writes (`queue_io.atomic_write_json`: tmp + fsync + `os.replace` + dir fsync), not SQLite — so the storage package itself does not transfer. Two micro-ideas ported, both with regression tests, both in `engines/application-executor/queue_io.py`:

1. **Pre-replace identity re-verification** (`StorageChangedError`, `_file_identity`, `_verify_write_identity`): snapshot `(st_dev, st_ino)` of the parent dir and existing target before the write; re-verify immediately before `os.replace()`. Target replaced/deleted/appeared or parent swapped → named error, no replace, temp unlinked, live file untouched. Direct analog of the candidate's `_check_storage` at its commit boundary.
2. **Cleanup-failure discipline**: the `finally`-block temp unlink is now best-effort (`try/except OSError`) — a cleanup failure can never mask the primary write error (the candidate's `failure()` rule: primary recorded once, cleanup errors separate).

**Tests:** 5 new tests in `engines/application-executor/tests/test_queue_io.py` (swapped target → `target_replaced`; appeared target → `target_appeared`; swapped parent → `parent_replaced`; happy-path transparency; unlink-failure-does-not-mask-primary). All green: 30 passed + 6 subtests. Mutation check: with the port reverted, 4 of 5 new tests fail (happy-path correctly passes); port restored, full suite green. Full `tests/` suite run alongside to confirm no consumer regressions.

## 6. Explicitly rejected (and why)

- **Storage-qualification package** (`keel_integration/storage.py`, `check_storage_host.py`, `StorageQualificationProvider`, 1-hour pinned regrading, `check_paths`): qualifies a *self-host Keel SQLite deployment* — disposable child probes, child-process lock-contention rehearsal. The pipeline has no SQLite stores and no "receiving host" concept. Zero applicability.
- **Six-fault campaign** (`storage_faults.py`, `reproduce_storage_failure.py`): rehearses filesystem/SQLite behavior in synthetic homes — would test the OS, not pipeline logic.
- **`StorageOperation` contextvar trace / `diagnostic_sink` plumbing**: coupled to transactional connection lifecycles the pipeline doesn't have (single-shot atomic writes, not sessions with phases); C-11 (no-double-logging) argues against a parallel sink.
- **Controller/qualification storage wiring**: admission-gate machinery for a native-host trust model that doesn't exist here.
- **Commit-outcome vocabulary as code**: the pipeline already encodes the analogous rule (ledger-checker field convention; `outcome_tracker` aging stale SUBMITTED to EXPIRED_UNKNOWN; Oura/BambooHR unreconciled claims kept out of published totals). A classifier would duplicate existing machinery.
- **`build_integration_release.py` / manifest / test-guard changes**: release packaging for the candidate's own handoff format; irrelevant live.

## 7. Reading scope (honest accounting)

- Personally read in full: both new docs (`INTEGRATION_0171.md`, `STORAGE_0171.md`), `canonical.py` diff, `storage_diagnostics.py`, `storage.py`, `storage_faults.py`, `controller.py`/`qualification.py` diffs, `__main__.py` diff, builder/guard/manifest diffs.
- Covered by the delegated deep-read (read-only, full diffs): `INTEGRATION_HOST.md`, `INTEGRATION_PORTS.md`, all five storage test modules, `check_storage_host.py`, `reproduce_storage_failure.py`, `test_integration_release.py`.
- Not re-run: the supplied 4,821-test suite (claim only). Not read line-by-line by a human: the ~787 unchanged files (hash-verified against the manifest instead).

## 8. Standing notes

- Candidate remains `INTEGRATION_CANDIDATE`, production readiness `BLOCKED`, original incident `UNRESOLVED`. Nothing about this intake changes the self-host track's need for Trent's real host and G1 privacy-counsel sign-off.
- Portable residue fully captured: the precommit-identity pattern now lives in `queue_io.py`; the value-free bounded diagnostic pattern is a design reference for externally-surfaced errors (no parallel logging path created, per C-11).
