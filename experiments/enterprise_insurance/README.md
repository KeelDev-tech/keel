# Enterprise qualification lane: executable feasibility laboratory

Status: **offline synthetic prototype; disabled by default; not deployed**.
Namespace: `enterprise_insurance_v0`. No business relationship is implied.
Public examples use anonymous agencies and reserved `.example` domains. Keep real
enterprise targets and interview correspondence outside this public repository.

## What this tests

A separate B2B account pipeline inspired by Keel's evidence-first discipline:

`intake -> deduplication -> evidence review -> qualification / parked -> simulated human review -> simulated pilot candidate`

This tests **qualifying organizations as possible enterprise clients**, not
finding consumers who will buy life insurance. Neither a recruiting interview
nor an agency's interest in hiring an agent establishes demand for this product.
The buyer-discovery and go/no-go plan is in [PILOT_PLAN.md](PILOT_PLAN.md).

## Implemented

- Strict allowlisted records, reserved domains, synthetic-only source permissions,
  bounded JSON including duplicate-key rejection, and explicit timestamps.
- Atomic intake batches of 1-100 records, at most 1 MiB; 1,000-account workspace cap.
  A conflict rolls back the entire batch. Exact replays do not reset decisions.
- Six evidence criteria with transparent 0-100 scoring. Threshold 75 is an
  experimental policy, not Keel's job fit model or a conversion probability.
  Missing, unknown, conflicting or future evidence parks; required negative
  identity/sector/need evidence cannot be offset by a high total score.
- Bounded evaluation, stable terminal decisions, explicit evidence revisions,
  exact-hash review leases, mock pilot permission expiry and persistent pause.
- SQLite transactions plus hash-chained accepted-mutation events; packet and
  state-projection consistency verified before each operation.
- Read-only JSON and HTML reports recheck expiry at read time. No web server,
  tracking, remote assets, executable report inputs or automatic browser launch.
- Suppression and logical expiry purge remain available while paused. Logical
  purge removes the current packet/domain, retains minimal tombstones/hashes,
  and is **not** secure erasure of SQLite pages, backups or other copies.
- Standard-library runtime, no paid APIs, subscriptions, credentials or models.

## Run from the repository root

Use Python 3.11+ on POSIX; the observed local runtime was Linux/Python 3.13.5.
A syntax parse is not a 3.11/3.12 runtime pass. See [VALIDATION.md](VALIDATION.md).
Choose a disposable synthetic workspace **outside** the source and applicant roots.
The parent must already exist; `init` and `demo` require a new final directory.

```bash
python3 -S -B -m unittest discover -s tests -p test_enterprise_insurance_lane.py -v

# Intentional opt-in to this synthetic CLI only. There is no live-enabled mode.
export KEEL_ENTERPRISE_INSURANCE_ENABLED=true
LAB_PARENT="$(mktemp -d)"
python3 -S -B experiments/enterprise_insurance/pipeline.py \
  --home "$LAB_PARENT/lab" --exclude-keel-home "$LAB_PARENT/applicant" demo
python3 -S -B experiments/enterprise_insurance/pipeline.py \
  --home "$LAB_PARENT/lab" --exclude-keel-home "$LAB_PARENT/applicant" report
python3 -S -B experiments/enterprise_insurance/pipeline.py \
  --home "$LAB_PARENT/lab" --exclude-keel-home "$LAB_PARENT/applicant" report --format html

python3 -S -B experiments/enterprise_insurance/benchmark.py --count 200 --compare-individual
python3 -S -B experiments/enterprise_insurance/benchmark.py --count 1000
```

When a real `KEEL_HOME` is configured, it is additionally excluded; it is never
used as this lane's storage root. Supply the actual applicant root to
`--exclude-keel-home` on a commissioned host. Do not manufacture an alternate
root to bypass overlap checks. The existing job runtime is not modified.

Commands: `init`, `demo`, `ingest --packet FILE`, `ingest-batch --packet FILE`,
`revise --packet FILE --expected-hash HASH`, `run --limit 25`,
`review --account ID --expected-hash HASH --decision approve --reviewer synthetic-reviewer-a`,
`pilot --packet FILE`, `suppress --account ID`, `pause`, `resume`, `purge`,
`report [--format html]`, `request-external --action email`.

Input for `ingest-batch` is a JSON array; the rest take objects. Fixtures are
constructed by `fixture()` or the `demo()` implementation. CLI failures return 2.
`request-external` always returns `allowed=false` and exit 2, including after
simulated approval. `benchmark.py` explicitly opts in only to its disposable
fixture workspace; it does not enable a service or alter configuration.

## Boundaries and limitations

**Real records are deliberately unsupported**, even when a caller claims company
permission. Applicant folders, emails, resumes, ATS queues, consumer records and
credentials are not inputs. Allowlisted shapes and synthetic labels are not a
DLP classifier or proof that someone did not disguise real facts as fixtures.
Do not relabel real records to bypass the test-only contract.

Human-review identities and permission references are **simulated**, not signed
or independently authenticated. Hashes verify consistency, not evidence truth,
source rights or legal compliance. A same-user attacker can rewrite the whole
local database and chain. This is not an immutable ledger, tenant boundary,
privilege boundary, egress sandbox or production security certification.

Path, ownership, mode and link checks reduce accidental overlap and unsafe file
reuse; they are not race-proof against a malicious same-user host. No encryption,
OS-enforced network isolation, multi-tenant ACLs or authenticated host bridge is
installed. Use only trusted local code and disposable synthetic storage.

An interrupted SQLite transaction leaving a journal fails closed with
`SIDECAR_REQUIRES_OPERATOR_REVIEW`. No automatic repair is attempted. Preserve
artifacts and investigate in a disposable copy; do not delete a journal or relax
guards to claim recovery. Ordinary exceptions roll back transactionally.

Full history is checked per transaction; bounded batch intake amortizes this
cost but does not make history checks constant-time. Caps are lab limits, not
proven production capacity. A `Lane` instance represents one command-time
snapshot; construct a fresh instance for subsequent real-time commands.

No live source collector, contact enrichment, messages, calls, CRM integration,
insurance quoting/selling, SRF adapter, job-queue adapter, scheduler or deployment
exists. Approval never grants external effects. A real pilot requires separately
reviewed source permissions, business scope, security and legal controls.
