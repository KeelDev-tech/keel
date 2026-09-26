# Keel 0.8: create the missing upstream source records

This is an additive extension for the verified Keel 0.7 source tree. It supplies the source-producing software that was absent upstream of the seven-revision adapter. It does not supply real applicant answers, observations, policy choices, or consent. The reported live result—837 leads with all seven source families absent—is the receiving agent's report, not a production export inspected in this environment.

The new `keel_sources` package persists actual supplied records, stages attachment bytes, checks cross-record constraints, presents a complete material review packet, records an explicitly requested operator decision, and emits the normalized snapshot and seven revision columns the existing adapter can consume. Existing 0.7 code, holds, logger/guard behavior, model clients, and workflow adapters are unchanged.

## Scope and limits

| New capability | What it establishes | What still needs real host evidence |
|---|---|---|
| Private versioned source store | Captured records, immutable material versions, optimistic concurrency, rollback/replay rejection, audit metadata | Upstream source authenticity and record truth |
| Source capture | Exact supplied source times and records; attachment bytes copied into content-addressed storage | A real form observation, verified profile facts, and the correct live route |
| Cross-record validation | Required fields, answer types/options, declared provenance, unassisted-field restrictions, explicit policy holds and exact target/account/route bindings | Factual correctness of an answer; evidence references are not automatically resolved |
| Approval workflow | Explicit request and immutable local operator decision bound to all six source revisions and attachment bytes | Authentication of that human and their actual authority |
| Revision export | Seven versioned columns joined by exact role/application identity when a flow export is supplied | Existing assurance, trust, consent and execution gates still apply |
| Host doctor | Observed hardware, executable availability and optional fixed version/metadata probes | Real model inference quality and successful rendered-browser rehearsal |

Every result remains `execution_authorized:false`. A source approval is input to existing gates, not permission to submit. Even `producer_inputs_complete:true` is a source-layer result, not application readiness. The patch contains no application submission, model download, model inference, browser launch, or external network operation.

## Add the package without replacing existing code

The transfer contains only the new package, tests, tools, documentation and measured evidence. It requires the existing 0.7 packages; it is not a replacement full source tree. Its manifest checks the exact prerequisite module hashes.

```sh
python3 -B Keel_0.8.0_Transfer.txt --verify-only
python3 -B Keel_0.8.0_Transfer.txt --out /new/isolated/keel-source-producers
python3 /new/isolated/keel-source-producers/tools/install_source_producers.py --target /actual/keel/tree
python3 /new/isolated/keel-source-producers/tools/install_source_producers.py --target /actual/keel/tree --install
```

The first installer command verifies without installing. `--install` creates new files exclusively and refuses changed base dependencies or conflicting existing files. Repeating an identical installation verifies and skips identical files. The installer does not activate a service, create records, grant approval, or touch the canonical pipeline. File publication uses Linux `renameat2(RENAME_NOREPLACE)` and refuses an unsafe fallback. Python 3.10+ and a local Linux filesystem are required for the supported capture/install path. No package subscription or paid API is required.

Run the tests using your existing development environment after installation:

```sh
python3 -m pytest tests/test_sources_capture.py tests/test_sources_store.py tests/test_sources_decisions.py tests/test_sources_integration.py tests/test_sources_host.py tests/test_sources_install.py -q
python3 tools/make_source_producer_demo.py --out /new/private/synthetic-producer-check
```

The demonstration's approval is explicitly synthetic. Its output must never be imported into a live source store.

## Initialize and register real identities

Run subsequent commands from the combined Keel tree. The store is separate from the existing canonical pipeline and uses private 0700 directories and 0600 database files. Protect it as applicant data; these permissions are not database encryption. Back up the complete source-store directory, including the database, identity marker and immutable attachments. Restoring only selected files is not a verified recovery.

```sh
python3 -m keel_sources init --home /absolute/private/keel-sources --workspace ACTUAL_WORKSPACE
python3 -m keel_sources import-scopes current-flow.json --action ACTUAL_ACTION --home /absolute/private/keel-sources --workspace ACTUAL_WORKSPACE
```

`current-flow.json` must be the real, complete and fresh existing flow export. This command registers its exact role/application identities. It creates zero source records and zero approval requests. Choose the actual intended action explicitly; the examples are placeholders. Alternatively use `scope-add scope.json` with exactly `workspace_id`, `role_id`, `application_id`, and `action`.

## Capture six actual source families

Read `SOURCE_CAPTURE.md` for exact input contracts. Every source descriptor has actual `source_ref`, `source_version`, `observed_at`, `expires_at`, and `record`. Export time never replaces a source observation. Genuine expired/revoked records can remain recorded and blocked. A real re-observation may renew observation metadata for the current unchanged material version; changing material requires a new source version. Old versions cannot be replayed as current.

| Family | Actual producer input |
|---|---|
| Policy | Your active, explicitly versioned action/account/destination allowlists, human approval requirement and holds |
| Form | Captured field definitions, required flags, options, assistance rules and attachment requirements |
| Answers | Exact values with per-field provenance; no invented qualification or attestation |
| Attachments | Existing local document files and their declared purposes |
| Target | The observed role/application identity and verified posting/application URLs with a verification reference |
| Route | The intended account, action, transport and destination, bound to the exact target source version |

Example capture commands:

```sh
python3 -m keel_sources capture actual-policy.json --component policy --scope scope.json --expected-generation 0 --home /absolute/private/keel-sources --workspace ACTUAL_WORKSPACE
python3 -m keel_sources capture actual-form.json --component form --scope scope.json --expected-generation 0 --home /absolute/private/keel-sources --workspace ACTUAL_WORKSPACE
python3 -m keel_sources capture actual-answers.json --component answers --scope scope.json --expected-generation 0 --home /absolute/private/keel-sources --workspace ACTUAL_WORKSPACE
python3 -m keel_sources capture-attachments actual-manifest.json --source-root /absolute/actual/documents --scope scope.json --expected-generation 0 --home /absolute/private/keel-sources --workspace ACTUAL_WORKSPACE
python3 -m keel_sources capture actual-target.json --component target --scope scope.json --expected-generation 0 --home /absolute/private/keel-sources --workspace ACTUAL_WORKSPACE
python3 -m keel_sources capture actual-route.json --component route --scope scope.json --expected-generation 0 --home /absolute/private/keel-sources --workspace ACTUAL_WORKSPACE
```

Generation 0 is valid only for an absent component. Each successful capture returns its new generation; use that value for the next update. An outdated generation is rejected. Capturing a record is not proof it satisfies every cross-record constraint; the export and approval request perform those checks together.

Version immutability is scoped to workspace, role, application, action, component, source reference and source version. It does not enforce one shared reference/version globally across every role. Replay and clock checks use the current database: restoring an older valid database plus identity marker is not detectable without an external monotonic anchor. Reconcile restores against current canonical history and revocations before resuming any work.

Do not choose permissive defaults merely to fill columns. Unknown policy restrictions or unsupported form semantics remain blocked. A field with no explicit assistance permission requires human-origin content. Generated drafts cannot stand in for factual answers or attestations. Source references and `verified_profile` labels remain host assertions; no model verdict establishes their truth.

## Obtain an explicit decision only when review is possible

```sh
python3 -m keel_sources approval-request --scope scope.json --expires-at ACTUAL_REQUEST_EXPIRY --home /absolute/private/keel-sources --workspace ACTUAL_WORKSPACE --out review-packet.json
python3 -m keel_sources approval-show --request-id RETURNED_REQUEST_ID --home /absolute/private/keel-sources --workspace ACTUAL_WORKSPACE --out review-copy.json
```

A request is created only by this explicit command and only after all six source families pass current structural and cross-record checks. Before that, missing source data remains a system diagnostic. The packet includes the actual fields, answers, policy, target, route, attachment metadata and hashes. Have the authorized person inspect it; a model must not impersonate that decision.

After an actual decision, trusted host code can record it:

```sh
python3 -m keel_sources approval-decide --request-id RETURNED_REQUEST_ID --decision APPROVE --actor ACTUAL_OPERATOR_ID --authority-ref ACTUAL_AUTHORITY_RECORD --reviewed-sha256 EXACT_REVIEW_SHA256 --expires-at ACTUAL_DECISION_EXPIRY --home /absolute/private/keel-sources --workspace ACTUAL_WORKSPACE
```

Use `REJECT` for an actual rejection. The command rechecks fresh sources, exact material, attachment bytes and the pending request inside a serialized database transaction. Descriptor re-observation alone does not change the material identity, but stale or changed material blocks the decision. Decisions are immutable. Revocation is a separate explicit `approval-revoke --request-id ... --actor ... --reason ...` event. Actor IDs and authority references are supplied assertions, not authentication by this library; keep these commands behind the host's existing operator access and consent controls.

Read `SOURCE_APPROVALS.md` for request expiry, decision renewal, supersession and rejection behavior. This package does not expose approval tools to models.

## Export to the existing adapter

```sh
python3 -m keel_sources export --flow current-flow.json --home /absolute/private/keel-sources --workspace ACTUAL_WORKSPACE --out producer-export.json
python3 -m keel_sources status --home /absolute/private/keel-sources --workspace ACTUAL_WORKSPACE
```

`producer-export.json.snapshot` is the exact `keel.revision_sources.v1` input for the existing 0.7 seven-source adapter. `revision_report` is its real result. Each `rows[]` entry has the full scope and named `policy_revision`, `form_revision`, `answers_revision`, `attachments_revision`, `target_revision`, `approval_revision`, and `route_revision` under `columns`.

Join these columns by workspace, role, application and action—not role ID alone. A supplied flow export is bound by its digest. If a registered role/application does not match the current canonical flow, its joinable columns are null and its producer completion is false. Export does not modify the canonical flow; the existing live export writer must consume this sidecar, preserving all original fields and restrictions. Feed authenticated original source records along with revisions to the existing envelope builder. Do not replace assurance/trust validation with the source-layer status.

Exit 0 means a command completed; for `export`/`status` it additionally means every requested scope has complete source inputs. Exit 3 means incomplete source inputs, and exit 2 means invalid input or a runtime error. Read the output: no exit status grants execution authority.

## Check the host before installing anything

```sh
python3 -m keel_sources doctor --out host-readiness.json
python3 -m keel_sources doctor --config local-runtime-config.json --detailed --out host-readiness-detailed.json
```

Default doctor starts no process and makes no network request. Detailed mode runs fixed Node version and package-resolution probes; it still launches no browser and calls no model. Read `HOST_READINESS.md` for configuration. Missing dependencies, available dependencies and unperformed tests remain distinct. Real inference and rendering require their own successful target-host checks.

## Next live gate

Start with one real role: capture six authentic source records, obtain a real decision, export seven revisions, then run the existing assurance/trust/workflow gates in shadow mode. Record actual before/after source coverage. Scale the producer hooks only after that path works. The package tests and synthetic 837-scope rehearsal are not evidence that any of the reported 837 live leads have been fixed.
