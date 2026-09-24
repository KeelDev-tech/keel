# KEEL BLOCKER RESOLUTION — Evidence Report
**Date:** 2026-09-18 · **Directive:** Keel Blocker Resolution Directive (Trent-authorized 2026-09-18)
**Coordinator session:** 874b6605-9d48-42a3-b98a-8aab78652736 · **8 parallel workstreams, all complete**

## Ten independent booleans (each reported on its own)

| # | Status | Value |
|---|--------|-------|
| 1 | RECEIVED | **true** — directive received, parsed into 8 workstreams |
| 2 | VERIFIED | **true** — every workstream's tests re-run green by the coordinator; B→C integration wiring verified live |
| 3 | IMPLEMENTED | **true** — all buildable machinery for §§1–9 implemented |
| 4 | INTEGRATED | **true** — privacy controller's real `verify_decision` injected into the publication state machine; verified publish still refuses while disabled; sibling file boundaries respected (keel/security/ owned by sibling except `blocker_resolution.json`) |
| 5 | TESTED | **true** — 274/274 green (details below) |
| 6 | SHADOW_VALIDATED | **false** — shadow pipeline built and tested on a labeled-synthetic fixture; real shadow validation requires Trent's genuine decision, which does not exist |
| 7 | HUMAN_DECISION_REQUIRED | **true** — Trent's genuine decision is the only path to APPROVE/REJECT; nothing infers it |
| 8 | PRIVACY_G1_REQUIRED | **true** — G1 unresolved; no counsel authorization exists; authority registry ships empty (fail-closed) |
| 9 | PUBLICATION_AUTHORIZED | **false** — publication DISABLED by construction while G1 is unresolved |
| 10 | DEPLOYED | **false** — nothing deployed externally; `execution_authorized=false` |

## Tests (coordinator re-ran every suite)

| Workstream | Suite | Run | Passed | Failed | Skipped |
|---|---|---|---|---|---|
| A api-direct | keel/tests/test_api_direct_policy.py (pytest) | 19 | 19 | 0 | 0 |
| B privacy G1 | keel/privacy/tests (unittest) | 91 | 91 | 0 | 0 |
| C publication | keel/publish/tests (unittest) | 32 | 32 | 0 | 0 |
| D vercel/funding | keel/tests/test_vercel_funding_state.py (pytest) | 12 | 12 | 0 | 0 |
| E canary | keel/canary/tests (unittest) | 38 | 38 | 0 | 0 |
| F review+shadow | keel/shadow/tests/run_tests.py | 48 | 48 | 0 | 0 |
| G scaling gates | keel/promotion/tests (unittest) | 34 | 34 | 0 | 0 |
| H policy JSON | inline validation | 1 | 1 | 0 | 0 |
| **TOTAL** | | **274** | **274** | **0** | **0** |

Note: G's suite fails under naive discovery from `~/workspace` (test file does `from promotion.gates import`); it is green when run from `~/workspace/keel` as documented by its workstream. Not a code defect.

## Files changed / new modules (all under ~/workspace/keel/)

- **A** `engines/api_direct_policy.py` (new), `engines/ats_matrix.py` + `engines/ats_matrix.json` (flags true→false, retirement provenance), `engines/submit_intent.py` (refuses api/api-direct intents), `tests/test_api_direct_policy.py` (new), backup `hidden_files/_backup-20260918-api-direct-retirement/`
- **B** `privacy/` (new): inventory, classifier, lineage, derivative_analysis, release_manifest, counsel_packet, counsel_decision, publication_guard, canary_adapter, _ed25519 (RFC 8032-tested), __init__, REVIEWER_GUIDE.md, tests/ (8 files)
- **C** `publish/` (new): state_machine.py, tests/test_state_machine.py, README.md
- **D** `funding/funding-tracker.json` (new), `funding/README.md`, `funding/vercel_oss/` (10 materials), `tests/test_vercel_funding_state.py` (new), backup note
- **E** `canary/` (new): capture.py, validate.py, run_canary_001.py, evidence/keel-canary-001/{bundle,verdict}.json, tests/
- **F** `review/` (new): packet.py, decision.py, KEEL-CANARY-001-review-packet.json; `shadow/` (new): pipeline.py, tests/
- **G** `promotion/` (new): gates.py, tests/test_gates.py
- **H** `security/policy/rules/blocker_resolution.json` (new; the ONLY keel/security/ file this directive owns — sibling owns the rest)

## Module hashes (sha256)

- engines/api_direct_policy.py: `549cacb3347c45b061e18f4b48956e0613eb4816513967bcb812030c174b4aca`
- security/policy/rules/blocker_resolution.json: `4b97c2fb97a23bf9ada3aaf50234dff73e3fc02c2180b01ce15449320efcd570`
- publish/state_machine.py: `06d3074e27ae5d6fb89c9376ba3cb0139a13d8a0386247965f69be1cc5e2e20f`
- canary/capture.py: `c443c1bfc680235f9b15a82f13bb05ceb1ee696bb71a5ac053014faa445581cc`
- canary/validate.py: `286aaba36b44486a8c26d9ea83e497f2bee6b19f863516eb590bbe1047065443`
- review/packet.py: `b4624ea7e4f94e6ef8532be0f9db37cb270577bca325a0529ba9282979719123`
- review/decision.py: `7ebf112e13e33078a73862e5c7365995582e55cf446a5dd45e8524cb56d73323`
- shadow/pipeline.py: `fe01d40be6d422e5c020fea255b9793f454c30cfee23863242b6871a4cf14f3f`
- promotion/gates.py: `012ec741f5c99f5883724dc5385c1d40c619babcf5c18f93178d8df18deecf39`

## API-direct write paths remaining

**0 in-tree** (enumerated by `WRITE_ENTRY_POINTS`, proven denied by `assert_all_write_routes_denied()` + 19 tests). Historical telemetry/outcome history/evidence preserved, never reinterpreted; UNKNOWN stays UNKNOWN.
**Residual (flagged, not in scope of the keel tree):** the actual HTTP POST layer (`api_submit.py` / `greenhouse_direct.py`) lives in the **private execution layer outside `~/workspace/keel`** — its dispatch point must consult the new policy before any external write. The sibling owning the private layer should apply the same gate.

## External-egress paths remaining

- Inside the privacy controller: **0 bypass** (zero network/subprocess imports; controller cannot exfiltrate).
- Pre-existing paths that **currently bypass the controller** (pre-date it; integration wiring needed — sibling C / coordinator scope): `keel/marketing-engine/integrations/instagram-graph/client.py`, `threads/client.py`, `keel/engines/*` HTTP clients (`lever_submit.py`, `apply_loop.py`, `ats_discovery/fetcher.py`), `keel/distribution/playbooks/`, `keel/marketing-engine/telemetry/`. Nothing outside `keel/privacy/` references the guard yet.

## Authentic source families captured (KEEL-CANARY-001)

Target: Amazon SDE I, FinOps FP&A, Job ID 10432823 — `https://www.amazon.jobs/en/jobs/10432823/software-development-engineer-i-finops-fp-a` (employer's own careers page, HTTP 200, observe-only, one public GET).
- **Captured: 1 of 6 — TARGET** (posting URL/ID/title, content sha256, 40,605 bytes, lossy text sample, method labeled)
- **Explicit UNKNOWN: 5 — POLICY, FORM, ANSWERS, ATTACHMENTS, ROUTE** (no public source exists without a real applicant flow; `mark_unknown()` only, never filled)
- **Verdict: BLOCKED** — 7/13 cross-record checks failed; CLI exit 1. UNKNOWN was never converted to PASS.
- Evidence: `keel/canary/evidence/keel-canary-001/bundle.json` + `verdict.json`; sealed review packet `keel/review/KEEL-CANARY-001-review-packet.json` = **BLOCKED-PENDING-EVIDENCE**.

## Genuine approval requests: **0** · Genuine decisions: **0**

`issue_decision_request()` refuses blocked packets; `record_decision()` requires an open request + named human actor + explicit authority reference + reviewed-sha256 binding. No genuine decision exists → HUMAN_DECISION_REQUIRED. Shadow: `execution_authorized=false`; consent=HUMAN_DECISION_REQUIRED; source completeness never authorizes execution.

## G1 / publication / canary status

- **G1: UNRESOLVED** — no counsel authorization exists; decisions require Ed25519 signatures against an out-of-band-registered reviewer key (private key never on this machine); self-grant impossible without detection.
- **Publication: DISABLED** — 8-state machine, no jumps, freeze-before-packet, mutation → PRIVACY_REVIEW_READY, byte-identical publish only; unreachable while G1 unresolved.
- **Canary: BLOCKED** (honest). Promotion stages 1→5→10→50→100→fleet implemented with six zero-tolerance invariants (fabricated_provenance, synthetic_to_live, unauthorized_egress, false_human_approval, unknown_to_pass_without_evidence, wrong_target_execution); any nonzero → BLOCK.

## Remaining UNKNOWNs

1. Five canary families (policy/form/answers/attachments/route) — require a real applicant flow (Trent's explicit go + account)
2. VERCEL_STARTUPS eligibility facts (legal entity, founding date, funding raised, Vercel account status)
3. Platform metrics (stars/forks/downloads/users) — explicit UNKNOWN in community_metrics.json
4. GitHub repo public visibility (origin `github.com/KeelDev-tech/keel.git` configured; not verifiable from this environment)
5. Code-of-conduct enforcement contact gap (marked GAP with remediation in code_of_conduct_check.md)

## Remaining blockers

1. **Trent's genuine decision** on KEEL-CANARY-001 (APPROVE/REJECT) — the only path past BLOCKED-PENDING-EVIDENCE
2. **Counsel authority registration** — real counsel engagement (public key + authority reference); ceremony in `privacy/REVIEWER_GUIDE.md`
3. **Execution-authorization grant UX** — deliberately not built; coordinator owns `review/execution-authorizations.jsonl` (`keel.execution_authorization.v1`)
4. Private-layer API-direct dispatch gating (residual write-path note above)
5. Egress-path integration wiring for marketing/engines paths (sibling C scope)
6. Next Vercel OSS window + Trent's explicit authorization before any submission; Vercel Startups eligibility verification

## FIM / SOC note (disciplined deviation, documented)

All directive changes live under `~/workspace/keel/`, which is **outside FIM watched roots** (watch covers `job-pipeline/engines` etc.) — the directive made **zero FIM-watched changes**. A post-work `fim.py --check` shows 3 modified watched files drifted during the window from other lanes: `dev-support/incidents/INCIDENTS.md` (sanctioned SOC self-appends, 23:30Z), `job-pipeline/engines/application-executor/technique_library.json` (charter learning-loop writes, 23:29Z), `.pytest_cache` churn. Per the standing rule (never re-baseline an unverified diff), the coordinator **deliberately withheld `fim.py --init`** rather than launder unrelated drift into the baseline. The Shield adjudication lane owns that decision. SOC change events for all 8 workstreams were logged before integration completed.

## Workstream file map

A `keel/engines/api_direct_policy.py`, `keel/tests/test_api_direct_policy.py` ·
B `keel/privacy/` (12 modules + 8 test files) ·
C `keel/publish/state_machine.py` (+ tests) ·
D `keel/funding/` (tracker + 10 vercel_oss materials + tests) ·
E `keel/canary/` (capture/validate/run_canary_001 + evidence + tests) ·
F `keel/review/` + `keel/shadow/` (+ tests) ·
G `keel/promotion/gates.py` (+ tests) ·
H `keel/security/policy/rules/blocker_resolution.json`

---
DONE MEANS EVIDENCE. This report is the evidence: 274/274 tests green, integration wiring verified live, and every false-positive avoided — no G1 synthesized, no decision inferred, no UNKNOWN converted, nothing deployed.
