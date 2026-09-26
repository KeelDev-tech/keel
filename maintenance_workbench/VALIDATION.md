# KEEL Maintenance Workbench — validation

**Build:** 0.1.0-review.1 · **Date:** 18 September 2026.

## Actual results before packaging

| Check | Observed result | Scope |
|---|---|---|
| Workbench tests | 131 run; 0 failures; 0 errors; 0 skipped; 0 expected failures | This extension's own suite |
| Targeted mutation probes | 6 of 6 selected mutations detected by failing tests | Bounded checks, not an exhaustive mutation score |
| Synthetic vertical demonstration | Baseline passes; contract change fails; mapping repair passes | Deterministic data adapter; protected fixture/policy unchanged |
| Reference source intake | 130 explicitly selected files read; 122 Python files parsed | KEEL 0.3.1 public review copy, not the live executor |
| Static dependency graph | 30 static/declared edges; dynamic-code paths remain flagged | Incomplete runtime dependency coverage |
| Reference integrity | All 180 original manifested files still match their hashes | No source writes |
| Paid services, network APIs, downloaded models | None required or invoked by the workbench | Existing local compute/storage still needed |

Environment actually tested: Linux, Python 3.13.5, SQLite 3.46.1, FTS5 available.
The real FTS5 path and explicit/fault-injected fallback paths were exercised. Older
Python versions were not executed here; 3.10+ is the intended syntax/API baseline.

Network-facing Python socket APIs were denied during the main workbench test run.
The shipped static parser is a separate trusted child process; this instrumentation
is not an OS sandbox or proof about arbitrary subprocess behavior. Tests never
imported target KEEL modules. One initial test fixture expected truncation despite
being shorter than its budget; it was corrected to use a genuinely long fixture.
No production gate was weakened to obtain a passing result.

The standalone delivery report records byte-identical reconstruction, reruns from
the reconstructed text bundle, reproducibility hashes, and final handoff sizes.
Those checks occur after this source file is frozen to avoid a self-referential
bundle hash. Use that report together with the evidence bundled here.

## Gates not run / capabilities not claimed

The original KEEL 262-test suite was NOT rerun. Target runtime behavior, the private
executor, authenticated user/approval integration, provider acceptance, browser
flows, OS sandbox behavior, production deployment, endurance, independent security
review and throughput improvement are NOT established by these results.

The supply-starvation helper is tested but has NOT been wired into the live guardian.
There is no automatic updater, credential provisioning, MCP/A2A server, TLA+ proof,
SLSA attestation, or production release executor in this delivery.

## Reproduce

```sh
RUN_ROOT="$(mktemp -d -t keel-maint-validation.XXXXXX)"
python3 -B tools/run_tests.py --out "$RUN_ROOT/tests"
python3 -B tools/mutation_probe.py --out "$RUN_ROOT/mutations"
python3 -B -m keel_maint demo --out "$RUN_ROOT/demo"
```

Every output must be new. A failed or zero-test run, skipped required check, parser
failure, stale snapshot or invalid contract remains visible. Local test success
never constitutes permission to submit, publish or deploy.
