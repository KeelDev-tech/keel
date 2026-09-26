# cost-metering prototype (G-5 / PP-11)

**Status: PROTOTYPE — not wired to production. Not a pricing claim. Nothing here publishes dollars; the T3 evidence bar plus G1 privacy-counsel sign-off gate any publication.**

## What it proves (9/9 tests green)

- **Known-cost replay:** 3 consecutive fabricated runs (clean API attempts; an api→browser failover with a failed sub-attempt and a human tap; a zero-activity dry run) meter within ±0.5% of independently audited totals (Decimal arithmetic in the test, float in the meter — the tolerance is exercised honestly, not trivially).
- **No double-count on transport failover:** the API attempt and the browser retry each cost their units separately, but the completion is counted exactly once — F22 one-durable-attempt identity holds across transports, and duplicate attempt IDs inside a completion are rejected.
- **The tolerance band discriminates:** a 10%-shaved audit is flagged `within_tolerance=False`.
- **Publication is fail-closed:** `publish_figure` raises `EvidenceBarNotMet` unless `evidence_bar_met=True` (default False). The published figure carries definition, denominator (total attempts + verified eligible completions), maturity window, exclusions, method, and tolerance — never a bare dollar number.

## ⚠️ Placeholder-rate warning

`RATE_CARD` holds **placeholder** dollars per unit (`placeholder-v0`) chosen only to make the pipeline testable. They are not real costs, not vendor quotes, not a pricing proposal, and not Trent's money. Every dollar figure this code produces is a test artifact until Pipeline Performance supplies audited real rates.

## What Pipeline Performance still owns before productization

1. **Real rate card:** replace placeholder-v0 with audited per-unit dollar costs (browser compute time, network egress, human-tap valuation, infra share); pin the version and keep the placeholder history so old figures stay reproducible.
2. **Live adapter:** map real telemetry to the five units. Open questions: how browser time is attributed when the lane multiplexes, how to count human taps from the tray/inbox without double-counting review work, and how infra share is apportioned across concurrent runs.
3. **T3 evidence bar:** define what "met" means operationally (mature 30/60-day cohorts, denominator rules, uncertainty) and where the flag lives — this prototype's boolean must become a real gate, not a call-site argument.
4. **G1 privacy-counsel sign-off:** any dollar figure that could become a price, pitch, or public claim needs counsel's review first — per standing rule, before publication, offering, pricing, or sale.
5. **Comparator context (FS-2):** comparable vendor checkout totals remain Trent-gated; obtain only with his explicit authorization.

Run: `python3 test_cost_metering.py`
