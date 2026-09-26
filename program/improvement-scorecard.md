# Keel Improvement Scorecard — standing weekly

**Order:** Trent, 2026-09-17 ~13:00 PDT (via Dev). **Purpose:** prove the whole system is getting better — not activity. **Rules:** week-over-week deltas; no vanity metrics; if a metric isn't improving, it gets named, not hidden.

**Cadence:** weekly, published every Monday for the prior week (Monday–Sunday, America/Los_Angeles). Canonical home: this file. The Build Log (`program/build/BUILD_LOG.md`) carries a standing section with the same numbers.

## Metrics (fixed set)

| # | Metric | Definition | Trend bar |
|---|---|---|---|
| M1 | Submissions — cumulative | Ledger submitted total (ledger semantics reconciled; 195-vs-196 caveat stays until resolved) | Up |
| M2 | Submissions — per-week | New ledger submissions in the week | Up-and-right; named when flat |
| M3 | Cost per verified submission | Time + taps + compute in real units per verified eligible completion; failures included, missing-meter coverage disclosed | **Must go down** (compound growth law) |
| M4 | Limitations burn-down | Register open/partial/closed counts: P0, P1, P2, cleared-with-tests | Open must go down; closed must go up |
| M5 | Verify promotion rate | Verify-retry: promoted-live ÷ verified attempts | Up; named when stale |
| M6 | READY floor breaches | Count of READY < 5 episodes in the week (emergency-refill arm triggers) | **Must trend to zero** |
| M7 | Learning-loop yield | Drafts mined → triaged → promoted (charter-evaluator pipeline) | Up; stuck stages named |
| M8 | Queue depth per department | Items queued / in progress / completed (Build, Competitive Intel, Pipeline Performance, Launch & Growth) | **Zero queued = P0** — same severity as a READY floor breach |

## Baseline snapshot — week of 2026-09-14 → 2026-09-20 (week 1)

| Metric | This week (as of Thu 2026-09-17) | Notes |
|---|---|---|
| M1 cumulative submissions | ~195 (pulse ledger; 195-vs-196 semantics unreconciled — do not quote as definitive) | Baseline only |
| M2 per-week | Tabulating from ledger history by Fri EOD | Week-1 series starts here |
| M3 cost per verified submission | **Not publishable.** Cost model v1 is composite units with HTTP and tap dimensions unmetered (D-H5). Named, not hidden. Build path: attempt-level metering incl. failures → real dollars (performance-targets T3) | Must go down once measurable |
| M4 limitations | Open P0 41 · P1 21 · P2 31 (93 open/partial) · cleared-with-tests 14 | Burn-down starts from here |
| M5 verify promotion rate | 65.1% (pulse 318) | Baseline |
| M6 READY floor breaches | Tabulating from pulse digests by Fri EOD (breaches known this week: READY hit 0, 3, 5, 6; emergency-refill arm formalized). **2026-09-17 14:26 PDT: P0 answer-mismatch flood resolved — 119 false-parked leads restored to READY** (31 genuine Trent-input stays parked, 3 ambiguous) — READY floor materially rebuilt | Must trend to zero |
| M7 learning-loop yield | Sieve live run: 136 pending → 73 promote-candidates, 45 review, 14 needs-evidence, 4 duplicate, 0 defer. Promotions pending Monday evaluator session (human approval) | Yield completes Monday |
| M8 queue depth (queued / in progress / completed) | Build 15/0/4 · Intel 9/0/4 · Pipeline 10/0/8 · Launch & Growth 8/0/0 (first status pending) | Zero queued = P0 |

**First full edition: Friday 2026-09-18 EOD** (covers this week). Week-over-week deltas begin with the week of 2026-09-21.

## No-vanity policy
- A metric that is unmeasurable is listed as unmeasurable with its path to measurability — never as a proxy dressed as the metric.
- A metric that is flat or worsening is named with the cause and the response, not averaged away.
- Denominator, date, and scope accompany every number.
