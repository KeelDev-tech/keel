# Keel 0.5.1: less repeated work, stronger recovery boundaries

This pass changes existing public runtime paths. It introduces no paid service, model dependency, higher fit threshold tolerance, automatic submission, or new application authority. The private guardian/fit-push executor is not part of this repository; its live integration and throughput were not measured.

## Changes

- **Verification reads each selected board as a group.** The existing oldest-first cohort is selected before regrouping, and memory/request limits remain intact. Grouping prevents interleaved leads from repeatedly evicting and re-fetching one another's boards.
- **A deferred task retains its turn.** Request/deadline exhaustion before a board read does not create an ambiguous posting observation, stamp a verification cooldown, increment source failure backoff, or consume that source's fair turn. Reports distinguish deferred work from observed evidence.
- **Identity is indexed per coherent snapshot.** Selection and commit each build their own current view. Terminal ledger entries exclude the same exact posting even under a different role ID. No identity or hold decision is cached across the commit boundary.
- **Idle verification avoids empty work.** With no observations to commit, it skips a redundant queue read/commit pass and final flush, while retaining the initial durable telemetry flush receipt.
- **Cache eviction uses one accounting pass.** Expiry indexes avoid scanning unexpired entries on every operation. Capacity admission calculates exact canonical byte usage once, selects deterministic victims in one pass, and deletes only after proving enough room. Live leases, corruption holds, fencing and transactional rollback remain intact. Index creation is idempotent for existing v1 stores.
- **Rate limits survive hostile or malformed bodies.** HTTP 429 status/headers reach the durable hold writer without reading a large, compressed or stalled body. Redirect bodies are also skipped; every destination still passes admission, URL/DNS checks, IP-pinned TLS and host cooldowns. TLS minimum remains 1.2.
- **Lost broker receipts stay unknown.** Reset, EOF, timeout, malformed receipt and uncertain send outcomes cannot be relabelled as a safe denial. The client does not retry; the host must reconcile the existing attempt. Authenticated structured denials remain denials.
- **Legacy preparation requires confirmed checks.** Missing or failing launch-lock tooling denies the claim. Prescreen exceptions and invalid verdicts stop both packet-buffer publication and IN-FLIGHT marking, release the held lock, and retire the active packet. Malformed answer-authority expiry causes abstention; absent expiry retains its existing convention.
- **CI executes the new function tests.** Unittest discovery alone does not execute pytest functions. The workflow now includes the explicit optimization/security suite and the matched cache benchmark. Contributor instructions reflect the actual Python 3.11/3.12 CI and existing development dependencies.

## Matched measurements

All inputs are synthetic, all reads are injected fixtures, and no paid model or live posting service was called. These results demonstrate reduced work for the specified workloads, not production latency, provider credits, launchability, or market superiority.

| Measured work | Baseline | Candidate |
| --- | ---: | ---: |
| Board dispatches for the same 96 posting observations | 96 | 12 |
| Identity parses in that verification snapshot | 288 | 96 |
| Identity parses in its supply report | 384 | 96 |
| Full cache usage aggregation queries for one admission | 194 | 1 |
| SQLite VM instructions for that admission | 630,846 | 12,534 |
| SQLite VM instructions for no-op deadline pruning | 2,335 | 38 |

The posting fixture has 12 boards, eight postings per board, interleaved selection order, and an eight-record cache cap in **both** versions. At a 12-dispatch cap, the baseline produces 19 live and 77 ambiguous observations; the candidate produces 96 live-presence observations. This is evidence availability, not READY promotion or application completion. The same cohort and supply classification are asserted in the benchmark.

The cache fixture has 256 one-KiB artifacts, one live leader, and a 192-KiB admission. Both implementations evict the identical 192 entries and retain the same 66,096 canonical bytes across 65 entries. VM counts were measured on SQLite 3.53.1 and can vary by SQLite version; no CPU-time or billing conversion is implied.

## Reproduce

From a clean checkout, install the existing free development requirements and use a fresh private test workspace:

```bash
python3 -m pip install -r requirements-dev.txt
python3 -m unittest discover -s tests
python3 -m pytest -q tests/test_optimization_*.py security/tests/test_optimization_security.py
python3 -m pytest -q tests/test_security_boundary_peer_denial.py
PYTHONPATH=. python3 tests/benchmark_optimization_efficiency.py
```

The pipeline benchmark imports its supplied baseline as trusted code. Use the pinned repository revision, not an untrusted downloaded script:

```bash
git show 8d6564e63f0e8ca63d5097518cffab9ca4015a48:engines/pipeline_service.py > /tmp/keel-pipeline-baseline.py
PYTHONPATH=. python3 tests/benchmark_optimization_pipeline.py --baseline /tmp/keel-pipeline-baseline.py
```

No network or model calls are made by either benchmark. Standalone JSON includes the workload and limitations; the pipeline report records exact implementation hashes.

## Security verification and limits

Local validation on Python 3.12.14 with the pinned development requirements completed with 979 unittest passes and 19 skips (998 cases discovered). The explicit optimization/security pytest suite completed with 121 passes and ten AF_UNIX skips. These suites overlap and their counts must not be added. Both matched benchmarks and the honesty-gates demo passed. CI repeats the suite on Python 3.11 and 3.12.

Adversarial tests exercise the real broker framing, boundary, host adapter and handler with in-memory transports before dropping or corrupting the response. Separate kernel AF_UNIX tests remain present. This environment denies their socket creation, so those cases are explicitly skipped rather than described as passed. An independent reviewer examined the implementation and matched benchmark semantics.

The transport suite checks that a recorded 429 blocks a later reader/restart, including malformed and oversized response metadata, while retaining destination admission. Storage and cache tests preserve live leases, exact byte caps, impossible-allocation rollback, corrupt-entry holds and stale-writer fencing.

The free Bandit scan covered 201 public runtime Python files without scanner errors. Four medium-severity alerts were reviewed against their call paths: state queries generate only placeholder counts and bind values; group-writable broker sockets require explicit group configuration and still authenticate kernel peer UIDs; isolation's `/tmp` paths refer to a fresh bounded tmpfs inside bubblewrap namespaces. These alerts are retained rather than suppressed. Static severity is not a measure of exploitability: review of lower-severity alerts also exposed legacy guard and expiry handling that required repairs.

A local static scan and regression suite are not a cloud penetration test or production security certification. Deployment configuration, browser sessions, private execution hooks, external provider schema compatibility and real workload efficiency still require host evidence. An unknown broker outcome requires reconciliation; this release creates no retry authority for it. Storage failure can prevent a hold from being shared across processes even though the current invocation is denied.

The existing async transport-wiring unit test now stubs DNS deterministically while retaining real URL admission and asserting the resolver was called. Previously it contacted public DNS before its mocked transport, making the offline CI result depend on network availability.

## Adoption

Review the changed source and CI results, then update the public runtime through the normal repository process. Keep live state and credentials outside the source checkout. Existing cache v1 stores receive additive indexes when opened; no cache content migration or application queue migration is introduced. Local state remains protected by the same cooperating-process boundary; arbitrary code running as the same OS user is not sandboxed by these changes.

For a real performance comparison, use the same selected cohorts and budgets, record useful verified outputs and deferred work separately, and include failed attempts. Watch source age, request counts, host holds and unknown outcomes. More labels marked READY is not a valid success metric on its own.

## Design references

- [SQLite query planning](https://www.sqlite.org/queryplanner.html): indexed lookup and avoiding unnecessary table work.
- [SQLite EXPLAIN QUERY PLAN](https://www.sqlite.org/eqp.html): inspecting query strategies; output format is not treated as a stable runtime API.
- [OWASP Top 10 for Agentic Applications 2026](https://genai.owasp.org/resource/owasp-top-10-for-agentic-applications-for-2026/): authority, tool misuse and inter-agent trust are review areas, not a certification granted by this patch.
