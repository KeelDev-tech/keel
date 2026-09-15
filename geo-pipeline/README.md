# Keel GEO measurement pipeline

The countable half of Keel's GEO (generative-engine-optimization) operation.
It keeps every public number honest: counted from the private production
pipeline's ledger and telemetry, never estimated, and refreshed on a
schedule. The other half — the monthly AI-engine citation spot-check — stays
a human/browser-executed protocol (see `docs/geo/spot-check-protocol.md`);
this pipeline does not fake it.

## What it is

Three scripts, one schedule:

| Script | Does | Writes |
|---|---|---|
| `recount.py` | Canonical recount of ledger + telemetry | `docs/geo/stats.json` (atomic), JSON to stdout |
| `probe.py` | Public-web probes, no auth, 15s timeouts | JSON to stdout only |
| `snapshot.py` | Runs both, archives the pair | `history/snapshots.jsonl` + one `outcome_observed` event in the marketing telemetry |

All three are public-safe: they read private local files (`~/workspace/job-pipeline/...`)
but output aggregates only. No credentials, no PII, no private project names,
no absolute local paths in output.

## Methodology (v2, 2026-09-15)

Full text lives in `recount.py`'s docstring. The short version:

- **Verified submissions** = ledger rows with status `SUBMITTED`. The ledger
  is *counted*, never re-verified, never estimated. The evidence split
  (explicit-confirmation evidence / posting-URL only / unevidenced) comes
  from the canonical reader `engines/outcome-tracking/evidence_gate.py`.
- **Launch figure 55** is a historical canon (counted 2026-09-15 00:17 PT).
  It is a constant — never recomputed.
- **Gate figures** = distinct `(role_id, gate)` pairs in `gate_blocked`
  events. Backfilled events dedupe *on the pair*: a backfilled pair that
  also appears live counts once; backfilled-only pairs are real historical
  stops and count. (2026-09-15 correction: the old "drop all backfilled"
  rule was verified to be an undercount — zero overlap between live and
  backfilled pairs in the log.) Queue-level events without `role_id` and
  `TEST-` fixtures are excluded.
- **Outcomes** (`INTERVIEW_INVITED`, `REJECTED`) counted directly from ledger
  statuses. `telemetry_events` = non-blank lines in `events.jsonl`.
- **Fail-closed:** any source read error → non-zero exit, no partial write.

## How to run

```bash
# Recount only (rewrites docs/geo/stats.json):
python3 geo-pipeline/recount.py

# Public probes only:
python3 geo-pipeline/probe.py

# Full snapshot (recount + probe + history + telemetry):
python3 geo-pipeline/snapshot.py
```

Paths are env-configurable: `PIPELINE_LEDGER`, `PIPELINE_TELEMETRY`,
`EVIDENCE_GATE`, `STATS_JSON`.

## Schedules

- **Weekly snapshot** (`keel-geo-pipeline-weekly`, goal-owned): runs
  `snapshot.py`. If `stats.json` changed, the run also refreshes
  `docs/data-story.md` and the launch-copy traction figures from the
  recount output, then commits + pushes (only when GitHub auth is valid
  and the public-safety gates pass — never auto-push blind).
- **Monthly AI-engine spot-check** (`docs/geo/spot-check-protocol.md`):
  six fixed questions × six engines (ChatGPT, Claude, Gemini, Copilot,
  Perplexity, Grok), fresh sessions, verbatim quotes, every cited URL
  verified. Executed by a human/browser session — this pipeline only
  handles the countable half. Check monthly; more often is noise.

## Outputs

- `docs/geo/stats.json` — the machine-readable canon (consumed by the site,
  `llms.txt` generators, and launch copy).
- `docs/data-story.md` — the human-readable data story; its tables are
  regenerated from recount output, prose stays hand-written.
- `history/snapshots.jsonl` — append-only record of every snapshot
  (recount + probe pairs).
- Marketing telemetry `outcome_observed` events (`avenue_id:
  geo-pipeline-snapshot`) — the scoreboard feed.
