# Verified completion cost

The offline report includes spend on failed and unknown tasks as well as all
attempts on successful tasks. It counts each verifier-positive task once.
This complements the submission-only `engines/cost_tracker.py` series; it
neither changes that series nor uses the prototype's placeholder rate card.

Run from a clean checkout with Python 3.11+ and no installed dependencies:

```sh
python -S tools/completion_cost_report.py /path/to/cohort.json
```

The command reads only the named file and writes JSON to stdout. Invalid
input returns exit status 2 and no report. A valid report may contain null
metrics; exit status 0 establishes valid accounting input, not a release gate
or a successful outcome. Reports contain cohort metadata, so keep identifiers
opaque and avoid applicant information before sharing them.

Example input (all values and credits are synthetic):

```json
{
  "schema_version": 1,
  "cohort": "fixture-cohort",
  "window": "fixture-window",
  "outcome_definition": "Synthetic preparation packet passes exact postconditions",
  "unit": "fixture_credits",
  "basis": "trace_estimate",
  "attempts": [
    {"task_id": "a", "attempt_id": "a1", "amount": "2"},
    {"task_id": "a", "attempt_id": "a2", "amount": "3"},
    {"task_id": "b", "attempt_id": "b1", "amount": "7"}
  ],
  "outcomes": [
    {"task_id": "a", "verified": true, "verifier_ref": "fixture:postcondition:a"},
    {"task_id": "b", "verified": false, "verifier_ref": "fixture:postcondition:b"}
  ]
}
```

This reports total spend 12, one completion, and cost per verified completion
12. Spend on the verified task alone is 5; reporting 5 as the overall cost per
completion would omit the failed task's spend. Every additional attempt has
its own unique ID. Outcomes are unique per task; conflicting or duplicated
records fail rather than being merged. Each task must have at least one
attempt, and each attempt must belong to a declared task.

Use `verified: null` for unknown outcomes and `amount: null` for unmetered
attempts. Known true/false outcomes require a verifier reference. Unknown
tasks retain spend in the numerator. Any missing amount suppresses the total
and headline ratio while exposing known spend and the missing count. Empty
cohorts and zero-completion cohorts have no ratio. Explicit measured zero
remains zero. Decimal strings preserve precision; numeric JSON amounts,
negative/nonfinite values, more than 18 fractional places, and values above
1e18 are rejected. Each collection is limited to 10,000 records.

Each report has exactly one cost unit and basis (`trace_estimate`,
`provider_usage`, or `invoice_allocation`). Prepare separate reports for
separate bases, units, windows and postconditions. Invoice allocation must be
supplied by an independently reconciled process; this tool does not fetch
provider usage, allocate invoices, estimate prices, or authenticate evidence.
Do not add the same spend from multiple accounting bases together.

Verifier references and amounts are caller-supplied. References are audit
pointers, not proof of authenticity. Validate actual postconditions in the
trusted task harness before exporting outcomes; model prose, a successful
HTTP response, and READY alone do not prove a completed application. The
report cannot detect omitted attempts or biased cohort selection, grants no
execution authority, and establishes neither production recovery nor credit
savings. No live Muse, model, browser or submission adapter is added.

The supplied evaluation briefs also recommend adversarial state checks and
release acceptance. Those are already being developed in PRs #38 and #39;
this report supplies the remaining bounded accounting component without
introducing another harness or paid platform.
