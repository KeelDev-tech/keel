# Keel Build — Build Log

Milestone record for the Build department. Newest first. Program backbone: `~/workspace/keel/program/PROGRAM.md`.

## Standing — Improvement Scorecard (Program Office, weekly; Trent's order 2026-09-17)

Canonical: `~/workspace/keel/program/improvement-scorecard.md`. Metrics: submissions cumulative + per-week, cost per verified submission (trend must go down), limitations burn-down (open/partial/closed), verify promotion rate, READY floor breaches (must trend to zero), learning-loop yield (drafts → triaged → promoted). Week-over-week deltas, no vanity metrics — a metric that isn't improving gets named, not hidden.

| Metric | Week of 2026-09-14 → 09-20 (as of Thu 09-17) |
|---|---|
| Submissions cumulative | ~195 (195-vs-196 ledger semantics unreconciled) |
| Submissions per-week | Tabulating by Fri EOD |
| Cost per verified submission | **Not publishable** — HTTP/tap dimensions unmetered (D-H5). Named, not hidden |
| Limitations | Open P0 41 · P1 21 · P2 31 (93) · cleared-with-tests 14 |
| Verify promotion rate | 65.1% |
| READY floor breaches | Tabulating by Fri EOD (breaches known: READY hit 0, 3, 5, 6) |
| Learning-loop yield | Sieve: 136 → 73 candidates, 45 review, 14 needs-evidence, 4 duplicate, 0 defer; promotions pending Monday evaluator |
| Queue depth (queued / in progress / completed) | Build 15/0/4 · Intel 9/0/3 · Pipeline 10/0/7 · Launch & Growth 7/0/0 (seeded, first status pending) |

First full edition: **Fri 2026-09-18 EOD**. Updated every Monday thereafter for the prior week.

## 2026-09-17 12:58 PDT — ARM 70 seam review: P1 APPROVED with one one-line fix

- Reviewed Pipeline Performance's P1 implementation (`api_direct_detect.stamp_entry`, verify_retry both wiring points, api_direct_loop short-circuit, 6 new tests).
- (a) Fail-closed reason wording approved as-is: the machine-matchable vocabulary is the `renderer` enum (fail-closed stamps "unknown"); reason string is consistent with detect()'s fail-closed family; no exact phrasing exists to reuse for the detection-raised case.
- (b) apply_loop ~3010 confirmed no-change: candidate=True ⟹ renderer="direct" structurally (single True branch); the check reads the fresh apply-time boolean (line 2505), so no-pinning holds on the apply path. Loop short-circuit is the only transport-selection change required. Noted a benign transient: verify-time renderer vs apply-time boolean can disagree if a board recovers between the two — fail-safe both ways, self-heals on next re-stamp.
- One fix required before shipping: register `renderer_employer_hosted` in GATE_TYPES (log_event.py) — emitted as details.gate but unregistered (warn-only today; ARM 123 precedent says register emitted values).
- Verified independently: new suite 6/6 green; short-circuit placement (post-blocklist, pre-guard) kills the 09:18 stale-lock pattern.
- **Update 12:59 PDT: PQ-9 SHIPPED.** GATE_TYPES registration in with the PQ-9 comment; gate emits with no unknown-value warning; 6/6 renderer tests green. P1 fully shipped in Pipeline Performance's lane. The ~48 attempt-time technique_blocked events/day are now short-circuited before guard — no launch lock, no packet, no fastlane screen.

## 2026-09-17 12:57 PDT — Triage sieve tooling SHIPPED (P-2026-09-17-arm3-octopus-1)

- `worker-charter/triage_sieve.py` (~470 lines): deterministic, read-only on `learning-proposals.md`, writes only its digest artifact. Parses draft headers (all four real variants), scores evidence strength (engine-file line refs, role/event IDs, quantified recurrence, telemetry paths), dedupes via fingerprint + title-token Jaccard (≥0.7), recommends PROMOTE-CANDIDATE / REVIEW / NEEDS-EVIDENCE / DUPLICATE-OF-x / DEFER with recorded reasons + one-line evidence summaries. Emits `triage-digest-<date>.md`: ranked top-15, auto-defer shortlist, needs-evidence list, duplicate groups, full ranked appendix.
- **HARD INVARIANT: never promotes** — verified behaviorally (sandbox run: proposals/charter/queue byte-identical) and statically (no other write target in source). Promotions stay human-approved.
- Tests: **18/18 green** (`test_triage_sieve.py`), neighboring charter suites **39/39 green**. All fixtures synthetic; live proposals file never written by tests.
- Live run on the real backlog: **136 pending drafts → 73 PROMOTE-CANDIDATE** (55 evaluator-vetted, awaiting human approval), 45 REVIEW, 14 NEEDS-EVIDENCE, 4 DUPLICATE, 0 DEFER (shortlist honestly empty — fail-closed below the high-precision defer bar). Digest: `worker-charter/triage-digest-2026-09-17.md`.
- Open proposal (not implemented): wire the digest into the weekly evaluator's prompt context — a cron-body touch for the coordinator's call. Ready for Monday's evaluator session.

## 2026-09-17 12:56 PDT — P1 sign-off: renderer-verdict persistence (Pipeline Performance APPROVED with conditions)

- Pipeline Performance approved the renderer-audit P1: persist the full `api_direct_detect` verdict (`renderer`, `reason`, `detected_at`) on queue entries at verify time; employer-hosted routes straight to the browser lane, skipping the api-direct attempt. Attempt-time block count had grown to 48 today at sign-off.
- Conditions: both verify wiring points (`verify_retry.py:2751`, `:2957`), re-detect on every verify (no permanent pinning), tests before shipping (unit + neighboring regressions green — TESTED VALUE ONLY), P2/P3/P4 sequenced, fail-safe error direction (false positive costs a browser slot on a proven lane; false negative reproduces today).
- Division of labor: Pipeline Performance implements in `verify_retry.py` (their engine); Build reviews the ARM 70 seam (verdict shape + transport-selection decision). Browser-lane ownership unchanged (routing lands in main-chat browser lane).
- Record correction: `~/workspace/keel-transfer/conflict-review.md` §1 now carries a dated 2026-09-17 note — the "5 live Stripe applications tonight" premise is not telemetry-supported (0 Stripe api-direct, 3 browser, 09-15→17). Kill-switch rejection stands on its merits (unconditional raise kills the transport as a mechanism regardless).
- Awaiting their diff for ARM 70 seam review.

## 2026-09-17 12:55 PDT — Greenhouse renderer audit complete (pulse 321 ARM 2)

- **Verdict: the api-direct HTTP route is DEAD (structural) for the flagged Stripe postings; the postings are LIVE and the browser route is VIABLE.**
- Evidence: 40 `technique_blocked` events today (36 Stripe + 4 Databricks), byte-identical signature (`source=api_submit`, "employer-hosted Greenhouse renderer: canonical board page redirects to the employer's careers page (no apply form over HTTP)"), 11:47–18:48 UTC. Zero successes on this route in 3 days of telemetry — structural (off-board 302, no form markers over HTTP), not flaky.
- 3 of the 36 Stripe roles submitted via the browser lane today with `confirmation_text` evidence (CSM J8170772, AE-Product-Payouts J8196259, Startup-Partnerships-YC J8181026). Jobs are live; the form submits in a real browser.
- Burn analysis: blocks fire at attempt time in `api_submit.py` (~L1069) after full verify→READY→packet→launch investment, because `api_direct_detect`'s renderer verdict detail is discarded (0/36 queue entries carry it).
- Disposition of the 36: 3 SUBMITTED, 25 PARKED-NEEDS-INPUT ("no form intel in packet"), 5 BLOCKED-FABRICATION-GATE (genuine blockers), 3 PARKED-LOW-FIT. 18 roles' latest telemetry is `form_started` with no later outcome — flagged for main-chat browser-lane check.
- **Discrepancy note:** `conflict-review.md` states api-direct "submitted 5 live Stripe applications tonight"; audit telemetry shows 0 Stripe api-direct submissions 09-15→17. The kill-switch rejection in the reconciliation design stands on its merits (unconditional raise kills the lane regardless), but the "5 submissions" premise is not telemetry-supported — treating it as unverified pending the main-chat lane check.
- Proposals (all gated, no action taken): **P1** persist full `api_direct_detect` verdict (`renderer`, reason) on queue entries at verify time; route employer-hosted straight to browser — needs Pipeline Performance sign-off (routing efficiency change). **P2** embed-endpoint fallback per-employer contract (code comment's own supersession plan) — needs implementation + tests + Trent approval. **P3** build Stripe form intel from the 3 successful submissions' field maps to unpark the 25 (no-AI/unaided-work boundary preserved). **P4** fastlane_browser re-evaluation stays proposal-only until the integrity cluster clears.
- Strictly read-only throughout: no browser spawns, no routing/queue/telemetry/code changes.
- Full audit: `greenhouse-renderer-audit.md` (this dir).

## 2026-09-17 12:54 PDT — Pipeline Performance coordination: bank_sweep conflict check

- Pipeline Performance shipped `engines/application-executor/bank_sweep.py` (bank-write → parked-lead sweeper) + hooks in `tray_answer.main` / `field_question_protocol.ensure_bank_key`. Measured: 126 blockers cleared across 94 leads, READY 3 → 13, 16 tests green.
- Build conflict check: clean. No references to evidence-chain modules (`record_outcome`, `submission_outcome`, `submit_intent`), no references to held transfer items, no touch on cherry-picked paths. F17/F18/F22 chain and Phase-0 hold untouched.
- Watch item logged as PQ-8 (P2) in their fix-queue.md: `prescreen.question_mappable` (looser, legal-suffix stripping) vs `preferences.py` exact matching — failure mode is wasted verification, not integrity breach. Joint test case when either matcher is next touched.

## 2026-09-17 12:11 PDT — Reconciliation design drafted (charter §3)

- `reconciliation-design.md` (this dir). Decision: live F17/F18/F20/F22 evidence chain stays the sole submission authority; transfer ideas adopted as augmentations only (receipt archive, gateway-as-pass-through-interface, UNKNOWN reconciler, dispatch-time frozen-bundle check). Kill switch out, no separate SQLite authority, no fabricated receipts, no auto-retries. Phased 0–4 with test gates; Phase 3 needs Pipeline Performance sign-off.
- Status posted to Program Office.
