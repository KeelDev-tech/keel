# Keel 0.17.0 intake verdict — 2026-09-19

## Provenance
- Files received twice 2026-09-19 (with and without `__1` suffix), byte-identical.
  - Transfer SHA-256: `8318e50cf4fef7aaa665f1e170cb19217b3f20fba71f0e786729891ec71f40c9`
  - Handoff SHA-256: `c7e1e97687a9755705d00dd691ae22337ffd066f8d88345fa1a9be6f412385f3`
  - Release evidence: `8c18e139bf69ffc31d2febe9c0465e8293874d64cf82c09dfc6d721eabce31fe`
  - Test evidence: `5e71dc945c02bc7668ccc38bc91ad70132c3ad219d4353031d7bccfb5b2ba74e`
- Transfer verified (`--verify-only`: VERIFIED, 799 files, publisher unauthenticated,
  `execution_authorized=false`, `production_deployed=false`), extracted ONLY to
  `~/workspace/keel-transfers/keel-0.17.0`. No live deployment, no source replacement.
- Handoff self-reports: 4,652 passed, 0 failures/errors, 13 skipped; gate INCOMPLETE
  (13 unavailable cases); `live_host_qualified=false`; one unresolved SQLite
  `readonly database` incident (production-readiness blocker).

## Reading scope (honest)
Read in full: handoff, docs/INTEGRATION_017.md, docs/INTEGRATION_PORTS.md,
docs/INTEGRATION_HOST.md, docs/INTEGRATION_LEGACY_WORKERS.md,
keel_integration/controller.py, keel_integration/journal.py,
keel_integration/legacy.py, engines/legacy_action_boundary.py,
qualification.py (structure + head), tests/test_integration_adversarial.py (head),
CHANGELOG.md, embedded change manifest.
Characterized (docstrings + entry points): keel_integration/evidence.py,
keel_composition/controller.py, keel_operator/commission.py, keel_operator/intake.py.
NOT claimed: all 799 source files individually read.

## Independent validation
- Fixture host rehearsal reproduced: `tools/check_integration_host.py
  --fixture-home ...` -> `FIXTURE_PASS`, `execution_authorized=false`,
  `submission_authorized=false`, exit 0. Matches the handoff's documented behavior.
- Targeted integration suite (6 files) run in the isolated 0.17 tree:
  **153 passed, 0 failures/errors, in 773s (12:53)** — completed 2026-09-19
  ~05:06 PDT in this environment (pytest 9.1.1, SQLite 3.45.1, vs handoff's
  pytest 8.4.2 / SQLite 3.53.1). Independently corroborates the candidate's
  integration-layer claims for those files. Not a production-readiness claim:
  still synthetic, no real browser/model/host, and the SQLite readonly incident
  plus 13 skipped gate cases stand.
- Local environment differs from handoff's (pytest 9.1.1 vs 8.4.2; SQLite 3.45.1 vs 3.53.1).

## Ported (proven, tested, live-behavior-preserving)
1. **Strict boolean provider opt-in** in
   `job-pipeline/engines/application-executor/triage_llm.py` — from the 0.17
   legacy-worker hardening ("missing, malformed, null, false, string, and numeric
   settings cannot enable a provider").
   - `enabled()`, `gemini_enabled()`, and `compare.enabled` now require an
     explicit JSON boolean `true` (`is True`); non-dict sections fail closed.
   - Retired the legacy "missing config == GPT enabled" default.
   - Live config uses strict booleans, so live behavior is unchanged (verified:
     GPT enabled, Gemini enabled, compare logging active).
   - Regression tests: 3 new (truthy impostors `"false"`/`"true"`/`1`/`None`,
     missing keys, non-dict sections). Suites: 12/12 and 13/13 green.

## Evaluated, NOT ported (with rationale)
1. **DispatchJournal / ExecutionController / qualification / evidence packages**
   (the 101 added files). Deep candidate-internal: require keel_muse, keel_live,
   keel_operational, keel_agent, keel_sources — none exist in the live tree. The
   unresolved SQLite `readonly` incident plus 13 skipped gate cases make wholesale
   adoption unsound. Live pipeline equivalents already exist and are battle-tested:
   one-lead-one-queue + launch locks (vs `_scope_available`), JSON append-only
   telemetry with no-secret rules (vs `_public()` stripping), submit_intent
   INTENT/UNKNOWN barriers, repair-inflight fail-closed guards (vs UNKNOWN semantics).
2. **Static I/O call-site inventory** (INTEGRATION_LEGACY_CALLS.json pattern: 41
   call sites, per-file SHA pins, drift-fails). Real pattern, but overlaps existing
   live controls (worker-charter constraints + child-allowlist audit hook +
   cartographer assumption sweeps). Defer; revisit if a bypass incident occurs.
3. **14-check host-qualification protocol** (make_binding/build_provider, fixture
   rehearsal, model-probe measurements). Genuine design value for the Keel self-host
   track, but requires Trent's real host (hardware, licensed weights, Playwright)
   — speculative until the host exists. Recorded as a design reference, not code.
4. **legacy_action_boundary.py permanent-disable pattern.** Our `mark_inflight`
   is live-essential (browser-lane claim mechanism); the candidate disables its
   own because its architecture differs. The underlying principle (markers !=
   action evidence) is already enforced here via repair-inflight guards.

## Open items
- 0.17 targeted integration suite result still pending; record when it lands.
- The unresolved SQLite `readonly database` incident remains a blocker for any
  production-readiness claim by the candidate.
- Candidate gate remains INCOMPLETE (13 skipped) by its own reporting.
