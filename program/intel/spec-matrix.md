# Keel Competitive Intel — Spec Matrix

**Author:** Keel Competitive Intel department (side chat `Keel Competitive Intel`), 2026-09-17.
**Ground truth:** `~/workspace/user/files/Market_Analysis_and_Competitive_Build_Plan_2026-09-17_6_eu68.md` — function-based comparison from the three comparators' public product pages accessed 2026-09-17, plus the supplied source audit and READY-pool review. It is not a verified ranking and contains no paid trials, live applications, or production measurements.
**Comparators:** JobCopilot, Jobright Agent, LoopCV — the three `benchmark-contract.json` names.

## Reading key

- **ADVERTISED** — appears on the vendor's public product pages (2026-09-17). Advertised capability is not measured quality; it never scores as proven.
- **CODE** — exists in our source tree per the 2026-09-16 source audit. Implementation behavior, not production performance.
- **TESTED** — passes a defined test suite with scope named; number and date cited.
- **UNMEASURED** — no production measurement exists. Any row with an UNMEASURED gap means we cannot yet claim superiority on it.

Verdict vocabulary: **PARITY** (we have it evidenced or buildable to match), **OPEN** (gap to close — no false claim), **BLOCKED** (cannot proceed without Trent's word: paid trials, outreach, signups on vendor terms).

---

## 1. Parity requirements (table stakes — must exist, not differentiators)

The market analysis finds these capabilities are expected across all three comparators. We do not win by implementing them; we lose by missing them. Truthful personalization, user review, tracking, and learning from corrections are **not claims we can own merely by implementing them** — we can compete only on independently measured accuracy, scope preservation, recovery quality, and reduced user effort.

| Capability | JobCopilot (advertised) | LoopCV (advertised) | Jobright Agent (advertised) | Keel (evidenced) | Gap verdict |
|---|---|---|---|---|---|
| Relevant discovery | Company-career-page discovery; up to 20 matches/day (Premium) / 50/day (Elite) | Recurring discovery workflows | Proactive matching | CODE: fit/eligibility checks, source freshness tracking, yield-ranked discovery | PARITY — with yield tracking as instrumentation, not advantage |
| Editable candidate profile | Personalization tool incorporates user edits | Editable Questions tab; retains unanswerable questions for user input | Tailored resumes | CODE: answer bank with Trent-words provenance + expiry + employer/role scope (prescreen consumes banked answers; tray_answer retro-resolves); standing rules banked for travel/office, comp, start date, heard-about | PARITY on mechanism; UNMEASURED on user-facing edit experience |
| Job-specific tailored materials | Per-application resume tailoring (Elite); chrome extension fills external forms, user reviews/submits | Browser-assisted route | Tailored resumes | CODE: brief_builder → launch packet; resume lanes A-AI-TECH-OPS / C-BD-SALES; launch packet copies queue `resume_lane` verbatim (Abridge fix 2026-09-15) | PARITY — packet lane discipline proven by one incident fix, not by suite |
| Application execution | Automatic application or saved-for-review; extension route | Application workflows | Applications via agent | CODE: ATS API paths (Greenhouse/Lever/Ashby per verify_retry) + real-browser path; 10-min pulse + never-halt lane | PARITY of routes; execution reliability unproven in production measurement |
| Application tracker | Tracking included | Tracker supports manual entries and status updates | Tracking included | CODE: append-only telemetry log, attempt-level outcome events, live dashboard (30-min refresh), ledger per artifact | PARITY, arguably strongest instrumentation — still UNMEASURED on user-facing tracker UX |
| Missing-answer / blocker resolution | — | Retained unanswerable questions for user input | — | CODE: input tray (family-grouped cards, bank-draft suggestions, NEEDS-YOU vs SYSTEM-BLOCKED taxonomy, aging, recurrence) + one-tap triage briefs (13 classes, 14 regression tests); never invents answers; genuine-input blockers route silently per Trent's skip-input directive | PARITY with distinctive discipline: the tray asks, never stalls; answers banked compound (tray_answer retro-resolution, 2026-09-17 compounding repair) |
| Guided user workflow | — | — | — | CODE: guided profile confirmation, one decision inbox (morning digest, tray), pause/resume | OPEN — complete independent-user journey unverified; public-edition docs describe manual setup friction. Usability pilot is the evidence gate (see performance-targets.md T2) |
| Cost visibility | Paid tiers advertised ($0.93/day Premium, $1.05/day Elite) | Free plan; paid from €9.99/month | Free entry advertised; candidate paid price unverified | CODE: cost instrumentation partial — composite units, some dimensions unmetered (`docs/cost-model.md`); window-level tracker uses browser launches as effort proxy | OPEN — no published cost per verified eligible completion. BLOCKED-adjacent: comparable vendor checkout totals never obtained |
| Coach / distribution tooling | — | Coach dashboard + white-label | Insider connections; career guidance | None | OPEN — deliberate deferral per market plan ("expansion to defer"); not table stakes for the career product |

**Parity summary:** our mechanism layer is CODE-complete across the bundle; the gaps are (a) the guided first-run user experience, (b) cost transparency in real dollars, (c) distribution tooling (deferred by strategy). None of the three comparators has ADVERTISED anything that disqualifies a parity claim — but parity is the floor, never the headline.

---

## 2. Differentiation (what we must *prove*, not just ship)

Trent's market-plan direction: **lead with advantages that improve with use — better opportunity selection, less user effort, stronger evidence, trusted distribution.** Answer learning / networking / coach partnerships are established expectations, not differentiators. Each differentiator below names the build that earns it and the measurement that proves it (defined in performance-targets.md).

| Differentiator | Comparator position (ADVERTISED) | Keel evidenced position | Evidence gap → closes with |
|---|---|---|---|
| **Constraint accuracy end-to-end** | Generic matching advertised; no public accuracy methodology disclosed | CODE: D1-final travel/office policy (Trent-approved 2026-09-15) enforced in prescreen with citation; GENUINE_PAT guards verified; tenure rules (5y AI / 7y ops / 12y total) enforced; answer bank preserves employer/role scope + expiry + provenance through every consumer (post-2026-09-17 repair) | T1 measurement: <1% preventable rejection after READY. Until measured, this is architecture, not advantage. |
| **Verifiable completion and recovery** | "Automatic application" language; no public receipt model | CODE: attempt IDs designed; submission receipts partially instrumented. OPEN: F17 (ambiguous HTTP success → submitted), F18 (final writer bypasses evidence), F22 (uncertain API attempts fall back to second channel) — all open P0s in the career pipeline. UNKNOWN state + enforced final writer + immutable bundles are the defined fix. | P0 closures (F17/F18/F20/F21/F22/F34) with adversarial fixtures: faults after handoff never produce false success or automatic duplicate submission. Current state: TESTED review-package patches only — production behavior UNMEASURED. |
| **Fewer repeated decisions** | JobCopilot personalization retains edits (advertised); LoopCV retains unanswerable questions (advertised) | CODE: answer bank + tray_answer `--live` retro-resolves every matching blocker and revives through canonical verify; banking is opt-in; employer-scoped answers with normalized scope matching (2026-09-17 compounding repair); one-tap triage briefs cite standing rules | Counting: repeated decisions per verified eligible completion, declining over time. Unpublished; requires attempt-level metering to be real dollars and bank-efficacy tracking. |
| **Lower user effort at equal or better quality** | Extension route leaves review/submission to user; no public effort metrics | CODE: never-halt lane, autopilot (5 pre-authorized attestations), staged launches, 10:05 digest tap-list | T2 (usability pilot: 8/10 unaided within 15 min) + T4 (matched comparator tasks, effort minutes measured on the same cohorts). Ambition from market analysis: 50% lower user effort than the best measured comparator without worse coverage or outcome quality — currently an unachieved target, stated as such. |

**What we explicitly do NOT claim as differentiators** (they are established expectations or unmeasured): answer learning, networking/insider connections, coach partnerships, white-label distribution, raw application volume, fit scores as hiring-chance predictors.

---

## 3. Evidence ledger (what exists vs what is asserted)

| Artifact | Status | What it proves | What it does not prove |
|---|---|---|---|
| 2026-09-17 market analysis report | Read, cited as ground truth | Advertised capabilities as of 17 Sept 2026 | Comparator quality, market share, results |
| 2026-09-16 source audit (46 findings; 16 corrected in review copies; 8 original P0 open, 6 career-pipeline incl. RP01 corrected in review) | Cited | Implementation behavior at review time | Production performance or deployed fixes |
| Tray-answer compounding repair (2026-09-17) | TESTED: 10 regression tests | Scope/provenance discipline holds under repair | User-facing resolution rates |
| Lane-stall post-mortem (2026-09-17) | TESTED: 23 regression tests | Parked-task sweep correctness | Live lane availability metrics |
| Verify-retry async (60 leads, 2.7 min) | Measured once 2026-09-15 | HTTP verification throughput | End-to-end application yield |
| `benchmark-contract.json` / `build-roadmap.json` | Referenced by market analysis, **files not found** at review | Comparator set named | That any benchmark has run |
| Outcome-listener triage (2026-09-17 09:26 UTC dry run) | Measured: 144 classifications, 0 new outcomes | Telemetry pipeline functions | Interview/rejection improvement |

## 5. Design teardowns (2026-09-19)

Deepened by `comparator-design-teardowns.md` (four parallel research agents, public pages only, 2026-09-19 — JobCopilot, Jobright Agent, LoopCV, plus adjacent Simplify Copilot). Key additions over the capability-level matrix:

- **Proof-of-absence:** none of the four products advertises submission receipts, answer provenance/scoping, constraint enforcement, outcome cohorts with denominators, or real-dollar cost per completion — the white space Keel is built to occupy is confirmed across all four designs.
- **Design-level gaps in ours** (G-1..G-10): no user-facing recurring machine; no execution escape-hatch surface; blocker resolution is our strongest mechanism but invisible as a product surface; the pipeline ends at submission while theirs continues to interview; no productized cost story; discovery breadth not productized; volume controls are operator levers not user policy; no user-facing outcome feedback loop; distribution is a deliberate strategic blank; privacy architecture unclaimed by anyone.
- **Anti-pattern rule:** never mirror their unverified-stat format (drifting user counts, methodology-free "X% more interviews") — our credibility is the differentiator; their format is the anti-pattern.
- Comparator product trials remain BLOCKED until Trent's explicit word.

## 4. Hard limits on what this matrix asserts

1. No comparator internals, security posture, answer quality, or results are asserted — their product pages establish advertised capabilities, not measured quality.
2. No market-share or revenue figures appear anywhere in this matrix, per departmental standard.
3. Every "Keel" row above marked CODE is implementation, not production performance. The five performance targets in `performance-targets.md` are the acceptance criteria that convert these rows into claims.
4. Competitive trials against JobCopilot / Jobright / LoopCV require Trent's explicit word (paid trials) and a matched-cohort protocol (per market analysis §8: disjoint candidate–job cohorts, same profile constraints, observation window, budget; no same-candidate–same-role dual submission; missing-data visibility). **BLOCKED until authorized.**
