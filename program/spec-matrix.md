# Keel Spec Matrix — advertised competitor capabilities vs evidenced Keel capability

**Owner:** Keel Competitive Intel. **Standing:** evidence before claims.
**Source:** `~/workspace/user/files/Market_Analysis_and_Competitive_Build_Plan_2026-09-17_6_eu68.md` (§1 vendor product-page research, §§2–10) and `~/workspace/user/files/Focused_Customer_Pilot_Plan_2026-09-17.md`.

**Global caveats (from the documents):** vendor descriptions establish *advertised capabilities, not measured quality*. No paid competitor trials, live applications, customer interviews, or production measurements were performed. The comparison is function-based, not a verified ranking. Competitors' internals and our production behavior remain untested — "This report does not establish that competitors have weaker security, worse answers or inferior results."

**Gap legend:** YES = we have nothing verified equivalent · PARTIAL = we have machinery but open defects or unmeasured equivalence · NO = evidenced parity (note the evidence).

---

## 1. JobCopilot — advertised capabilities

| # | Advertised capability | Our evidenced capability | Gap |
|---|---|---|---|
| JC-1 | Company-career-page discovery | Not explicitly addressed; our connector coverage unverified (F43/F46) | YES |
| JC-2 | Automatic application OR saved review (user-chosen auto-apply vs review-first) | Our API/browser execution paths exist but false-success and fallback defects (F17/F22) mean no verified equivalent completion contract | YES |
| JC-3 | Application tracking | We have outcome tracking, but central outcome recording bypasses the evidence gate (F18); tracker completeness unverified | PARTIAL |
| JC-4 | Personalization tool incorporating user edits / retained corrections | We have an answer bank, but scope metadata was dropped before use in briefs (F34) and scope matching was only repaired 2026-09-17; tray-answer compounding repaired but the brief-construction path is unverified | PARTIAL |
| JC-5 | Chrome extension filling external forms (review and submission left to the user; subscription required) | Browser-assisted path exists but browser clicks create no durable intents and uncertain results fall back blindly (F22) | YES |
| JC-6 | Tiered plans: Premium $0.93/day (1 copilot, 20 job matches/day); Elite $1.05/day (3 copilots, 50 matches/day, per-application resume tailoring, hiring-manager contact credits) | We have no measured cost per completion and no verified match/completion volume claims; pricing pressure from this tier structure (E-I11) | YES |
| JC-7 | Per-application resume tailoring (Elite) | Tailored packets exist but approval doesn't bind bytes (F20) | PARTIAL |
| JC-8 | Hiring-manager contact credits (Elite) | Deferred — recruiter-contact automation explicitly deferred pending separate demand and authorization decisions | YES |

## 2. LoopCV — advertised capabilities

| # | Advertised capability | Our evidenced capability | Gap |
|---|---|---|---|
| LC-1 | Recurring discovery and application workflows | We have discovery/verification/launch machinery but starved supply — documentary snapshot 2,600 queue entries with zero READY; live 2026-09-17 READY 6, effective launchable 0 | PARTIAL |
| LC-2 | Recruiter outreach | Deferred — broad outreach excluded from near-term scope; drafts only, agents never send (standing boundary) | YES |
| LC-3 | Browser-assisted application route | Browser path exists but intent/hold coverage is API-only; fallback duplication risk (F22); zero browser lifecycle telemetry (D-H7) | PARTIAL |
| LC-4 | Editable Questions tab; retains unanswerable questions for user input | We have the input tray (family-grouped cards, triage briefs, banking), but tray-answer compounding was an open defect until the 2026-09-17 repair; answered-question re-parking is fixed-with-tests, scope-matching at brief construction unverified (F34) | PARTIAL |
| LC-5 | Tracker with manual entries and status updates | Outcome tracking exists; READY-label-on-blocked-work conflates counts (B-D2); dashboard lacks executable-READY by route, runway, replenishment ETA (B-D10) | PARTIAL |
| LC-6 | Career-coach channel — multi-client dashboard, white-label options | Plausible but uncontested channel per the documents; we have nothing equivalent built | YES |
| LC-7 | Free plan; paid plans from €9.99/month | No pricing set; unit economics unmeasured (E-I6); documents advise against unlimited-volume promises until failure/support/usage distributions are known | YES |

## 3. Jobright Agent — advertised capabilities

| # | Advertised capability | Our evidenced capability | Gap |
|---|---|---|---|
| JR-1 | Proactive matching | Fit scoring exists but uncalibrated and unproved against independent labels (F40/F41) | PARTIAL |
| JR-2 | Tailored resumes | Tailored packets exist but approval doesn't bind bytes (F20) and brief construction drops scope (F34) | PARTIAL |
| JR-3 | Applications and tracking (integrated agent) | Execution paths exist but open submission-integrity defects (F17/F18/F22) block trustworthy completion | PARTIAL |
| JR-4 | Insider connections and career guidance (broader product) | Deferred — general career coaching explicitly deferred | YES |
| JR-5 | Free entry advertised (candidate pricing unverified by the documents) | No pricing; free/prep experience only proposed as acquisition support | YES |

## 4. Adjacent baseline + cross-cutting expectations

| # | Advertised capability | Our evidenced capability | Gap |
|---|---|---|---|
| SB-1 | Simplify Copilot: free autofill, tracking, job matches, basic resume builder, no advertised autofill limit | The documents say this raises the bar for charging for basic application assistance; we cannot compete on generic autofill | YES (baseline pressure) |
| X-1 | Answer learning / retained corrections over time (all three competitors, advertised) | Established expectation, not a differentiator (per Trent 2026-09-17 and the documents) — and our answer-bank provenance/scope handling had defects (F12/F34), partially repaired 2026-09-17 | PARTIAL |
| X-2 | Networking / insider connections (Jobright / LoopCV, advertised) | Established expectation per the documents; we have nothing equivalent | YES |

---

## 5. Parity requirements (must match to compete on specs)

These are table stakes — customers will expect them because competitors advertise them; we close gaps here but do not win on them.

1. **Trustworthy application tracking** — close F18 (evidence gate) and B-D2/B-D10 (dashboard conflation). (JC-3, LC-5, JR-3)
2. **Reliable completion semantics** — close F17/F22 so "submitted" means submitted on every route. (JC-2, LC-1, JR-3)
3. **Retained corrections that actually hold** — finish F34 end-to-end (brief construction path) and keep the tray-answer compounding repair regression-covered. (JC-4, LC-4, X-1)
4. **Per-application tailored materials that bind to approval** — close F20. (JC-7, JR-2)
5. **Measured route coverage** — publish the provider-and-form support matrix (E-I10) so users know what Keel can apply to before paying. (JC-1)
6. **Browser-assisted route with durable intent** — close A-B3/F22 browser-origin uncertainty. (LC-3, JC-5)

## 6. Differentiation opportunities (advantages that improve with use)

Per Trent (2026-09-17): lead with advantages that improve with use — better opportunity selection, less user effort, stronger evidence, trusted distribution. Answer learning / networking / coach partnerships are established expectations (JobCopilot, Jobright, LoopCV), not differentiators.

1. **Better opportunity selection** — calibrated fit + independent relevance labels + job-age checks + canonical band policy (RP01) compound into fewer unsuitable applications per user hour. Currently blocked by E-I7 (no independent relevance labels) and F41 (observational learning). Owner: Keel Competitive Intel.
2. **Less user effort** — the tray-answer compounding repair already demonstrates one-answer-unblocks-N; full F34 wiring plus per-employer consent handling and posting-text extraction (D-H1) compound into less repeated input. Currently blocked by D-H1/D-H2/D-H3/D-H4. Owner: Keel Build.
3. **Stronger evidence** — verified receipts, UNKNOWN-until-reconciled semantics, no invented scores, published cost per verified eligible completion, interview-rate cohorts with denominators and uncertainty. This is where competitors are weakest (advertised capability ≠ measured quality) and where F17/F18/A-C2 and E-I6/E-G6 are the blockers. Owner: Keel Build / Keel Competitive Intel.
4. **Trusted distribution** — no invented claims, no outreach without authority, no reference contact, per-post approval for any publishing, consent-gated pilots. This is a stance competitors cannot easily claim; the G-blocker cluster (F27–F43) and F38 are the engineering backlog beneath it. Owner: Keel Program Office.

## 7. Competitive-evidence readiness checklist

- [ ] Close P0 submission-integrity cluster (A: F17, F18, F20, F21, F22, F34)
- [ ] Re-verify the receipt writer under adversarial review (currently FAIL — A-C2)
- [ ] Publish cost per verified eligible completion (currently unmetered — D-H5, E-I6)
- [ ] Build the measured provider-and-form support matrix (E-I10)
- [ ] Run the 8/10-users-to-first-approved-packet usability target (currently unbuilt — E-G4)
- [ ] Produce the named benchmark contract + build-roadmap files (missing — E-I9)
- [ ] Run the Step-5 matched competitor comparison under the pilot plan (requires funded-sidebar items: paid competitor trials, reviewer compensation)
- [ ] Publish interview-rate cohorts with age, denominators, and uncertainty (E-G6, E-I7)

**Counts:** 23 capability rows — 11 YES, 11 PARTIAL, 1 baseline-pressure (SB-1), 1 established-expectation (X-1). 6 parity requirements. 4 differentiation pillars. 8 evidence-readiness gates.
