# Keel 0.4 Spec — P5: Tested backup/restore

**Status:** draft for review
**Date:** 2026-09-17
**Grounding rule:** every claim about current behavior was verified by reading
the code cited. Where the code contradicts the handoff brief, the code wins
(discrepancies noted inline).

---

## 1. Problem

The tree's backup convention is **write-only**: restores have never been
tested, and there is no restore tooling at all.

Verified inventory:

| What | Backup mechanism | Restore mechanism |
|---|---|---|
| Queue files | `queue/_backup-<date>-<reason>/` dirs with `.bak` copies — **1,172 dirs** and counting, no retention, no pruning | none |
| Ledger | `_backup_ledger()` per live run (`apply_loop.py:4410`, `shutil.copy2`) | none |
| Engine files | `<name>_backup-YYYYMMDD-<slug>.py` next to the live file | manual `cp` |
| Intent store | none (relies on atomic writes) | none |

Two durability gaps found while reading the write paths:

1. **The ledger write is not crash-durable.** `apply_loop._ledger_submit()`
   (apply_loop.py:4408) does `json.dump(rows, open(LEDGER, "w"), indent=2)`
   — a plain truncating write. A crash mid-dump tears the 254-row ledger.
   The K38 port hardened `queue_io.atomic_write_json` (mkstemp, 0600,
   file-fsync before `os.replace`, dir-fsync after, temp cleanup —
   queue_io.py:443-477) but the ledger path was never migrated. **This is
   the single most valuable line-item in this spec.**
2. **No disk-full handling.** `atomic_write_json` lets `OSError` (ENOSPC)
   propagate. The tmp+replace structure means the *live file is never
   torn* (the temp is unlinked in `finally`), but the caller gets a raw
   traceback, no telemetry event names the cause, and nothing checks headroom
   before a 5.7 MiB queue write. Disk is healthy today (12% used, 88G free
   on /home/hatch) — this is about failing *clearly* when that changes.

**Discrepancy vs the handoff brief:** the brief implied K38 made "writes"
(durability half) crash-safe across the board. It did not — it covered
`queue_io.atomic_write_json` only. The ledger, the telemetry append path,
and the intent store each have their own write code with their own
properties. The spec scopes each explicitly (§3.4).

## 2. Goals

1. A **tested restore procedure**: corrupt queue+ledger in a sandbox,
   restore from a backup point, verify integrity — green before this spec
   ships.
2. **Retention with a ceiling**: backups stop accumulating unboundedly
   (1,172 dirs today); every retained backup is restorable by the script.
3. **Disk-full fails closed and loud**: a named error, a telemetry event,
   the live file untouched — never a half-written queue or a silent skip.
4. **Post-restore reconciliation**: leases, markers, staged launches, and
   the intent store are re-validated after every restore, reusing the P1
   startup reconciliation (§3.3 of keel-04-p1-attempt-identity.md).

## 3. Design

### 3.1 `restore_point.py` (new, in `engines/application-executor/`)

CLI, dry-run default, `--live` writes:

```
restore_point.py --list
    # enumerate restorable points: queue/_backup-*/ dirs + ledger backups,
    # each with ts, reason, and a completeness manifest (which of
    # standard/needs_input/strategic/ledger/intent-store it contains)

restore_point.py --restore <point> [--live]
    # 1. pre-restore snapshot: copy CURRENT queue+ledger+intent-store to
    #    queue/_backup-<ts>-pre-restore/ (so a bad restore is itself
    #    restorable — restores are never destructive)
    # 2. restore in dependency order:
    #    a. queues (standard, needs_input, strategic) via atomic_write_json
    #    b. ledger (application-ledger.json) via atomic_write_json
    #    c. submit-intents.json (only if the point contains it; else keep
    #       current — the intent store is append-favored and a stale
    #       restore could resurrect a resolved UNKNOWN bar)
    #    d. launch locks: do NOT restore (leases are 2h TTL by design;
    #       restoring stale leases re-arms phantom launches)
    # 3. post-restore validation (§3.2); any failure → automatic rollback
    #    to the pre-restore snapshot, loud error, no partial state
    # 4. post-restore reconciliation (§3.5)
```

Completeness manifest: backup dirs created after this spec ship include a
`manifest.json` (ts, reason, files, sha256 per file). Older dirs without
manifests are listed as `legacy-unverified` — restorable file-by-file with
an explicit `--allow-legacy` flag, never by default.

### 3.2 Post-restore validation (all must pass, else rollback)

- **Counts sanity:** restored queue lengths within expected bounds vs the
  pre-restore snapshot (a restore that *loses* 500 leads is a failure, not
  a restore — threshold: no more than 5% fewer entries unless `--force`
  with a reason).
- **One-lead-one-queue:** no `role_id` appears in more than one queue file
  (the standing invariant; `backfill.py`/`analyze.py` assume it).
- **Ledger monotonicity:** SUBMITTED count never decreases across a
  restore (C-14: submissions are append-only facts).
- **Telemetry hash chain:** K25 chain verifies over `telemetry/events.jsonl`
  (telemetry is append-only and is *never* restored — restores don't
  rewrite history; the chain check just confirms we didn't touch it).
- **JSON strictness:** restored files parse under the K50 `strict_loads`
  rules (no duplicate keys, no non-finite numbers).

### 3.3 Retention policy

- Queue backup dirs: keep the **last 30 days**, plus one per week for 90
  days, then prune. Pruning is a `--prune` subcommand, dry-run default,
  and never prunes the most recent 7 days or any dir tagged `keep` in its
  manifest reason.
- Per-file engine backups (`*_backup-*.py`): keep the **3 most recent**
  per source file; older ones move to `hidden_files/backup-archive/`
  (still restorable by hand, out of the import path).
- Ledger backups: same 30-day rule.
- A weekly cron reports backup disk usage; over 2 GiB total → alert in the
  digest (not silent growth to 1,172+ dirs again).

### 3.4 Write-path hardening (the two verified gaps)

1. **Ledger → atomic.** Migrate `apply_loop._ledger_submit` (line 4408)
   from `json.dump(rows, open(LEDGER, "w"))` to
   `queue_io.atomic_write_json(LEDGER, rows)` under `queue_io.queue_lock()`.
   Audit the other ledger writers found (`backfill_fit_scores.py`,
   `backfill_ledger_source.py`, `cost_backfill.py`, `cost_tracker.py`,
   `build_personal_registry.py`) — migrate or document why each is
   out of scope. Regression test: torn-write simulation leaves the prior
   ledger intact.
2. **Disk-full fails closed and loud.** In `atomic_write_json`, catch
   `OSError` with `errno.ENOSPC` (and EDQUOT) around the write/fsync/
   replace sequence and raise a named `DiskFullError` (new, in queue_io)
   instead of a bare traceback. The live file is already safe (tmp never
   replaced on failure — state this as a tested invariant). Callers that
   must not silently skip work (`mark_inflight`, `_ledger_submit`,
   `record_outcome` paths) log a `disk_full` telemetry event via
   `log_event` with the target path and bytes attempted, then propagate.
   Add a preflight: before writes over 1 MiB, `shutil.disk_usage`
   headroom check; under 256 MiB free → refuse early with the same named
   error (cheap, avoids burning a 5.7 MiB serialization into ENOSPC).

### 3.5 Scheduler reconciliation after restore

After a successful restore, before the lane resumes:

1. Re-validate launch leases: `launch_lock.check()` each; drop expired
   (existing TTL semantics — never restore-resurrect).
2. Re-run `parked_task_sweep --repair-inflight` in dry-run; report, don't
   auto-apply on first post-restore run (operator confirms — the queue
   contents just changed under it).
3. Prune `staged-launches.json` entries whose roles left READY
   (existing staging-watch logic).
4. Run the P1 startup reconciliation (§3.3 of
   keel-04-p1-attempt-identity.md) — the restore lands the stores in a
   *known-past* state, and reconciliation is what re-derives *current*
   truth from it.

## 4. Test plan

| Test | What it proves |
|---|---|
| `test_ledger_atomic_write` | torn-write simulation during `_ledger_submit` leaves the prior ledger byte-identical |
| `test_restore_drill` | sandbox: corrupt standard-queue + ledger → `--restore` → all §3.2 validations pass; counts match the backup point |
| `test_restore_rollback` | restore with a tampered backup (fails hash) → automatic rollback to pre-restore snapshot; live files untouched |
| `test_restore_never_decreases_submitted` | backup with fewer SUBMITTED rows → restore refused (C-14 monotonicity) |
| `test_one_lead_one_queue_after_restore` | duplicate role_id across restored queues → validation fails |
| `test_disk_full_named_error` | ENOSPC injected at fsync → `DiskFullError`, live file intact, `disk_full` telemetry event emitted |
| `test_legacy_backup_requires_flag` | manifest-less dir → listed but not restored without `--allow-legacy` |
| `test_retention_prune` | 40 days of fake backup dirs → `--prune` keeps 30d + weeklies, never the newest 7d |

All tests run against temp-dir fixtures via the existing `set_store_dir`-style
hooks — never the production queues/ledger.

## 5. Acceptance criteria

- [ ] `restore_point.py --list` enumerates every `queue/_backup-*/` dir
      with a completeness manifest (legacy dirs flagged).
- [ ] The restore drill passes end-to-end on fixtures, including the
      automatic rollback path.
- [ ] `_ledger_submit` and the audited ledger writers go through
      `atomic_write_json`; no plain truncating ledger writes remain.
- [ ] `DiskFullError` is raised (not a bare OSError) with a `disk_full`
      telemetry event; the torn-write invariant is tested.
- [ ] Retention cron is live; backup growth is bounded and reported.
- [ ] Post-restore reconciliation checklist runs green after a drill
      restore.

## 6. Non-goals

- **No new storage engine.** This spec hardens and tests the existing
  JSON-file stores; it does not migrate to SQLite or anything else.
- Telemetry `events.jsonl` is never restored (append-only history).
- Launch leases are never restored (TTL semantics).
- No cloud/off-site backup — local generations only (out of scope until
  the funding lane clears a storage budget).

## 7. Risks

- **Restore as a new destructive primitive.** A wrong `--restore` could
  discard a day's verified work. Mitigations, all in the design:
  dry-run default, mandatory pre-restore snapshot, automatic rollback on
  validation failure, SUBMITTED-monotonicity refusal, legacy flag.
- **Retention pruning deletes the only good copy.** Mitigation: never
  prune the newest 7 days; prune is dry-run default with a digest report;
  `keep`-tagged manifests are exempt.
- **Validation thresholds as new magic numbers.** The 5% count-loss
  threshold and 2 GiB alert are starting points — the spec requires them
  to be constants with comments, tuned from the first month of drill
  data, not tuned silently.
- **Intent-store non-restore.** Keeping the *current* intent store across
  a queue/ledger restore can create intent↔ledger skew (intent says
  SUBMITTED, restored ledger doesn't). This is exactly what the P1
  `ledger_intent_divergence` check exists to surface — the two specs
  compose; neither silently resolves the skew.
