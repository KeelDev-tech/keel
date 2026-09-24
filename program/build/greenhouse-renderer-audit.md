# Audit: Greenhouse employer-hosted renderer path (Stripe/Databricks) — viable or dead?

**Auditor:** Keel Build (read-only audit, pulse 321 ARM 2 follow-up) · **Date:** 2026-09-17 ~13:00 PDT
**Scope:** read-only. No browser spawns, no routing changes, no queue/ledger/telemetry/code writes.
**Verdict: the api-direct HTTP route is DEAD (structural); the postings are LIVE and the browser route is VIABLE (3 confirmed submissions today). The burn is real but it is attempt-cycle waste + browser-lane second-wall parks, not a dead posting set.**

## 1. The events

40 `gate_blocked` events today (2026-09-17) with an identical signature — 36 Stripe, 4 Databricks:

- `event_type: gate_blocked`, `details.gate: technique_blocked`, `source: api_submit`, `ats: greenhouse`
- `details.reason`: `"employer-hosted Greenhouse renderer: canonical board page redirects to the employer's careers page (no apply form over HTTP) — route to the browser application path"`
- Time range: 2026-09-17T11:47:55Z → 2026-09-17T18:48:16Z (04:47–11:48 PDT). The pulse's "13×" was the count at pulse time; the burst grew through the day.
- 36/36 Stripe events share the byte-identical reason string; the 4 Databricks events match it too (roles: `ATS8-GREENHOUSE-DATABRICKS-STRATEGIC-HUNTER-ACCOUNT-EXECUTIVE-20260915-J8737167002`, `...-STRATEGIC-CORE-...-J8191741002`, `...-STARTUP-HUNTER-...-J8438936002`, `...-DNB-GEO-HUNTER-...-J8644183002`).
- Contrast with 2026-09-15's 2 Stripe technique_blocked events — a different signature (`source: record_outcome`, technique "live-option click + per-field verify", WhatsApp-consent genuine input blockers). The renderer burst is new today.

Sample role IDs: `ATS8-GREENHOUSE-STRIPE-ACCOUNT-EXECUTIVE-PRODUCT-TERMINAL-20260915-J8144513`, `ATS8-GREENHOUSE-STRIPE-CUSTOMER-SUCCESS-MANAGER-20260915-J8170772`, `ATS8-GREENHOUSE-STRIPE-STARTUP-PARTNERSHIPS-Y-COMBINATOR-20260915-J8181026` (full list: 36 role IDs in `/tmp/renderer_roles.txt` on the audit machine).

## 2. The code path

**Detection** — `engines/application-executor/api_direct_detect.py` (ARM 70, 2026-09-17): `detect()` fetches the canonical `job-boards.greenhouse.io/<board>/jobs/<id>` page; if it 302-redirects off `*.greenhouse.io` to the employer's careers page, verdict is `candidate=False, renderer="employer-hosted"` with the machine-matchable reason. Verified on the burst: canonical fetched 173KB/699KB of employer careers HTML with **zero** fingerprint/submitPath markers. Wired into `verify_retry.py` (promotion + release paths) and `apply_loop.py` — but only the **boolean** `api_direct_candidate` is persisted to the queue entry (`is_api_direct_candidate()` wrapper); the `renderer` detail is discarded. Zero of the 36 queue entries carry any renderer marker.

**Emission (attempt-time)** — `engines/application-executor/api_submit.py` ~line 1063–1072 (J-20260917-0229-gate-1105): inside the "live re-verify + fetch the real form" gate, when `fetch_posting` raises `fingerprint/submitPath not found` and the URL carries `gh_jid=`, it returns `_refuse(..., "technique_blocked", "employer-hosted Greenhouse renderer: ...")`. The comment is explicit: *"The posting is NOT dead (verify_retry resolves these as LIVE) — api-direct HTTP just cannot submit here"* and *"Superseded by J-20260917-0052-ats--1069 when its embed-endpoint fallback ships (then submissions succeed and this branch stops firing)."*

So the block fires at **attempt time** — after verify → READY promotion → packet build → launch → api_submit invocation — not at routing time.

## 3. Viability analysis

**Api-direct HTTP route: DEAD, structural.** 40/40 blocks share one signature; the mechanism is structural (off-board 302, no form markers over HTTP — confirmed on full page fetches, not a transient fetch failure). Zero Stripe submissions via api-direct in 3 days of telemetry (2026-09-15 → 17). This is not flakiness; no retry will change it.

**Postings: LIVE. Browser route: VIABLE.** 3 of the 36 Stripe roles were **submitted via the browser lane today** (16:13–16:29 UTC), all with `evidence_proof_type: confirmation_text` (real provider confirmation text):
1. `ATS8-GREENHOUSE-STRIPE-CUSTOMER-SUCCESS-MANAGER-20260915-J8170772` — "confirmation page received, no code gate, first-click submit" (source: browser-task, 16:13:57Z)
2. `ATS8-GREENHOUSE-STRIPE-ACCOUNT-EXECUTIVE-PRODUCT-PAYOUTS-20260915-J8196259` — "Thank you for submitting your application to Stripe. …" (source: record_outcome, 16:20:37Z)
3. `ATS8-GREENHOUSE-STRIPE-STARTUP-PARTNERSHIPS-Y-COMBINATOR-20260915-J8181026` — "Thank you for applying. Thank you for submitting your application to Stripe." (technique: browser, 16:29:03Z)

verify_retry resolves these postings as LIVE. The renderer path is dead only as an HTTP route — the jobs accept applications through the rendered form.

## 4. Slot-burn quantification

1. **Api-direct attempt cycles:** 40 attempt-time blocks today, each after full launch investment (verify → READY → packet → launch → api_submit). Adjacent evidence of lock churn on the same cohort: the 09:18 PDT api-direct-loop run acquired launch locks on Stripe roles then refused them all as `fastlane_browser`, leaving stale 2h locks (`~/workspace/AGENTS.md` refill note). The ARM 70 pre-detection exists but its verdict is not persisted, so enforcement stays at attempt time.
2. **Browser lane:** 26 `browser_launched` → 20 `browser_task_started`/`form_started` → **3 submitted** (15% of launched; 8.3% of the 36). 18 roles' latest telemetry event is `form_started` with no subsequent outcome event — outcome unknown from telemetry alone (needs the main-chat browser-lane check; do not infer stall from this alone).
3. **Second wall (browser lane):** 33 `gate_blocked`/`needs_input` events (source: verify-retry) — "Employer form pattern for Stripe with no form intel in packet to rule it out — needs Trent's explicit [input]". The browser lane works where the form is mappable (3 proofs above) but parks where form intel is missing.

**Current disposition of the 36 Stripe roles (standard queue):** 3 SUBMITTED · 25 PARKED-NEEDS-INPUT (form-pattern intel gap) · 5 BLOCKED-FABRICATION-GATE (genuine: e.g. education dropdown with no truthful option — "DO NOT select a university he did not attend") · 3 PARKED-LOW-FIT. All 36 now show `api_direct_candidate=False`.

## 5. Proposal (no action taken — for Pipeline Performance / main chat)

- **P1 — Persist the renderer verdict at verify time (near-term, highest ROI):** store `api_direct_detect.detect()`'s full verdict (`renderer`, reason) on the queue entry, not just the boolean. Transport selection then routes `employer-hosted` leads straight to the browser lane without entering the api-direct attempt path — eliminating the 40 attempt-time blocks/day and their lock churn. Note: this changes routing *efficiency* within existing lanes; per the audit's constraints it stays a proposal for Pipeline Performance sign-off, not an action.
- **P2 — J-20260917-0052-ats--1069 embed-endpoint fallback (structural fix):** the code comment's own supersession plan. Needs: per-employer embed-endpoint contract discovery, implementation, tests, Trent approval for the new transport. When it ships, the technique_blocked branch stops firing and api-direct becomes viable for these boards again. Owned by Build when scheduled.
- **P3 — Stripe form intel (unparks 25):** build form intel for stripe.com careers pages from the 3 successful submissions' field maps, so the browser lane stops parking on "no form intel in packet". Respects the no-AI/unaided-work boundary (the WhatsApp-consent items stay genuine Trent-input).
- **P4 — fastlane_browser re-evaluation:** stays a PROPOSAL until the integrity cluster clears, per direction. Current behavior already routes to browser; the open question is browser-first (skip api-direct attempt entirely) for renderer-flagged leads vs attempt-then-fallback.

## 6. Data discrepancy note

`~/workspace/keel-transfer/conflict-review.md` (§1) describes the api-direct transport as "the lane that submitted 5 live Stripe applications tonight" as the rationale against the kill switch. Telemetry 2026-09-15 → 17 shows **0** Stripe submissions via api-direct and **3** via the browser lane. The kill-switch rejection stands on its own merits (unconditional raise with no gateway to wire to), but the "5 Stripe via api-direct" premise is not supported by the telemetry reviewed here — it may reflect a different window or counting method. Flagging so the record is accurate.

## 7. Limits of this audit

- Read-only: no live fetches were performed to re-verify the 302 behavior; the structural claim rests on the ARM 70 verification note in code (173KB/699KB fetches, zero markers) plus 40/40 identical block signatures.
- 18 roles sit at `form_started` with no later telemetry — their browser outcomes are unknown from telemetry alone; the main-chat browser lane owns that check.
- `/tmp/renderer_roles.txt` (audit-machine-local) holds the 36 Stripe role IDs; regenerate from `telemetry/events.jsonl` if needed.

---
*Audit only. No code, queue, telemetry, or routing changes made. Proposed actions (P1–P4) require Pipeline Performance sign-off and/or Trent approval before implementation.*
