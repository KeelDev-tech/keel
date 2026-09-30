# Verification and READY recovery

This portable source copy is separate from the running Muse workspace. The
September 29 handoff describes that live workspace, while this checkout starts
from September 28 public source. Local tests and synthetic recovery checks can
verify code behavior here; they cannot establish current live supply, replace
private Muse adapters, or certify that a production repair was deployed.

The raw checkout also retains legacy asynchronous and replay helpers for
historical review. Their presence is not evidence of live Muse integration,
current task ownership, or a qualified recovery route. Use the maintained
allowlist candidate and exact host adapters when integrating these changes.

## One workspace, one source candidate

Use Python 3.11+ on Linux or WSL. The local core uses only the Python standard
library. Linux/Python 3.12 is the previously qualified profile; macOS and native
Windows are not release-qualified. Bash is required only for the wrappers.

```bash
export KEEL_HOME="$HOME/keel-workspace"
./setup.sh "$KEEL_HOME"
python3 keel.py --home "$KEEL_HOME" doctor --capabilities
python3 keel.py --home "$KEEL_HOME" pipeline-doctor
python3 keel.py --home "$KEEL_HOME" supply
```

Initialization preserves existing files. New applicant answers and commitments
remain unknown, so the ordinary doctor returns exit code 1 until required
assertions are recorded. `pipeline-doctor` reads the existing conversion state;
its findings are observations of that workspace, not queue modifications.

Record the exact workspace path in the operator handoff. Passing a different
`--home` can create a second empty queue beside a healthy running queue. Neither
source control nor a successful import proves that the right live data was read.

## Distinguish the conversion stages

| Stage | Evidence needed | What the stage does not establish |
| --- | --- | --- |
| Intake | Exact approved source and canonical posting identity | Applicant eligibility or fit |
| Fit | Evidence-backed score and the operator's current floor | Posting liveness or consent |
| Posting verification | Fresh matching observation from an exact public board cohort | Complete rendered form or READY |
| Human input | Actual unresolved question text or a structured commitment | A free-text note containing `needs_input` is insufficient |
| Readiness | Current identity, fit, transport, posting, materials, and question gates | Application submission authority |
| Preparation | Current review packet bound to workspace inputs | Employer acceptance or submitted outcome |

The supply report conserves queue rows across disjoint operational states.
Nominal `READY` counts and rows that pass current gates are different quantities.
Inspect both before claiming supply recovery. Count attempted observations,
committed observations, and gate-approved conversions separately.

`pipeline-doctor` reports nominal READY, static admissibility, and valid packet
coverage. Its `launchable_ready` remains `null`: live runtime ownership, current
rendered form state, and execution approval belong to the host and were not
observed by this offline command. Unknown launchability is not zero and is not
permission to launch.

The legacy `export_flow_snapshot.py` observer also exports launch approval as
unobserved: `approval_valid=false`, `approval_expires_at=null`, and the approval
dependency is `unobserved`. Staged packet presence and identity markers are
artifact observations. Preparation-only packets, mutable execution flags, and
unvalidated receipt objects cannot establish host authorization. This exporter
has no authoritative host approval receipt adapter.

The dated incident handoff described a mismatch between nominal READY and
reconciled launchable inventory: a durable office exclusion still blocked a row
that a monitor had counted as launchable. Reconcile source observations against
current structured policy evidence before claiming usable supply. Private live
application counts are not included in this source guide. Older classifier and
pacing cohorts are causal history; recensus them before selecting a live repair.
The main lane fit floor remains 75. A separately approved recovery-proposal lane
must retain its explicitly scoped rules and cannot silently change that floor.

## Run one bounded public-board check

Register a source only when you have its exact employer board token. Supported
source forms are `greenhouse:board`, `lever:board`, `lever_eu:board`, and
`ashby:board`. Replace the placeholder below before running it:

```bash
python3 keel.py --home "$KEEL_HOME" source-add greenhouse:EXACT_BOARD_TOKEN
python3 keel.py --home "$KEEL_HOME" discover --max-new 25 --timeout 30
python3 keel.py --home "$KEEL_HOME" verify --limit 25 --timeout 30
```

Discovery reads registered public boards and commits deduplicated intake.
Verification without `--live` still reads public HTTP sources; it previews
observations without committing posting state. After reviewing the selected
cohort, a bounded committed run is:

```bash
python3 keel.py --home "$KEEL_HOME" verify --live --limit 25 --timeout 30
python3 keel.py --home "$KEEL_HOME" pipeline-doctor
python3 keel.py --home "$KEEL_HOME" supply
```

The verifier never promotes a role to READY. An exact match is posting-presence
evidence only. Absence or source failure stays ambiguous; it does not prove
posting death. Request-budget deferral preserves retry state. A 429 or persisted
host hold stops the affected reads and withholds unsafe success conclusions.
Concurrent queue edits are compared with the selected row before committing.

Avoid expanding a scan just because the last report has zero promotions.
Separate scannable rows from rows that could pass readiness at the unchanged
fit floor. A pacing correction can reduce wasted requests without creating
eligible supply. Keep the floor consistent across intake and readiness.

## Repair the cause of a hold

An empty-question hold should become verification work only after checking
that no structured question, attestation, policy decision, identity conflict,
active attempt, terminal receipt, or transport hold remains. Classify the
underlying task, then obtain current posting evidence. Free-text notes are not
authority for approving or refusing readiness.

Human answers require their own provenance and current role/form binding.
Do not infer authorization, credentials, experience, travel commitments,
essays, or no-AI attestations from other rows. Existing submitted and unknown
outcomes remain protected from replay.

Preparation of an explicitly selected role uses a real material path inside
the workspace:

```bash
python3 keel.py --home "$KEEL_HOME" prepare-role ROLE_ID --resume data/resumes/actual-resume.pdf
```

This command creates a human-review packet and does not grant execution
permission. Never create a text placeholder with a `.pdf` extension, rewrite
SKIP/PARKED to READY, or lower fit to demonstrate throughput.

The clean candidate also includes the repaired staging writer, batch planner,
and staged preflight. They recheck current queue placement and scoped packet
state; a preparation-only packet remains unavailable for launch instructions.
These helpers do not provide a host's execution approval, browser adapter, or
submission authority. Their presence is not qualification of a live fire path.

## Validate a clean delivery

The maintained local release path is the reviewed allowlist in
`release-files.json`. It deliberately excludes the root's historical marketing
README, private runtime state, tracked backup copies, old distributions, and
generated audit outputs. This guide supplies the operational quickstart inside
the clean candidate. The broader checkout retains historical files for review.

```bash
python3 tools/package.py --out /tmp/keel-source-candidate.zip
python3 tools/package.py --verify /tmp/keel-source-candidate.zip
python3 -m unittest discover -s tests -p test_release_profile.py
```

The output must not exist. The ZIP records deterministic per-file hashes and
remains a source review candidate with `publication_authorized: false`.
Integrity verification does not authenticate the sender, deploy Muse, or
replace independent privacy review. `package.sh` is the historical packaging
path; it is not the maintained dependency closure for the current local CLI.

The extracted-profile test uses a new workspace, removes inherited source
imports, disables third-party site packages with `-S`, and exercises real local
initialization, assertions, source service fixtures, packet preparation,
read-only status, and the offline demo. For broader development tests, install
the free tools from `requirements-dev.txt` in a disposable development
interpreter; optional material/browser tools have separate requirements.

The clean candidate includes `requirements-dev.txt`, the maintained workflow
`.github/workflows/recovery-profile.yml`, all eleven test modules named in that
workflow, and their source dependencies. For the same local regression set:

```bash
python3 -m pip install -r requirements-dev.txt
python3 -m pytest -q --import-mode=importlib \
  tests/test_queue_transactions.py \
  tests/test_task_liveness_repair.py \
  tests/test_verification_transport_contract.py \
  tests/test_ready_admission.py \
  tests/test_staged_admission_recovery.py \
  tests/test_pipeline_recovery_contract.py \
  tests/test_preparation_identity_guards.py \
  tests/test_verify_retry_scan_apply_port.py \
  tests/test_export_adapter_v11.py \
  tests/test_run_tests_profiles.py \
  tests/test_release_profile.py
```

The workflow is configured to run that set on Python 3.11 and 3.12, then build
and verify the ZIP. Configuration is not proof that CI ran or passed. The
extracted-profile check verifies the packaged dependency closure. The full
checkout contains additional development tooling and optional suites beyond
the clean candidate.

The fresh checkout does not contain `tools/test_guard/sitecustomize.py`.
The broader runner refuses suites requiring that missing guard unless you
explicitly select reviewed local execution. From the full checkout, where
these additional security and privacy suites exist:

```bash
python3 tools/run_tests.py --allow-unguarded --suite core --suite security --suite privacy --report-dir /tmp/keel-local-checks
```

Reports for that mode say `python_audit_hook: unavailable`; no inherited hook or
OS sandbox is claimed. Merely configuring a guard file also does not prove its
installation or enforcement. The extracted `local_profile` suite can run alone
without this override because it configures no runner guard and tests synthetic
`-S` subprocesses. Earlier runner reports that labeled the absent hook inherited
must be treated as unguarded test outcomes, not isolation evidence.

Before applying this candidate in Muse, compare its source with the canonical
live revision, take a recoverable checkpoint, rehearse the exact selected
cohort without submission, and record before/after gate counts and committed
changes. Restore the checkpoint if protected rows change or the conversion
counts fail conservation. This implementation did not run a live migration or
production-state repair. Commands with `--live` commit observations to the
explicitly selected workspace; they must not be mistaken for read-only checks.
