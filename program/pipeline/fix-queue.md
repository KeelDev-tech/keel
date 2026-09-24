# Pipeline Fix Queue — Keel Pipeline Performance

Drafted 2026-09-17 ~12:15 PDT. Status: DRAFT, for Program Office review.
Scope: READY refill crisis, api-direct lock layer, no-AI/unaided-work attestation wave.

Gating rules: TESTED VALUE ONLY (propose → test → ship; never ship on a pulse's say-so)
and PUSH ON VALUE. Engine changes touching live queue behavior ship only with
Keel Build coordination (per PROGRAM.md). Browser lane stays main-chat only —
nothing here spawns, steers, or manages browser tasks.

Evidence base: live queue files (`job-pipeline/queue/*.json`), telemetry
(`job-pipeline/telemetry/events.jsonl`, 14,910 events), lock files
(`job-pipeline/hidden_files/launch-locks/`), engine source
(`job-pipeline/engines/application-executor/`), board stats
(`hidden_files/api-direct-board-stats.jsonl`). Every number below was read
from these sources on 2026-09-17 ~12:00–12:15 PDT unless noted.

## Pulse 321 maximum-overdrive — front of queue (Trent order 2026-09-17 ~12:44 PDT)

Two items moved to the absolute front. Both are now resolved (shipped / rejected)

## P0 answer-mismatch flood — RESOLVED 2026-09-17 14:05 PDT

**Incident:** 153 `answer_mismatch` events (10:36–10:48 UTC) falsely parked verified-live leads. Root cause: pre-fix guard misclassified canonical `personally_completed_certification: NEEDS_INPUT` deferral as `out_of_scope`.

**Repair:** Cleared `never_auto_submit_attestation` markers (stamped on misfire); fixed GENUINE_PAT false-positive in gate_note; canonical `verify_retry --live` rescreen.

**Yield:** 119 promoted to READY | 31 remain parked (genuine Trent blockers) | 3 ambiguous. Boundary intact (personally_completed = DO NOT CERTIFY).

**Lesson:** The never-auto-submit marker permanently bars leads whose attestation abstention is by design. Needs scoping to resolvable keys or a misfire-clearance path.
with measured evidence. They supersede the sequencing below for READY refill.

### PQ-0A: Bank-write → parked-lead sweeper — SHIPPED, READY 3 → 13
- **Claim vs verified:** the pulse brief claimed 10 APPLY-band leads (fit 75–89)
  parked after applicable answers were banked. Verification found **41** APPLY-family
  leads (fit 75–89) with all blockers currently mappable via the canonical
  `prescreen.question_mappable` — but broad-match inspection showed most mappings
  were generic fields (email, location, travel, phone country), not proof a genuine
  blocker was answered. The claimed exact set of 10 could not be reproduced
  (provenance frequently records only a date, so post-park ordering is unreliable).
  The hook was therefore built on **strict matching only** — no bulk sweep of the 41.
- **What shipped:** `engines/application-executor/bank_sweep.py` (new) + hook wiring
  in `tray_answer.main` (after every successful bank write) and
  `field_question_protocol.ensure_bank_key` (after every new-key write). After each
  bank write it sweeps all PARKED* leads in standard + needs_input queues, clears
  blockers the bank now answers via the canonical matcher (scope governance and the
  no-AI/unaided-work boundary inherited, not reimplemented), and revives
  fully-unblocked leads as PARKED-PENDING-VERIFICATION through canonical
  `verify_retry` (targeted `--role-ids`; never promotes directly). A second-pass
  repair moves fully-cleared leads stuck in non-pooled statuses
  (PARKED/PARKED-NEEDS-INPUT in the standard queue) into the verify pool —
  `verify_retry.collect_candidates` only pools PARKED-PENDING-VERIFICATION there,
  and the first deploy caught 5 leads in exactly this stall.
- **Measured effect (2026-09-17 12:47–12:55 PDT):** 126 blockers cleared across 94
  leads; **0 no-AI/unaided-work boundary violations** (audited); READY **3 → 13**
  (order expected ~13). Clearing keys were all standing-authorized answers
  (location 31, travel_commitment 22, email 17, phone_country 16, education 8, …).
- **Tests:** 16 regression tests green (`tests/test_bank_sweep.py`) — strict
  matching, employer-scope enforcement, no-AI boundary, partial clears, classifier
  fail-closed, canonical status transition, stuck-status repair, idempotency,
  dry-run, test isolation. Neighboring suites green (tray_answer_compounding,
  field_question_protocol, bank_scope — 57 passed).
- **Safety:** queue backups before every live write; hook is fail-safe
  (exceptions swallowed, never break the bank write); strategic queue untouched;
  dry-run default. One incident during development: the hook fired live during its
  own test run (test_bank_scope's e2e `tray_answer.main(--live)` hit production
  paths before the isolation fix) — audited clean (all 126 clears legitimate),
  then fixed by honoring caller-supplied paths; verified tests no longer touch
  production. Disclosed to Keel Build in the coordination message.
- **Status:** SHIPPED and live. Every future bank write now sweeps parked leads.

### PQ-0B: READY-starvation cooldown override — REJECTED with measured evidence
- **Proposal:** a bounded override in `verify_retry.py` when READY < 5 and >90% of
  the verification pool is cooldown-locked.
- **Measurement (2026-09-17 ~13:00 PDT):** READY = 13 (clause 1 false — the sweeper
  fixed the starvation); pool = 775, cooldown-locked = 771 (**99.5%**, clause 2
  true). At order time (~12:44) READY was 3, so the condition did hold then — but
  its root cause was parked leads with answerable blockers (fixed by PQ-0A:
  +10 READY from cleared blockers), not cooldown mechanics.
- **Why rejected:**
  1. The 99.5% locked figure reflects a *fresh* pool (hourly cadence just swept it),
     not a stuck one — overriding would re-burn ~770 HTTP verifications on
     just-checked leads with near-zero expected yield.
  2. The pool is a rotating window (~30+/hour unlock naturally as 24h cooldowns
     expire); no starvation mechanism exists that an override would fix.
  3. The legitimate new-information bypass already exists and is shipped: the
     sweeper clears `last_verify_attempt` on bank writes (PQ-0A), which is the
     surgical override — a blanket one is redundant.
  4. Risks are concrete: a 770-lead HTTP burst invites 429s/blocks (hard stop per
     policy) for no expected READY gain; the real READY bottleneck is promotability
     (fit-band, materials), not verification latency.
- **Status:** REJECTED. Recorded here per the order's ship-or-reject instruction.
  Revisit only if READY < 5 recurs *with* evidence that cooldowns (not blockers or
  promotability) are the binding constraint.

### Escalation — READY hit zero (Dev, 2026-09-17 ~12:53 PDT)
- **What happened:** READY hit 0 at ~12:42 PDT — the Stripe launch consumed the
  last one and refill produced zero. The lane did its job (17 launches in 2h);
  supply didn't keep up.
- **Standing rule (effective immediately):** READY must never hit zero. When READY
  drops below 5, every pulse automatically runs an emergency-refill arm BEFORE the
  lane starves — supply protection outranks everything except integrity. Lane
  idling on empty READY is a P0, same as a stall.
- **Emergency refill executed 2026-09-17 ~13:05 PDT:**
  - bank-sweeper `--live`: clean no-op (scanned 225, 0 cleared — all mappable
    blockers already cleared by the 12:47 run).
  - PQ-1 C-17 stale-refusal re-run on 4 role_ids: **+1 READY** (Drata promoted;
    the 3 others pass the widened gate but hit genuine essay blockers at
    pre-promotion screening — correctly parked, not stale).
  - Cooldown-bypass candidates: evaluated, **none legitimate** — locked pool is
    432 structural repost-watch (168h), 297 already-known-live API (72h), 39 fresh
    24h. Bypassing any of these burns HTTP for ~zero READY yield with 429 risk.
    This confirms the PQ-0B rejection inside the emergency-refill context.
  - Result: READY **13 → 14**, supply back above 5.
- **J8187570 outcome (Dev: "evidence or it didn't happen"):** NOT submitted.
  Telemetry shows browser_launched 12:26 PDT, form_started 12:30 PDT, then nothing
  — no submitted/confirmation event; absent from submission ledger. Queue status
  PARKED-NEEDS-INPUT: "browser session crashed before any fill; 4 required
  questions unanswerable". Recorded honestly as a crash-park, not a submission.
- **Arm implementation:** `engines/application-executor/emergency_refill.py` —
  READY<5 gate → bank_sweep --live → targeted verify_retry on revived →
  before/after report. Deliberately excludes the blanket cooldown bypass (PQ-0B);
  the sweeper's stamp-clearing is the legitimate bypass and is automatic.
  Reconciliation with the standing rule: the arm runs as ordered; the "starvation
  override" component is implemented as targeted legitimate-bypass verification,
  not a blanket cooldown burn, per measured evidence.

## Current state (the crisis, verified)

- READY = **5** (standard-queue.json, 2,717 entries; no READY elsewhere). Down from 47.
  Drain mechanism: the 10:00 PT staged-launch burst consumed ~45 READY leads
  (staged 4→53 at 09:07 PT, fired at window end). The pool is draining, not refilling.
- Promotion-gate stack: **214** leads (brief claimed ~205; drift is snapshot timing).
- One IN-FLIGHT marker is 7.7h old
  (`ATS8-GREENHOUSE-STRIPE-ENTERPRISE-ACCOUNT-EXECUTIVE-REVENUE-20260915-J8029128`,
  updated 2026-09-17 04:27 PDT) — possibly phantom. Main chat owns the live-task
  check before any `--repair-inflight` (browser lane is main-chat only).

## P0 — refill the pool, unfreeze the lane

### PQ-1: Re-run the C-17 fit-band gate on the 4 stale refusals — expected +4 READY
- **Evidence:** 4 leads (3 `PRIORITY APPLY`, 1 `APPLY`), fit ≥ 75, refused by the
  C-17 fit-band gate (`verify_retry.py`, ARM 141) *before* RP01 (2026-09-17) widened
  the band predicate from hardcoded `band == "APPLY"` to the canonical APPLY family
  (`verify_retry.py:205-233`). They now pass the canonical predicate; the refusals
  are stale, not substantive.
- **Fix:** run `fit_band_gate` on the 4 role_ids; promote those that pass. Queue op
  only — no engine change.
- **Test plan:** unit — `fit_band_gate` on the 4, assert ok; live — apply with queue
  backup, assert READY; regression — existing `test_verify_retry*` suite green.
- **Expected READY effect:** +4, near-certain, near-zero risk. Do first (minutes).

### PQ-2: Re-band the 28 PARKED-band fit-band refusals — expected up to +28 READY
- **Evidence:** of 32 C-17 refusals, 28 fail on band = `PARKED` despite fit 75–87
  (top: 81×6, 84×4) and verified-LIVE postings. Highest-yield legitimate re-screen
  target in the stack.
- **Fix:** dry-run band audit first (proposed re-bands with cited reasons), spot-check
  5 against live postings, then apply with backup + `queue_notes` and re-run the gate.
- **Test plan:** dry-run audit output reviewed before any write; post-apply assertion
  on promotion count; regression suite green.
- **Expected READY effect:** up to +28; realistic yield depends on how many re-bands
  are evidence-legitimate (bar stays 75 — re-banding corrects stale labels, it does
  not lower the bar; fabrication-gate adjacency, so every re-band needs a cited reason).
- **Caution:** needs a written re-band bar before running, or refill becomes bar-lowering.

### PQ-3: Normalize list-typed queue_notes → strings; fix the writer — de-risks all promotion
- **Evidence:** 1,342 standard-queue + 103 needs_input-queue entries carry list-typed
  `queue_notes`. Root cause is the writer: `prescreen.py:1644-1650` and `:1741-1747`
  normalize notes to a list before appending (since 2026-09-15). The reader then
  misfires: `genuine_pat.py` `_field_text` joins list items with `"; "` onto one line,
  but `CLEARED_HISTORY_PAT` (`genuine_pat.py:81-83`) is `(?m)` line-anchored — a
  cleared-history item ("attestation answered YES and banked") strips the *entire
  joined line*, including sibling items carrying genuine blockers ("essay required…
  Trent must answer"). Demonstrated live: identical notes classify verify-only=True
  as a list, False as a string. **157 standard entries currently fail-OPEN
  misclassify** (152 PARKED-PENDING-VERIFICATION, 4 SUBMITTED, 1 PARKED-LOW-FIT)
  plus 1 in needs_input. Second defect on the same schema: `rescreen.py:259`
  `cur["queue_notes"] += f" Not promotable: {gnote}"` on a list appends *each
  character* as a separate element (char-shredding); `:243-245` writes repr-garbage.
- **Fix:** (a) one-time normalization — newline-join list-typed notes to strings
  (backup first); (b) `prescreen.py` appends strings (revert the list normalization);
  (c) `rescreen.py:259/:243-245` coerce to string before concatenating.
- **Test plan:** type-parity regression test — identical notes as list vs str must
  classify identically in `is_verify_only`; before/after misclassified count 157→0;
  char-shred repro test on `rescreen.py:259`.
- **Expected READY effect:** ~0 direct — but it stops fail-open misclassification
  (wrongful READY promotions of genuinely-blocked leads; protects the <1%
  preventable-rejection target) and un-hides genuine blockers so `verify_retry`
  stops resurrecting them. Run before PQ-2/PQ-4.
- **Coordination:** engine change — Keel Build before shipping.

### PQ-4: Fit-score backfill for the 112 stays_parked — largest single block
- **Evidence:** 112 leads parked with `status_reason` "verify-retry: posting live but
  lead not actionable (missing fit_score and/or posting URL)". Verified live: **all
  112 are missing fit_score; 0 are missing the URL** — the "or job_url" half is empty
  in practice. Verified-LIVE postings with no score.
- **Fix:** run the evidence-scorer/fit pipeline over the 112, backfill fit_score,
  re-run the promotion gate.
- **Test plan:** dry-run scoring on 10, spot-check score rationale; live backfill with
  backup; assert gate pass/fail distribution before promoting.
- **Expected READY effect:** largest single block — up to +112 potential; realistic
  yield = fraction scoring ≥75 AND in an APPLY-family band. Unknown until scored;
  estimate from the dry-run sample before committing the full run.

### PQ-5: api-direct release-on-refuse (+ task_id unification) — unfreezes the lane
- **Evidence:** `api_direct_loop.py` acquires the launch lock via `--guard` as
  `TASK_ID = "api-direct-loop"` (L77) and there is **zero `--release` call anywhere
  in the file**. Every `no_packet` skip, `fastlane_browser` refusal, and standing-grant
  failure leaks a 2h lock; `inflight_marker.py --spawn` then refuses with "launch lock
  not acquired: HELD", freezing the browser lane on exactly the roles it should
  handle. Current: 31 `api-direct-loop` locks (16 Stripe, 11 stale). All-time
  `api-direct-board-stats.jsonl`: 225 rows — 139 refused, 86 skipped, **0 submitted**.
  Deeper: the live POST path (`api_submit.py`) uses `API_DIRECT_TASK_ID =
  "api-direct-submit"` (L392) — a *different owner* — so the loop's own guard lock
  self-blocks the submit acquire (`launch_lock_held`, `api_submit.py:1198-1206`).
  **As written, the loop can never complete a live POST.** Stripe slice: 92 refused
  (91× `fastlane_browser`, 1× `guard: HELD`), 49 skipped (`no_packet`).
  Note: pulse 317's "42 stale Stripe locks" could not be quoted from workspace files
  (pulses post to the Log chat); this item cites lock-file + board-stats evidence instead.
- **Fix:** (a) release the guard lock on every non-submit, non-unknown exit of
  `process_lead` (try/finally or explicit releases; keep the lock on `submitted` and
  `unknown` per F22 — no retry until reconciliation); (b) unify the task_id (move
  `guard()` after `fastlane_screen`, or use one owner id) so the submit path can acquire.
- **Test plan:** extend `tests/test_api_direct_loop.py` — fastlane-refused run →
  lock absent after return; no_packet run → lock absent; mocked live submit →
  `submitted` reached and lock released; full existing suite green.
- **Expected READY effect:** 0 direct (api-direct submits bypass READY) — but unfreezes
  ~31 currently-locked roles for the browser lane and restores the api-direct submit
  path (0 submissions all-time → working). Lane-throughput fix.
- **Coordination:** engine change — Keel Build before shipping. Main chat's immediate
  workaround stands (`~/workspace/AGENTS.md`: move the stale lock to `_released/`,
  re-run marker `--spawn`).

### PQ-6: Restart broad discovery — refill the top of funnel
- **Evidence:** last broad new-lead sweep wave2a 2026-09-15 ~19:51 PDT (**~40h ago**);
  last targeted batch (census3x board-API re-enumeration, 528 role_ids) ~22h ago;
  hourly staged-ingest enumerators are alive (audits through 12:06 PDT today) but
  yield **~0** (0–1 leads/run, all dupes/no-yield); only 3 role_ids dated 20260917
  exist in the standard queue. Supply inflow has effectively stopped.
- **Fix:** wave2a-class broad sweep; separately investigate the enumerator yield
  collapse (source exhaustion vs dedupe bug — the audits say dupes, but 40h of
  dupes-only deserves one skeptical pass).
- **Test plan:** dry-run sweep with yield accounting; dedupe hit-rate vs historical baseline.
- **Expected READY effect:** lagged — discovery → verify → score → READY is hours —
  but without it PQ-1..PQ-4 promote from a fixed stock. The 47→5 drain was
  *consumption*, not just gating; sustained refill needs inflow.

## P1 — attestation wave (surface facts; the boundary stands)

### PQ-7: No-AI/unaided-work attestation wave — scoped facts + process response
- **Evidence (verified 2026-09-17 ~12:15 PDT):** 199 leads carry no-AI/unaided-work
  attestation blocker text across **23 employers** — Stripe 97, Samsara 43,
  Databricks 13, OpenAI 9, Fastly 7, Salesloft 5, MongoDB 4, Coinbase 3, rest small.
  Telemetry `attest`/`ai_attestation` gate events: 09-14: 1, 09-15: 5, 09-16: 51,
  09-17: 1 — the wave **peaked 09-16**; today is quiet because it already parked its
  leads. Sample texts: Perplexity — "no-ai-attestation: two required free-text
  questions demand 'own origin'… needs Trent's explicit answer; no pre-authorize";
  OpenAI — certification "I, the undersigned applicant, have personally completed
  this application" (confirmed on two prior OpenAI Ashby forms 2026-09-14). The
  originating brief's "8 employers / 176 events on 09-17" could not be re-verified
  from workspace files (likely a pulse-time Log-chat snapshot); the verified figures
  above supersede it for this document.
- **Boundary (restated, not revised):** Trent's never-attest rule for
  personally-completed / no-AI attestations stands. Nothing here proposes changing it.
- **What's actionable without touching the boundary:**
  (a) employer-scoped banked answers — the 2026-09-17 tray-answer compounding repair
  already lets prescreen consume Trent's-words banked answers (Samsara no-AI ack,
  essays, heard-about); audit whether Stripe/Samsara/Databricks no-AI questions have
  banked Trent's-words answers that apply, and bank the recurring ones once —
  compounding, one answer unblocks the pattern forever;
  (b) cross-employer early warning — flag when a *new* employer starts asking
  (the wave spreads employer-to-employer; catch each once);
  (c) honest accounting — the 199 parked leads are the wave's cost; the performance
  dashboard's preventable-rejection metric must exclude boundary parks, or it lies.
- **Test plan:** banked-answer consumption already covered
  (`tests/test_tray_answer_compounding.py`, 10 tests); new: early-warning detector
  test — synthetic new-employer no-AI gate event → alert emitted exactly once, deduped.
- **Expected READY effect:** 0 by design — the boundary costs leads and that is
  Trent's call. Value is (a) recovering only what his own words already authorize and
  (b) evidence-grade accounting for the targets.

## P2 — watch items (no action until the seam is touched)

### PQ-8: Matcher divergence — prescreen.question_mappable vs preferences.py exact matching
- **Source:** Keel Build conflict-review watch item (2026-09-17 ~13:10 PDT) on the
  bank-sweeper. Build verified no conflicts (F17/F18/F22 chain untouched,
  cherry-pick regression anchors stand, revival boundary correct) — green.
- **The divergence:** the sweeper clears blockers via prescreen's matcher (looser,
  with legal-suffix stripping in employer matching) while preferences.py now uses
  exact matching with no legal-suffix stripping. If a future bank key relies on
  the looser prescreen match, the sweep could clear a blocker that
  preferences.py would still scope-reject at dispatch.
- **Failure mode:** wasted verification (lead revives, then re-parks at dispatch),
  NOT an integrity breach — nothing promotes without canonical verify + prescreen
  re-check. Watch item, not a conflict.
- **Action:** joint test case when either matcher is next touched. No action now.

### PQ-9: Persist api_direct_detect renderer verdict; route employer-hosted → browser (Build audit P1) — SIGNED OFF
- **Source:** Keel Build greenhouse-renderer-audit (2026-09-17 ~13:00 PDT). 48
  attempt-time `technique_blocked` events today (36 Stripe, 4 Databricks, growing)
  — each after full verify→READY→packet→launch investment. Root cause: only the
  boolean `api_direct_candidate` is persisted at verify time (verify_retry.py:2751,
  :2957); the `renderer` detail from `api_direct_detect.detect()` is discarded,
  so routing can't avoid the doomed api-direct attempt upstream. 0 queue entries
  carry renderer detail (verified).
- **Sign-off (2026-09-17 ~13:15 PDT):** APPROVED with conditions — persist full
  verdict (renderer, reason, detected_at) at both wiring points; employer-hosted
  routes straight to browser lane; re-detect on every verify (no permanent
  pinning); unit + regression tests before shipping; P2/P3/P4 stay sequenced
  (P4 deferred). Fail-safe error direction: false positive costs a browser slot
  (lane proven viable — 3 confirmed Stripe submissions today); false negative
  reproduces today. No integrity exposure.
- **Record correction flagged to Build:** telemetry shows 0 Stripe via api-direct
  (3 via browser) 2026-09-15→17 — the "5 Stripe via api-direct" premise in
  conflict-review.md §1 is unsupported; kill-switch rejection stands on its own.
- **Status:** signed off, awaiting implementation (Pipeline Performance lane, Build
  reviews the ARM 70 seam). Complements PQ-5; no conflict with emergency-refill.
- **Implementation shipped 2026-09-17 ~13:20 PDT:**
  - `api_direct_detect.stamp_entry(entry, url, ats)` — stamps full verdict
    (candidate/renderer/reason/detected_at), fail-closed.
  - `verify_retry.py` both wiring points → `stamp_entry` (re-stamped every
    verify, no pinning).
  - `api_direct_loop.process_lead` — employer-hosted short-circuits before
    `guard()` (no lock, no packet, no fastlane screen); missing verdict falls
    through (fail-safe).
  - `apply_loop.py` apply-time boolean re-detection untouched (test mocks).
  - Tests: 6 new (`test_renderer_verdict.py`) + 157 neighboring = 163 green.
  - Sent to Build for ARM 70 seam review with two questions (fail-closed reason
    wording vs machine-matchable vocabulary; confirm loop short-circuit is the
    only transport change needed).
- **Build review APPROVED 2026-09-17 ~13:25 PDT** with one fix: fail-closed
  reason wording approved as-is (renderer enum is the matchable field; wording
  consistent with detect()'s fail-closed family); apply_loop ~3010 confirmed
  no-change (candidate=True implies renderer="direct"; fresh apply-time boolean
  is the most current signal). Non-blocking observation accepted: verify-time
  renderer vs apply-time boolean can briefly disagree on a recovering board —
  apply fires on the fresh signal, loop short-circuits conservative (browser
  route, fail-safe), self-heals on next re-stamp.
- **Fix applied:** `renderer_employer_hosted` registered in `log_event.GATE_TYPES`
  (one line + comment). Verified no unknown-gate warning; 6/6 renderer tests green.
- **Status: SHIPPED 2026-09-17 ~13:27 PDT.**

## Sequencing

PQ-1 (minutes, +4) → PQ-3 (de-risk the classifier before promoting anything) →
PQ-2 (+≤28) → PQ-4 (dry-run sample, then full backfill) → PQ-5 (with Build) →
PQ-6 (top-of-funnel) → PQ-7 (ongoing watch).

Realistic near-term READY recovery: 4 + (re-band yield) + (backfill yield). The pool
will not *stay* refilled without PQ-6.

Compounding notes: PQ-3's type-parity test and PQ-5's lock-release tests are permanent
regression assets; PQ-7's early-warning detector compounds (each new employer pattern
caught once, handled forever); PQ-4's backfill makes the 112 permanently scored.

## Open flags for Program Office

1. The 7.7h-old IN-FLIGHT Stripe marker needs main chat's live-task check before any
   `--repair-inflight` (browser lane is main-chat only).
2. PQ-2 needs a written re-band bar before it runs — refill must never become
   bar-lowering (fabrication-gate adjacency).
3. `hidden_files/launch-locks/` is being written by two schemas (one file carries an
   in-flight-marker schema: `browser_task_id`/`in_flight_at`, no `task_id`/
   `acquired_at`) — benign today, worth one cleanup pass with Build.
