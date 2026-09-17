# Architecture

## Pipeline stages

```
 DISCOVERY            SCORING             MATERIALS            VERIFICATION
 discovery_queries    fit-scoring-model   resume_tailor        verify_retry
 sweep_worker_prompt  score_roles         cover_letter_        feeder_watchdog
                                          generator
      |                   |                    |                    |
      v                   v                    v                    v
                      +-----------------------------------------------+
                      |            PRE-LAUNCH SCREEN                    |
                      |  prescreen.py (PARK before spend)               |
                      |  employer_patterns.py · rate_limits.py          |
                      +----------------------+------------------------+
                                             |
                                             v
                                      apply_loop.py
                                      (launch packet)
                                             |
                        +--------------------+--------------------+
                        |                                         |
                 PUBLIC STOPS HERE                    PRIVATE EXECUTION LAYER
                 launch packet +                      (not in this repo;
                 EXECUTOR CONTRACT                     see SPLIT.md)
                        |
                        v
              record_outcome.py → data/telemetry/events.jsonl
              log_event.py      → append-only gate/lead events
                        |
                        v
              outcome_analytics.py · inbox_listener.py → dashboard
```

## Module guide

| Module | Stage | What it does |
|---|---|---|
| `discovery_queries.md` | discovery | Search-query playbooks per lane, with placeholders you fill |
| `sweep_worker_prompt.md` | discovery | Prompt template for running discovery sweeps |
| `fit-scoring-model.md` | scoring | The 100-point rubric + banded action policy (source of truth) |
| `score_roles.py` | scoring | Template scorer: hard-requirement classifier wired, judgment components yours to fill |
| `resume_tailor.py` | materials | PDF resume from your applicant profile; truthfulness-checked |
| `cover_letter_generator.md` | materials | Cover-letter prompt template with placeholders |
| `answer_bank.example.json` | materials | Canonical answers, banded rules, hard gates (copy → personalize) |
| `verify_retry.py` | verification | Re-verifies parked leads; run singleton, 24h cooldowns, pool cursor, stale-park guard, sync/async parity probe; promotes LIVE, buries dead |
| `verify_retry_async.py` | verification | Async (aiohttp) transport for the verify pool: per-host/global semaphores, jitter; importable without aiohttp (fail-closed) |
| `verify_cron.py` | verification | Standing cadence tick: PID lockfile, cursor, run records, dry-run by default |
| `live_cache.py` | verification | Fail-closed 2h liveness cache so the verify pool stops re-fetching known-live postings |
| `http_cache.py` | verification | Process-local shared GET cache for verify workers |
| `genuine_pat.py` | verification | Genuine-input classifier: genuinely operator-blocked leads stay parked; verification-only items return to the pool |
| `title_triage.py` | intake | Staging-side title-family triage; keeps the verify HTTP budget on promotable leads |
| `feeder_watchdog.py` | verification | Queue-health monitor; refills via verify_retry when dry+idle |
| `queue_intake.py` | intake | Single validation point for every entry written to the queue files |
| `queue_io.py` | intake | Atomic queue-file IO (lock-protected read-modify-write) |
| `staging_ingest.py` | intake | Persistent staging → queue ingestion worker |
| `dedupe_gate.py` | intake | Discovery-side duplicate guard: one lead, one queue |
| `launch_lock.py` | launch | One live application task per role (atomic lock + pre-launch duplicate/twin-submit guard) |
| `prescreen.py` | screen | Pre-launch screen: commitment/essay/attestation/question checks, posting-eligibility + pre-promotion screens; PARK verdicts go to your input queue; opt-in FRP hook (`KEEL_FRP=1`) |
| `employer_patterns.py` | screen | Per-employer blind-ATS priors (example file; yours is private) |
| `rate_limits.py` | screen | Employer application budgets (defaults; override per employer) |
| `ats.py` | detection | ATS platform detection from URLs (read-only) |
| `ats_discovery/` | discovery | Board-enumeration adapters for extra ATS platforms (detection only) |
| `greenhouse_json_enumerate.py` | discovery | Greenhouse JSON board enumeration (detection only) |
| `ashby_lever_json_enumerate.py` | discovery | Ashby/Lever JSON board enumeration (detection only) |
| `greenhouse_board_classify.py` | discovery | Classifies Greenhouse boards for enumeration |
| `clean_board_watch.py` | discovery | Watches enumerated boards for new postings |
| `page_capture.py` | detection | Read-only posting-page capture for evidence |
| `edge_probe.py` | detection | Monthly capability radar: CONFIRMED/FLIP/INCONCLUSIVE probes |
| `api_direct_detect.py` | detection | Detects direct-board URL candidates (detection only) |
| `lever_submit.py` | detection | Read-only Lever automation-friendliness probe (verdict: browser path only) |
| `breezy_preflight.py` | detection | Read-only Breezy apply-page probe: CAPTCHA wall, question set, honeypots (flagged, never filled) |
| `form_intel.py` | intel | Pre-launch form intelligence over HTTP (read-only probes) |
| `input_resolution/` | input | Structured handling of operator-input blockers: collapse, metrics, dry-run reports, apply with backups |
| `input_broker.py` | input | Brokers applicant-blocked input gaps to an external draft model; local verifier refuses invented content; nothing unparks |
| `field_question_protocol.py` | input | One-time-cost required-question protocol: classify once, bank verifiable answers, instant-park applicant-only questions |
| `keel_paths.py` | paths | Single resolver for the workspace root: `KEEL_HOME`, default `~/keel`. No module hardcodes a path. |
| `apply_loop.py` | packet | Builds launch packets for eligible READY leads; stops at launch packets per the executor contract |
| `packet_watchdog.py` | packet | Starvation watchdog: flags IN-FLIGHT packets with no launch claim |
| `parked_task_sweep.py` | packet | Never-halt sweeper: proposes closing parked tasks holding lane slots (never applicant-only items) |
| `batch_staged_launches.py` | packet | Wake-up batch planner: re-checks the launch-lock guard per staged entry, emits bounded spawn instructions |
| `staged-launch-preflight.py` | packet | Re-validates staged launches (guard, freshness, budget, prescreen) before firing |
| `log_event.py` | telemetry | Append-only event logging; never rewrites history |
| `record_outcome.py` | telemetry | Outcome telemetry writer: ATS-key enforcement, placeholder refusal, consent-gated classification |
| `cost_model.py` | telemetry | Per-submission cost dimensions recorded additively on ledger rows (unmetered dims never read as 0.0) |
| `cost_tracker.py` | telemetry | Cost series builder: least-squares trends, strict compounding verdict, gaps explicit |
| `lane_watchdog.py` | telemetry | Sustainability red-line evaluator against live data; one deduped gate per breach window |
| `outcome_tracking/` | analytics | Evidence gate for submission claims; invite/offer surfaces over the telemetry log; capture-rate audits |
| `outcome_analytics.py` | analytics | Lane-level ack/reject/invite rates; fail-closed on thin data |
| `inbox_listener.py` | analytics | Employer-response intake; pluggable MailSource (Maildir default) |
| `worker-charter/` | charter | Autonomous-worker prompt template + envelope ingest/validation; the public assembler mines operator exemplars, never a private library |
| `build_dashboard.py` | dashboard | Self-contained HTML dashboard from ledger + queues |

## Data flow

1. Discovery produces raw leads → `data/scored.json` via `score_roles.py`
   (carries `fit_score`, `action_band`, `score_breakdown` — no `status`).
2. Scored leads enter `data/queues/*.json` with `fit_score`, `action_band`,
   and `status` (the queue owns status; `score_roles` does not set it).
3. `apply_loop.py` picks the highest-fit eligible lead (`status` READY,
   `action_band` APPLY, blocklist/ledger/rate-limit/materials gates, plus a
   pre-launch live re-verify: HTTP 200 proceeds, 404/410 skips as dead,
   anything else unverifiable proceeds with a note and the executor
   re-verifies), runs `form_intel`, builds a generic brief, writes
   `data/launch-packets/<role_id>.json`, marks the lead IN-FLIGHT.
4. `prescreen.py` screens every packet first; PARK verdicts park the lead in
   your input queue — the packet never reaches an executor.
5. Your executor (private layer, or your own) consumes the packet under the
   EXECUTOR CONTRACT in the brief, then reports the outcome.
6. Outcomes land in the ledger + `data/telemetry/events.jsonl`; analytics and the
   dashboard read from there. History is append-only — corrections are new
   events, never rewrites.

## Key design decisions

- **One lead, one queue.** A role_id lives in exactly one queue file.
  Deduping is by role_id; reruns never emit duplicate rows.
- **Evidence-only counts.** The dashboard increments submissions only on
  ledger rows with explicit confirmation. Counts are never narrated ahead
  of evidence.
- **Fail closed.** Unverifiable liveness, unmappable required questions,
  missing attestations: park, never proceed. Thin outcome data (< 5) reads
  "insufficient outcome data", never a rate.
- **Truthfulness gates.** The answer bank is the single source of truth;
  banded questions have pre-approved rules; the resume tailor reports
  qualification gaps instead of bridging them.
- **No fingerprinting surface.** Detection is public; submission behavior
  is private (SPLIT.md).
- **Config over code.** All paths resolve under `KEEL_HOME` (default
  `~/keel`); personal data lives in `data/` working copies, never in
  the repo tree that ships.

## Telemetry schema

`data/telemetry/events.jsonl` (KEEL_HOME-relative) — one JSON object per line:

```json
{"ts": "2026-09-15T02:00:00Z", "type": "brief_built",
 "role_id": "EXAMPLE-001", "company": "Example Corp", "ats": "greenhouse",
 "source": "apply_loop", "details": {"fit_score": 84}}
```

Event types (the stable list documented in `log_event.py`): `lead_discovered`,
`lead_verified`, `lead_dead`, `brief_built`, `browser_launched`,
`gate_encountered`, `gate_cleared`, `gate_blocked`, `account_created`,
`submitted`, `employer_response`, `error`. The fine-grained vocabulary
(`edge_flip`, `new_ats_detected`, `feeder_empty`, `stale_inflight`, …) lives in
`details.gate`, centralized in `log_event.py`'s `GATE_TYPES` — those are gate
names, not event types. Secret-looking detail keys (passwords, tokens, codes,
cookies) are scrubbed and never logged.
