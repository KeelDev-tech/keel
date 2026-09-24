# Validation: 0.3.2-supply-review.1

The 0.3.1 baseline was reproduced at **262 passed**. The supply recovery
candidate passes **312 tests: zero failures, errors or skips**, including all
262 existing cases and 50 new regressions. Results are in
`audit/supply-baseline-pytest.*` and `audit/supply-candidate-pytest.*`.

The existing network-disabled test runner and fixture-write restrictions remain
unchanged. The in-process guard is not an OS security boundary. Tests exercise
synthetic observations, local files and fake logger failures. No live ATS request,
queue mutation, consent reply, browser task or production deployment occurred.

Coverage includes the reported `(5,0)` regression; a grid of counts and throttle
boundaries; unchanged floor/cadence/spawn constants; a shape-checked diff that
preserves other synthetic guardian decisions; append failure and preservation of
old telemetry bytes; starvation before the unchanged-file pulse shortcut; stale
health events; malformed observations; structural holds and conflicting evidence;
fit/consent/rate-limit/dedupe holds; duplicate IDs; cooldown arithmetic, timestamps,
expiry and policy-output mismatches; and conservative question grouping without
answer values, consent reuse or false resolution.

The live guardian's seven-test file is absent. The supplied synthetic guardian
fixture faithfully reproduces the report's early return, but passing it is not
passing that unavailable suite. The private structural/jitter functions, 793
leads, answer bank and 34 unclassified questions were not provided. Therefore
no live audit result or recovered-lead count can be reported. The private EWMA
consumer and real scheduler integration remain unverified. Browser work is
explicitly outside this incident's scope.

```bash
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -r requirements-dev.txt
python3 tools/run_tests.py --report-dir /tmp/keel-supply-tests
```

This run used Python 3.12.14 on Linux with pytest
8.4.2. Dependencies were installed only in scratch test tooling; no production
account, subscription or runtime dependency was added. The original 0.3.1
validation history is retained in `docs/VALIDATION_0.3.1.md` and its existing
`audit/` files. Its 61-finding register is historical and is not represented as a
new comprehensive audit in this targeted revision.
