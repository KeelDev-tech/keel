# Reconciliation design: gateway/store transfer vs the live evidence chain

**Department:** Keel Build · **Author:** Build department lead · **Date:** 2026-09-17
**Status:** DESIGN ONLY — no code changed, nothing shipped. Every phase below is gated on green tests before the next begins (TESTED VALUE ONLY).
**Charter item:** Keel Build scope §3 — the gateway/store architecture question.

## 1. The question

The code transfer (`~/workspace/keel-transfer/keel-transfer/`, SHA-256 verified, conflict-reviewed 2026-09-17 ~11:30 PDT in `conflict-review.md`) contains two held items:

1. **`keel_legacy_hold.require_gateway()`** — a 17-line new module whose only behavior is raising `GatewayRequired`. The patch inserts it into `api_submit.py`, `apply_loop.py`, and `greenhouse_direct.py`. Applied, it kills the live api-direct transport — the lane that submitted 5 real applications on 2026-09-17. The patch's stated path forward ("wire the Keel gateway") does not exist.
2. **`keel_core/{contracts,gateway,measurement,readiness,store}.py`** — a parallel trust architecture: a separate SQLite `Store` positioned as the submission authority (`record_outcome` rewrite commits via `verified_store.accept_receipt(...)` and, per its own comment, "Legacy JSON/telemetry are not written"), an unwired gateway, answer contracts duplicating live `answer_resolver`, a readiness evaluator, and measurement helpers.

The transfer package's own `NEXT_STEPS.md` declares **"Production status: NOT DEPLOYED / NO-GO for cutover from this package,"** and its handoff rule 6 forbids turning fixture signatures into production evidence or faking provider application IDs.

**Decision (this design):** adopt the transfer's *ideas* as augmentations to the live system. The forbidden cutover — replacing the live F17/F18/F20/F22 evidence chain with the held modules — stays forbidden.

## 2. Non-negotiable constraints

From the charter and standing rules:

- **C1.** `keel_legacy_hold.require_gateway()` and `keel_core.store.Store` must NOT replace the live F17/F18/F20/F22 evidence chain.
- **C2.** The API-direct kill switch stays out. No unconditional `GatewayRequired` raise in any live path.
- **C3.** No fabricated receipts — every submission claim cites provider-correlated evidence; the reconciler never synthesizes evidence.
- **C4.** UNKNOWN reconciles before retries — an ambiguous attempt is never auto-retried or failed over to another channel.
- **C5.** The receipt-writer redesign stays shadow-only (Gemini adversarial FAIL, EV-1) until its hold clears. This design does not depend on it; the CONFIRMED_SUBMITTED path uses the live F18 gate.
- **C6.** PUSH ON VALUE + TESTED VALUE ONLY: each phase ships only with green tests; phases are sequential.
- **C7.** Browser lane is main-chat only. Build never spawns, steers, or manages browser tasks; browser dispatch-boundary work is *proposed* to main chat, never implemented from a side chat.
- **C8.** Engineering changes touching live queue behavior are coordinated with Keel Pipeline Performance before shipping.

## 3. The live authority (what must not be replaced)

The live evidence chain, all in `~/workspace/job-pipeline/engines/application-executor/`:

| Component | Role | Finding |
|---|---|---|
| `submission_outcome.py` — `classify_attempt()` | ONE decision point for "did this HTTP attempt prove a submission?" SUBMITTED only with provider/attempt-correlated receipt; UNKNOWN opens a durable hold (append-only JSONL, attempt_id-keyed); FAILED for refusals/pre-POST transport failures. Correlated receipts count once (duplicate=True on re-seen). | F17 |
| `submit_intent.py` | Durable pre-submit intent. State machine INTENT → UNKNOWN → SUBMITTED&#124;FAILED; terminal states immutable. While open for a role_id, the API lane refuses new attempts and the browser lane refuses to launch. `reconcile_unknown` takes an injected probe and NEVER issues a POST or launches a browser. Store: `hidden_files/submit-intents.json` (atomic tmp+os.replace, fcntl-serialized). | F22 |
| `record_outcome.py` — final writer | `record(..., "submitted", ...)` requires structured `evidence={confirmation_text\|provider_receipt\|receipt_id\|contract}`; unproved claims are REFUSED before any mutation. T1 `verify_receipt_against_store()` ties receipt_id / contract+attempt_id to non-duplicate SUBMITTED rows; fabricated IDs and double-counts are refused. Enforced by `test_record_outcome_evidence_gate.py`. Downstream consumers (digest, expansion log, technique library, downtime analytics) read the legacy JSON/telemetry submitted rows. | F18 |
| `api_submit.py` `_live_submit` | F20 exact-byte re-hash vs approved bundle; F17 duplicate-receipt check before recording. | F17/F20 |
| `answer_resolver` (input_resolution) + `brief_builder.vet_extra_answers` | F34 context-aware answer resolution: provenance, source, employer/role scope, expiry, revocation. | F34 |
| Cherry-picked 2026-09-17 (regression-tracked, §9) | `log_event.py` recursive secret scrubber; MUSE `gates.py` disclosure-text + media_path gates; `blocker.py` employer exact-scope (prefix inference removed); `preferences.py` expiry boundary + exact scope + source-required. | F18/F34 partial |

Known documented limits of the live chain (carried into the design, not hidden): the F17 confirmation-page receipt is structural, not cryptographically bound to the POST bytes — the realistic failure direction is a false UNKNOWN, which is safe; terminal UNKNOWNs need provider-side reconciliation (this design's §5.3).

## 4. Why each held item stays held

| Held item | Verdict | Reason |
|---|---|---|
| `require_gateway()` inserts (§§1,2,4 of conflict-review) | REJECTED as cutover | Unconditional raise kills the live api-direct lane. The gateway it demands does not exist. |
| `keel_core.store.Store` as submission authority + `record_outcome` rewrite | REJECTED as cutover | Two competing receipt-integrity architectures cannot coexist in `record()`; the rewrite breaks live callers (`greenhouse_direct.py:707-714` proven-submission recording, `apply_loop.py:4427`, `worker-charter/ingest_envelope.py:180-184`), the record_outcome CLI, `test_record_outcome_evidence_gate.py`, and silently stops all legacy submitted writes the downstream consumers read. The standing decision bars a separate SQLite authority without reconciliation. |
| `keel_core.contracts.resolve_answer` | SUPERSEDED | Live `answer_resolver` (F34) is newer and wired. |
| `keel_core.readiness` evaluator | DEFERRED to Pipeline Performance | Readiness is their lane (PROGRAM.md); a second evaluator would be a duplicate authority. Build specifies only the interface the evidence chain exposes (§5.5). |
| Brief policy-language rewrite | REJECTED | Revokes Trent's standing authorizations (account creation, CAPTCHA auto-solve, EEO handling, market-midpoint compensation) from every generated brief. |
| `verify_retry` hunks | SUPERSEDED | Live RP01 (2026-09-17) implements the canonical band policy more robustly. (F21 RP01 — no work.) |
| `keel_core.measurement` helpers | ADOPTABLE later | Useful, but meters remain unconnected; adopt as read-side helpers only, after phases 1–3. No authority claim. |

## 5. The reconciliation design

**Guiding principle: one authority, many corroborators.** The live chain (§3) remains the SOLE authority for "submitted" claims. Everything below *augments* it — as new read-side or pre-dispatch modules — and no new module may write, replace, or bypass a submission outcome.

### 5.1 D1 — Receipt archive (additive, content-addressed)

Adopts the *receipt-integrity idea* from `keel_core.store` minus its authority claim, and implements step 1 of the Submission Integrity Review's sequence.

- New append-only, content-addressed archive of raw receipt artifacts. Each record: `{sha256(raw_artifact), raw_artifact, attempt_id, canonical_identity, provider/board, candidate_ref, payload_digest, attachment_digests, authority_reference (which verifier captured it), captured_at, source}`.
- **Write path runs AFTER the live F17/F18 decisions, never before.** It records what the live chain decided and the evidence it cited. Archive writes never change an outcome; archive misses never block a live decision (fail-open on read — the gate still decides).
- Gives the live T1 correlation reads (`verify_receipt_against_store`) a durable, tamper-evident backing, and makes the live docstring's promise real: "every SUBMITTED is auditable post-facto against the provider."
- Lives with the operational state (`hidden_files/`), retained as the review doc requires — deleting it loses duplicate protection.
- **Tests (gate for Phase 2):** round-trip write/read; digest-mismatch refusal on read; `attempt_id` join with `submit_intent` records; no import of live writers (no cycles); a chaos test proving an archive outage changes zero live verdicts.

### 5.2 D2 — Gateway as interface, not kill switch

Adopts the *explicit-boundary idea* from `keel_core.gateway` minus the raise.

- Define a narrow gateway *interface* — a policy checkpoint the transports call — whose **only shipped implementation is `LivePolicyGateway`, a pass-through adapter over the current live gates**: F17 `classify_attempt`, the F18 final-writer gate, F20 byte-hash, F22 intent/UNKNOWN state, D1 caps, standing authorizations. It delegates every decision to the live code and returns its verdicts.
- The seam exists so Phase-4 provider-correlated receipt adapters have somewhere to plug in. Nothing more.
- **Hard invariant, test-enforced:** no code path in the live tree imports or calls `keel_legacy_hold.require_gateway`. A grep-test fails the build if `GatewayRequired` appears anywhere outside `~/workspace/keel-transfer/`. A decision-parity suite runs the existing fixture corpus through both the direct live path and the gateway path and asserts identical verdicts.
- **Tests (gate for Phase 3):** parity suite green; assert-no-raise in all live paths; adapter-delegates-everything (no independent policy logic in the adapter).

### 5.3 D3 — UNKNOWN reconciler (evidence-cited, transactional, authenticated)

The charter's core ask: **UNKNOWN reconciles before retries.** Builds on live `submit_intent.reconcile_unknown` (already probe-injected, never POSTs, never launches a browser).

- **Canonical application identity.** Dedupe key becomes `(provider/board, canonical job id, candidate)` instead of the mutable `role_id`; `role_id → canonical_id` mapping is resolved at reconcile time. Cross-role duplicates for the same logical job are detected, not re-attempted. (F22 closure criterion: "canonical cross-role identity".)
- **Evidence-cited resolutions.** Every resolution records `{attempt_id, canonical_id, verdict, evidence_digests[], probe_name, decided_by, decided_at, reason}`. No verdict without cited evidence — **no fabricated receipts, ever** (C3).
- **Exactly three outcomes:**
  - `CONFIRMED_SUBMITTED` — only with provider-correlated receipt evidence. Recorded through the **live** F18 final-writer gate with `provider_receipt` evidence. The reconciler never writes a submission itself.
  - `CONFIRMED_NOT_SUBMITTED` — only with *positive* evidence of non-submission (verified pre-POST refusal, or authoritative provider-side absence). Local absence and generic NOT_FOUND never prove provider absence. The hold releases and the lead returns via canonical `verify_retry` — never a direct queue write.
  - `REMAINS_UNKNOWN` — hold stays; re-probe later. The default. Safe direction.
- **Transactional resolve + enqueue-once.** Resolving an attempt and enqueueing its outcome event is one atomic step; replayed resolutions are idempotent (second call sees the terminal state, records "already resolved", emits nothing — never a duplicate event).
- **Authenticated decision.** Until the measured pilot passes, any `CONFIRMED_SUBMITTED` requires Trent/trusted-verifier sign-off; every decision retains evidence + reason (per the review doc's reconciliation requirements).
- **Failure-injection suite:** crash before/after handoff, delayed receipts, replayed outcome events, concurrent reconciliations (fcntl-serialized), corrupt journal → hold (fail closed).
- **Tests (gate for Phase 4):** the injection suite green; a dry-run mode that resolves nothing but reports what *would* resolve, reviewed before any live resolution.

### 5.4 D4 — Dispatch-time frozen-bundle check (F20/F21/F34)

Adopts the transfer's "exact submission bundle" idea as a *dispatch-time check inside the live path*, not as a separate contracts module.

- Immediately before handoff (API `_live_submit` and the browser submit proposal), verify: attachment bytes SHA-256 == approved bundle hash (extend the live F20 re-hash to the exact bytes about to be sent — no rereading paths, no field mutation), answer values == approved values with scope/expiry/authority intact (F34 enforcement point; `answer_resolver` remains the reader).
- Mismatch → refuse dispatch, park with a typed blocker. Never silent, never auto-corrected.
- **Tests:** byte-flip fixtures refused; answer-scope/expiry violations refused; approval-binding positive cases pass.

### 5.5 D5 — Readiness interface (no duplicate authority)

The evidence chain exposes `attempt_state(canonical_id)` → `{INTENT|UNKNOWN|SUBMITTED|FAILED, attempt_id, evidence_digests}`. Pipeline Performance's shared READY contract is the sole consumer-facing readiness authority; Build builds no second evaluator. Interface spec is delivered to Pipeline Performance for their sign-off (C8).

## 6. Explicit non-goals (the forbidden cutover, enumerated)

1. No `require_gateway()` raise in any live transport path — the api-direct lane keeps running.
2. No `keel_core.store.Store` (or any new SQLite store) as a submission authority; no second writer of submission truth.
3. No `record_outcome` rewrite; the F18 structured-evidence gate + T1 correlation stay canonical.
4. No brief policy-language rewrite; standing authorizations stay in every brief.
5. No `verify_retry` hunks; RP01 stands.
6. No automatic UNKNOWN → retry/fallback on any channel, ever.
7. No dependency on the shadow receipt-writer redesign until EV-1 clears.
8. No new submission-affecting behavior without Pipeline Performance sign-off where queue behavior is touched, and no browser-side changes from a side chat (C7).

## 7. Phasing and test gates

| Phase | Work | Queue-behavior touch? | Gate to next phase |
|---|---|---|---|
| **0 — Hold the line** (now) | Ratify §3 as the authority; transfer package stays in `~/workspace/keel-transfer/`; `keel_legacy_hold.py` / `keel_core/` are NOT copied into the live tree. | None | This document accepted |
| **1 — Receipt archive** | §5.1 additive archive; read-side only. | None (notify Pipeline Performance) | §5.1 tests green |
| **2 — Gateway interface** | §5.2 `LivePolicyGateway` pass-through; parity suite; assert-no-raise grep-test. | None (notify Pipeline Performance) | §5.2 tests green |
| **3 — UNKNOWN reconciler** | §5.3 canonical identity, evidence-cited resolutions, transactional resolve+enqueue-once, injection suite; dry-run mode first. | Yes — holds/UNKNOWN/READY | §5.3 tests green **+ Pipeline Performance sign-off**; live resolutions need Trent/trusted-verifier sign-off until pilot passes |
| **4 — Provider receipt adapters** | One adapter per route, plugged into the §5.2 seam; each with a measured pilot. | Per-adapter | Per-adapter: pilot metrics + Trent approval. Only here does the gateway gain real provider adapters |

`keel_core.measurement` helpers may be adopted as read-side metering after Phase 3, with no authority claim.

## 8. P0 mapping

| Finding | Current live state | This design moves it to… | Closed when |
|---|---|---|---|
| F17 unsupported success evidence | Partial (API paths) | D1 archive backs T1 correlation; D2 seam ready for adapters | Provider-specific evidence contract per route (Phase 4) |
| F18 final writer bypass | Text gate + hold + T1 (live) | D2 preserves final-writer authority; D1 makes receipts auditable | Trusted verifier attests captures; provenance closed |
| F20 attachment binding | Byte re-hash in `_live_submit` | D4 dispatch-time frozen-bundle check | Exact approved request enforced at every dispatch |
| F21 approval binding | RP01 superseded live | — (no work) | — |
| F22 uncertain result → replay | Intent journal + UNKNOWN holds (live) | D3 reconciler + canonical identity + injection suite | Failure-injection coverage green; browser dispatch boundary covered (proposed to main chat) |
| F34 answer scope | `answer_resolver` + vetting (live) | D4 dispatch-time enforcement | Scope/expiry/authority preserved through dispatch |

## 9. Regression health anchors (the four cherry-picks)

Standing Build responsibility per the charter. Watch suites and known caveats:

- `log_event.py` scrub() — log-event test suite (27 green at ship). Anchor: recursive scrub of nested dicts/lists; expanded alias set.
- MUSE `gates.py` disclosure/media_path — MUSE adversarial suite (25 green at ship). Anchor: `disclosure` text in claim discovery; `(has_media or media_path)` gate.
- `blocker.py` employer exact-scope — input-resolution suite (80 green at ship). Anchor: prefix inference removed; does not re-break the 2026-09-17 tray-answer compounding repair (different function, `prescreen.py`).
- `preferences.py` tightening + test fix — same suite. **Known caveats (conflict-review §9):** exact matching has no legal-suffix stripping (unlike prescreen's repair — minor matcher inconsistency, accepted); source-required rule ignores sourceless prefs — verify no live writer creates sourceless prefs before any future change here.

Any regression in these suites blocks Phase 1–4 work until green.

## 10. Open decisions for Trent

1. **Phase-4 adapter pilots:** per-route approval and pilot scope (which boards first, how many live applications, success criteria).
2. **Authenticated-decision threshold:** who signs `CONFIRMED_SUBMITTED` during the pilot — Trent per case, or a named trusted verifier?
3. **Receipt archive location:** recommend `hidden_files/` alongside `submit-intents.json` (retained with operational state, per the review doc). Confirm.
4. **Browser dispatch boundary:** durable intent before browser submit (review step 2) is proposed to main chat when D3 is green — Build will not implement it from a side chat.

---
*Design only. No code changed, no behavior changed, nothing shipped. Transfer package untouched at `~/workspace/keel-transfer/keel-transfer/`. Conflict analysis: `~/workspace/keel-transfer/conflict-review.md`.*
