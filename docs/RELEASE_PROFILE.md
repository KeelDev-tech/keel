# Local agent ecosystem source profile

This private candidate contains the local application workflow and all eight
new subsystem implementations plus the machine computation/cache/monitoring
layer, a concrete durable host for fixed local computations, and a synthetic
pipeline scale benchmark, with an explicit source dependency closure. The
core requires Python 3.11+ on Linux or WSL and the Python standard library.
SQLite FTS5 is required for persistent search. This audit exercised Linux with
Python 3.12; WSL and other supported Python versions are compatibility targets,
not independently qualified environments. Native Windows lacks required POSIX
file operations; macOS remains unqualified.

No paid service, model API, package installation, or external network access is
needed for the core synthetic demonstration. Actual browser qualification needs
an independently installed free Playwright/Chromium pair. Worker enforcement
needs Linux cgroup v2 delegation, bubblewrap, the reviewed x86_64 seccomp profile,
and an appropriately configured supervisor. Neither browser binaries nor kernel
configuration are included in the ZIP. Missing prerequisites remain blocked.

## Start without connecting services

From the extracted source directory:

```bash
python3 -m keel_next doctor --home /tmp/keel-workspace
python3 -m keel_next demo --home /tmp/keel-new-synthetic-demo
python3 -m keel_machine capabilities
python3 -m keel_machine demo --home /tmp/keel-new-machine-demo
python3 -m keel_machine init --home /tmp/keel-new-runtime --workspace-id local --account-id local-owner
python3 -m keel_machine submit --home /tmp/keel-new-runtime --plan sample_data/machine-plan.example.json
python3 -m keel_machine work --home /tmp/keel-new-runtime
python3 -m keel_machine result --home /tmp/keel-new-runtime --run-id document-compare-1
python3 -m keel_next benchmark --home /tmp/keel-new-benchmark --boards 3 --jobs-per-board 100 --max-new 150
python3 keel.py --home /tmp/keel-workspace init
python3 keel.py --home /tmp/keel-workspace doctor --capabilities
```

The demo path must not exist. It creates only private synthetic state, exercises
observation authentication using an explicitly synthetic verifier, indexes
scoped evidence, prepares a measured source proposal, revokes that evidence,
and checks that retrieval, dependent packets, and source allocation stop using
it. It also checks distinct-employer identity and runs local reliability
callbacks. It makes no model or external network calls and grants no approval.
The synthetic verifier is not a production identity provider.

The machine demo also requires a new private directory. Its six checks exercise
initial graph computation, reuse of unchanged nodes, recomputation of only a
changed branch, current holds blocking cached results, contract rejection, and
detection of an injected failure-rate increase. It uses real local SQLite and
reviewed deterministic functions with synthetic inputs. It makes no model,
network, or external-action calls. These checks demonstrate local behavior, not
measured production speedup or production integration.

The local machine runtime accepts only the bundled deterministic `tokenize_text`
and `summarize_terms` operations. It persists submitted plans, coordinator state,
and results, and supports separate `submit`, `work`, `status`, `result`, and
`hold` commands. Read `docs/MACHINE_RUNTIME.md` for plan format and runtime
limits. This supplies a concrete local host; it does not authenticate external
people or providers, authorize application submissions, or run arbitrary plugins.

The benchmark drives the actual local pipeline with generated ATS board and
posting responses. The workspace must be new. Its measurements describe that
synthetic run on the executing machine, not live board speed, job quality, or
competitive superiority. Read `docs/PIPELINE_SCALE.md` for workloads and bounds.

A fresh real workspace reports missing applicant assertions until you record
your own values. `confirm-answer --key first_name --source "applicant assertion"`
prompts for a value; repeat for required identity fields. Assertions record their
origin without proving their truth. Public-board discovery and posting checks
require separately configured sources and network access.

## Included additions and their remaining host requirements

| Addition | Included entry point | Remaining requirement |
| --- | --- | --- |
| Authenticated observation store | `python3 -m keel_observability --help` | Trusted producer verifier; CLI imports remain unverified |
| Canonical identity index | `python3 -m engines.dedupe_index --help` | Explicit approved source registry; ambiguous similarities require review |
| Reliability laboratory | `python3 -m keel_eval reliability-demo` | Authentic held-out labels and appropriately isolated real runners |
| Browser qualification | `python3 -m keel_loki.browser_lab --help` | Local Playwright/Chromium; fixed synthetic scope only |
| Persistent evidence index | `python3 -m keel_memory --help` | Authenticated observation store, FTS5, explicit account/scope/use |
| Worker resource enforcement | `python3 -m security.execution.resource_limits --help` | Delegated cgroup and reviewed isolation configuration |
| Implementation trace checks | `python3 -m keel_eval trace-check --help` | Real canonical authority history and instrumented handler paths |
| Statistical improvement controller | `python3 -m keel_learning capabilities` | Frozen experiments, authentic outcomes, fresh host attestations |
| Bounded read-only account report | `python -S -B -m keel_connector.demo` | Authenticated host principal and complete workspace; production Muse integration remains separate |

Detailed contracts are in `docs/NEXT_SYSTEM.md`, `docs/PERSISTENT_MEMORY.md`,
`docs/IDENTITY_INDEX.md`, `docs/RELIABILITY_LAB.md`, `docs/BROWSER_LAB.md`,
`docs/WORKER_LIMITS.md`, and `docs/IMPROVEMENT_CONTROLLER.md`. Existing scoring,
decision-session, durable-host, outcome and source-feedback documentation is
included too. The package contains the complete source needed by these paths;
it does not contain every historical subsystem or private integration.

The additional `keel_machine` package includes the computation cache, dependency
graph coordinator, closed contract checks, sequential failure monitor,
host-adapter APIs, and the fixed-operation durable local runtime. Read
`docs/MACHINE_SYSTEM.md`, `docs/MACHINE_CACHE.md`,
`docs/MACHINE_SENTRY.md`, and `docs/MACHINE_COORDINATOR.md` for their contracts.
The bounded concurrent public-read cache remains in the existing
`engines/http_cache.py` reader path; its limits and hold handling are documented
in `docs/HTTP_CACHE_MACHINE.md`. All these modules use the standard library.
The bundled runtime provides current admission only for its owner-controlled
local computation namespace. A custom embedding host must supply reviewed pure
operations and current admission callbacks for any wider scope. Cached
computation cannot supply current consent, approval, authentication, provider
verification, or permission to take an external action.

`doctor --capabilities` distinguishes module presence, in-memory runtime checks,
valid configured source counts, and disconnected services. Its FTS5 check uses
only an in-memory database. It does not open private application databases,
launch a browser, authenticate a producer, constrain a worker, or establish
statistical improvement. The runtime and benchmark inventory entries report
code availability without opening their stores or running a workload. Module
presence is never live-operation evidence.

## Bounded connector source package

The current packaging baseline is main `d5f66bf487bf1d6859ebec762050d4fdf333535a`,
which merged [PR #142](https://github.com/KeelDev-tech/keel/pull/142)'s post-read
authorization correction. Its tree matched the reviewed correction head
`cd71fbe92a396e62d7524205a2a5bb522b1a19f9`, and all eight main CI checks passed.
The original connector merged through [PR #141](https://github.com/KeelDev-tech/keel/pull/141)
at `6de159aaea28ce85c3d444392c0d49838a933ff0`. Its source evidence stays historical;
the reconciled archive passes separate extracted-runtime verification below.
This follow-up includes the two read-only `ReadinessAdapter.call` operations,
their existing qualification reducers and the synthetic connector demo in the
private source ZIP. Read [MUSE_CONNECTOR.md](MUSE_CONNECTOR.md) for permissions,
input/output/error contracts, freshness, data minimization and remaining gaps.

The original `6de159aa` gap artifact contained 379 members including `MANIFEST.json`; its
SHA-256 was
`cecd8d71f8ddfbb0b5a63eaa9de52de7cc845edd69a3f89cb7840a30c6e1f647`.
Extraction reproduced the missing connector import on Python 3.11 and 3.12.
That is the initial failure artifact, not a digest for the revised candidate.

The allowlist adds 23 runtime/demo files and six supporting files: the connector
documentation, historical qualification evidence, existing connector tests,
the new package test, and `maintenance_workbench/LICENSE` and `NOTICE`.
Together with the 31 already included dependencies, these provide the 54-module
import set for the bounded entrypoints. The verified reconciled ZIP contains
**407 payload files plus `MANIFEST.json` (408 members)**. Each fresh run verifies
its own manifest and records its actual artifact digest in an external report.

The extracted-runtime qualification target is **Linux, Python 3.11/3.12 and
the standard library**. This statement applies to the bounded connector only;
it does not expand the platform qualification of the other subsystems above.
SQLite and existing POSIX no-follow file operations remain prerequisites.
There is no pip installation, site-packages environment, hosted endpoint,
authentication setup or public publication in this source-package workflow.
The full historical Live/Workbench/source-producer APIs and the MCP sample are
not supported distribution entrypoints. Historical integration references link
to pinned repository files rather than imply that those complete products ship.
Optional authentication token/request-boundary modules and their associated
tests, dependency requirements and documentation are separate work. Those six
optional-auth files are outside the explicit core allowlist; this source ZIP
does not include, review or qualify that backend. The 54-module count applies
only to the bounded report/demo/proof paths, with no optional-auth dependency
or installation claim.

From the extracted directory, the bounded entrypoints are:

```bash
python -S -B -m keel_connector.demo
python -S -B -m keel_connector.demo --benchmark --samples 21
python -S -B -m unittest discover -s tests -p test_connector_readiness.py
```

To qualify the real packaged source from the checkout or extracted root:

```bash
python -S -B -m unittest discover -s tests -p test_connector_package.py
# Optional: retain the private candidate and evidence in a new directory.
python -S -B -m tests.test_connector_package --evidence-dir /tmp/keel-connector-check
```

CI runs this dedicated stdlib check before installing development dependencies
on both Python versions. It uses the real builder and verifier, checks identical
archive bytes for the same Python/zlib toolchain, extracts only after integrity
verification, and starts an isolated `-I -S -B` interpreter. Every module origin
must be inside the extraction root or interpreter standard library; inherited
checkout paths and site packages are excluded. Current coverage must include all 37
connector tests, both operations, nine synthetic scenarios, and 21 serial
samples for each complete/missing-source profile at 20 and 30 applications,
plus predictable rejection at 31. These are complete authorized workspace
sizes, not pages selected from a larger graph. The 2-second p95 and 256-KiB
output budgets stay fixed. Fixture writes are temporary and separate from
read-only report execution; no browser, model or outbound service is required.

The preserved pre-correction candidate `ed99de9fe36493d71c41ca4ac9731296fd55abf5`
passed all three package tests on CPython 3.11.16 and 3.12.14, including its 34
extracted connector tests with no skips, nine scenarios, all four 21-sample
profiles and 31-role rejection. Its thirteen guard probes were rejected, with
zero intercepted forbidden effects during qualification; source bytes and
directory inventory remained identical and temporary state was cleaned up.
These are historical package results. The old harness then failed two of its
three tests on corrected main under both interpreters: it still expected 34
tests and equality with the original adapter hash. The current target is 37
tests, including post-read error authorization. Every response after a host
read must recheck the grant: changed, revoked or unavailable current authority
yields `ACCESS_DENIED`; stable grants retain their earlier diagnostic codes.

The reconciled package passes all three package tests on CPython 3.11.16 and
3.12.14, including all 37 extracted readiness tests with no skips, nine scenarios,
four 21-sample profiles within the fixed budgets and a 117-byte rejection at
31 roles. All thirteen guard probes are rejected; qualification records zero
intercepted forbidden effects. The full extracted file/directory inventory is
unchanged and fresh homes, temporary directory and working directory are empty
after cleanup. Each final artifact requires its own external evidence binding;
the historical package pass and corrected source-test results do not certify it.
The guards are Python regression
instrumentation, not an OS sandbox or universal protection against native code,
pre-opened Python file objects or arbitrary host callbacks. Local attachment
reads and hashes remain allowed during evaluation.

The earlier separate profile regression group retained its known local advanced-storage
ancestor-owner failure; no storage guard is relaxed. See `MUSE_CONNECTOR.md`
for that limitation and the archive regression counts.

Keep actual counts, timing
samples, environment, commands, source head, archive digest and failures in an
evidence directory outside the ZIP. The included
`keel_connector/evidence/qualification.json` stays unchanged as historical
PR #141 source evidence. It does not certify the new artifact. Adding the new
archive digest or measurement report to its own payload would change that
archive; bind it from an external report instead. Compare its five historical
source hashes honestly: `keel_connector/__init__.py`, `demo.py` and `synthetic.py`
remain matching; `keel_connector/adapter.py` and
`tests/test_connector_readiness.py` differ after PR #142. Do not alter the old
record or present its two superseded hashes as current qualification. Fresh
evidence must bind the actual current manifest and its 37-test execution.
The existing manifest still
provides exact member sizes and hashes, and `publication_authorized` stays false.

## Build and verify a private candidate

```bash
python3 tools/package.py --out /tmp/keel-source-candidate.zip
python3 tools/package.py --verify /tmp/keel-source-candidate.zip
python3 -m unittest discover -s tests -p test_release_profile.py
```

The output ZIP must not exist. `release-files.json` is a sorted explicit
allowlist. Runtime databases, applicant files, credentials, audit snapshots,
old candidates, and private exports are excluded. Python source packages named
`security/data` and `security/ledger` are allowed only as reviewed `.py` files.
No database, model weights, browser binary, dependency environment, or live
configuration is bundled.

The deterministic build records per-file size and SHA-256 hashes. Hashes detect
corruption; they do not authenticate the sender. The result remains a private
review candidate with `publication_authorized: false`. It does not bypass
`package.sh --release` or its exact-artifact publication authorization. The
allowlist is not a universal personal-information scanner: original source
comments can retain project history. Review the exact source before publishing.

The eight profile tests extract the real generated ZIP, disable third-party
site packages with `-S`, remove the inherited Python import path, use fresh
synthetic homes, and exercise:

- Local setup, assertions, capability checks, dashboard, supply and offline demo.
- Evidence-backed scoring and bounded decision-session projection.
- Imports and CLI help for all eight additions from extracted source.
- Actual identity indexing, local reliability callbacks, synthetic experiment
  registration/randomization/outcome/evaluation, and SQLite trace checking.
- The integrated observation-to-memory-to-source-feedback revocation workflow.
- Extracted machine imports and CLI capabilities, plus all six machine demo
  checks with Python socket creation rejected and zero reported model, network,
  or external-action calls. This check adds no operating-system sandbox claim.
- Actual durable runtime initialization, idempotent submission, computation,
  passive reads, reuse across separate CLI processes, and holds that suppress
  already persisted results. Each command runs from extracted source under
  `-S` with Python socket creation rejected.
- The actual bounded pipeline benchmark command with generated responses,
  integrity assertions, and no third-party packages or external network calls.
- Explicit browser `BLOCKED` and worker `NOT_CONFIGURED` outcomes when those
  optional runtime capabilities are absent.

The full-source `tools/run_tests.py` runner includes these eight tests in its
separate `local_profile` suite. They are excluded only from `core`, then run
without a runner-configured audit guard so actual `-S` subprocesses can run.
The fresh checkout does not include `tools/test_guard/sitecustomize.py`; broader
suites refuse that missing guard unless reviewed local execution is explicitly
selected with `--allow-unguarded`. Such reports mark the hook `unavailable`.
An existing guard file is reported as configuration, never proof of hook
installation or enforcement. No mode is an operating-system sandbox. Run this
suite alone without an unguarded override:

```bash
python3 tools/run_tests.py --suite local_profile --report-dir /tmp/keel-profile-checks
```

The report directory must be new. The runner needs the free pytest development
dependency; direct unittest checks and the extracted core remain standard
library only. Private integration tests and host-specific browser/kernel checks
retain their own prerequisites and must be reported separately.

The candidate includes `.github/workflows/recovery-profile.yml`, pinned free
development tools, all eleven test modules selected by that workflow, and their
source dependencies. The workflow is configured to run queue, task-liveness,
transport, READY, staged-admission, preparation-identity, diagnostic, and
related regression tests with the extracted-profile suite on Python 3.11 and
3.12, then build and verify the source package. Configuration is not evidence
of a completed or passing CI run. [PIPELINE_RECOVERY.md](PIPELINE_RECOVERY.md)
contains the exact local test command for the maintained set. Optional full
checkout suites remain separate.
