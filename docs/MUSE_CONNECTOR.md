# Muse connector: bounded read-only account report

This bounded connector answers: **What is blocking my applications, how current is that evidence, and what review or verification step could help next?** The outcome is a report. It does not prepare, approve, release or submit an application.

Current packaging source baseline: `KeelDev-tech/keel` main commit `d5f66bf487bf1d6859ebec762050d4fdf333535a`, which merged the post-read authorization correction in [PR #142](https://github.com/KeelDev-tech/keel/pull/142). Its tree matched reviewed correction head `cd71fbe92a396e62d7524205a2a5bb522b1a19f9`, and all eight main CI checks completed successfully. The correction adds authorization rechecks before post-read error responses. The reconciled source archive passes the extracted-runtime checks below on both supported interpreters; each final artifact requires its own external evidence binding.

Earlier [PR #141](https://github.com/KeelDev-tech/keel/pull/141) merged as `6de159aaea28ce85c3d444392c0d49838a933ff0`; its reviewed head was `24272a49ad1b1e1fafe8df29a28293d7ff400ee9`, based on `cb6a90d8aaf9b95183a7bc990ec097805dfe679e` after PR #136. Its original source tests and measurements are retained as historical evidence below. Platform requirements were checked on **2026-10-08**.

The initial pre-connector baseline `706ef51a81d0418a33d390105a693bddf3507a70` lacked `keel_connector`: `importlib.util.find_spec("keel_connector")` returned `None`, while `keel_live.surface` and `keel_live.proof` were present. PR #141 added the transport-neutral contract over those reducers. The follow-up packages its bounded runtime and synthetic demo in the existing private source ZIP. Source availability or successful extraction establishes neither a pip installation nor interoperability with Muse.

## Public platform requirements and remaining qualification

[Muse's connector guidelines](https://muse.ai/platform/docs) allow a useful read-only account report. They require documented tools, permissions, authentication setup, errors and limits; minimal data access, user isolation and revocation; a dedicated reviewer demo/account; business verification, privacy/product terms and support contacts. Review covers risk, tools and end-to-end behavior. Compliance does not guarantee approval. [Directory distribution follows approval](https://muse.ai/platform).

The [Connector Platform Terms](https://muse.ai/platform/terms) displayed a login requirement when checked. Exact transport, authentication details, fees and terms remain unverified. No terms have been accepted, credentials created, connector submitted, endpoint exposed or deployment performed by this slice.

| Requirement | Evidence for this slice | Remaining gap |
| --- | --- | --- |
| Useful account outcome | Scoped blocker/readiness report with freshness and next steps | Representative authenticated host integration |
| Tool documentation | Contract, limits and errors below | Verified API/MCP transport and endpoint |
| Permissions and isolation | Host principal; complete-workspace authorization; revocation tests | Production identity, session and grant lifecycle |
| Data minimization | Allowlisted projection; no applicant material in responses | Host logging, encryption and recipient policy |
| Retention and deletion | No adapter persistence/cache; host responsibilities below | Operational deletion and revocation evidence |
| Dedicated test demo | Synthetic scenarios and commands below | Accessible reviewer account and secure credentials |
| Organization and legal | Gaps explicitly recorded | Business verification, branding, privacy/terms, support/security contacts and maintenance owner |
| Review and discovery | No approval claim | Submission, risk/tool review and end-to-end QA |
| Source distribution | Explicit dependency allowlist; reconciled extracted ZIP passes the stdlib harness on Linux Python 3.11/3.12 | Installation channel, production host integration and public distribution authorization |

## Existing contracts remain authoritative

`keel_live.surface.qualified_view` calls `keel_live.proof.build_proof`; the report must use this qualification path instead of inventing another set of readiness gates. See the [pinned Live integration reference](https://github.com/KeelDev-tech/keel/blob/6de159aaea28ce85c3d444392c0d49838a933ff0/docs/LIVE_INTEGRATION.md), [surface.py](../keel_live/surface.py), [proof.py](../keel_live/proof.py) and [review.py](../keel_live/review.py). The reference describes a broader historical subsystem; the source ZIP supports only the connector entrypoints documented here.

`REVIEW_CHECKS_PASSED` describes capture-to-review checks in the supplied snapshot. It does not establish source truth, authenticated provenance, final packet correctness, rendered form values, current provider acceptance or permission to act. Every result preserves `execution_authorized: false`. Synthetic evidence stays synthetic; no test result counts as an operational application or successful submission.

Source and packet revisions, target and route bindings, assurance, trust, holds, history and timestamps are checked through the existing reducers. A new report time never renews an observation. Stale, future, incomplete or missing-history evidence yields refresh/unknown behavior rather than an actionable readiness claim. Missing source records are system work, not an instruction for the applicant to supply arbitrary private data.

Conditional question-impact counts are omitted. `keel_flow.questions.prioritize` alone does not establish current dependencies. `keel_flow.board.build` checks them against canonical gates and holds and sets `questions.current_dependencies_verified`; the Workbench projection does not preserve that flag. No response promises that answering one question creates a submission.

The existing [MCP sample](https://github.com/KeelDev-tech/keel/blob/6de159aaea28ce85c3d444392c0d49838a933ff0/integrations/mcp-server/mcp-server/README.md) is loopback-only, unauthenticated and limited to synthetic data. Its warning against unauthenticated proxy/tunnel exposure remains intact. It is neither repurposed nor included by this packaging change.

## Host boundary and permission setup

The embedding host injects `principal_provider`, `snapshot_provider`, `workspace_id`, `synthetic`, `attachment_root` and an aware evaluation `host_clock`. These are trusted in-process configuration, never model/client JSON. The host establishes the actual user identity before producing a `keel_live.review.Principal` and supplies only the existing **`review:read`** operation grant.

Every call obtains the current principal and a complete, internally consistent workspace snapshot. It checks authorization for **all** role/application scopes before invoking the full-workspace projection. A requested application is selected only after that authorization and qualification. Filtering out unauthorized rows before evaluation would invalidate graph, identity and history checks and is forbidden. A new role outside the current grant denies the report; the host must rebuild a valid export or update the actual grant.

Before every response after reading host state, including errors, the adapter rechecks that the host still supplies the same authorized principal and reauthorizes collected scopes. A revoked, changed or unavailable current grant returns `ACCESS_DENIED`, overriding any scope-dependent diagnostic. This check is local evidence of the host callback contract, not independent proof of identity or distributed revocation guarantees. The host must promptly update that callback on logout, revocation or reassignment and protect responses already delivered to a caller.

The client cannot choose a principal, account/workspace, application identity, filesystem path, source body, attachment directory, operational/synthetic label or evaluation time. Unknown input fields are rejected. Host-provider failures are returned as bounded diagnostics without exception text or private values. Host callbacks must themselves be read-only; their implementation is outside this adapter. The host must serialize grant/account changes with response delivery, since the second check alone is not a distributed revocation protocol.

## Tool contract

Both operations are classified **Read**, with no sensitive-write operation. They require a current `review:read` grant over the complete host snapshot. There is no mutation tool and no transport endpoint in this slice.

| Operation | Client JSON arguments | Result |
| --- | --- | --- |
| `list_application_blockers` | `{}` | Bounded account report containing scoped references, blocker/review status, reason codes, ownership, freshness, source pins and next review/verification steps |
| `get_application_readiness` | `{"scope_ref":"<64 lowercase hexadecimal characters>"}` | The same qualified report information for one authorized application scope |

`scope_ref` is a SHA-256 reference derived from workspace, role, application and `PREPARE` scope. It is **pseudonymous, not anonymous**, and is not an authorization capability. Only references returned for the current authorized host workspace are usable. A reference from another workspace or an obsolete application identity does not select that workspace or expose its contents.

The Python interface is `ReadinessAdapter(...).call(operation, arguments=b"{}") -> bytes`.
The operation name is limited to 64 characters and the argument bytes are strict UTF-8 JSON.
For example, an authenticated embedding host supplies the callbacks and constants:

```python
adapter = ReadinessAdapter(
    workspace_id=host_workspace_id, synthetic=host_synthetic,
    principal_provider=current_host_principal,
    snapshot_provider=complete_host_snapshot,
    attachment_root=host_attachment_root, host_clock=host_clock,
)
report_bytes = adapter.call("list_application_blockers", b"{}")
```

No callback, account identity, path or clock is resolved from the client request.
Example argument JSON, with a reference copied from the first response:

```json
{}
```

```json
{"scope_ref":"<copy the actual 64-character scope_ref from the authorized list response>"}
```

The second example shows the input shape; its placeholder is intentionally not a valid reference. The operation name belongs to the host invocation contract, not to additional arbitrary fields in the JSON arguments.

### Output and uncertainty

Responses expose only a bounded allowlist: pseudonymous scope identifiers, fixed status/reason/action codes, human-versus-system responsibility, original observation and evaluation times, relevant digest pins, explicit freshness/uncertainty and `execution_authorized: false`. A passing review diagnostic still requires existing human approval and execution boundaries.

Raw source references, applicant answers, question text/options, resumes/attachment bytes, full evidence, company/title labels, actor/authority references, filesystem paths and entire ReviewService or `keel_muse.review` objects are not forwarded. Caller-controlled reason text and exception messages must not become output. Source hashes identify supplied content; they do not authenticate that content.

The success schema is `keel.connector.readiness.v1`, with `ok: true`;
`workspace_roles` always counts the complete authorized snapshot and `returned_roles`
counts the selected report rows. `list_application_blockers` includes every role,
including qualified ones, so missing applications cannot silently disappear.
`current` requires both canonical freshness and a valid source-export wrapper;
`canonical_current` reports the original reducer freshness separately.
Each component still has its own status and original timestamps: `current` does
not mean every component is ready. `source_snapshot` exposes wrapper status,
allowlisted reason and original observation/expiry. Pins are SHA-256 values for
the snapshot, flow, source snapshot and source revision. Source statuses are
`READY`, `MISSING`, `INVALID`, `STALE`, `REVOKED`, `REJECTED`,
`DEPENDENCY_MISMATCH` or conservative `UNKNOWN`. Application states are
`UNKNOWN`, `BLOCKED` or `CAPTURE_TO_REVIEW_CHECKS_PASSED`.

Reports are strictly **as of `evaluated_at`**, even if a host callback takes time
before delivery. The report cannot be reused as an approval or execution token.

For stale/partial evidence, the next step is a host refresh or verification of the missing source/history. For current evidence, next steps identify bounded review/verification work and its owner class. Source repair and canonical blockers take precedence over a pending human review. Only an authoritative pending-approval absence record creates a human review step; missing evidence never invents one. A report does not resolve a hold, refresh a source, contact an employer, request approval, or silently retry an application.

### Errors and recovery

All failures contain only `schema`, `ok: false`, `error: {"code": "..."}` and
`execution_authorized: false`; there is no exception text, input echo or partial
application report. Errors remain under 200 bytes. Exact codes are
`INVALID_REQUEST`, `ACCESS_DENIED`, `NOT_FOUND`, `HOST_UNAVAILABLE`,
`INVALID_SNAPSHOT`, `WORKLOAD_EXCEEDED`, `OUTPUT_LIMIT_EXCEEDED` and
`EVALUATION_FAILED`. Expected malformed/binding failures use `INVALID_SNAPSHOT`;
unexpected reducer failures use `EVALUATION_FAILED`.

Those diagnostic codes apply only while the original host grant remains current.
Every return after the host snapshot/clock read passes through the authorization
recheck, including `HOST_UNAVAILABLE`, `NOT_FOUND`, `WORKLOAD_EXCEEDED`,
`INVALID_SNAPSHOT`, `OUTPUT_LIMIT_EXCEEDED` and `EVALUATION_FAILED`.
If the current principal is changed, revoked or cannot be obtained, the response
is `ACCESS_DENIED`. Stable grants preserve the existing error codes. Request
validation still occurs before any host read; malformed client JSON cannot use
an error response to inspect host state.

| Failure class | Required behavior | Appropriate recovery |
| --- | --- | --- |
| Invalid JSON, duplicate keys, wrong shape, unknown field/tool | Reject before evaluation | Correct the documented request |
| Missing/changed/revoked grant or cross-workspace access | Deny without material details | Re-establish authorized host access |
| Unknown/obsolete scope reference | Return no application material | Refresh the authorized list |
| Host snapshot/provider unavailable or malformed | Fail closed with a fixed diagnostic | Host repairs or refreshes its export |
| Stale/future/incomplete/missing-history evidence | Never report current actionable qualification | Refresh/verify the actual evidence |
| Input, workspace, complexity or output limit exceeded | Bounded rejection; no partial-success claim | Host supplies a supported complete workspace |
| Existing reducer rejects inconsistent bindings | Fail closed or retain its named blocker | Review the current canonical source bindings |

## Data handling and effects

The adapter has no durable report store, result cache, telemetry writer or request history. It does not write canonical data or mutate the supplied snapshot. It makes no outbound, model, browser or messaging calls. Host-controlled attachment verification may read local bytes through the existing proof semantics; client input never chooses a path. This is not a claim of zero filesystem reads.

The adapter does not call `Workbench.run`, whose process-local run cache changes state, or HTTP/review helpers that persist requests and decisions. Synthetic fixture construction and test evidence files are separate from report execution and must be documented as such.

The host owns source storage, access control, attachment retention, snapshots, logs and delivered responses. It must minimize logging, keep secrets out of tool material, revoke access promptly, and apply its documented retention/deletion policy. A returned digest can still be linkable and must be treated as account data. Adapter no-cache behavior cannot delete copies retained by a caller or prove a hosting service's deletion behavior.

## Supported workload and 1.5x headroom protocol

The local qualification target is **20 applications in one complete authorized workspace**, with **30 applications** as the 1.5x headroom case. These counts cover the entire authorized workspace, not a page or selected result set. A larger canonical graph must not be sliced to fit either target; a workspace above the hard cap is rejected before projection. Measure serial warm report calls using the same fixed **p95 latency budget of 2 seconds** and output budget at both sizes. These are local adapter budgets, not provider capacity, network latency, concurrent-user throughput or Muse service guarantees.

| Dimension | Adapter boundary | Existing source-contract context |
| --- | --- | --- |
| Client JSON bytes | 256 bytes maximum | Deliberately narrower than general Workbench JSON |
| Workspace roles | 30 hard maximum; supported target 20 | Workbench permits at most 2,000 |
| Snapshot serialized bytes | 2 MiB maximum | General canonical JSON permits 8 MiB |
| Inspected nodes | 100,000 maximum | Existing source canonicalizer uses 100,000 |
| Nesting | Depth 40 maximum | Existing canonical JSON depth limit |
| Response serialized bytes | 256 KiB maximum | Adapter output bound |
| Latency acceptance target | p95 <= 2 seconds | Local serial warm-call measurement only |

All limits apply together. A small role count does not admit arbitrarily large records or attachments. Existing source limits remain in force: 32 attachments, 16 MiB per file and 64 MiB total; the bounded synthetic headroom workload must state whether any actual attachment bytes were read. Do not generalize a no-attachment measurement to maximum attachment workloads.

Measure both incomplete-source reports and representative fully supplied synthetic source reports where supported. Record fixture composition, serialized input/output bytes, node count, source/attachment coverage, interpreter version, OS, CPU/memory, warmup/sample counts and latency distribution. Measurement timestamps are distinct from the fixed synthetic evidence clock.

Exercise 31 roles, oversized byte/node/depth inputs and oversized results separately. Overload must reject predictably within the bounded error contract. Do not raise security caps, cut unauthorized rows from a canonical snapshot or omit expensive checks to manufacture headroom. A latency target failure is a failed qualification result, not proof that the entire report remains current.

The following historical PR #141 checkout measurements were recorded on
2026-10-08 using CPython 3.11.16 and 3.12.14, Linux x86_64
6.18.44, AMD EPYC 9V74, 5 available logical CPUs and a 16 GiB cgroup memory
limit (shared resources). Each profile has one warmup and 21 serial samples;
p95 uses nearest rank. Timings include both host grant checks, complete-workspace
qualification, minimization and JSON encoding.

| Python | Profile | Complete workspace roles | Snapshot bytes / nodes | p95 ms | Output bytes |
| --- | --- | ---: | ---: | ---: | ---: |
| 3.11.16 | complete | 20 | 184,855 / 5,840 | 131.193 | 42,381 |
| 3.11.16 | missing_sources | 20 | 97,454 / 2,812 | 66.922 | 36,313 |
| 3.11.16 | complete | 30 | 274,925 / 8,660 | 165.437 | 62,981 |
| 3.11.16 | missing_sources | 30 | 143,894 / 4,122 | 107.135 | 53,923 |
| 3.12.14 | complete | 20 | 184,855 / 5,840 | 97.686 | 42,381 |
| 3.12.14 | missing_sources | 20 | 97,454 / 2,812 | 51.921 | 36,313 |
| 3.12.14 | complete | 30 | 274,925 / 8,660 | 130.835 | 62,981 |
| 3.12.14 | missing_sources | 30 | 143,894 / 4,122 | 85.921 | 53,923 |

The complete profile has seven sources per role, independent approvals and one
58-byte attachment per role: 1,160 bytes at 20 roles and 1,740 bytes at 30.
It shares one synthetic trust packet and uses depth-10 snapshots. The missing
profile references no attachments and uses depth-8 snapshots. All four profiles
on both interpreters pass the unchanged 2-second/256-KiB budgets. Thirty-one
roles return a 117-byte `WORKLOAD_EXCEEDED` response without a partial report.

This is 1.5x headroom in roles and their proportional fixture source/attachment
work, not proof of 1.5x every resource dimension or support for every input below
the security caps. Large attachments, adversarial maximum-size evidence, cold
storage, concurrency, network service and actual backlog capacity are unmeasured.

Full samples, environment, minimized synthetic demonstrations and source-file
hashes are retained unchanged in [qualification.json](../keel_connector/evidence/qualification.json).
That file is historical PR #141 evidence. It does not certify a newly built ZIP
or replace measurements made from its extracted source.


## Synthetic demo and required verification evidence

The dedicated offline demo belongs under `keel_connector/`, with tests in `tests/test_connector_readiness.py`. It uses generated synthetic identities, sources and host principals only. It needs no account, paid service, live collection or scan, external model, browser or credentials.

| Scenario | Required assertion | Historical PR #141 evidence |
| --- | --- | --- |
| Current synthetic workspace | Real qualified reducers; correct bounded report; no execution authority | Verified in synthetic tests |
| Stale and future evidence | Original timestamps retained; refresh/unknown | Verified in synthetic tests |
| Incomplete export/missing history/sources | No actionable qualification invented | Verified in synthetic tests |
| Malformed and oversized input | Fixed bounded rejection; no raw values leaked | Verified in synthetic tests |
| Cross-user/workspace/scope reference | Full-workspace authorization precedes projection | Verified in synthetic tests |
| Grant revoked or changed during evaluation | No report returned under the old grant | Verified in synthetic tests |
| Source binding changed | Previous source pins do not remain qualified | Verified in synthetic tests |
| Sensitive fields populated with canaries | No raw applicant/source/authority material in output | Verified in synthetic tests |
| No effects | Input/canonical data unchanged; outbound/model/browser calls forbidden | Verified in synthetic tests |
| 20 and 30 applications | Same p95/output budgets; actual input dimensions recorded | Verified in synthetic tests |
| Overload | Predictable failure without raising hard caps | Verified in synthetic tests |

Run from the repository checkout with Python 3.11 or 3.12:

```bash
python -m keel_connector.demo
python -m keel_connector.demo --benchmark --samples 21
PYTHONPATH=tests:. PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -q --import-mode=importlib tests/test_connector_readiness.py
python -m unittest discover -s tests -p test_connector_readiness.py
```

The demo prints JSON and exits nonzero on failed checks. Its nine scenarios include
current, stale, incomplete, malformed, cross-user, revoked grant, pending human
review, expired source and revoked approval. Fixture setup writes tiny synthetic
files only inside a temporary directory and deletes them at the end. Tests also
cover future evidence, missing history, changed attachment bytes, pending approval
combined with source failures, missing/expired source wrappers, and every bound.
No paid service, external model or browser is required.

The PR #141 combined connector, adjacent reducer and preparation/answer regression suite
passed **643 tests plus 169 subtests on each interpreter** at reviewed head
`24272a49ad1b1e1fafe8df29a28293d7ff400ee9`. This included all **34 connector tests**, also
collected by the existing stdlib CI discovery, and 76 preparation identity/lease,
sealed-packet, answer-authority, tray-reuse and digest-guard tests. The unchanged
offline MCP suite passed **78 tests on each interpreter**.

The historical integrated full Python 3.12 stdlib run executed **1,639 tests** with **2 failures,
95 errors and 19 skips**, using the documented temporary `KEEL_HOME`. All 97 failing
identifiers match the pre-rebase temporary-home run. The initial default-home
baseline below has 12 additional read-only-home errors. Neither full-suite run is
a pass, and no guard was bypassed. Broader CI command results from before the
reconciliation are retained separately in the evidence and are not relabeled as
final-head full CI. PR #141 records its exact-head checks. The merged main CI
success is separate from these retained local environment failures.

The committed evidence records source-file SHA-256 pins and log hashes; transient
full logs are under `/tmp/keel-connector-evidence/` in the verification workspace.

The later PR #142 correction raises the current connector suite to **37 tests**.
Three new tests exercise 47 subtests covering authorization changes on post-read
error paths and stable-grant diagnostics. At correction head `cd71fbe92a396e62d7524205a2a5bb522b1a19f9`,
the combined source regressions passed **646 tests plus 216 subtests**, and the
offline MCP suite passed **78 tests**, on each Python version. These corrected
source results do not certify the newly reconciled ZIP.

Failing-before evidence includes the missing portable adapter and two independent
review findings (source repair precedence and wrapper freshness). Each was
reproduced failing and is covered by passing regressions. PR #141 records
independent review and verification against its exact remote head; no later code
edit inherits an earlier exact-head claim.

## Baseline failures, separate from connector regressions

On the initial main baseline `706ef51a81d0418a33d390105a693bddf3507a70`, Python **3.12.14** with `requirements-validation.lock` installed ran `python -m unittest discover -s tests`: **1,592 tests; 2 failures; 107 errors; 19 skips**. These historical results are not a passing full-suite baseline.

| Baseline result | Diagnosis |
| --- | --- |
| 95 errors | `keel_machine.common._ancestors` rejects `/` and `/tmp` owned by UID 65534; test process UID is 1000 |
| 12 errors | Legacy default paths attempt writes under read-only `/home/agent/keel` |
| Packaged-profile smoke failure | Same ancestor ownership restriction, explicitly reported by the demo |
| Crash qualification failure | Worker exits 1 on the same ownership guard before the expected SIGKILL |
| Broad pytest collection error | Tracked `security/tests/test_secrets.py` imports missing `security.secrets`; package absent from baseline tree |

The crash-worker reproduction matched the baseline diagnostic SHA-256 exactly: `2ad75a3eb841bf4ab87f5b7ee1ea26f33f64e8ced85177934e0507783a448505`. No permission guard was bypassed or disabled. Broad pytest collection is distinct from CI's selected pytest invocations.

Local investigation evidence: `/tmp/keel-connector-evidence/baseline-unittest-py312.txt`, `baseline-pytest-py312.txt`, `baseline-triage-py312.json` and `baseline-crash-diagnostic-py312.txt`. These temporary paths are audit-session evidence, not packaged files or durable published artifacts.

Applicable checks include the connector tests; `test_live_proof`, `test_live_surface`, `test_live_review`, `test_live_connector`; source capture/decision tests; Workbench model/workflow/integration tests; `test_flow` and `test_trust_evidence`. The existing CI matrix is Python 3.11/3.12. Its offline MCP and packet-review suites remain useful regression checks. Browser acceptance, production transport and authenticated end-to-end Muse operation remain outside this slice.

## Distribution and release boundary

At packaging baseline `6de159aaea28ce85c3d444392c0d49838a933ff0`, the unchanged
source builder produced a ZIP with **378 payload files plus `MANIFEST.json`
(379 members)** and SHA-256
`cecd8d71f8ddfbb0b5a63eaa9de52de7cc845edd69a3f89cb7840a30c6e1f647`.
The missing connector import was reproduced from that artifact on Python 3.11
and 3.12. This digest identifies the initial gap artifact, not the new candidate.

The revised `release-files.json` adds **23 runtime/demo source files** and
**six supporting files** to the existing `tools/package.py` source profile.
The verified reconciled result is **407 payload files plus `MANIFEST.json`
(408 members)**. Each fresh run checks its own manifest and records the actual
archive digest outside the ZIP. The
builder and its manifest/path/hash verification are unchanged.

| Added group | Explicit paths |
| --- | --- |
| Connector (4) | `keel_connector/__init__.py`, `adapter.py`, `demo.py`, `synthetic.py` |
| Live qualification (4) | `keel_live/__init__.py`, `proof.py`, `review.py`, `surface.py` |
| Source records (5) | `keel_sources/__init__.py`, `capture.py`, `decisions.py`, `service.py`, `store.py` |
| Trust (1) | `keel_trust/replay.py` |
| Workbench dependencies (5) | `keel_workbench/__init__.py`, `api.py`, `model.py`, `service.py`, `workflows.py` |
| Synthetic builders (4) | `tools/make_assurance_demo.py`, `tools/make_flow_demo.py`, `tools/make_source_producer_demo.py`, `tools/make_trust_demo.py` |
| Documentation/evidence/tests (4) | `docs/MUSE_CONNECTOR.md`, `keel_connector/evidence/qualification.json`, `tests/test_connector_readiness.py`, `tests/test_connector_package.py` |
| Retained notices (2) | `maintenance_workbench/LICENSE`, `maintenance_workbench/NOTICE` |

Names following a directory-qualified entry in the same table row share that
directory. The existing 31 dependencies complete the **54-module import set**
for the bounded adapter, synthetic demo and proof path. This is not a complete
distribution of every branch in historical Live, Workbench or source-producer
modules. In particular, Live server setup, its OpenAPI endpoint, broader producer
inventory tooling and the MCP sample are outside this packaged contract.
`muse-release-files.json`, `operational-release-files.json` and `package.sh`
retain their existing scopes.

Optional authentication token and request-boundary modules, their tests,
dependency requirements and authentication documentation belong to separate
work. Those six optional-auth files are outside the explicit stdlib core
allowlist and are not included, reviewed or qualified by this artifact, even
if they later exist in a source checkout. The 54-module count describes only
the advertised bounded report/demo/proof paths; it makes no dependency claim
for an optional authentication backend.

### Extracted-artifact qualification

The supported verification target is **Linux with Python 3.11 or 3.12 and the
standard library**, including SQLite and the existing POSIX no-follow file
operations. Windows, macOS, WSL, other interpreters and full historical Live/MCP
operation are not qualified by this connector package check. It does not install
anything into site-packages or register a connector with Muse.

Run the dedicated stdlib harness from the checkout or extracted source root:

```bash
python -S -B -m unittest discover -s tests -p test_connector_package.py
# Optional: retain the private candidate and evidence in a new directory.
python -S -B -m tests.test_connector_package --evidence-dir /tmp/keel-connector-check
```

CI places this check before dependency installation on both Python versions.
It builds the real archive twice, verifies identical bytes with the same
Python/zlib toolchain, and verifies the manifest, paths and content hashes before
extraction. Tampered connector bytes, unsafe names, duplicate members and
symlinks are rejected. The `-I -S -B` child excludes inherited `PYTHONPATH`,
checkout imports and site packages, and checks every filesystem-backed module
origin against the extraction directory or interpreter standard library.

The reconciled qualification target covers all **37 current connector tests**, both read
operations, nine demo scenarios, and 21 serial samples of each 20/30-role complete
and missing-source profile. Counts refer to the complete authorized workspace;
the harness must also verify 31-role rejection. Existing 2-second p95 and 256-KiB
output budgets, synthetic attachment sizes, no-effect checks and source-binding
hashes remain unchanged. Fixture construction writes only synthetic temporary
files and is separate from adapter evaluation.

The preserved pre-correction packaging candidate
`ed99de9fe36493d71c41ca4ac9731296fd55abf5` passed all **three package tests on
CPython 3.11.16 and 3.12.14**, including its extracted 34-test suite with no skips,
nine scenarios and all four 21-sample profiles. Its 20/30-role cases met the same
budgets; 31 roles returned bounded `WORKLOAD_EXCEEDED`. Thirteen negative probes
checked its evaluation guard, with zero intercepted forbidden effects during
qualification. Canonical/attachment checks passed, its extracted file/directory
inventory stayed unchanged, and its fresh homes, temporary directory and
unrelated working directory were empty afterward. These are historical package
results; they do not establish a pass for the corrected adapter or new ZIP.

Applying that unchanged harness to corrected main reproduced **two failures
out of three package tests on both interpreters**: its hard-coded 34-test count
and its assertion that the old adapter hash matched the current manifest.
The reconciliation requires 37 current tests and distinguishes the historical
source bindings below. Full failure logs are retained as
`/tmp/keel-connector-package-evidence/reconcile-before-py311.log` and
`reconcile-before-py312.log`.

The reconciled archive passes all **three package tests on CPython 3.11.16 and
3.12.14**, including all **37 extracted readiness tests with no skips**, nine
scenarios and all four 21-sample profiles. The 20/30-role profiles meet the fixed
budgets; 31 roles return a 117-byte `WORKLOAD_EXCEEDED` response. All thirteen
guard probes are rejected, with zero intercepted forbidden effects during
qualification. Canonical inputs, attachments and the complete extracted
file/directory inventory remain unchanged; fresh homes, temporary directory and
unrelated working directory are empty after cleanup. Each final run records its
own archive digest, source inventory and measurements externally. Earlier
source or archive results do not replace that execution.

The guard covers Python write-open/mutation/socket/process events and named
descriptor-write, socket-send, model, browser and service APIs. It is regression
instrumentation, not an operating-system sandbox or a universal interceptor for
native code, pre-opened Python file objects or arbitrary host callbacks. These
synthetic cases hold no writable streams during evaluation. Synthetic attachment
reads and hashes are allowed; fixture writes occur outside evaluation.

For the pre-correction package candidate, the existing archive regressions
passed 110 tests on each interpreter with one existing deferred skip. Its
profile group passed 13 tests and 24 subtests
per interpreter but retains one local `untrusted_ancestor_owner` failure at the
advanced-storage check; that guard remains unchanged. This is separate from the
passing connector archive checks and is not a full-suite pass claim.

Keep each new run's artifact digest, source head, environment, exact commands,
test counts, scenarios, samples and failures in an evidence directory **outside
the artifact**. Do not insert a ZIP's own digest or its new measurement report
into a file that the same ZIP contains: doing so changes the artifact being
qualified. The included `qualification.json` remains unchanged historical
PR #141 evidence. Of its five recorded source bindings, three still match the
current files (`keel_connector/__init__.py`, `demo.py`, `synthetic.py`); two
differ after the correction (`keel_connector/adapter.py` and
`tests/test_connector_readiness.py`). Preserve and report that comparison
explicitly. The old hashes and timings are not current qualification of those
two files. A fresh external report binds every current shipped file through
the actual new archive manifest and records the current test results.

Endpoint selection, secure transport, authentication configuration, credential handling, live user isolation, operational retention/deletion, reviewer access, business/legal requirements, submission and approval remain separate qualification work. This report supplies evidence toward those decisions; it cannot make them.
