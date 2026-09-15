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
              record_outcome.py → telemetry/events.jsonl
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
| `verify_retry.py` | verification | Re-verifies parked leads; promotes LIVE, buries dead |
| `feeder_watchdog.py` | verification | Queue-health monitor; refills via verify_retry when dry+idle |
| `prescreen.py` | screen | Pre-launch screen: PARK verdicts go to your input queue |
| `employer_patterns.py` | screen | Per-employer blind-ATS priors (example file; yours is private) |
| `rate_limits.py` | screen | Employer application budgets (defaults; override per employer) |
| `ats.py` | detection | ATS platform detection from URLs (read-only) |
| `edge_probe.py` | detection | Monthly capability radar: CONFIRMED/FLIP/INCONCLUSIVE probes |
| `api_direct_detect.py` | detection | Detects direct-board URL candidates (detection only) |
| `form_intel.py` | intel | Pre-launch form intelligence over HTTP (read-only probes) |
| `apply_loop.py` | packet | Builds launch packets for eligible READY leads |
| `log_event.py` | telemetry | Append-only event logging; never rewrites history |
| `record_outcome.py` | telemetry | Outcome telemetry writer (technique-library hooks stubbed) |
| `outcome_analytics.py` | analytics | Lane-level ack/reject/invite rates; fail-closed on thin data |
| `inbox_listener.py` | analytics | Employer-response intake; pluggable MailSource (Maildir default) |
| `build_dashboard.py` | dashboard | Self-contained HTML dashboard from ledger + queues |

## Data flow

1. Discovery produces raw leads → `data/scored.json` via `score_roles.py`.
2. Scored leads enter `data/queues/*.json` with `fit_score`, `action_band`,
   `status`.
3. `apply_loop.py` picks the highest-fit eligible lead (`status` READY,
   `action_band` APPLY, blocklist/ledger/rate-limit/materials/live-posting
   gates), runs `form_intel`, builds a generic brief, writes
   `data/launch-packets/<role_id>.json`, marks the lead IN-FLIGHT.
4. `prescreen.py` screens every packet first; PARK verdicts park the lead in
   your input queue — the packet never reaches an executor.
5. Your executor (private layer, or your own) consumes the packet under the
   EXECUTOR CONTRACT in the brief, then reports the outcome.
6. Outcomes land in the ledger + `telemetry/events.jsonl`; analytics and the
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

`telemetry/events.jsonl` — one JSON object per line:

```json
{"ts": "2026-09-15T02:00:00Z", "type": "brief_built",
 "role_id": "EXAMPLE-001", "company": "Example Corp", "ats": "greenhouse",
 "source": "apply_loop", "details": {"fit_score": 84}}
```

Event types: `lead_discovered`, `lead_verified`, `gate_blocked`,
`brief_built`, `prescreen_parked`, `submitted`, `employer_response`,
`edge_flip`, `new_ats_detected`, `feeder_empty`, `stale_inflight`.
Gate vocabulary is centralized in `log_event.py`.
