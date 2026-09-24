# Muse measurement, constrained repairs and runtime backup

These modules add executable offline mechanisms. The fixture interpreter, supplied host observations, SQLite state and live browser execution are different evidence sources. None of these modules submits applications, authenticates a human reviewer or certifies competitive superiority.

## Complete measured preparation runs

`keel_muse.evaluation.freeze_plan(dataset, systems, source_sha256=..., trials=...)` binds the complete dataset, ordered case IDs, fixture hashes, expected outcomes, split, synthetic flag, declared system configuration digests, source digest and repeated trial count. Keep its digest separately before running. The caller must compute the source digest from the actual reviewed code; the Python argument is a trusted declaration.

Datasets use schema `keel.muse.e2e-dataset.v1`, a dataset ID, `synthetic`, `split` (`development` or `held_out`) and cases containing `case_id`, a full existing Loki form `fixture`, and `expected_outcome` (`PREPARED` or `BLOCKED`). Each system has `system_id`, `adapter` (`fixture` or `external`) and a complete host configuration's digest. Up to 64 cases, 8 systems, 20 trials and 2,048 total rows are accepted. Labels and split independence require a trustworthy external evaluation process.

`run(plan, dataset, expected_plan_sha256=..., source_sha256=..., transports=...)` attempts every scheduled system/trial/case. Existing form planning checks enforce the exact values, revisions and supplied approvals before a transport may run. The built-in adapter actually executes typed preparation into in-memory field state, decodes uploaded fixture bytes, hashes them and returns every field's readback. It is labelled `FIXTURE`; it is not Muse or browser execution.

An external callback receives `(system_config, fixture, prepared_plan)` and must return exactly `{"readback": ..., "trace": [...]}`. It is labelled `INJECTED`; its observations are not independently authenticated. An unavailable external callback produces retained `ERROR` rows, not success or an omitted denominator. Callback code is trusted host code; this module neither sandboxes it nor attests its model calls, side effects or runtime limits. Direct model calls from this module are zero. The built-in fixture adapter refuses nonsynthetic data.

Only deterministic comparison with the approved complete readback produces `PREPARED`; a callback saying `PASS` is invalid. Reports retain observations and their digests for independent regrading. They can contain applicant values if the operator supplies real records, so keep reports private. `compare(report, expected_plan_sha256=..., dataset=..., source_sha256=...)` revalidates the manifest and observations, regrades readback and recomputes all metrics. Repeated trials are grouped by case for consistency counts; they are not claimed as independent samples. Failed preparation, unavailable transports, readback mismatch, transport error and all rows after a 429 stop remain counted.

## Counterexample-driven repair proposals

`keel_muse.repair` implements a bounded procedural repair loop with three known actions:

| Observed fixture fault | Candidate action | Actual state changed in replay |
|---|---|---|
| Missing locator | Rebind a known locator | A locator map is rebuilt from the bound field inventory |
| Wrong uploaded bytes | Recheck attachment bytes | The upload buffer reloads the bound fixture bytes |
| Stale form observation | Refresh the bound snapshot | The driver's captured contract digest is refreshed |

`freeze_fixtures(cases)` freezes at least two distinct local synthetic cases. `propose(failure_trace, frozen_fixtures_sha256=..., source_sha256=...)` requires a recognized failure, exact field and form digest, and nonempty counterexample evidence. The trace is supplied observation data, not authenticated evidence. The candidate is versioned and `QUARANTINED`.

`evaluate(candidate, frozen, expected_candidate_sha256=..., expected_fixtures_sha256=..., source_sha256=...)` corrupts the same actual driver state for baseline and candidate, executes the allowlisted repair action, then runs the same preparation interpreter and readback grader. It records each repair action and before/after driver hashes. Wrong attachment bytes are measured from the upload buffer. An unknown required control remains blocked; a repair cannot create its answer or approval. No repair changes facts, approved values, source revisions, policy, approval state or expected benchmark labels.

`recommend(report, expected_report_sha256=..., candidate=..., frozen=..., source_sha256=...)` reruns the fixture replay instead of accepting supplied success claims. It recommends review for integration only when at least one outcome improves, none worsens and no false-ready result appears. This is a recommendation only: no code is installed, no candidate is promoted and no live browser is repaired automatically. Frozen local fixtures alone do not prove held-out independence or real-site effectiveness.

## Explicit runtime-store backups

`keel_muse.backup` currently supports these exact profiles:

| Profile | Included files |
|---|---|
| `recovery` | `recovery.sqlite3`, `recovery.sqlite3.key` |
| `coordinator` | `coordinator.sqlite3`, `coordinator.sqlite3.key` |

This is a partial runtime backup, not a backup of all Keel files, browser sessions, attachments, intent stores or credentials. Arbitrary directories and secret trees are never enumerated. The included 32-byte host keys are necessary to validate the corresponding state and are sensitive. Backup directories use private permissions.

1. Obtain `checkpoint(runtime_home, workspace_id, profile=...)` from the trusted current host. Retain its complete value and the final manifest digest independently from the backup.
2. Call `snapshot(runtime_home, backup_home, workspace_id, expected_checkpoint=..., profile=...)` with a new backup directory. The function checks the read-only HMAC/event checkpoint, uses SQLite's online backup API with WAL support, checks the current checkpoint again and verifies the copied state. SQLite copies into memory; descriptor-safe exclusive writes publish only allowed files. Copies are bounded to 8 MiB and 30 seconds; a busy source is rejected rather than retried indefinitely.
3. Call `restore(backup_home, new_destination, authoritative_checkpoint=..., expected_manifest_sha256=...)`. The authoritative checkpoint must represent the current trusted head or an independently retained latest checkpoint. It must not merely be read out of the backup being restored. If no authoritative current checkpoint exists, restore remains blocked.

Restore verifies the manifest pin, exact file allowlist, file hashes and authenticated store state. The backup checkpoint must equal the authoritative current checkpoint exactly: an older known head cannot roll back an UNKNOWN attempt, persistent 429 restriction or revoked approval. A restore can never overwrite an existing destination. It validates through read-only APIs, preserving the controller generation and state; no controller starts and no attempt is retried. A later explicit coordinator restart applies the existing fencing and UNKNOWN rules.

The checkpoint's external authority is a host trust boundary, not independently authenticated by this library. A filesystem/key owner can replace a whole history or supply an obsolete checkpoint; no local backup can prove that newer state does not exist elsewhere. Inspect failed partial output directories as incomplete artifacts; only a completed manifest or restore receipt identifies a verified operation.

## Demonstrations and checks

```python
from keel_muse import evaluation, repair, backup
measurements = evaluation.demo()       # Fixture execution plus unavailable host rows
candidate = repair.demo()              # Actual local driver repair; no promotion
copied = backup.demo('/private/new-demo-directory')  # SQLite backup/restore; 429 retained
```

The three focused test modules cover label/source/plan tampering, readback regrading, malformed callback claims, repeated inconsistency, changed upload bytes, global 429 stopping, forbidden repair actions, same-state repair replay, missing checkpoints, old backups against newer heads, UNKNOWN/revocation preservation, coordinator inbox preservation, symlink/hardlink rejection and refusal to overwrite existing state.
