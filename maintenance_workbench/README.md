# KEEL Maintenance Workbench

**Version:** 0.1.0-review.1 · **Status:** working local review extension, not a deployment.

A dependency-free Python maintenance layer alongside KEEL/MUSE. It captures an
explicit source allowlist, retrieves bounded evidence, analyzes static dependencies,
checks small data contracts, runs finite recipes, stages content-bound candidate
changes, and emits review evidence. It does not execute target repository code.

This is a new `keel_maint` namespace, not KEEL 0.4, a replacement executor, another
answer bank, another scheduler, or an alternative receipt database.

## Run the included demonstration

Use an existing Linux/POSIX machine with Python 3.10 or later. Only Python 3.13.5
on Linux was actually tested in this delivery. No pip installation, model download,
subscription, API key, or network access is required. FTS5 is optional; explicit
literal-token retrieval is available when SQLite lacks it.

From this source directory:

```sh
python3 -B -m keel_maint doctor
RUN_ROOT="$(mktemp -d -t keel-maint-run.XXXXXX)"
python3 -B tools/run_tests.py --out "$RUN_ROOT/tests"
python3 -B -m keel_maint demo --out "$RUN_ROOT/demo"
```

The demo uses synthetic data only. It demonstrates:

1. A small declared field mapping passes its original request contract.
2. A new required request field breaks the mapping's fixture check.
3. Static and declared dependencies identify affected files.
4. An explicit replacement proposal repairs the mapping in a NEW candidate.
5. The fixture check passes without modifying the protected input fixture/policy.

This is a real deterministic mapping check, not runtime execution of a production
connector. `LOCAL_CHECKS_PASSED` never means production-ready or authorized.

## CLI

| Command | Purpose | Writes |
|---|---|---|
| `doctor` | Inspect Python, SQLite, FTS5 and supported capabilities | Standard output only |
| `capture` | Two-pass, explicit-allowlist source observation | New snapshot directory |
| `verify` | Verify snapshot manifest and content hashes | None |
| `search` | Search explicit paths in one workspace snapshot | In-memory SQLite only |
| `stage` | Check base hashes/protected paths; produce a candidate and diff | New candidate directory |
| `recipe` | Run supported deterministic analysis/check operations | New result file |
| `review` | Compare two same-config snapshots and inspect impact | New result file |
| `supply-health` | Analyze supplied buffer and refill counts | None |
| `conformance` | Inspect an explicitly normalized event export | None |

Use `python3 -B -m keel_maint COMMAND --help` for exact parameters.
Outputs must be new. Parent directories must exist. Capture, stage, recipe and
review commands reject outputs inside their input trees. There is deliberately no
`apply`, `deploy`, `submit`, `publish`, `browser`, or arbitrary `shell` command.

## Review the retrieved KEEL source without executing it

The included reference allowlist targets the retrieved KEEL 0.3.1 review archive,
not the unseen private executor. Supply the extracted reference path yourself:

```sh
python3 -B tools/inspect_reference.py \
  --source /path/to/keel-0.3.1-review \
  --config examples/reference_keel_0_3_1.json \
  --out "$RUN_ROOT/reference-analysis"
```

A path mismatch fails instead of silently importing a different source. No target
modules are imported, no target test runner is launched, and no operational state
is opened. The 122 Python files parsed in the delivered reference run are not 122
runtime tests; the original KEEL test suite was not rerun.

## Search a snapshot with an explicit context scope

```sh
python3 -B -m keel_maint search \
  --snapshot "$RUN_ROOT/reference-analysis/snapshot" \
  --workspace keel-0.3.1-review \
  --path docs/ASSURANCE.md --path docs/HTTP_ADMISSION.md \
  --query 'receipt uncertainty approval'
```

Matches retain file paths, line ranges, source hashes, snapshot identity and an
untrusted-source label. Rank does not become authority. Workspace strings are NOT
user authentication: the trusted host must authenticate callers and choose the
allowed snapshot and paths. Do not expose this local CLI as an unauthenticated API.

## Source tree

`keel_maint/` contains the runtime and finite built-in operations. `tests/` contains
this extension's tests. `tools/` contains the trusted static-reference inspector,
local test runner, targeted mutation probes, and plaintext bundle tool. `examples/`
contains a reference allowlist and normalized synthetic inputs. `docs/` documents
contracts, integration, security boundaries, and implemented/deferred scope.

Read `docs/SECURITY_BOUNDARIES.md` and `docs/MUSE_INTEGRATION.md` before integration.
Read `VALIDATION.md` and `evidence/` for measured evidence and limitations.
