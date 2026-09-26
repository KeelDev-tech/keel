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
separate `local_profile` suite. They are excluded only from guarded `core`, then
run without the inherited Python audit hook so actual `-S` subprocesses can run.
Other suites retain the hook. This separation is recorded in test reports and
is not an operating-system sandbox. Run this suite alone with:

```bash
python3 tools/run_tests.py --suite local_profile --report-dir /tmp/keel-profile-checks
```

The report directory must be new. The runner needs the free pytest development
dependency; direct unittest checks and the extracted core remain standard
library only. Private integration tests and host-specific browser/kernel checks
retain their own prerequisites and must be reported separately.
