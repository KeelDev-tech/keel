# Muse connector: bounded read-only account report

This first slice answers: **What is blocking my applications, how current is that evidence, and what review or verification step could help next?** The outcome is a report. It does not prepare, approve, release or submit an application.

Initial source baseline: `KeelDev-tech/keel` main commit `706ef51a81d0418a33d390105a693bddf3507a70`. The seven additive files were then reconciled onto final main `cb6a90d8aaf9b95183a7bc990ec097805dfe679e` after PR136, without duplicating or changing its fixes. Platform requirements were checked on **2026-10-08**. Local synthetic qualification and its limits are recorded below; the draft PR records the exact reviewed head.

The baseline lacks the portable `keel_connector` package: `importlib.util.find_spec("keel_connector")` returned `None`, while `keel_live.surface` and `keel_live.proof` were present. This is an additive, transport-neutral contract over those existing reducers. Presence in a checkout establishes neither installed distribution nor interoperability with Muse.

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
| Distribution | Source changes only | Release inventory, installation and artifact qualification |

## Existing contracts remain authoritative

`keel_live.surface.qualified_view` calls `keel_live.proof.build_proof`; the report must use this qualification path instead of inventing another set of readiness gates. See [LIVE_INTEGRATION.md](LIVE_INTEGRATION.md), [surface.py](../keel_live/surface.py), [proof.py](../keel_live/proof.py) and [review.py](../keel_live/review.py).

`REVIEW_CHECKS_PASSED` describes capture-to-review checks in the supplied snapshot. It does not establish source truth, authenticated provenance, final packet correctness, rendered form values, current provider acceptance or permission to act. Every result preserves `execution_authorized: false`. Synthetic evidence stays synthetic; no test result counts as an operational application or successful submission.

Source and packet revisions, target and route bindings, assurance, trust, holds, history and timestamps are checked through the existing reducers. A new report time never renews an observation. Stale, future, incomplete or missing-history evidence yields refresh/unknown behavior rather than an actionable readiness claim. Missing source records are system work, not an instruction for the applicant to supply arbitrary private data.

Conditional question-impact counts are omitted. `keel_flow.questions.prioritize` alone does not establish current dependencies. `keel_flow.board.build` checks them against canonical gates and holds and sets `questions.current_dependencies_verified`; the Workbench projection does not preserve that flag. No response promises that answering one question creates a submission.

The existing [MCP sample](../integrations/mcp-server/mcp-server/README.md) is loopback-only, unauthenticated and limited to synthetic data. Its warning against unauthenticated proxy/tunnel exposure remains intact. It is not repurposed by this adapter.

## Host boundary and permission setup

The embedding host injects `principal_provider`, `snapshot_provider`, `workspace_id`, `synthetic`, `attachment_root` and an aware evaluation `host_clock`. These are trusted in-process configuration, never model/client JSON. The host establishes the actual user identity before producing a `keel_live.review.Principal` and supplies only the existing **`review:read`** operation grant.

Every call obtains the current principal and a complete, internally consistent workspace snapshot. It checks authorization for **all** role/application scopes before invoking the full-workspace projection. A requested application is selected only after that authorization and qualification. Filtering out unauthorized rows before evaluation would invalidate graph, identity and history checks and is forbidden. A new role outside the current grant denies the report; the host must rebuild a valid export or update the actual grant.

Before returning, the adapter rechecks that the host still supplies the same authorized principal. A revoked or changed grant denies the result. This check is local evidence of the host callback contract, not independent proof of identity or distributed revocation guarantees. The host must promptly update that callback on logout, revocation or reassignment and protect responses already delivered to a caller.

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

Measured on 2026-10-08 using CPython 3.11.16 and 3.12.14, Linux x86_64
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
hashes are retained in [qualification.json](../keel_connector/evidence/qualification.json).


## Synthetic demo and required verification evidence

The dedicated offline demo belongs under `keel_connector/`, with tests in `tests/test_connector_readiness.py`. It uses generated synthetic identities, sources and host principals only. It needs no account, paid service, live collection or scan, external model, browser or credentials.

| Scenario | Required assertion | Evidence status |
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

The combined connector, adjacent reducer and preparation/answer regression suite
passes **643 tests plus 169 subtests on each interpreter** against final main
`cb6a90d` plus this additive slice. This includes all **34 connector tests**, also
collected by the existing stdlib CI discovery, and 76 preparation identity/lease,
sealed-packet, answer-authority, tray-reuse and digest-guard tests. The unchanged
offline MCP suite passes **78 tests on each interpreter**.

The integrated full Python 3.12 stdlib run executes **1,639 tests** with **2 failures,
95 errors and 19 skips**, using the documented temporary `KEEL_HOME`. All 97 failing
identifiers match the pre-rebase temporary-home run. The initial default-home
baseline below has 12 additional read-only-home errors. Neither full-suite run is
a pass, and no guard was bypassed. Broader CI command results from before the
reconciliation are retained separately in the evidence and are not relabeled as
final-head full CI. The draft PR records final exact-head checks.

The committed evidence records source-file SHA-256 pins and log hashes; transient
full logs are under `/tmp/keel-connector-evidence/` in the verification workspace.

Failing-before evidence includes the missing portable adapter and two independent
review findings (source repair precedence and wrapper freshness). Each was
reproduced failing and is covered by passing regressions. The draft PR records
independent review and verification against its exact remote head; no later code
edit inherits an earlier exact-head claim.

## Baseline failures, separate from connector regressions

On the pinned main baseline, Python **3.12.14** with `requirements-validation.lock` installed ran `python -m unittest discover -s tests`: **1,592 tests; 2 failures; 107 errors; 19 skips**. These results are not a passing full-suite baseline.

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

At the pinned baseline, `release-files.json`, `muse-release-files.json` and `operational-release-files.json` contain no entries under `integrations/mcp-server/`, `keel_live/`, `keel_workbench/` or `keel_sources/`. `package.sh` also omits those packages. This slice does not change release packaging. A follow-up must qualify the adapter and all actual dependencies in the appropriate inventory, installation path and artifact before claiming distribution.

Endpoint selection, secure transport, authentication configuration, credential handling, live user isolation, operational retention/deletion, reviewer access, business/legal requirements, submission and approval remain separate qualification work. This report supplies evidence toward those decisions; it cannot make them.
