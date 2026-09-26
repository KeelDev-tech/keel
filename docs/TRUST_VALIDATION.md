# Keel 0.5.0-review.1 local validation

Date: 2026-09-18. Environment: Python 3.12.14 on Linux.

**690 tests passed, with zero failures, errors or skips.**

| Suite | Tests | Scope |
|---|---:|---|
| Main, flow and trust | 559 | All 388 previous tests plus 171 new trust/twin/transfer regressions |
| Maintenance | 131 | Original separate workbench suite; all 59 original files byte-identical |
| Total | 690 | Actual combined runner count; historical runs are not added again |

Command: `python3 -B tools/run_release_checks.py --out /new/checks/path`.
The builder supplied pinned pytest dependencies through PYTHONPATH. No new
runtime library or paid service is required. Other Python/OS versions were not
tested here. The test guards were preserved: the main suite isolates state,
disables network and permits only reviewed child CLIs; the maintenance suite
retains its separate parser guard. They are not OS security sandboxes.

The first transfer test attempted a child TXT invocation that the existing guard
correctly refused. The test now loads the generated trusted extractor in-process;
the guard was not relaxed. Final delivery verification separately runs the actual
TXT as a CLI, checks a clean byte-identical restoration, and reruns both suites
from that restored tree. That rerun does not increase the 690 distinct-test count.

## Included evidence

- `audit/trust-validation.json`, `audit/trust-main-junit.xml`,
  `audit/trust-main-pytest.log`: actual combined/main test results.
- `audit/trust-maintenance-tests.json`, `audit/trust-maintenance-tests.log`:
  current run of the original workbench.
- `audit/trust-provenance.json`: exact 0.4.0 base and preserved maintenance check.
- `audit/trust-changes.json`, `audit/trust-changes.patch`: source diff against
  the 0.4.0 candidate; generated audit outputs are excluded from the patch.
- `audit/trust-source-inventory.json`: syntax/import/hash enumeration, not an
  independent security certification.
- `audit/trust-example-output.json/.md`, `audit/trust-evidence-output.json`:
  actual combined report and evidence CLI outputs.
- `audit/trust-changed-evidence-output.json`: actual report after a synthetic
  source revision change; no canonical state was modified.
- `audit/trust-twin-model.json`, `audit/trust-twin-delay-output.json`,
  `audit/trust-twin-drift-output.json`: actual capture, scenario and drift output.
- `audit/trust-replay-output.json`: six explicit incident expectations passed;
  these are demo replay cases, not six extra tests added to the suite count.

## Demonstrated behavior

The synthetic flow has five nominal READY records but one executable estimate.
That estimate survives valid material bindings. Changing the source revision
invalidates resume, packet and interview dependencies, reduces the executable
estimate to zero, removes affected forecast supply and suppresses misleading
question unlocks. Original input data is unchanged.

The synthetic twin starts with 300 seconds of runway and a first refill at
1200 seconds. A hypothetical 600-second release delay moves refill to 1800
seconds while exhaustion remains at 300. Doubling consumption halves runway;
packet invalidation and unknown attempts reduce readiness; explicit 429 holds
stop discovery; aged exports cannot become current through simulation.
These are fixture results, not measurements of Trent's private queue.

New tests exercise transitive evidence invalidation, conflicting claims,
publisher deduplication, scope/wording/expiry and dependency cycles; integrated
material holds; forged/expired grants, wrong identity/workspace, field overshare,
policy pause, stale/changed resources and prompt text as data; actual logger
concurrency, replay, append failure and consumed grants after handler failure;
review signature binding, disagreement, missing owners and repeated identities;
question bundles, budget conservation, research deduplication and 429 stops;
all supported twin interventions, tamper detection and drift; replay failures,
empty runs, candidate regressions and reported side effects; transfer corruption,
path traversal/collisions, file/directory conflicts and no-overwrite extraction.

## Not established by this release

No private adapter, queue, answer bank, guardian, live browser lane, provider
receipt service or EWMA consumer was available. There is no production
installation, live synchronization, throughput gain, learned model calibration,
provider exactly-once guarantee or permission to release applicant consent.

Evidence labels and material bindings are caller/host assertions; the adapters
must authenticate real documents, approvals and files. Local signed admission
must be wired into the host, with protected keys, current policy and complete
canonical history. It does not constrain callers that bypass it. Review signatures
prove configured key possession, not independent reasoning. The twin implements
an offline model and protocol derived from prior design work, not a previously
deployed digital-twin service. See MUSE_HANDOFF.md for exact remaining work.
