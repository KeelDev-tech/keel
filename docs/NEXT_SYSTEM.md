# Keel next system: eight local additions

`python3 -B -m keel_next benchmark --home "$HOME/keel-pipeline-benchmark"`
runs the actual discovery and verification pipeline against bounded local
fixtures. See `PIPELINE_SCALE.md` for resource limits and measurement scope.

The machine execution extension adds incremental pure computation, automatic
contract/failure monitoring, and bounded concurrent HTTP request reuse. See
[machine execution extensions](MACHINE_SYSTEM.md) and run
`python3 -B -m keel_machine capabilities` for its separate local interface.

This candidate extends the existing local execution and review system. All eight
additions work without paid-service dependencies. The core uses Python's standard
library, SQLite with FTS5, and the existing Keel modules. Optional rendered browser
qualification uses locally installed Playwright and Chromium. Kernel worker
enforcement requires a compatible Linux host and an explicitly delegated cgroup.
Compute, storage, electricity, and any independently chosen hosting still have
their normal costs. No cloud deployment or paid account is provisioned.

## Start with the capability inventory and integration demonstration

From the extracted source root, with Python 3.11 or later on Linux:

```bash
python3 -B -m keel_next doctor --home /absolute/path/to/keel-home
python3 -B -m keel_next demo --home /absolute/path/to/new-synthetic-demo
```

The parent of the demonstration directory must exist and be owner-controlled;
writable non-sticky ancestors are rejected. Its final directory must
be new: the command refuses to reuse existing state. It creates private synthetic
SQLite stores and reports eight integration checks. These exercise authenticated
test observations, evidence retrieval, source-budget proposals, revocation,
dependent artifact invalidation, distinct-employer deduplication, and synthetic
evaluation restrictions. It makes no model or network calls and performs no
application, schedule, routing, or deployment action. Passing this demonstration
does not qualify production data, a browser, or kernel resource limits.

## Components and integration points

| Addition | Code | Integration and detailed contract |
|---|---|---|
| Authenticated observations | `keel_observability` | Durable lineage, receipt/effort/source-feedback adapters, measurement revisions, HMAC producer binding; [observation store](OBSERVATION_STORE.md) |
| Canonical identity | `engines/dedupe_index.py` and discovery/intake callers | Explicit source registry, exact identity, conservative alias review and freshness; [identity index](IDENTITY_INDEX.md) |
| Reliability laboratory | `keel_eval/reliability.py` | Frozen paired trials, fault cases, held-out adjudication and regression import; [reliability lab](RELIABILITY_LAB.md) |
| Rendered browser qualification | `keel_loki/browser_lab.py`, `playwright_adapter.py` | Fixed local fixture, DOM/attachment readback and injected failures; [browser lab](BROWSER_LAB.md) |
| Persistent evidence memory | `keel_memory` | Scoped FTS5 retrieval bound to current authenticated observations and existing temporal artifact dependencies; [persistent memory](PERSISTENT_MEMORY.md) |
| Worker resource limits | `security/execution/resource_limits.py`, `seccomp_profile.py` | Explicit cgroup-v2 delegation, CPU/memory/PID limits, reviewed seccomp profile; [worker limits](WORKER_LIMITS.md) |
| Implementation trace checking | `keel_eval/trace_conformance.py` | Checks real durable-authority events, handler ordering, process crashes and worker races; [reliability lab](RELIABILITY_LAB.md) |
| Statistical improvement | `keel_learning` | Frozen experiments, logged randomization, paired doubly robust estimates, conservative confidence sequences and empirical routing matrix; [improvement controller](IMPROVEMENT_CONTROLLER.md) |

## Production wiring

The observation store starts with imports unverified. A trusted host supplies
authentication callbacks or producer keys. HMAC verifies possession of a producer
key; it does not establish that a provider confirmed an outcome, that a human
spent the reported time, or that document text is true. Those stronger assurances
need the corresponding independent host integration. Never install a callback
that returns success for arbitrary request JSON. Keys and live evidence are not
included in this package.

An evidence document needs both its text digest and full metadata digest in its
verified observation. Use the separate `evidence` event kind for new revisions
under an existing identity. Use authenticated `measurement` snapshots for evolving
qualification/submission facts. Read through `EvidenceIndex` so revocation,
expiration, account scope, permitted purpose and conflicts are checked again.
Changed evidence makes dependent artifacts stale and requires review.

Learning and reliability reports are evidence for a proposal. The host must
verify adjudication, outcome maturity, independence assumptions, current pins,
and the exact proof bindings described in their contracts. Synthetic experiments
cannot qualify production improvements. Human approval and existing execution
authority remain required where the existing workflow requires them.

The browser lab qualifies a fixed local fixture and specific adapter/skill pins;
it does not establish compatibility with every employer form. Worker limits fail
closed when explicitly requested capabilities cannot be enforced. A capability
inventory is not a successful enforcement test. Source and configuration changes
invalidate qualifications tied to their hashes.

## Delivery and validation scope

The standalone package uses an explicit source allowlist and excludes runtime
records, private history archives, dependencies and model weights. It remains a
private development candidate; this is not a public-release or universal privacy
certification. See [release profile](RELEASE_PROFILE.md).

The accompanying handoff records the actual test totals, known historical
failures, and host qualifications. Real provider authentication, employer-browser
compatibility and cgroup limits need validation on the intended deployment host.
No benchmark in this package proves state-of-the-art superiority over other
systems. Improvements must be measured against a frozen baseline on relevant,
independently adjudicated tasks.
