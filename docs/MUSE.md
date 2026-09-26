# Keel 0.13.0 — Muse integration candidate

This release implements the Muse optimization blueprint over the complete
Keel 0.12 reference. The 700 predecessor files remain byte-identical. The new
package adds evidence capture, bounded context, a native-tool protocol, durable
coordination, review projections, receipt reconciliation and controlled
experiments. Its acceptance runs are synthetic. They do not establish actual
Muse account compatibility, employer-site performance or competitive superiority.

## What is implemented

| Blueprint item | Implementation | Receiving-host work |
| --- | --- | --- |
| Capability and command bridge | `capabilities.py`, `bridge.py`, `__main__.py`: closed schemas, fixed operations, source/state pins, deadlines | Map the actual tools and current state providers |
| Authentic input producers | `sources.py`: descriptor-anchored files, actual attachment bytes, transactional six-family capture and separate existing-review decision import | Connect genuine records and authenticated human decisions |
| Context compiler | `context.py`: complete field coverage, evidence pins, current revisions/validity, bounded model context, host-only human values and cache invalidation | Supply the mandatory policy and real scoped records |
| Native browser preparation | `browser.py`, `session.py`: typed actions, accessibility snapshots, one bounded request per dispatch and validated readbacks | Map genuine native tools; rehearse on authorized fixtures |
| Durable coordination | `coordinator.py`: event deduplication, source CAS, coalesced wakeups, locks, leases, bounded concurrency and uncertainty preservation | Run one long-lived controller with trusted host callbacks |
| Review desk | `review.py`, `dashboard.py`: packet changes, evidence, scoped questions and downloadable review intent | Authenticate ingestion through the existing review service |
| Receipt reconciliation | `reconciliation.py`: actual receipt/proof file pins, exact attempt/target bindings and read-only recovery proposals | Verify the observed external outcome through the authoritative workflow |
| Operational dashboard | `dashboard.py`: local interactive HTML, blocker visibility, event timeline, explicit unknown metrics | Produce the defined snapshot from actual stores |
| Repair and evaluation | `repair.py`, `evaluation.py`: frozen inputs, repeated full-form fixtures, independent readback grading and stateful counterexample repairs | Run held-out real trials through an authorized host transport |
| Portable recovery | `backup.py`: SQLite online snapshots, fixed recovery/coordinator profiles and external-checkpoint restore | Retain the authoritative checkpoint independently; cover other stores separately |

The existing assurance, trust/twin, workflow, source, agent, benchmark and Loki
packages remain separate controls. A successful operation in one package does
not satisfy another package's evidence, approval or hold requirements.

## Run the delivered reference

The TXT attachment is an executable Python transfer. It contains source, tests,
fixtures, documentation, historical evidence and the measured 0.13 audit files.
Use a new directory under an existing parent:

```sh
python3 -B Keel_0.13.0_Transfer.txt --verify-only
python3 -B Keel_0.13.0_Transfer.txt --out /existing-parent/keel-0.13
cd /existing-parent/keel-0.13
python3 -B -m keel_muse --help
python3 -B tools/run_muse_checks.py --out /existing-parent/keel-0.13-checks
python3 -B tools/run_muse_acceptance.py --out /existing-parent/keel-0.13-acceptance
```

The guarded test suite uses the unchanged development requirements. New runtime
modules use Python's standard library and the bundled Keel packages. The code
requires no new paid API, inference SDK or hosted database. Python availability
and native-tool mappings must still be checked in the receiving account.

`tools/install_muse.py --target /clean-0.12-reference` checks the additive port;
adding `--install` performs it. It never overwrites conflicting files. Read
`MUSE_HANDOFF.md` before applying it to an actual Keel tree. A modified live tree
requires an informed port against its current files, not resetting the host to
make an installer check pass.

## CLI request and result contract

The command names and flags are available through `python3 -B -m keel_muse
--help` and `python3 -B -m keel_muse session --help`. Ordinary input files use
`{"operation":"status","arguments":{}}`; the `bridge` command instead takes
the exact `keel.muse.bridge-request.v1` document. Never add callable code,
transports, principals, clock, workspace or host-authority overrides to request
JSON. Those are host-owned configuration boundaries.

```sh
python3 -B -m keel_muse inspect-host --out /existing-parent/host-inspection.json
python3 -B -m keel_muse demo --home /existing-parent/new-demo --out /existing-parent/demo-result.json
python3 -B -m keel_muse session --home /private/session --workspace-id WORKSPACE --input /private/status-request.json --out /private/new-status-result.json
```

An unmapped native host returns an incomplete result; it does not infer a tool
from a Python executable. Source files, state and outputs must remain separate.
Outputs must be new private files. The result envelope is
`keel.muse.command.v1`; its `result` member contains the module's typed result.
For a session, only `result.request` from a valid issued result is a dispatch
candidate. Do not send the entire CLI envelope to a native tool or treat
`RECORDED`, `REVIEW_ONLY` or exit code zero as task completion or approval.
Standard output contains only status metadata, so private compiled human values
are not printed into the agent transcript.

| Command | Request operations | Trusted host inputs |
| --- | --- | --- |
| `session` | `create`, `status`, `next`, `observe`, `recover` | `--home`, `--workspace-id`, `--manifest`, `--approvals`, `--host-snapshot`; `--native-gate` on creation and `--dispatch` only on issuance |
| `sources` | `head`, `capture` | `--workspace-id`, `--host-config` with existing `store_home`, `source_root`, registered `scope`, `producer_components` |
| `context` | `compile`, `validate_cache` | `--workspace-id`, `--host-config` with existing `store_home`, `source_root`, registered `scope`, `mandatory_policy` |
| `coordinator` | `snapshot` | Existing `--home`, `--workspace-id`; no writable controller construction |
| `review` | `project_review`, `validate_projection`, `make_review_request`, `validate_review_request` | Runtime clock; existing current snapshots and expected pins |
| `dashboard` | `render` | New `--html` path and a validated review `report` |
| `evaluation` | `freeze_plan`, `run`, `compare` | Actual running source fingerprint; no external transport injection through CLI |
| `repair` | `freeze_fixtures`, `propose`, `evaluate`, `recommend` | Actual running source fingerprint |
| `backup` | `checkpoint`, `snapshot`, `restore` | `--home`, `--backup-home`, `--destination`, `--workspace-id`, `--profile`; authoritative external checkpoint |
| `reconciliation` | `propose_receipt` | Existing `--home`, `--workspace-id`, `--host-config` with `source_root` and `expected_target` |

The per-operation arguments are the documented module parameters, minus the
host-injected parameters. The optional request argument
`expected_running_source_sha256` compares a caller's expected release with the
actual code. A mismatch cannot select different code. Exit code 2 means invalid
input or an error; 3 means blocked, incomplete or waiting; 4 means source
quarantine. Keep the complete typed result when deciding what remains blocked.
The CLI does not expose approval ingestion from an arbitrary JSON principal.

## How the native handoff works

The ordinary Muse model can execute a CLI and separately invoke its exposed
native tools. A Python callback is not automatically one of those tools.
`NativeSession` therefore stores a bounded JSON conversation:

1. Keel validates the contract, evidence, scoped approvals and current host
   snapshot, then proposes the next normalized request.
2. An explicit dispatch persists the request as issued before the receiving
   agent invokes its genuinely mapped native tool.
3. The receiving agent normalizes the observed tool output and returns it with
   the exact issued request hash. Keel validates its account, target, action,
   snapshot and revision bindings.
4. Keel either proposes the next action, records the final preparation readback
   or stops. Repeated polling never issues the same pending effect again.

An interrupted issued effect remains uncertain. Recovery cannot infer that
nothing happened and does not clear UNKNOWN or permit a replay. An observed 429
must also tighten the existing host's global guard. These local stores are not a
replacement for that canonical guard. A final preparation result never grants
submission authority.

Host mode declares `NATIVE_TOOL_ENFORCES_PERMISSION` only after the mapping has
been established. This means the actual native invocation remains subject to
the platform gate. It is not a Keel-issued permission, a signed Sentinel receipt
or independent authentication. The real-mode session accepts no fabricated
`allow` response in place of the native gate. Per-application Keel approval is
still a distinct requirement. See `MUSE_SESSION.md` and `MUSE_BROWSER.md` for
the exact schemas and host-side sequence.

## Source and context boundaries

`FileSourceProducer.capture` reads six families from actual pinned files in a
trusted private root. It checks producer bindings, expected generations, source
head, flow and attachment bytes within the existing producer model. It neither
creates an approval request nor manufactures a human decision.

`observe_approval` requires an already existing request and a separately supplied
trusted `Principal` object. JSON containing a person's name is not that object.
The host must authenticate the human and preserve all existing scope rules.

`compile_context` checks the current seven-family source-head object against
the contract revisions and the support document. Missing, expired, rejected,
revoked or stale evidence blocks coverage. Mandatory policy is never silently
truncated to fit a budget. Human-only and attestation values stay in the private
`host_only` result, outside `model_context`. Consumers must pass only the latter
to model inference, and only where policy permits it.

The cache binds actual file bytes, policy, support, source head and optional
temporal-memory head. Revalidation checks current files and validity again.
Claims partitioned into evidence-backed text establish explicit provenance;
they do not prove semantic truth. All effectful consumers must additionally
check fresh host gates at their action boundary. See `MUSE_EVIDENCE.md`.

## Coordination, reviews and receipts

Create one writable `Coordinator` per controller lifecycle. Reconstructing it
for each CLI call is a restart and fences old work. Passive clients use
`open_readonly`. Trusted callbacks are installed by host code; task JSON cannot
load code or transports. Event revision updates use compare-and-swap. A worker
persists intent and checks gates at the callback boundary, including after
blocking database work. Timeout or interruption after intent preserves locks
and UNKNOWN. Callback reservations are not measured model-token usage.

The review dashboard is an offline projection. A downloaded review request is
user intent, not authenticated approval. Ingestion validates current packet,
question, scope, evidence and time bindings before the existing host workflow
can interpret it. Unknown telemetry remains unknown; missing observations do
not become zero-cost or successful outcomes. See `MUSE_REVIEW.md`.

`reconciliation.propose_receipt` reads a normalized receipt and its separately
pinned proof file through `PrivateFileRoot`. The host supplies the expected
origin/account/application. The existing `RecoveryJournal` validates the
workspace, job, attempt, revision, fence and observation time against an exact
checkpoint. Both files and the checkpoint are rechecked. CONFIRMED creates a
review proposal; NOT_OBSERVED and INCONCLUSIVE require investigation. None of
these outcomes clear a hold or authorize retry. Byte identity is checked;
external source authenticity and factual truth remain unverified.

The normalized receipt schema is `keel.muse.external-receipt.v1`, with exactly
`workspace_id`, `job_id`, `attempt_id`, `revision_sha256`, `attempt_fence`,
`observation`, `observed_at`, `origin`, `account_id`, `application_id`,
`source_kind` and `evidence_sha256`, plus `schema`. Its file binding contains
`path`, `sha256`, `evidence_path` and `evidence_sha256`. Supported source kinds
are `employer_portal`, `confirmation_email` and `manual_observation`; these
labels describe the supplied observation, not an authenticated connector.

## Measure improvement before promotion

`evaluation` freezes source/configuration/dataset pins before repeated trials,
executes form actions against an actual local fixture state and grades final
readback independently. Missing external transports count as unavailable
errors. Failed, blocked and abstained cases remain in the denominator.

`repair` binds a candidate to a counterexample and frozen fixture set. Its
locator, upload-buffer or snapshot repair changes the local driver's state,
then the same observer measures the result. It produces a recommendation only.
It does not edit policy, facts, approvals, model weights or a production site.

Use reviewed held-out tasks to compare the existing Keel/Muse workflow with
each proposed change. Freeze the task set and evaluation criteria before
examining results. Report complete-case preparation, unsupported claims,
interventions, UNKNOWN attempts, native errors, latency and model usage
separately. Synthetic success cannot be used as a real Muse score. See
`MUSE_OPERATIONS.md` for the exact experiment and recovery APIs.

Backups cover the recovery and coordinator stores only. They do not cover
source/memory stores, native session intents, platform state or arbitrary Keel
files. A restore requires a separately retained authoritative checkpoint and a
new destination. Local HMACs detect unkeyed edits; they cannot authenticate an
operator who owns the private key or prove an independently current checkpoint.

## Muse product compatibility and remaining verification

Meta documents a personal Muse cloud computer with filesystem, terminal and
background work. That supports the CLI handoff design, but does not establish
that a particular account exposes every operation this package needs.
Source: [How We Designed Muse](https://introducing.muse.ai/), checked 2026-09-19.

Meta describes browser accessibility snapshots and constrained actions, with
CDP and page JavaScript unavailable to the browser agent. Sentinel remains
outside the agent-editable cell and governs external permission. Keel's
normalized schemas deliberately do not invent a public Sentinel receipt API.
Source: [How We Built Safety Into Muse](https://research.meta.ai/blog/security-and-safety-for-ai-agents-our-approach-with-muse), checked 2026-09-19.

Muse Code's hooks, MCP and headless features are a separate product surface;
their availability is not evidence that personal Muse exposes the same APIs.
Source: [Muse Code: Extending and automating](https://dev.meta.ai/docs/muse-code/extending), checked 2026-09-19.

Personal Muse is hosted. This package can run locally, but it does not turn Muse
into a self-hosted model or change the service's terms or allowances. A separately
licensed local model still needs suitable hardware, installed weights and real
quality measurements through Keel's existing runtime qualification. This
delivery performs no model download, paid inference, real browser action,
external communication, application submission or production deployment.

Report RECEIVED, VERIFIED, INTEGRATED, SHADOW_VALIDATED and DEPLOYED separately.
The delivery's source-bound evidence is the record of what ran here. Real Muse
mapping, authentic upstream evidence and the receiving host's guarded fixture
rehearsal remain necessary before live use.
