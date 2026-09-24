# Keel: local remediation build

Built 17 September 2026. **Code and local tests delivered; not deployed to the live MUSE pipeline.**

The included tools need Python 3.10+ and an existing Linux/POSIX machine. They
use the Python standard library. No new service subscription, paid API, model
download, employer API key, or third-party Python package is required. Existing
compute, hosting, network access, and operator time still apply.

## What is included

- Five incremental patches: lock lifecycle, read-only preflight, structured guard
  messages, durable packet/browser handoff requests, list-valued parking notes,
  configurable queue locks, fsync writes, and queue-wrapper preservation.
- Local modules: byte-frozen bundles, scoped answer resolution, shadow readiness,
  dependency invalidation, adaptive refill, buffer/scheduler diagnostics, an
  idempotent preparation outbox, public job discovery, and honest meter coverage.
- Regression tests, exact patch hashes, reproduction evidence, examples, and a
  row-by-row status map for all 100 table entries in the supplied register.

## Try the code without touching a pipeline

Run these from the extracted package directory:

```bash
python3 -S run_tests.py
python3 -S -m keel_local readiness examples/readiness.json --now 2026-09-17T12:00:00Z
python3 -S -m keel_local refill examples/refill.json
python3 -S -m keel_local discover --board synthetic_fixture --fixture examples/greenhouse.json --observed-at 2026-09-17T12:00:00Z
```

`-S` excludes installed site-packages and demonstrates that these tests need
no third-party dependencies. The example data is synthetic and dated; it is not
the current application queue. An executable result in the readiness report is
a shadow classification of supplied observations, never authority to submit.

Read `MUSE_HANDOFF.md` for staging the engine patches and running their tests.
The current live source is newer than the recovered review source, so MUSE must
port the small changes onto its authoritative repository. The staging tool
refuses a base-hash mismatch instead of overwriting newer work.

## What remains open

This delivery does not replace the live F18 writer, introduce a second submission
ledger, deploy the rejected gateway, clear UNKNOWN attempts, authorize employer
attestations, or supply unavailable provider receipt evidence. Authenticated
approval issuance, trusted provider receipts, whole-workflow transactionality,
production isolation, the independent-user journey, and measured production
reliability remain open. See `REMEDIATION_STATUS.md` for exact boundaries.

The optional SQLite file is marked **diagnostic storage only**. It accepts
preparation/review/measurement events; it rejects submission outcomes and refuses
to adopt an existing live database. Its local restoration drill is not a backup
or recovery claim about the production pipeline.

Public Greenhouse discovery uses its documented unauthenticated GET route.
It only stages postings. It never POSTs applications, follows redirects, handles
CAPTCHAs, or converts an HTTP success into proof of application arrival.

The deterministic fallback removes paid-model dependence from these routing and
validation functions. It does not replace the quality of an unavailable GPT
red-team/polish service, and it does not unlock API quota.

## Evidence

`evidence/current/summary.json` and `tests.log` record this package's actual run.
`evidence/prior-build-reproduction.json` records the independently rerun earlier
344-test selection from the recovered ZIP. That is reproduction of its test
claim, not independent security approval or proof of production correctness.

The original 537-file source tree, runtime files, answer bank, credentials, and
operational queues are excluded from this delivery. Patches contain only the
needed changed source and context. The original register is preserved unchanged.

Documentation used for the implementation is listed in `SOURCES.md`.
