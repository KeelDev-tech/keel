## 0.6.6 — Supply conversion and evidence safeguards

- Build on merged verification, fit-policy and Muse recovery contracts.
- Enforce the canonical intake floor independently during question planning
  and application; bind mutations to exact current card roles.
- Keep provisional sweep answers and assisted drafts from acquiring automatic
  authority, and enforce the supplied standing applicant gates.
- Share reviewed factual aliases without hiding active obligations or clearing
  qualified prompts by substring overlap.
- Add private per-lead conversion diagnostics and bounded durable observations
  of READY within 24 hours. Unknown history and host execution remain unknown.
- Refuse pending queue recovery in QRESOLVE diagnostics and metadata writes.
  Cleared questions await canonical preparation/admission; they do not become
  READY through the posting-only verifier. No paid service or dependency added.

## Unreleased — Verification, READY admission, and clean source recovery

- Reuse receipt-validated tray custom answers for their exact captured questions
  and variants while preserving scope, expiry, consent, and no-AI holds.

- Recognize current modern preparation packets in supply planning while
  preserving READY transitions, execution holds, and current input validation.

- Keep LinkedIn URL questions held when banked quotations are negative
  statements or non-profile URLs; preserve scoped factual reuse and conflicts.

- Keep full Workbench refreshes authoritative over timer-only updates, and
  discard obsolete refresh errors alongside obsolete responses.

- Discard superseded Workbench refresh responses so delayed automatic or
  manual requests cannot replace a newer snapshot or rebuild its review view.

- Preserve keyboard focus and action identity during Workbench's automatic
  refresh; return focus to search when the focused opportunity disappears.

- Keep the current GEO snapshot, evidence buckets and public count surfaces consistent;
  sanitize all supported operator-input gate aliases in current publication artifacts.

- Preserve pending buffer-refill visits across queue additions, removals and
  reranking so held prefixes do not starve later eligible work. Every visit rechecks admission;
  scan limits, approval gates and concurrency limits are unchanged. Publish
  advisory scan progress atomically so interrupted writes retain the previous
  complete cursor; temporary partial state never grants admission.

- Pipeline doctor reports unevaluated packet integrity as null when static
  admission blocks inspection, without inventing a missing-packet loss reason.

- Require exact integer revision binding on native preparation receipts. Boolean
  or float revisions halt with an unknown effect, without retrying or continuing.

- Add value-free advisory profile-versus-answer contact differences to doctor.
  Equivalent phone/LinkedIn formatting is normalized; findings never overwrite
  applicant facts or change readiness. Experience cannot be inferred from
  incomplete résumé chronology and is explicitly not compared.

- Surface unresolved resume requirement feedback for UNKNOWN and PARTIAL as
  well as MISSING, normalize whitespace, and treat invalid statuses as UNKNOWN.
  Feedback remains advisory and does not infer absent qualifications.

- Bind applicant profile presence and content in modern preparation packets and
  legacy ready manifests. Creating, deleting or changing the profile invalidates
  packets; older packets without this binding require rebuilding. Hashes detect
  drift, not truth, and no applicant authority is inferred.

- Apply value-bound applicant receipts and exact scope checks to every packet
  prescreen. Empty, unconfirmed, expired or out-of-scope answers and unverified
  banded rules cannot clear required questions; valid scoped facts remain usable.
  Required consent controls use their own question and cannot inherit an adjacent
  approval. Ordinary option controls retain their existing behavior.
  Fresh tray and FRP human captures now carry value-bound receipts and governed
  scope; derived/legacy values gain no authority. Ambiguous consent still needs
  explicit scope, and validated custom-question metadata survives prescreen.
  Fresh FRP receipts do not inherit legacy aliases; CLI capture output contains
  counts and status flags rather than private result details.
  Failed tray overwrites retain the prior answer and provenance while the
  rejected replacement is quarantined; legacy values gain no new authority.

- Restore `validate-packet` against complete current queues and ledger. Refuse
  duplicate, held, changed or stale review packets and pending queue recovery.
  Canonical hold fields, nested gates and all matching ledger outcomes apply;
  successful validation remains preparation-only and grants no execution authority.

- Label legacy preparation brief rules as unverified references requiring
  applicant review, rather than claiming approval that the rules do not prove.
  Existing buffered briefs with the obsolete approval header must be rebuilt.

- Preserve exact question provenance on exported holds so topical policy,
  verification and cooldown labels do not reject their own question dependencies;
  unrelated holds still block and review reuse is bound to question provenance.

- Reject question-unlock claims that conflict with canonical non-question
  holds; retain conditional question counts without authorizing any release.

- Cover the existing workbench CSP on HTML, JSON and application error responses
  with socket-free handler tests selected in CI; no server policy changes.

- Add offline YAML regression tests for the CI and Recovery workflows' current
  read-only permissions, literal hosted runner and absence of secret references.
  Pin PyYAML for validation only; workflow settings and CodeQL are unchanged.

- Audit exact subject fingerprints across supplied development, demonstration
  and held-out datasets without changing frozen plans or claiming semantic
  independence; reject invalid identities instead of omitting cases.

- Keep queue-lock retries paced and deadline-bounded when stale diagnostic
  metadata cannot be removed; retain kernel lock authority and transaction
  recovery, and cap sleeps to the remaining waiter budget.

- Exclude CLOSED and CLOSED-EXPIRED queue/ledger states from automatic posting
  verification and supply selection, including terminal holds added during a
  read; retain existing active-state eligibility and approval requirements.

- Isolate retained-history replay correctness accounting from host scheduling
  delays; label synthetic controller time separately from observed elapsed time
  and retain production budget-overrun refusal with regression coverage.

- Qualify reported corroboration as declared publisher diversity, expose shared
  reference/hash signals, and state that source independence is not established.

- Reflow dashboard score cards on narrow screens and wrap long values without
  clipping or changing displayed counts.
- Add keyboard skip targets to dashboard/review content and accessible names and
  column headers to the dashboard submission-claims table.

- Show field labels and required/optional status in both sides of existing packet
  comparisons, including changes where the value stays the same.

- Keep prescreen reason prose in queue review artifacts rather than gate telemetry;
  retain gate categories and reason counts, and omit raw probe exceptions from
  packet-builder diagnostics. Evidence holds and applicant approvals are unchanged.

- Keep an undated submission-claim count visible alongside dated dashboard rows,
  including when the recent list is capped at eight records.

- Separate untrusted MCP triage role fields from fixed prompt guidance using
  JSON serialization; preserve string output and analysis-only tool behavior.

- Order eligible verification work by normalized observation instants so timezone
  offsets and equivalent timestamp spellings preserve oldest-first scheduling.

- Keep dashboard activity panels unknown when their source records are missing,
  unreadable or corrupt. Retain empty-state messages for known empty records,
  and distinguish undated submission claims from no recorded claims.

- Bind verification retry cooldowns to the scheduling attempt's own posting
  identity, including first failures without a prior decisive observation.

- Run operational control/runtime and machine-contract function tests explicitly
  in CI. Add a read-only advanced storage ancestor diagnostic to `keel_next
  doctor` and blocked CLI output, retaining the production ownership guard and
  failure exit. Document the pinned development bootstrap separately from
  applicant workspace initialization.

- Hold records with out-of-range cooldown timestamps without aborting verification
  of other eligible records in the same batch.

- Accept structured URL-bound form/posting evidence in the MCP prescreen
  diagnostic, preserving brief-only PARK behavior and example-bank isolation.
  Report no execution authority even when the diagnostic is CLEAN.

- Run the existing outcome-safety and truthful-analytics function regressions
  explicitly in CI, covering receipt correlation, chronology and rate denominators.

- Derive the tray and standard re-screen fit defaults from the canonical
  intake `FIT_BAR` (currently 75). Preserve explicit tray environment/API/CLI
  overrides and the re-screen CLI override. Add regression coverage for floor
  drift, default filtering, override precedence and invalid values; document
  the remaining fixed policy consumers separately.
- Journal cross-file queue changes before replacement, compare full snapshots,
  and recover interrupted moves before cooperating readers use the queues.
- Preserve posting evidence through transport failures, pace bounded retries,
  stop on rate limits, and remove implicit READY promotion from the operational
  verification entry point.
- Recheck all four queue homes, ledger history, the fit floor of 75, current
  employer policy, durable holds, and scoped packet/material integrity before
  preparation or staged admission. Preparation packets carry no execution
  authority; staging alone cannot become exported approval.
- Require exact authoritative terminal-task evidence before clearing stale
  ownership, and serialize fresh claims without stealing foreign leases.
- Add offline conversion diagnostics, preserve unknown applicant assertions
  during setup, and ship a complete maintained source/test dependency profile
  with dedicated recovery CI configuration.
- Qualify local behavior separately from inherited full-checkout failures and
  unavailable host adapters. No live migration, submission, or measured supply
  recovery is claimed.

## 0.6.5 — Recoverable and fair question resolution

- Add conservative interrupted-intent inspection and explicit recovery. Proven
  untouched or completed operations can close their holds; partial/conflicted
  operations remain held without replay or rollback.
- Add exact FACT/JUDGMENT draft approval that revalidates source, context and
  scope, preserves bank provenance, and records human approval separately.
- Add persistent fair scan selection with changed-work priority and retained
  active proposals so repeated high-ranked cards cannot starve older work.
- Reject unsafe lock/diagnostic paths before QRESOLVE live operations and align
  provenance date admission with the resolver's UTC freshness clock.
- Keep all new commands offline and read-only by default. No new service or
  runtime dependency; no submission authority or production improvement claim.

## 0.6.4 — Evidence-backed question resolution

Added offline QRESOLVE retrieval, conservative classification and exact scoped
answer proposals. Automatic factual reuse remains opt-in and is revalidated
inside the sanctioned tray actuator's queue lock. Reuse preserves provenance;
durable intents and verified completion receipts keep interrupted writes held.

Unified digest/applier workspace paths, retained full prompts, made the tray
fit threshold configurable (default 60 at that release; now derived from the
canonical intake floor), and made bare digest runs read-only.
Structural cards cannot carry drafts or be cleared through the manual answer
path. Packaged the tray dependency closure and added synthetic classification,
provenance, crash-hold and fresh-process integration tests. Private backlog and
real-card acceptance remain host tasks. See docs/QUESTION_RESOLUTION.md.

Console output now reports counts/status without printing private questions,
banked answers or provenance excerpts; full review data stays in private files.

## 0.6.3 — Canonical advice and process-crash qualification

Added read-only `productivity-advice`, which validates retained trial evidence,
reports observed bottlenecks and bounds proposed follow-up cohorts by remaining
allowance across ancestor scopes. It preserves work settings and transport,
reserves nothing and declines proposals when evidence is incomplete or work
needs investigation. No causal improvement or credit savings are inferred.

Added five actual SIGKILL/restart scenarios for the public controller's
reservation, intent, queue commit, receipt and accounting boundaries. Preflight
now validates the requested policy and reservation limits, keeping the original
default diagnostics separate. See docs/EVIDENCE_LOOP.md.

## 0.6.2 — Host qualification and recovery fixes

Added offline `host-preflight` checks and an isolated synthetic productivity
rehearsal with explicit live-host unknowns. Budget inspection now opens existing
ledgers read-only without schema initialization or migration; reads no longer
take immediate writer transactions. SQLite WAL coordination sidecars remain a
filesystem consideration.

Fixed completed trial replay after an unrelated shared-budget lock, and receipt
acknowledgment lock/read failures after a known queue commit. Recovery preserves
canonical evidence, pending outboxes and uncertain write outcomes. No new paid
service, private executor or submission authority. See docs/HOST_HANDOFF.md.

## 0.6.1 — Sustained public operation

Replaced the 256-run productivity journal ceiling with indexed, retained replay history and restart-safe migration. Ledger checkpoints detect replacement and rollback without rescanning every historical request. Added fixed, resumable trial cohorts with actual workload/build fingerprints and evidence-based reports; unknown or mismatched observations cannot establish gains.

Added an exact-attempt receipt projection interface for qualified host validators, plus a read-only CLI review path. It preserves unresolved attempts, approvals and holds; no private executor or billing adapter is supplied. See docs/SUSTAINED_OPERATION.md for operation, storage and deployment boundaries.

## 0.6.0 — Measured public pipeline scheduling

Connected supply inspection, durable source scheduling, verification outbox recovery and shared resource accounting through `productivity-status`, `productivity-once` and explicit `productivity-recover`. Each live cycle has an immutable run ID and at most one bounded public-read stage. Persisted receipts prevent duplicate dispatch and support settlement recovery; unknown outcomes retain uncertainty. Intake pauses at the configured supply/backlog limits and repeated zero-progress stages receive cooldowns.

Verification reports count successfully committed verdicts separately from observations, and fresh presence must match the current posting identity. Metrics separate new queued postings from committed presence observations; actual credit consumption remains unobserved. The controller adds no model requirement, paid service, applicant assertions or submission authority. See docs/PRODUCTIVITY.md.

## 0.5.1 — Measured runtime optimization and recovery hardening

Grouped public-board verification, preserved unattempted fair turns and cooldowns, per-snapshot exact identity indexes, indexed one-pass cache eviction, header-first durable 429 handling, and honest UNKNOWN broker outcomes. No new paid service or application authority. CI runs the new adversarial regressions and matched cache benchmark. See docs/OPTIMIZATION_0_5_1.md for reproducible measurements and limits.

Legacy packet preparation now refuses unavailable launch guards and unconfirmed prescreen results. Malformed answer-authority expiry abstains instead of silently retaining authority.

## 0.5.0 — Resource efficiency candidate

Shared local budgets and usage accounting; qualified preparation routing; bounded context, pure-work reuse, adaptive review plans, shadow contextual bandits, data-only procedure qualification and matched replay benchmarks. See docs/CREDIT_EFFICIENCY.md for controls and limitations.

# Changelog

All notable changes to Keel are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [0.4.0] - 2026-09-26

### Added
- **Durable fair-intake scheduler** (`engines/source_scheduler.py`,
  `keel_next/source_scheduler_demo.py`): selects bounded source batches,
  divides intake allowance across them, remembers fair turns and backoff,
  stops on rate limits, and holds interrupted runs for explicit recovery —
  admits only parked rows; a SIGKILL after queue commit reconciles as
  `HELD_RECOVERY` with no duplicates on replay.
  Docs: `docs/SOURCE_SCHEDULER.md`.
- **Skill ancestry** (`keel_evolve/lineage.py`): derived lessons/procedures
  pin their parent, inherit context limits, reject incompatible parents, and
  are blocked by withdrawn/expired/corrupt ancestors without disabling
  healthy siblings.
- **Balanced response-protocol evaluation** (`keel_eval/frontier.py`):
  exercises the actual response validator against valid, invalid and
  missing inputs with confusion matrices, per-family and per-label recall,
  error/abstention risks, and conservative sequential cluster bounds so
  repeats are not counted as independent evidence.
  Docs: `docs/evaluation-frontier.md`.
- **Recovery and security repairs** (`security/execution/`,
  `security/actions/approval_gate.py`): withdrawal stays possible when
  application quotas exhaust audit capacity; restored artifacts fork only to
  fresh candidates; backup ancestors and metadata are checked; raw failure
  fixtures cannot be relabelled as held-out subjects; FIFO state files fail
  without hanging.

### Changed
- `engines/http_cache.py`, `engines/pipeline_service.py`,
  `engines/outcome_tracking/evidence_gate.py`,
  `engines/ashby_lever_json_enumerate.py`,
  `engines/greenhouse_json_enumerate.py`: hardened intake/evidence paths to
  match the 0.4.0 scheduler and evaluation contracts.
- `keel.py`, `keel_next/__main__.py`: expose the `frontier` demonstration
  entrypoint (`python3 -B -m keel_next frontier`).

### Fixed
- Release profile tooling (`tools/release_profile.py`,
  `tools/apply_source_update.py`) and evolution engine/hardening modules
  updated for the 0.4.0 freeze; `release-files.json` regenerated.

## [Unreleased]

- Recover bounded packet refill when its advisory watermark contains invalid text encoding; current admission gates are still rechecked.

### Added
- Safety rails as code: `docs/OPERATING-CONSTRAINTS.md` documents the
  numbered operating constraints (C-01…C-20), each enforced in a named
  engine module; the operator may extend them but never weaken them.
- Credentials-vault convention (`docs/CREDENTIALS.md`): one file per
  account, `0600` permissions, gitignored and excluded from packaging.
- Operator policy template (`engines/policy.example.json`): travel /
  office / relocation caps, attestation hard-stop classes, and the
  pre-authorized attestation scope — example data only.
- ATS discovery pack (`engines/ats_discovery/`): additional platform
  adapters for board enumeration (detection only — no submission
  behavior).
- Direct ATS detection: Greenhouse/Lever/Ashby JSON board enumeration
  and board classification (detection only — no submission behavior).
- Outcome-tracking module (`engines/outcome_tracking/`): evidence gate
  for submission claims and invite/offer surfaces over the append-only
  telemetry log.
- Input-resolution module (`engines/input_resolution/`): structured
  handling of operator-input blockers with banking of reusable answers.
- Launch lock (`engines/launch_lock.py`): one live application task per
  role, so concurrent lanes cannot double-fire the same lead.
- Lane watchdog (`engines/lane_watchdog.py`): evaluates pipeline health
  red lines against live data.
- Cost metering (`engines/cost_model.py`, `engines/cost_tracker.py`):
  per-submission cost dimensions recorded additively on ledger rows;
  zero-submission days render as explicit gaps, never interpolated.
- Discovery-side dedupe gate (`engines/dedupe_gate.py`) and input-tray
  classifier (`engines/genuine_pat.py`): parked items are classified so
  genuinely operator-blocked leads stay parked while verification-only
  items return to the verify pool.
- Packet starvation watchdog (`engines/packet_watchdog.py`), clean-board
  watch (`engines/clean_board_watch.py`), and page capture
  (`engines/page_capture.py`) for discovery hygiene.
- Field-question protocol (`engines/field_question_protocol.py`):
  classifies form questions as answerable, derivable, or
  applicant-only — applicant-only items park, never invented.
- Batch staged-launch helper (`engines/batch_staged_launches.py`) and
  parked-task sweep (`engines/parked_task_sweep.py`).
- Worker charter (`worker-charter/`): the autonomous-worker prompt
  template with constraint-tagged exemplars; the public assembler
  deliberately does not mine the private technique library.
- Verify hardening: `verify_retry.py` gains the run singleton, 24h
  cooldowns, pool cursor rotation, stale-park guard, sync/async parity
  probe, and prescreen promotion hooks; `verify_retry_async.py`
  (async transport), `verify_cron.py` (cadence tick), `live_cache.py`
  and `http_cache.py` (fail-closed HTTP caching layers).
- Outcome recording: `record_outcome.py` (ATS-key enforcement,
  placeholder refusal, consent-gated classification),
  `outcome_analytics.py` (OFFER outcome, fail-closed ledger linking),
  `inbox_listener.py` (OFFER classification, LinkedIn/Indeed
  channel tripwires).
- Queue governance: `queue_intake.py` (single validation point),
  `queue_io.py` (atomic queue writes), `staging_ingest.py`
  (staging → queue worker), `title_triage.py` (staging-side triage).
- Executor-adjacent: `apply_loop.py` (READY-lead buffer/claim engine;
  stops at launch packets per the executor contract),
  `prescreen.py` (posting-eligibility + pre-promotion screens, opt-in
  field-question-protocol hook), `rate_limits.py`.
- Input brokering: `input_broker.py` (external draft brokering with a
  local anti-invention verifier), `lever_submit.py` and
  `breezy_preflight.py` (read-only automation-friendliness probes;
  verdict: browser path only).
- Answer-bank example (`engines/answer_bank.example.json`):
  configurable attestation scope (pre-authorized keys, hard-stop
  classes) — example data only.
- Tests: launch-lock race (`test_launch_lock_x20.py`), evidence gate
  (`test_evidence_gate.py`), input-resolution port
  (`test_input_resolution_port.py`), preference sanitization
  (`test_preferences.py`), cost model/tracker, worker-charter suite.
- HTTP hardening (verification transport): `engines/http_cache.py` gains
  URL/DNS admission, redirect admission, a 4 MiB response cap, and
  durable cross-process 429 cooldowns (`engines/host_cooldowns.py`,
  `engines/http_policy.py` — fail-closed admission policy shared by the
  transport).
- Async verify scan budget: `verify_retry_async.scan_window_async` accepts
  a caller-configurable outer budget (default 600s), returns
  `(results, unscanned_role_ids)`, and settles pending work on expiry —
  unscanned roles stay unstamped for the next cadence instead of being
  silently dropped.
- Territorial work-authorization gate (`engines/work_auth_gates.py`):
  canonical `work_auth_unverified` taxonomy with legacy
  `{country}_work_auth_(unverified|no)` normalization, a pure
  pre-staging screen that never infers authorization from city/ZIP,
  and relocation-never-equals-work-auth separation; wired into
  `staging_ingest.py` before the title gate with an append-only
  reject ledger.
- Form intelligence: `form_intel.py` uses the hardened shared
  `http_cache`, adds conservative per-process memoization (deep-copied
  results), and detects Greenhouse job IDs via public verifier-board
  guesses (read-only probing — no submission mechanics).

### Fixed
- `setup.sh` writes queue files in the canonical top-level-list form and
  no longer mutates the tracked `keel.config.json`; identity answers land
  in `data/` only.
- `docs/PERSONALIZE.md` rewritten as the single operator-neutral
  "clone → first launch packet" guide.

## [0.2.0] — 2026-09-15

### Added
- Public web home (`site/`): static GitHub-Pages-ready page with
  `llms.txt`, sitemap, and OG hero image — a visitor-facing front door
  for the repo.
- GEO discoverability layer: citation-ready docs under `docs/geo/`
  (comparison, FAQ, alternatives keyword pages), `llms.txt` at the repo
  root, JSON-LD structured data, `docs/releases.xml` release feed, and
  `docs/assets/demo.gif`.
- GEO measurement pipeline: canonical recount probes and snapshot
  history (`docs/geo/stats.json`,
  `geo-pipeline/history/snapshots.jsonl`) tracking verified submission
  figures over time.
- GitHub Actions CI workflow (`.github/workflows/ci.yml`) running the
  stdlib test suite (`python3 -m unittest discover -s tests`) on every
  push.
- Keel Doctrine (`KEEL_DOCTRINE.md`): one operating spine across the
  five departments.
- `docs/ROADMAP.md`: public roadmap covering shipped work (v0.1.0),
  post-release items, and planned direction — no timelines.
- Keel spine logo (`docs/assets/keel-logo.png`) in the README header.
- Contributor footing: `CONTRIBUTING.md` onboarding,
  `CODE_OF_CONDUCT.md` (Contributor Covenant v2.1, no personal contact
  data), and `.github/FUNDING.yml` sponsorship entry points.
- README cold-visitor audit: the quickstart is genuinely end-to-end —
  a documented demo-seed step bridges `score_roles` output into the
  READY queue so `apply_loop` builds a launch packet; network note
  added (read-only HTTP liveness/form-intel probes); submission figure
  refreshed to the ledger-verified canon (94 verified, 2026-09-15).
- `docs/ARCHITECTURE.md` accuracy pass: telemetry path corrected to
  `data/telemetry/events.jsonl`, event-type list aligned to
  `log_event.py`'s stable list, data-flow step 2 corrected (score_roles
  sets no status — the queue owns it), live re-verify tri-state
  documented, `keel_paths.py` added to the module guide.

### Fixed
- The apply loop exits with actionable setup guidance instead of a traceback
  when the workspace queue file is missing.

### Added
- Safety rails as code: `docs/OPERATING-CONSTRAINTS.md` documents the
  numbered operating constraints (C-01…C-20), each enforced in a named
  engine module; the operator may extend them but never weaken them.
- Credentials-vault convention (`docs/CREDENTIALS.md`): one file per
  account, `0600` permissions, gitignored and excluded from packaging.
- Operator policy template (`engines/policy.example.json`): travel /
  office / relocation caps, attestation hard-stop classes, and the
  pre-authorized attestation scope — example data only.
- ATS discovery pack (`engines/ats_discovery/`): additional platform
  adapters for board enumeration (detection only — no submission
  behavior).
- Direct ATS detection: Greenhouse/Lever/Ashby JSON board enumeration
  and board classification (detection only — no submission behavior).
- Outcome-tracking module (`engines/outcome_tracking/`): evidence gate
  for submission claims and invite/offer surfaces over the append-only
  telemetry log.
- Input-resolution module (`engines/input_resolution/`): structured
  handling of operator-input blockers with banking of reusable answers.
- Launch lock (`engines/launch_lock.py`): one live application task per
  role, so concurrent lanes cannot double-fire the same lead.
- Lane watchdog (`engines/lane_watchdog.py`): evaluates pipeline health
  red lines against live data.
- Cost metering (`engines/cost_model.py`, `engines/cost_tracker.py`):
  per-submission cost dimensions recorded additively on ledger rows;
  zero-submission days render as explicit gaps, never interpolated.
- Discovery-side dedupe gate (`engines/dedupe_gate.py`) and input-tray
  classifier (`engines/genuine_pat.py`): parked items are classified so
  genuinely operator-blocked leads stay parked while verification-only
  items return to the verify pool.
- Packet starvation watchdog (`engines/packet_watchdog.py`), clean-board
  watch (`engines/clean_board_watch.py`), and page capture
  (`engines/page_capture.py`) for discovery hygiene.
- Field-question protocol (`engines/field_question_protocol.py`):
  classifies form questions as answerable, derivable, or
  applicant-only — applicant-only items park, never invented.
- Batch staged-launch helper (`engines/batch_staged_launches.py`) and
  parked-task sweep (`engines/parked_task_sweep.py`).
- Worker charter (`worker-charter/`): the autonomous-worker prompt
  template with constraint-tagged exemplars; the public assembler
  deliberately does not mine the private technique library.
- Verify hardening: `verify_retry.py` gains the run singleton, 24h
  cooldowns, pool cursor rotation, stale-park guard, sync/async parity
  probe, and prescreen promotion hooks; `verify_retry_async.py`
  (async transport), `verify_cron.py` (cadence tick), `live_cache.py`
  and `http_cache.py` (fail-closed HTTP caching layers).
- Outcome recording: `record_outcome.py` (ATS-key enforcement,
  placeholder refusal, consent-gated classification),
  `outcome_analytics.py` (OFFER outcome, fail-closed ledger linking),
  `inbox_listener.py` (OFFER classification, LinkedIn/Indeed
  channel tripwires).
- Queue governance: `queue_intake.py` (single validation point),
  `queue_io.py` (atomic queue writes), `staging_ingest.py`
  (staging → queue worker), `title_triage.py` (staging-side triage).
- Executor-adjacent: `apply_loop.py` (READY-lead buffer/claim engine;
  stops at launch packets per the executor contract),
  `prescreen.py` (posting-eligibility + pre-promotion screens, opt-in
  field-question-protocol hook), `rate_limits.py`.
- Input brokering: `input_broker.py` (external draft brokering with a
  local anti-invention verifier), `lever_submit.py` and
  `breezy_preflight.py` (read-only automation-friendliness probes;
  verdict: browser path only).
- Answer-bank example (`engines/answer_bank.example.json`):
  configurable attestation scope (pre-authorized keys, hard-stop
  classes) — example data only.
- Tests: launch-lock race (`test_launch_lock_x20.py`), evidence gate
  (`test_evidence_gate.py`), input-resolution port
  (`test_input_resolution_port.py`), preference sanitization
  (`test_preferences.py`), cost model/tracker, worker-charter suite.

### Fixed
- `setup.sh` writes queue files in the canonical top-level-list form and
  no longer mutates the tracked `keel.config.json`; identity answers land
  in `data/` only.
- `docs/PERSONALIZE.md` rewritten as the single operator-neutral
  "clone → first launch packet" guide.

## [0.2.0] — 2026-09-15

### Added
- Public web home (`site/`): static GitHub-Pages-ready page with
  `llms.txt`, sitemap, and OG hero image — a visitor-facing front door
  for the repo.
- GEO discoverability layer: citation-ready docs under `docs/geo/`
  (comparison, FAQ, alternatives keyword pages), `llms.txt` at the repo
  root, JSON-LD structured data, `docs/releases.xml` release feed, and
  `docs/assets/demo.gif`.
- GEO measurement pipeline: canonical recount probes and snapshot
  history (`docs/geo/stats.json`,
  `geo-pipeline/history/snapshots.jsonl`) tracking verified submission
  figures over time.
- GitHub Actions CI workflow (`.github/workflows/ci.yml`) running the
  stdlib test suite (`python3 -m unittest discover -s tests`) on every
  push.
- Keel Doctrine (`KEEL_DOCTRINE.md`): one operating spine across the
  five departments.
- `docs/ROADMAP.md`: public roadmap covering shipped work (v0.1.0),
  post-release items, and planned direction — no timelines.
- Keel spine logo (`docs/assets/keel-logo.png`) in the README header.
- Contributor footing: `CONTRIBUTING.md` onboarding,
  `CODE_OF_CONDUCT.md` (Contributor Covenant v2.1, no personal contact
  data), and `.github/FUNDING.yml` sponsorship entry points.
- README cold-visitor audit: the quickstart is genuinely end-to-end —
  a documented demo-seed step bridges `score_roles` output into the
  READY queue so `apply_loop` builds a launch packet; network note
  added (read-only HTTP liveness/form-intel probes); submission figure
  refreshed to the ledger-verified canon (94 verified, 2026-09-15).
- `docs/ARCHITECTURE.md` accuracy pass: telemetry path corrected to
  `data/telemetry/events.jsonl`, event-type list aligned to
  `log_event.py`'s stable list, data-flow step 2 corrected (score_roles
  sets no status — the queue owns it), live re-verify tri-state
  documented, `keel_paths.py` added to the module guide.

### Fixed
- Pre-launch hygiene: CONTRIBUTING test command now matches the README
  (`python3 -m unittest discover -s tests`; stdlib only, no pytest).
- CI: guard `sys.exit` so unittest discovery can import
  `test_employer_patterns`.
- Sample docs cross-references are now working relative links
  (`docs/samples/` → `../SPLIT.md`, `README.md`).
- Launch working docs: removed private-business naming from the
  no-mention rule and private workspace paths from measurement notes.
- README: ledger-verified 55 submissions at launch; unprovable "48
  hours" window dropped.
- GEO layer: unverified rival claims replaced with a verified set;
  launch data-story audited and corrected.
- Cleanliness audit: founder name redacted from launch docs; internal
  docs/credentials quarantined from the public tree via `.gitignore`.

## [0.1.0] — 2026-09-14

First public release of the Keel open-core job-application autopilot.

### Added
- Discovery/scoring engine: search playbooks, sweep prompts, and the
  100-point fit model with a generic scorer template.
- Truthful resume tailoring: templates driven by the operator's applicant
  profile, with truthfulness gates.
- Sanitized answer bank: canonical answers plus banded-question rules and
  hard gates (ships with example data only — real profiles are never
  committed).
- Prescreen gates: automated checks that park leads needing operator
  input instead of inventing answers.
- ATS detection and capability radar: platform identification plus
  HTTP-level probes of which submission paths are viable (detection
  only — no submission behavior).
- Launch-packet builder: the public application loop prepares complete
  launch packets and stops there, per the open-core boundary in
  SPLIT.md.
- `verify_retry`: verification worker that re-checks parked leads over
  plain HTTP and promotes live postings or marks dead ones.
- Feeder watchdog: watches the ready queue and verification pool so a
  dry queue surfaces instead of going silent.
- Telemetry framework: append-only, additive-only event logging.
- Outcome analytics: ledger + telemetry analysis with fail-closed
  reporting.
- Pluggable inbox listener: template for wiring operator responses
  (employer replies, interview invites) back into the system.
- HTML dashboard builder: self-contained dashboard generated from the
  ledger and queues.
- Setup wizard and stock config: guided first run with sane defaults and
  example data.

### Notes
- Licensed under Apache-2.0. See `LICENSE`.
- Public/private boundary documented in `SPLIT.md`.
- Standard library only — no third-party dependencies to install.
