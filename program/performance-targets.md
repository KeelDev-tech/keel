# Keel Performance Targets — dashboard

**Owner:** Keel Program Office. The five measurable beat-the-market targets Trent set 2026-09-17. No target is claimed until its evidence lands here. **Proposed targets where noted are not statistical market validation — they are product bars, stated as such.**

> Scaffolding v0, 2026-09-17 12:07 PDT; coordinator register folded in 2026-09-17 12:15 PDT. Target definitions and decision rules owned by Keel Competitive Intel at `intel/performance-targets.md`; this register owns live metric tracking. **Ledger caveat (standing):** a prior pulse reported submitted=196 vs the 195 snapshot — ledger semantics unreconciled; do not quote a definitive submitted total until reconciled.

---

## T1 — Supply reliability: <1% avoidable idle in feasible-supply replay; <1% preventable rejection after READY

**Why it matters:** answers "Why is nothing happening?" — READY must be a reliable supply controller, not a label on blocked work (RP01 / READY review).

**Measure:** (a) feasible-supply replay of lane telemetry — idle time where supply was feasible vs avoidable idle; (b) post-READY rejections that policy, not reality, caused (e.g. lock-layer refusals, parking-schema stalls, stale locks), over total READY claims.

**Baseline (pulse 318, 2026-09-17 19:04Z; pulse 321, ~12:44 PDT):** lane in refill crisis — RL-2 breached (supply:burn 0.37 < 1.0, lane expansion frozen); READY 6 but effective launchable pool **0** (all six held by fresh api-direct refuse-locks); rolling submission rate 1.0/hour; verification promotion 65.1%; RL-3 verification backlog 607. PQ-0A shipped: bank-write → parked-lead sweeper cleared 126 blockers across 94 leads, READY 3 → 13 (no-AI/unaided-work boundary intact); PQ-0B (cooldown override) rejected with measured evidence. Emergency refill (~12:53 PDT): PQ-1 stale-refusal re-run +1 READY (Drata; 3 others hit genuine essay blockers, correctly parked), bank-sweeper clean no-op, cooldown-bypass candidates evaluated and confirmed none legitimate — READY 13 → 14. J8187570 honestly recorded as PARKED-NEEDS-INPUT after a browser crash before any fill — evidence or it didn't happen. Live 19:53Z: READY 13 with one IN-FLIGHT (C3X-PINGID-87433600) — lane consuming the refill, expected dynamics. Live 19:42Z: READY 0 — the refill was consumed by the lane; this is expected dynamics, not a stall (stalled=false). The refill bridge (re-screen 330 hi-fit leads, verify @200/hr) is the immediate supply repair; the readiness evaluator (one current readiness decision, qualified reserve vs executable inventory, adaptive bounded refill, visible shortage explanation) is the structural build. Measurement harness: TBD (Keel Pipeline Performance).

**Status:** RED — supply crisis first, measurement second. Cannot claim until the refill lane is healthy.

## T2 — Usability: 8 of 10 target users reach a first approved packet unaided within 15 minutes

**Why it matters:** answers "Can I use this without operating the machinery?" The complete independent-user journey is unverified; public-edition docs describe manual setup friction (F30, F45).

**Measure:** usability pilot — 10 target users, unaided, 15-minute cap, first *approved* packet as the finish line; failures recorded with actionable causes. Ten users expose usability; a larger study is needed for outcome claims (pilot plan SHELVED 2026-09-17 per Trent — runs only after integrity + READY fixes are solid).

**Baseline:** zero pilot runs. Mechanism pieces exist (guided profile confirmation, one decision inbox, pause/resume).

**Status:** BLOCKED by design — fires only after integrity + READY fixes are solid (Trent's order).

## T3 — Cost transparency: published cost per verified eligible completion

**Why it matters:** answers "Is the price worth it?" — cost instrumentation is partial (composite units, proxies, unmetered dimensions).

**Measure:** attempt-level metering against stable attempt IDs: browser, network, failed attempts, infrastructure, and human recovery cost — in real dollars, failures included, with missing-meter coverage disclosed.

**Baseline:** no published figure. Window-level tracker uses browser launches as effort proxy (`docs/cost-model.md`).

**Status:** RED — metering build (Keel Build) + EV-4 unblocked first.

## T4 — Matched competitor-task comparative results with missing data visible

**Why it matters:** answers "Will I get better results?" — the only honest route to a superiority claim over JobCopilot / Jobright / LoopCV.

**Measure:** matched-cohort protocol (per market analysis §8): disjoint candidate–job cohorts, same profile constraints, observation window, budget; no same-candidate–same-role dual submission; missing data VISIBLE, never imputed. Reports coverage, effort minutes, and outcome quality on the same cohorts. Comparator price totals obtained for cost comparison.

**Baseline:** `benchmark-contract.json` / `build-roadmap.json` referenced but files not found at review — no benchmark has run.

**Status:** BLOCKED — paid comparator trials require Trent's explicit word (FS-2); not a funding-lane item.

## T5 — Interview-rate cohorts with age, denominators, and uncertainty — no invented chance-of-hire scores

**Why it matters:** answers "Will I get better results?" honestly. Fit scores and observational learning do not prove improved interviews (F40/F41).

**Measure:** outcome ingestion with consent; independent relevance labels; job-age checks; controlled changes to ranking/presentation; calibration, response and interview rates with cohort age, denominators, and uncertainty; validated on held-out periods.

**Baseline:** outcome-listener triage live (2026-09-17 dry run: 144 classifications, 0 new outcomes); cohorts not yet mature.

**Status:** YELLOW — machinery exists; needs mature cohorts + held-out validation.

---

## Dashboard protocol

- Each target carries status RED (blocked/no evidence), YELLOW (machinery present, evidence thin), GREEN (evidence published with denominators).
- Baselines refresh from live pipeline snapshots; any target whose acceptance evidence depends on the refill lane (T1) is gated on lane health first.
- Targets are never relabeled downward to look better. If a bar is unreachable, the Program Office says so with the evidence.
