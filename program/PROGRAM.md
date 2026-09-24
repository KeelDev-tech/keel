# Keel Max-Potential Program

Authorized by Name Example, 2026-09-17. Mission: push Keel to the best product on the market — competitor specs, our standard (honest automation, human-gated AI, evidence before claims).

## Performance goals (the measurable beat-the-market targets)
1. <1% avoidable idle time in feasible-supply replay; <1% preventable rejection after READY.
2. 8 of 10 target users reach a first approved packet unaided within 15 minutes.
3. Published cost per verified eligible completion (attempt-level metering, failures included).
4. Matched competitor-task comparative results with missing data visible (JobCopilot / Jobright / LoopCV).
5. Interview-rate cohorts with age, denominators, and uncertainty — no invented chance-of-hire scores.

## Departments (side chats, independent, hub-connected)
| Department | Chat | Owns |
|---|---|---|
| Keel Build | Keel Build | Transfer integration health, receipt-writer adversarial hold, gateway/store reconciliation design, P0 closures (F17/F18/F20/F21/F22/F34) |
| Keel Competitive Intel | Keel Competitive Intel | Spec matrix vs JobCopilot/Jobright/LoopCV; measurable performance targets |
| Keel Pipeline Performance | Keel Pipeline Performance | READY refill crisis, api-direct lock layer, attestation wave, compounding efficiency — pipeline as Keel's proving ground |
| Keel Launch & Growth | Keel Launch & Growth | Show HN readiness checklist + evidence gates, keeldev Sep-18 retry, positioning |
| Keel Program Office | Keel Program Office | Hub: this backbone, limitations register, funded sidebar, performance dashboard, weekly digest |

Funding stays its own department (existing Funding chat + goal) — Program Office liaises, never absorbs.
Browser application lane stays main-chat only. No department spawns, steers, or manages browser tasks.

## Channels
- Departments report status to the Program Office chat; Program Office resolves cross-department conflicts.
- Engineering changes touching live queue behavior: Build ↔ Pipeline Performance coordinate before shipping.
- All shipping gated by PUSH ON VALUE + TESTED VALUE ONLY.

## Registers (Program Office owns)
- `limitations-register.md` — every known limitation, impact, owner, status, path to clear.
- `funded-sidebar.md` — upgrades requiring funds, sidebarred until grants come in; cost, what unlocks, which grant/credit covers it. Build everything else possible.
- `performance-targets.md` — the five targets above, tracked against live metrics.
- `intel/spec-matrix.md` — Competitive Intel's departmental spec matrix (parity requirements + differentiation pillars; department-owned working analysis).
- `spec-matrix.md` — the fuller 23-row competitor-capability matrix (11 gaps, 11 partial, 6 parity requirements, 4 differentiation pillars), evidence source for the registers; Competitive Intel owns both.
- `improvement-scorecard.md` — the standing weekly improvement scorecard (Trent's order 2026-09-17): submissions, cost per verified submission, limitations burn-down, verify promotion rate, READY floor breaches, learning-loop yield. Week-over-week deltas, no vanity metrics. First edition Fri 2026-09-18 EOD; weekly every Monday after.

## Cadence
- Departments run independently, continuous.
- Program Office posts a weekly digest to Trent: proof, not ceremony — yields, shipped fixes with test evidence, limitations cleared, sidebar status.

## Standing guards (program integrity, non-negotiable)
1. **Refill is never bar-lowering.** Re-banding (PQ-2) requires a written re-band bar before it runs; the bar stays 75, every re-band needs a cited reason (fabrication-gate adjacency).
2. **No live queue-behavior change ships without cross-department sign-off.** Build ↔ Pipeline Performance coordinate before shipping (reciprocal: Build's Phase 3 needs Pipeline Performance sign-off; Pipeline Performance's PQ-3 writer fix and PQ-5 lock fix need Build coordination).
3. **Browser lane is main-chat only.** Side chats never spawn, steer, or manage browser tasks. Lane flags (e.g. stale IN-FLIGHT markers) are recorded for main chat's lane check, not acted on from a side chat.
4. **Emergency refill is procedural, never a bypass.** The emergency-refill arm runs when READY < 5; blanket cooldown bypass stays excluded per PQ-0B measured evidence (sweeper stamp-clearing is the legitimate bypass). Emergency refill never becomes bar-lowering — re-bands still need the written bar and cited reasons.

## Self-sustaining work queues (permanent, Trent's order 2026-09-17)
- Every department holds a standing prioritized queue (`program/queues/<dept>-queue.md`), sourced from the limitations register, the spec-matrix gaps/partials, blackboard findings, and chartered deliverables. Highest value first; integrity-gated items flagged 🔒.
- **The pull rule:** when a department finishes an item it pulls the next one itself. When its queue drops below 5 items, it refills from the limitations register by priority AND runs its own gap-hunt to generate new items. A department never waits for direction — the queue is the direction.
- **Zero queued items = P0** — same severity as a READY floor breach. Queue depth (queued / in progress / completed per department) is tracked weekly on the improvement scorecard.
