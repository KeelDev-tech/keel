# Keel 0.4 Spec — P1: Application identity + recoverable state

**Status:** draft for review
**Date:** 2026-09-17
**Grounding rule:** every claim about current behavior was verified by reading
the code cited. Where the code contradicts the handoff brief, the code wins
(discrepancies noted inline).

---

## 1. Problem

A single application attempt is currently recorded in **four separate places**,
written by different writers at different times, with no atomicity between them:

| Record | Writer | Store | Atomicity today |
|---|---|---|---|
| Queue IN-FLIGHT marker | `inflight_marker.mark_inflight()` | `queue/standard-queue.json` (K38 `atomic_write_json`) | lock+marker together; nothing else |
| Launch lease | `launch_lock.acquire()` | `hidden_files/launch-locks/<sha256(role_id)>.json` (K19/K20) | atomic create (O_EXCL/link); corrupt fails closed |
| Submit intent | `submit_intent.record_intent()` / `browser_intent_verdict()` | `hidden_files/submit-intents.json` (fcntl + tmp+replace) | per-store atomic; cross-store none |
| Ledger SUBMITTED row | coordinator via `apply_loop._ledger_submit()` | `ledger/application-ledger.json` | **plain `json.dump(rows, open(LEDGER,"w"))` — NOT K38 atomic** (apply_loop.py:4408) |

Known incidents this fragmentation has caused:

- **Phantom IN-FLIGHT (2026-09-17):** OpenTable sat IN-FLIGHT ~2.5h with no
  live browser task and no held lock — the queue claimed an application that
  did not exist. Fixed reactively by `parked_task_sweep.py --repair-inflight`
  with fail-closed guards (stale snapshot / live task / completed task /
  birth-proximity). The guards are correct; the *detection* is still a
  separate sweep, not a startup invariant.
- **Encord duplicate-application conflict (2026-09-15):** apply_loop built a
  packet + IN-FLIGHT marker for a lead the browser lane already owned, because
  the queue marker path could not see the lane's ownership.
- **inflight_marker queue_notes list-crash (2026-09-17):** a crash *mid-claim*
  left an open UNKNOWN submit intent and a refused re-claim — recovery
  required manual `browser_intent_verdict(attempt_id, "failed")`.
- **Ledger undercount (pulse 347):** two confirmed submissions missing from
  the ledger because the F18 evidence gate fail-closed on missing quoted
  confirmation text — the intent store knew, the ledger didn't. No
  cross-check reconciles them.

**Discrepancy vs the handoff brief:** the brief asked to "define exactly how
attempt_id is derived — e.g. role_id + packet content hash + launch
timestamp". The live derivation (`submit_intent.py:107-112`, `_attempt_id`)
is `si-{sanitized role_id}-{UTC stamp}-{3 random bytes}` — **no packet
content hash**. The random suffix makes each attempt unique but
content-unbound: two attempts on the same packet are indistinguishable
except by timestamp. §3 strengthens this.

**Second discrepancy:** the brief implied `inflight_marker.py` already
participates in intent reconciliation. It does not — it imports
`launch_lock` and `queue_io` only (lines 53-55). The open-intent bar
(`apply_loop._open_intent_blockers`, line 2293) lives in the claim path,
not the marker path. Wiring the marker path to the intent store is the
core change of §3.2.

---

## 2. Goals

1. Every application attempt has **one stable identity** from intent to
   ledger, content-bound so replays are detectable.
2. The four records commit as **one logical unit** with a defined order;
   any crash leaves a state the startup reconciliation can classify and
   repair without human judgment.
3. **Two attempts on the same role fail closed**: the second never starts
   while the first is open, on any path (claim, marker, spawn, api-direct).
4. **Restart never duplicates an external submission**: reconciliation
   replays are read-only against providers; terminal transitions require
   provider-correlated evidence (existing F22 contract).

## 3. Design

### 3.1 Attempt identity

**Change `submit_intent._attempt_id`** to bind content:

```
attempt_id = "si-" + sha256(role_id + "|" + packet_digest + "|" + launch_ts_iso).hexdigest()[:16] + "-" + UTC-stamp
```

- `packet_digest`: sha256 of the canonical packet JSON at claim time
  (the packet already carries a content hash at build — `claim_packet()`
  stamps `attempt_id` into the packet at `apply_loop.py:2322-2339`; the
  digest is computed from the pre-stamp bytes).
- `launch_ts_iso`: the claim timestamp.
- The 16-hex prefix is deterministic for (role, packet, launch-second);
  the UTC stamp keeps human sortability. Collision between two attempts on
  the same packet in the same second is impossible in practice (single
  claim path serializes per role via `queue_io.queue_lock`).

**Propagation (additive, no renames):**

- Queue marker: `e["attempt_id"]` on IN-FLIGHT transitions
  (`inflight_marker.mark_inflight`, both phases).
- Ledger: `attempt_id` field on every new SUBMITTED row
  (`_ledger_submit`; backfill script for historical rows where the intent
  can be joined on role_id + date — best-effort, never guessed).
- Telemetry: `details.attempt_id` on launch/submit/verdict events
  (K25 event_id stays the replay key; attempt_id is the join key).

The existing `submit-intents.json` remains the authority for attempt
lifecycle; the other three stores carry the id as a foreign key.

### 3.2 The commit unit and its order

Define the **launch commit** as these steps, in order, with the failure
action for each:

1. `submit_intent.record_intent()` → INTENT. *Crash here:* INTENT with no
   verdict; startup treats as UNKNOWN (existing contract, fail closed).
2. `launch_lock.acquire()` → lease. *Crash here:* stale lease expires in
   2h (`LOCK_TTL_H`) or is taken over; INTENT still open so no new attempt
   starts.
3. `mark_inflight(phase="claim")` → queue marker **with attempt_id**,
   **after consulting `submit_intent.open_unknown_for(role_id)`**:
   if an open intent exists for a *different* attempt_id → refuse
   (`_refuse("open intent <id> bars new attempt")`). This closes the
   marker-path gap (§1, second discrepancy).
4. Browser task spawns (main chat) → `mark_inflight(phase="spawn")`
   transfers lease, stamps `browser_task_id`.
5. Verdict: `browser_intent_verdict(attempt_id, …)` transitions the intent
   (SUBMITTED requires verbatim confirmation text — existing F22/F18
   contract, unchanged).
6. **Ledger + release as one reconciled step:** the coordinator writes the
   SUBMITTED row **via K38 `atomic_write_json`** (fix the plain-dump gap at
   apply_loop.py:4408) and releases the lease. If the ledger write fails,
   the lease is NOT released and the intent stays SUBMITTED-without-ledger —
   a named inconsistent state the startup reconciliation repairs (see §3.3),
   never silently dropped.

Steps 1–3 already exist as separate calls; the spec adds the ordering
contract, the intent check in step 3, and the step-6 repair rule. No new
storage, no SQLite (non-goal).

### 3.3 Startup reconciliation (runs before any new launch)

A new `reconcile_attempts.py --startup` (dry-run default, `--live` writes),
executed by the lane before apply_loop claims anything:

1. **Intent resume:** `submit_intent.resume_open()` — list INTENT/UNKNOWN.
2. **Marker cross-check:** every IN-FLIGHT queue marker must have a live
   lease OR a live browser task OR an open intent; otherwise it is a
   phantom → `repair_inflight` rules apply (existing guards reused verbatim).
3. **Ledger cross-check:** every SUBMITTED intent must have a ledger row
   with the same attempt_id; every ledger SUBMITTED row should have a
   terminal intent. Mismatches are reported as `ledger_intent_divergence`
   with the exact repair (backfill via sanctioned path, or intent
   reconciliation) — never auto-resolved by guessing.
4. **Lease cross-check:** every fresh lease must have a marker or an open
   intent; orphaned leases older than `LOCK_TTL_H` are released (existing
   takeover semantics).

Reconciliation is **read-only against providers**: it never POSTs, never
spawns. `reconcile_unknown` keeps its injected-probe contract. C-14
canonical counting is untouched — a submission counts only when the
canonical submitted count increases.

### 3.4 Conflict detection

- Same role, second attempt while first open → refused at **all four**
  gates: `claim_packet` (existing `_open_intent_blockers`),
  `mark_inflight` (new §3.2 step 3), `launch_lock.prelaunch_guard`
  (existing HELD/ALREADY_SUBMITTED/TWIN_SUBMITTED), api-direct
  (must call `open_unknown_for` before `record_intent` — verify and add
  if missing).
- Same packet, second launch → the content-bound attempt_id makes the
  replay visible in the intent history; the terminal-state immutability
  (`_TRANSITIONS`) bars re-execution.

---

## 4. Test plan

| Test | What it proves |
|---|---|
| `test_attempt_id_content_bound` | same role+packet+second → same id prefix; different packet → different id |
| `test_kill9_mid_commit` | kill -9 between steps 2 and 3 (lease, no marker): startup reconciliation classifies correctly, no phantom marker, no duplicate attempt |
| `test_kill9_mid_commit_2` | kill -9 between marker write and verdict: marker + open intent → reconciliation holds the lead (no auto-revert, no relaunch) until verdict |
| `test_double_attempt_same_role` | open INTENT exists → `mark_inflight(phase="claim")` refuses with the open intent id; second `record_intent` path also refuses |
| `test_marker_no_live_task` | the OpenTable case: stale marker, no lock, no task, no open intent → repaired to READY (existing `repair_inflight` behavior, now invoked from startup) |
| `test_ledger_intent_divergence` | SUBMITTED intent without ledger row → reported, not auto-created; ledger row without intent → reported |
| `test_ledger_atomic_write` | `_ledger_submit` uses `atomic_write_json`; torn-write simulation leaves the previous ledger intact |
| `test_replay_no_duplicate_post` | reconciliation run twice in a row performs zero provider calls (probe-call counter) |

All tests use `set_store_dir` / temp dirs (the existing test hooks) —
never the production stores.

## 5. Acceptance criteria

- [ ] `attempt_id` is content-bound and present on marker, intent, ledger
      row, and telemetry for every new attempt.
- [ ] `mark_inflight` refuses when an open intent for another attempt
      exists (test: `test_double_attempt_same_role` green).
- [ ] Kill -9 at each commit boundary → startup reconciliation classifies
      the state with no human judgment and never issues a provider call.
- [ ] Ledger writes go through `atomic_write_json` (no plain dumps remain
      on the submit path).
- [ ] The four existing incident classes (phantom marker, duplicate fire,
      mid-claim crash, ledger undercount) each have a regression test.
- [ ] C-14 counting, F18 evidence gate, and F22 UNKNOWN-barring are
      unchanged (their test suites green).

## 6. Non-goals

- **No SQLite migration.** K38's tmp+fsync+rename already gives crash
  durability; the remaining gap is commit *coordination*, not the storage
  engine. A migration would re-litigate every tested invariant for no
  demonstrated failure.
- No change to the F22 state machine vocabulary (INTENT/UNKNOWN/
  SUBMITTED/FAILED) or terminal-state immutability.
- No automatic provider-side reconciliation (no POSTs, no browser) —
  UNKNOWN stays barred until evidence arrives.
- No backfill of attempt_id where the join is ambiguous (never guessed).

## 7. Risks

- **Content-bound ids change the id format.** Anything parsing the old
  `si-<role>-<stamp>-<rand>` shape (dashboards, blackboard entries)
  must tolerate the new shape; the `si-` prefix and trailing UTC stamp
  are preserved to ease this.
- **Startup reconciliation adds lane latency.** It is read-only and
  lock-serialized; measure it — if it exceeds ~5s on the live stores,
  gate it behind a dirty-flag written by the commit path.
- **Step-6 coupling (ledger+release).** Holding the lease until the ledger
  write lands lengthens lease lifetime on ledger failure; the 2h TTL and
  the named inconsistent state bound the blast radius.
- **`open_unknown_for` failure modes.** It returns `si-UNREADABLE-STORE`
  on store errors (submit_intent.py:304-325) — `mark_inflight` must treat
  that as *refuse* (fail closed), not as *no open intent*.
