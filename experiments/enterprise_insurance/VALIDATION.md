# Validation evidence — October 9, 2026

## Scope and source

Candidate for existing draft PR #146, based on
`df76e86f347aca7ded4370387e068f1b577dd101`. Only the experiment directory
and its standalone unittest file are changed. No job runtime, SRF bridge,
private state, scheduler, workflow configuration or production deployment is changed.

## Observed local checks

- Linux / Python **3.13.5**; standard-library-only execution with `-S -B`.
- **77 unittest test methods passed**, no skips, 0 failures/errors.
- One test exhaustively checks **729** yes/no/unknown scoring combinations
  against a separately expressed deterministic policy oracle. This is rule
  consistency testing, not held-out accuracy or evidence authenticity testing.
- Eight concurrent identical intakes create exactly one account. Eight
  competing revisions with one old hash accept exactly one new version.
- Injected transaction interruption rolls back the whole evaluation batch.
  An actual child-process exit during a SQLite write leaves a journal and
  the next command refuses operation pending operator review; automatic crash
  recovery is **not** implemented or claimed.
- Read-only expiry reports do not rewrite database bytes. Logical purge is
  tested but is not secure storage erasure.
- Demo covers 9 unique fixtures: 1 simulated pilot candidate, 1 qualified,
  5 parked, 1 rejected, 1 suppressed. One duplicate is not counted again;
  one extra-field record is rejected before storage. No real leads exist.
- Tests exercise default disablement, path/link/permission checks, applicant
  source refusal, allowlist validation, input limits, evidence staleness and
  conflicts, review leases, pilot expiry, pause/suppression, caps, audit and
  projection corruption, HTML escaping/CSP and CLI demo/report behavior.
- Fixture demo completed with Python socket creation patched to raise.
  Static import checks find no job-engine/network/CRM imports. This does not
  establish an OS egress sandbox or qualify arbitrary future code.

## Matched 200-account timing

Same generated cohort, rules and outcome counts, fresh roots for both modes.
200 accounts -> 160 qualified / 40 parked / 601 accepted-mutation events.

| Intake mode | Intake seconds | Qualification seconds | Read-only report seconds |
| --- | ---: | ---: | ---: |
| Batches of 100 | 0.0465 | 0.1199 | 0.0467 |
| Individual transactions | 2.9521 | 0.1221 | 0.0405 |

## 1,000-account bounded rehearsal

Batched intake **0.8143 s**; qualification **2.0018 s**; read-only report
**0.2066 s**. Exactly **800 qualified / 200 parked / 3,001 events**.
10 nonempty evaluation batches plus one empty completion call. These are
one local synthetic run's timings, not service levels or production-scale
claims. Full-history verification remains linear per transaction.

## Reproduction

```bash
python3 -S -B -W error::ResourceWarning -m unittest discover -s tests -p test_enterprise_insurance_lane.py -v
python3 -S -B experiments/enterprise_insurance/benchmark.py --count 200 --compare-individual
python3 -S -B experiments/enterprise_insurance/benchmark.py --count 1000
```

## Not observed / not qualified

The full repository could not be cloned into this execution environment
because its network could not resolve GitHub. Relevant source and project
contracts were inspected through the connected GitHub tools. Consequently
**the complete existing Keel regression suite was not run locally**.
Python 3.11 and 3.12 interpreters were unavailable; syntax parsing for both
versions passed, but their runtime behavior and remote CI are not claimed.
The existing CI unittest-discovery step will collect this new test file.

A Chromium screenshot attempt timed out; visual browser verification was
not completed. HTML generation/escaping and the CLI report are tested.

No real source acquisition, source permission verification, legal review,
buyer interview, CRM integration, production host qualification, held-out
lead-quality assessment, conversion or revenue was tested. Human identities
and pilot permissions are fixtures, not authenticated real approvals.

## Exact executed source hashes

- `experiments/enterprise_insurance/pipeline.py` SHA-256: `f8dbbdd12ab6c438f2db15d5622d03656cf15d920d3e7cde8f5a33b632321a52`
- `tests/test_enterprise_insurance_lane.py` SHA-256: `f4441b6e76d7df7f18f02ef3719d0682197e6ce398f49c2edb9bbc3be55d1a58`
- `experiments/enterprise_insurance/benchmark.py` SHA-256: `dcd85289bc15e5fa42d97b37ab82edbb535a85effe0b6fa5b76d0675007b57fb`
