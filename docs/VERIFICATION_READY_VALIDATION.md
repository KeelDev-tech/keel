# Verification and READY validation — 2026-09-30

The maintained source candidate passed its complete extracted verification path:
**301 unique tests across 11 modules, with zero failures, errors, or skips**.
The original full checkout is not a green release: 44 original core failures
remain, and private subsystem dependencies are unavailable. No production
rollout, current launchability, or recovered live supply is claimed.

## Measured local results

These checks ran on Linux with Python 3.12.14 and pinned development tools.
The extracted run used fresh synthetic HOME/KEEL_HOME paths with inherited
source imports removed. The eight installation/profile tests are included in
the 301-test maintained set; the rows below overlap and must not be summed.

| Check | Passed | Failed | Errors | Skipped | Interpretation |
| --- | ---: | ---: | ---: | ---: | --- |
| Maintained extracted profile | 301 | 0 | 0 | 0 | All 11 shipped modules pass |
| Original core baseline | 5,932 | 50 | 0 | 23 | Original commit below |
| Repaired core | 6,231 | 44 | 0 | 23 | Zero new failing testcase identities; six original failures resolved |
| Extracted installation/profile | 8 | 0 | 0 | 0 | Wrappers, isolated CLI, dependencies and offline walkthrough |
| Security | 316 | 0 | 1 | 1 | Missing public `security.secrets.broker`; incomplete |
| Privacy | 94 | 0 | 0 | 5 | Passing collected cases; five unqualified skips |
| Monitors directory | 0 | 0 | 0 | 0 | No tests collected, exit 5; not a pass |

Baseline: `326532ea250c3e00e7924ad175e2fb744b3104d9`. Core runs used the same
pinned interpreter, synthetic workspace configuration and import mode. An old
security assertion was aligned with the canonical `submission_claimed` event:
this outcome writer records a claim, while employer acceptance requires its
separate receipt projection. Submission authority checks remain in place.

The six resolved core failures cover the portable verification entry point,
canonical claim-event naming, and deterministic synthetic-event classification.
The retained core failures are:

| Original test group | Failures | Unresolved scope |
| --- | ---: | --- |
| Monitor audit | 10 | Legacy/private monitor API mismatch |
| Packet dependency shadow | 3 | Private retained snapshots/reports absent |
| API-direct pulse snapshots | 11 | Retired/private counter-interface expectations |
| Funding state | 12 | Private subsystem source excluded from checkout |
| Vendor dispatch regressions | 8 | Private application-executor source absent |

Exact testcase identities and counts are recorded in
[VERIFICATION_READY_VALIDATION.json](VERIFICATION_READY_VALIDATION.json).

## Reproduce the maintained path

Install `requirements-dev.txt` in a disposable development interpreter, then
run the complete 11-file command in [RELEASE_PROFILE.md](RELEASE_PROFILE.md).
The shipped `.github/workflows/recovery-profile.yml` configures the same path
and source-package integrity checks on Python 3.11 and 3.12. Configuration is
not evidence that hosted CI ran; only Python 3.12 was measured here.

For the broader checkout comparison:

```bash
python3 tools/run_tests.py --allow-unguarded --suite core --suite local_profile --suite security --suite privacy --suite monitors --report-dir /tmp/keel-review-unique-path
```

The report path must not exist. The separately referenced audit guard is
missing from this checkout, so broad suites refuse by default. These reviewed
local runs explicitly used `--allow-unguarded`; they do not prove audit-hook
installation, enforcement, or OS isolation. Earlier baseline reports that
called the absent hook inherited are interpreted only as unguarded test
outcomes.

## Qualified behavior and remaining integration

Process-kill and randomized queue tests exercise journal recovery and snapshot
conflict refusal. Transport cohorts preserve previous evidence through NONE
attempts, stop on rate limits, and do not invent READY promotions. Admission
checks reconcile all four homes, current policy, ledger history, fit, ownership,
answer scope, materials and packet integrity. Preparation packets cannot confer
execution authority, and staging does not become exported approval.

Cross-file recovery protects cooperating queue-lock users; lock-free readers
can observe individual replacements. Runtime task providers must use exact,
bounded authoritative lookups. Hashes detect changed inputs and do not prove
applicant assertions or governance authority.

The public checkout has no canonical live Muse queues, task-state adapter,
`flow_attempt` runtime, or complete private material/form/receipt integration.
Compare this candidate with the canonical live revision, checkpoint the actual
state, and rehearse a bounded cohort without submission before claiming live
recovery. Unknown launchability stays unknown, and the main fit floor stays 75.
