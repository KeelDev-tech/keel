# Keel 0.5 — Teardown-Gap Acceptance Criteria

**Source:** `~/workspace/keel/program/intel/comparator-design-teardowns.md` (public pages only, 2026-09-19) + ten Keel gaps logged in `intel/spec-matrix.md` §5.
**Status:** DRAFT — acceptance criteria, not built. Nothing here may become a launch claim until integrity gates pass.
**Standing rules:** 🔒 = integrity-gated (test gates, no false closure). Evidence labels CODE / TESTED / UNMEASURED / BLOCKED / SHELVED apply. No vendor outreach, paid trials, or new external accounts without Trent's explicit authorization. Privacy-counsel sign-off (G1) still gates publication/offering/pricing/sale.

This document covers the five highest-value gaps: G-1, G-2, G-3, G-4, G-5. Remaining gaps (G-6, G-7, G-8, G-9, G-10) have queue items with inline criteria in their department queues; full AC drafts follow when a department pulls them.

---

## G-1 — User-facing recurring run object (Build B-22) 🔒

**Objective:** Give the user a visible, controllable recurring machine — the Keel equivalent of a Loop-style loop or JobCopilot copilot — instead of internal READY/refill machinery the user never sees.

**Non-goals:** Changing the internal executor's cadence or gating logic; marketing copy about loop speed.

**Functional requirements:**
1. One named, persistent run object per user (e.g. a "Run") with: schedule/cadence controls, pause/resume, status (ACTIVE / PAUSED / PARKED-AWAITING-INPUT), and a visible run history (per-run: scans completed, leads promoted, submissions attempted/confirmed, parked blockers).
2. Run history rows are derived ONLY from the append-only telemetry log — never hand-written, never inferred. A run that did nothing says so ("0 promotions this run — supply dry" is a valid, honest row).
3. Launch limits are visible and user-settable within operator bounds (see G-8): max submissions/run, max/day, cadence. Operator caps stay operator-set; user settings can only tighten, never exceed.
4. Pausing stops work within one cadence tick; resuming restarts from queued state without re-running completed work (attempt IDs are stable and deduplicated — one durable attempt, F22).

**Integrity gates (🔒):**
- History-accuracy test: N fabricated-telemetry replay runs produce history rows matching the telemetry exactly, including a zero-activity run and a run with ambiguous submissions.
- No-double-run test: pause/resume mid-run cannot double-launch or double-count an attempt (F22 identity holds).
- UI shows provenance for every number (hover/click → source event), or labels it UNMEASURED.

**Acceptance checklist:**
- [ ] User can see, pause, resume, and re-limit their run object without operator help.
- [ ] 10 replay scenarios: history rows == telemetry, zero mismatch.
- [ ] Fresh user unaided (T2 cohort): can start, configure, and interpret their first run within the 15-minute window.

**Dependencies:** B-04 (attempt identity), B-20 observers (readiness/readiness diagnostics behind adapter), CI-05 guided first-run spec.

---

## G-2 — Designed execution escape hatch (Build B-23) 🔒

**Objective:** A user-facing assisted-completion workflow for unsupported routes — the "execution escape hatch" — instead of silent operator handling or a dead end when automation cannot proceed.

**Non-goals:** Auto-solving CAPTCHAs (bypass strategy stays; no solver without Trent's explicit re-approval). Full-autopilot scope changes.

**Functional requirements:**
1. When a lead's route is unsupported (no API lane, browser lane blocked by CAPTCHA/verification the policy won't auto-handle, attestations outside pre-authorized keys), the run object shows a single, honest state: "Needs your help — [one-sentence reason]" with a bounded assisted-completion action (e.g. "Finish in browser" guided handoff, "Answer one question" tray entry, or "Skip this route" with the reason recorded).
2. The escape hatch never manufactures evidence: an assisted completion records WHO completed WHAT step (user vs automation), and downstream receipts/provenance reflect that split. A submission the user finished by hand is labeled user-completed; it never masquerades as an automated verified completion.
3. Trent-only blockers (no-AI attestations, travel commitments, essays, references) route to the tray with existing parking semantics — the escape hatch does not re-open settled policy.

**Integrity gates (🔒):**
- Attribution test: every completion path (auto / assisted / user-only) emits the correct provenance label end-to-end through receipts and bundles.
- No-silent-dead-end test: any lead that leaves READY for an unsupported route lands in exactly one visible state (escape hatch / tray / skipped-with-reason) — none disappear from the run object.
- CAPTCHA policy unchanged: a second failed auto-attempt parks rather than escalating (standing rule).

**Acceptance checklist:**
- [ ] Unsupported-route lead produces a guided, bounded action the user can complete in one sitting.
- [ ] Downstream receipts carry the correct automation-vs-user attribution on 10/10 replay scenarios.
- [ ] Zero silent dead ends on a 200-lead unsupported-route stress run.

**Dependencies:** B-21 (sidecar diagnostics, lowest), tray surface (G-3/B-24), standing attestation policy.

---

## G-3 — Productized blocker inbox (Build B-24) 🔒

**Objective:** Turn the Tray from operator tooling into a first-class second-user product surface: one decision inbox with answer bank, provenance, recurrence, and retro-resolution — visible, one-tap, and honest about what the machine learned.

**Non-goals:** Changing what parks (policy stands); re-litigating Trent-only boundaries.

**Functional requirements:**
1. One inbox: every blocker (essay, attestation, missing fact, pursue/drop) appears once with: the exact question, why it blocked (gate cited), where it recurs (which leads), and provenance of any proposed answer (his-words vs machine-suggested, with scope).
2. Answer bank writes from the inbox carry provenance: user-confirmed (his words, banked, applies forward) vs user-rejected vs user-edited. Retro-resolution: answering one question retro-resolves every parked lead waiting on that question, and the inbox shows the count before/after ("answered → 7 leads unblocked").
3. Recurrence view: questions that repeat across leads are surfaced as patterns ("6 leads asked about Salesforce — answered once, applied to all"), feeding the compound-growth answers loop.
4. The inbox never fabricates: a suggested answer the user has not confirmed stays labeled SUGGESTED — never banked, never submitted.

**Integrity gates (🔒):**
- Provenance test: banked answers trace to the exact user message that confirmed them (Trent: nothing in the bank without his words).
- Retro-resolution test: answering one shared question unblocks exactly the set of leads blocked on it — no more, no fewer — on replay.
- Never-suggests-as-banked test: suggested-but-unconfirmed answers cannot appear in packets (answer_resolver abstains).

**Acceptance checklist:**
- [ ] Inbox shows blockers with gate citations, recurrence, and provenance on a live 50-blocker sample.
- [ ] One answer retro-resolves its full blocked set; count shown honestly.
- [ ] T2 pilot: 8/10 users clear a blocker unaided within the first-run window.

**Dependencies:** Answer bank schema (B-20 answers.py adapter prerequisites: question_hash / expires_at / scope / source_ref / attestation_key), F34 resolver, tray cron watermark conventions.

---

## G-4 — Application-to-interview continuity (Build B-26) 🔒

**Objective:** Keel no longer ends at submission. A post-submission tracker surface owns the application → interview funnel with honest denominators, tying into the mature 30/60-day cohort measurements (T5).

**Non-goals:** Auto-claiming interview outcomes; coaching products (Jobright's Orion layer is a funded-expansion analog, not a launch requirement).

**Functional requirements:**
1. Every confirmed submission enters a per-user tracker with lifecycle states: SUBMITTED → (REJECTED | WITHDRAWN | INTERVIEW-SCHEDULED | OFFER | HIRED | EXPIRED-UNKNOWN). State changes come from: user-tap (with timestamp), observed signals (rejection email via connected mailbox, only with user authorization), or cohort reconciliation. Nothing advances on vendor claims or inference.
2. EXPIRED-UNKNOWN is a first-class terminal-ish state: submissions older than the cohort window (30/60 days, T5) with no observed outcome are reported as unknown-with-denominator, never dropped, never counted as successes.
3. Tracker feeds the CI-04 interview-rate cohort pipeline: denominators, maturity windows, uncertainty bands. The user sees their own funnel (applications → responses → interviews) with the same honest labels the cohorts use.

**Integrity gates (🔒):**
- Denominator test: every submitted attempt appears in exactly one tracker state; totals reconcile against the ledger on replay (no missing, no double-counted).
- No-inference test: a fabricated "congratulations on your interview" signal cannot advance state without the authorized evidence path.
- Consent test: mailbox-based outcome signals are disabled until the user authorizes the connection; unauthorized reads fail closed.

**Acceptance checklist:**
- [ ] Tracker states reconcile 1:1 with the ledger on a 500-submission replay.
- [ ] EXPIRED-UNKNOWN cohort displays with denominator + uncertainty; no successes claimed from unknowns.
- [ ] Privacy-counsel review clears the outcome-ingestion data handling (G1 gate).

**Dependencies:** CI-04 (cohort design), B-05 (immutable bundles — the tracker references bundle IDs, not copies), mailbox connector authorization flow, G1 privacy-counsel sign-off.

---

## G-5 — Attempt-level dollar metering (Pipeline Performance PP-11) 🔒

**Objective:** Publish real dollars per verified eligible completion (T3) — the cost story Keel can charge on — built from attempt-level metering with stable attempt IDs.

**Non-goals:** Setting prices (G1 gates any price/offer). Claiming a margin story before measurement.

**Functional requirements:**
1. Every attempt carries cost attribution: browser time/compute, network, failed attempts, human recovery taps, infra share — against the stable attempt ID (F22 identity). Composite internal units (CU/HU/seconds) are instrumented alongside, but the published figure is dollars.
2. The metering pipeline is test-gated: a replay with known costs produces per-attempt dollar figures within a documented tolerance band; tolerance and method are published with the numbers.
3. Publication format (T3): dollars per verified eligible completion, with: the eligibility definition used, denominator, maturity window, and what is EXCLUDED (e.g. Trent's own time, third-party mailbox inference). Comparable vendor checkout totals (FS-2, Trent-gated) contextualize but never anchor the claim.

**Integrity gates (🔒):**
- Known-cost replay test: fabricated run with audited costs → metered dollars match within tolerance, 3 consecutive replays.
- No-double-count test: an attempt retried across transports (API→browser failover) costs its attempts separately but its completion is counted once (F22).
- Nothing publishes until T3's evidence bar is met; all intermediate figures are labeled UNMEASURED or INTERNAL.

**Acceptance checklist:**
- [ ] First published dollar figure for one cohort, with definition/denominator/exclusions, reviewed by Pipeline Performance and Program Office.
- [ ] Metering survives the adversarial re-verification posture (B-01/B-02): costs attach to verified receipts, not caller flags.
- [ ] Comparator context (FS-2) obtained only after Trent authorizes; never scraped via gray-area routes.

**Dependencies:** B-04 (attempt identity), B-14 (existing cost instrumentation), B-02 (receipt contract — costs attach to verified receipts), T3 definition in performance-targets.md.

---

## Prioritization

| Tier | Gaps | Rationale |
|---|---|---|
| Core launch requirements | G-1, G-2, G-3, G-8 | Without a visible run object, escape hatch, blocker inbox, and user volume controls, Keel is operator tooling with users attached — not a product. |
| Post-integrity usability | G-4, G-5, G-6, G-7, G-10 | Submission-continuity, cost story, corpus contract, outcome feedback, privacy claim — these win the market once integrity + launch requirements are solid. |
| Funded strategic expansion | G-9 | Distribution (coach/API/white-label) stays deferred until integrity, READY reliability, and demand evidence support it. |

## Queue mapping

| Gap | Queue item | Department |
|---|---|---|
| G-1 | B-22 | Build |
| G-2 | B-23 | Build |
| G-3 | B-24 | Build |
| G-4 | B-26 | Build (+ CI-04) |
| G-5 | PP-11 | Pipeline Performance |
| G-6 | CI-10 | Competitive Intel |
| G-7 | PP-12 | Pipeline Performance |
| G-8 | B-25 | Build |
| G-9 | LG-09 | Launch & Growth |
| G-10 | CI-11 | Competitive Intel |
