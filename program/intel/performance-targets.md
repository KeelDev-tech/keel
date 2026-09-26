# Keel Competitive Intel — Measurable Performance Targets

**Author:** Keel Competitive Intel department, 2026-09-17.
**Standing rule:** these are acceptance criteria, not achieved results. Nothing below is claimed as measured until a measurement with named scope, date, denominator, and missing-data disclosure exists. This feeds the Program Office's performance-tracking register; the intel department owns the definitions and decision rules, the Program Office owns the live metric tracking.

**Sources:** the five targets from Trent's market-plan direction (MEMORY.md, 2026-09-17 ~03:10 PDT) and the definitions/decision rules proposed in the 2026-09-17 market analysis (§5, §8).

---

## T1 — READY reliability: the supply controller is trustworthy

**Why it matters:** "Did my application actually arrive?" and "Why is nothing happening?" — the two first customer questions in the market analysis. READY must mean executable, not merely labeled.

**Definitions (one readiness evaluator, single source of truth):**

- **Feasible-supply replay:** a recorded pipeline history (queue events, verification results, launch windows, provider slots) replayed against the canonical readiness decision. A slot-minute is *feasible supply* when, at that minute, at least one packet existed that was prepared, authorized, fresh, and routed to a permitted provider slot.
- **Avoidable idle:** slot-minutes the lane idled while feasible supply existed and no recorded blocker (SYSTEM-BLOCKED tray item, provider outage, approval hold) explains the idle. Excludes SUPPLY_LIMITED, PROVIDER_LIMITED, AWAITING_DECISION when those states are evidence-backed.
- **Preventable rejection after READY:** a lead promoted to READY that is later rejected or parked for a cause the readiness evaluator could have detected at promotion time (stale verification, invalidated answer/policy/material version, duplicate attempt identity, out-of-scope attestation). Excludes rejections caused by genuinely new information (posting closed after promotion, newly surfaced employer signal).
- **Unused expensive packet preparation:** packets built (model calls + brief + attachments) for leads that never launched because readiness was misjudged — reported as a percentage of total preparation cost. Proposed ceiling: **<5%**.

**Decision rules:**

| Metric | Target | Denominator | Report |
|---|---|---|---|
| Avoidable idle time | **<1%** of feasible-supply slot-minutes | All feasible-supply slot-minutes in the replay window | Unavoidable holds (SUPPLY_LIMITED / PROVIDER_LIMITED / AWAITING_DECISION / INTERNAL_FAILURE) itemized separately, never folded into the numerator |
| Preventable rejection after READY | **<1%** of READY promotions | All READY promotions in the window | Each rejection classified preventable / genuinely-new with evidence pointer |
| Wasted packet preparation | **<5%** of preparation spend | Total packet-preparation cost (model + build + verify) | |

**Measurement method:** replay harness over append-only queue/ledger history; run weekly on a rolling 7-day window; first baseline run scheduled after the canonical readiness evaluator ships (Build department, weeks 4–6 build sequence). Missing instrumentation (e.g., browser-lane slot accounting) is disclosed per run, not imputed.

**Current status:** UNMEASURED. The 2026-09-17 lane-stall post-mortem (9 of 10 parked tasks dead ~2h while READY work queued; RP01 fixed in review package; canonical-queue discipline) is exactly the class of defect this target detects. The conditional arithmetic in the market analysis (verification limit 200/hr × 0.056 yield → 11.2 promotions/hr ceiling) is documentary, not a capacity measurement, and is cited only to show why queue targets cannot fix conversion bottlenecks.

---

## T2 — Usability: a new user succeeds without operating the machinery

**Why it matters:** "Can I use this without operating the machinery?" — a new user must reach value before patience runs out. Ten users expose usability problems; this is not statistical market validation and not an interview-rate claim.

**Definition:** a *target user* is an experienced job seeker with meaningful constraints (role fit, geography, compensation, work arrangement, or employer exclusions) matching the segment under test. *Unaided* means no operator intervention: the user gets the product, the segment's onboarding, and the public documentation — nothing else. *First approved packet* means a launch-ready packet the user has reviewed and approved in the decision inbox.

**Decision rule:** **at least 8 of 10 target users reach a first approved packet unaided within 15 minutes**, with every failure recorded as an actionable defect (what broke, at which step, fix owner). A run that misses 8/10 fails the target; it does not trigger scope reduction — it triggers defect fixing.

**Method (per market analysis §8–9):** ten-user pilot on the independently reviewed opportunity set for the chosen segment; session recordings + task timing; the three largest wasted-attention sources fixed between runs. Pre-registration: segment definition, task script, success criterion (above), and the failure taxonomy — before the first session.

**Current status:** UNMEASURED. The focused customer pilot plan exists (`~/workspace/user/files/Focused_Customer_Pilot_Plan_2026-09-17.md`) but is **SHELVED at Trent's order** (2026-09-17 ~03:16 PDT) until integrity + READY fixes are solid. This target stays defined-but-dormant; no user sessions are scheduled, recruited, or run. Dormancy is reported, not hidden.

---

## T3 — Economics: published cost per verified eligible completion

**Why it matters:** "Is the price worth it?" — a premium needs demonstrable incremental value; our cost claim must be in real dollars, failures included, or it is marketing.

**Definition:** *Verified eligible completion* = one unique eligible application with independently accepted submission evidence (receipt correlated to the provider, attempt identity, no UNKNOWN/duplicate/ineligible attempt in the numerator — ever).

**Decision rule:** publish, per reporting period, **total attributable service cost ÷ verified eligible completions**, where total attributable cost includes model calls, browser runtime, network, failed attempts, infrastructure, and human recovery cost against stable attempt IDs. Report **active user minute** separately. Report the user's own labor as an explicit sensitivity line, not folded into service cost. Disclose every unmetered dimension by name.

**Current status:** UNMEASURED in dollars. The archive's composite cost units are explicitly not dollars (`docs/cost-model.md` marks dimensions unmetered; window-level tracker uses browser launches as an effort proxy). Publishing a dollar figure before attempt-level metering ships (Build weeks 4–6) would violate evidence-before-claims. **Do not quote internal cost as a competitor-price comparison** — our service cost and a competitor's subscription fee are different quantities.

---

## T4 — Matched comparator-task comparison (JobCopilot / Jobright / LoopCV)

**Why it matters:** "beating the market, measurably" — the only honest way to claim it. Advertised features are not measured quality, on either side.

**Protocol (from market analysis §8, binding when authorized):**

- Real consenting candidates; **disjoint, matched candidate–job cohorts**; same verified profile constraints, observation window, and budget per arm.
- **Never submit the same candidate to the same employer role through multiple products.**
- Randomize within candidate and role/source strata where feasible; cluster analysis by candidate/employer.
- Record plan, account, region, quotas, and every manual intervention per arm.
- Report **discovery coverage separately from execution** on the common supported set — broad reach and easier task selection must not be confused.
- A comparator that cannot use a synthetic fixture is marked **UNTESTED on that fixture**, never assigned a failure score.

**Metrics and decision rules:**

| Metric | Definition | Decision rule |
|---|---|---|
| Eligible opportunity coverage | Unique independently eligible postings surfaced ÷ eligible postings in the observed reference corpus | Match or exceed the best measured comparator **within the declared segment**; no internet-wide recall claim |
| Verified eligible completions | Unique eligible applications with independently accepted submission evidence | UNKNOWN, duplicate, and ineligible attempts never enter the numerator |
| User effort | All active setup, review, correction, recovery, tracking minutes ÷ verified eligible completions | **Ambition: 50% lower than the best measured comparator, without worse coverage or outcome quality.** Currently an unachieved target — stated as such in every communication |
| Economic efficiency | Total attributable service cost ÷ verified eligible completions; user labor reported separately and as sensitivity | Improve against the measured alternative with costs and missing meters disclosed |

**Integrity gate (applies to T4 and everything else):** any observed unauthorized action, fabricated candidate claim/evidence, or duplicate side effect **blocks expansion pending correction**. Zero observed events is not a universal guarantee.

**Current status:** UNRUN. Requires Trent's explicit word for paid trials/vendor signups and a recruited consenting cohort. **BLOCKED until authorized.** The comparator set is fixed by `benchmark-contract.json` (JobCopilot, Jobright Agent, LoopCV); the contract file itself was not found in the delivered package, so the first step on authorization is re-verifying the contract, not running the benchmark.

---

## T5 — Interview-rate cohorts with uncertainty

**Why it matters:** "Will I get better results?" — the outcome claim. Fit scores and observational learning do not prove improved interviews; only mature, consented cohorts do.

**Definitions:** *Qualified responses and interviews ÷ unique eligible applications*, in cohorts with prespecified maturity. **Primary endpoint: 30-day window; follow-up: 60 days.** Report timing/censoring, sample size, denominators, and confidence intervals. A longer window may be needed for the target market — prespecify before measuring.

**Decision rules:**

- Endpoints, comparisons, and the multiple-comparison disclosure plan are defined **before** seeing results.
- Observational tags are never promoted into causal claims.
- Choose powered sample sizes **after** observing baseline conversion and candidate-level variability — not before.
- **No invented chance-of-hire scores.** Ever. The product's selection guidance uses eligibility + observed outcomes + effort (market analysis §6), not a hire-probability number.

**Current status:** UNMEASURED. Outcome ingestion with consent, cohort maturation, and controlled ranking changes (market analysis F40/F41 build items) are future work. Interview-outcome observation may extend beyond the 12-week build scenario.

---

## Target dashboard (Program Office tracking view)

| ID | Target | Status | Evidence |
|---|---|---|---|
| T1a | <1% avoidable idle in feasible-supply replay | UNMEASURED — awaiting canonical readiness evaluator | Lane-stall post-mortem (2026-09-17) is the motivating defect |
| T1b | <1% preventable rejection after READY | UNMEASURED | Same |
| T1c | <5% unused expensive packet preparation | UNMEASURED | Same |
| T2 | 8/10 users to first approved packet unaided ≤15 min | DEFINED, SHELVED — pilot plan exists, no sessions until integrity + READY fixes land (Trent 2026-09-17) | Pilot plan file; shelve order recorded |
| T3 | Published $ cost per verified eligible completion | UNMEASURED — composite units are not dollars | `docs/cost-model.md` unmetered dimensions |
| T4 | Matched comparator-task results (JobCopilot / Jobright / LoopCV) | BLOCKED — needs Trent's word for paid trials + consenting cohort | `benchmark-contract.json` not found at review; re-verify on authorization |
| T5 | Interview-rate cohorts, 30/60-day windows, uncertainty shown | UNMEASURED — future work | Outcome ingestion with consent not yet built |

**Cadence:** the intel department re-verifies these definitions against the market analysis quarterly or when the comparator set changes. The Program Office owns the live measurement tracking; a target moves from UNMEASURED/BLOCKED/SHELVED to measured only on a dated, scoped, denominator-bearing measurement with missing data disclosed.
