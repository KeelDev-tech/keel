# Worker Charter System

Turns the autonomous-worker prompt template into a working process by filling
its three dynamic slots from operator-supplied sources — no placeholders left
empty, ever (the assembler asserts this).

## Files

- `CHARTER_TEMPLATE.md` — the charter template. Do not edit the template's
  prose; worker-type differences are injected by the assembler.
- `assemble_charter.py` — fills the three slots:
  - `DYNAMIC_FEW_SHOT_EXEMPLARS` ← the operator's `exemplars.example.json`
    (evidence of what actually worked, not theory). OPEN-CORE BOUNDARY: the
    private production pipeline mines this slot from a per-ATS technique
    library, which is deliberately NOT included here (see SPLIT.md).
  - `DYNAMIC_NEGATIVE_CONSTRAINTS` ← ID-tagged operating lessons
    (C-01..C-10, C-16, C-17 shared; C-11/C-12/C-15/C-18/C-19/C-20
    executor-only; C-13/C-14 cron-only)
  - `AVAILABLE_TOOLS_AND_SCHEMAS` ← the real tool surface a child agent has
- `exemplars.example.json` — example exemplar file; copy to your own and
  replace the entries with your own evidence-based exemplars, or point
  `$KEEL_CHARTER_EXEMPLARS` at your file.
- `charter.<worker-type>.<date>.md` — assembled charters, ready to hand to a
  worker subagent.
- `ingest_envelope.py` — the single logging path for charter workers. Workers
  must NOT call `log_event.py` or write the outcome evidence store themselves
  (C-11). The ingester validates the JSON envelope strictly, routes each
  attempt into `record_outcome.py` exactly once, **fail-closes** any
  `submitted` claim without quoted confirmation evidence (C-12/C-05), and
  appends novel edge cases to `edge-case-review.md` for the evaluator loop.
  Resume-lane resolution uses the configurable `$KEEL_RESUME_FILENAME_RE`
  pattern (default matches a generic `<name>_<LANE>_` naming convention).
- `edge-case-review.md` — created on first real ingestion; the evaluator's
  reading list.

## Worker types

- `verify-test` — HTTP-only posting verification (read-only).
- `application-executor` — prepares one application attempt per task
  (re-verify → packet build → form intel → launch packet), reports the
  attempt in the envelope; the parent performs the live browser submission
  from the packet under the executor contract.
- `cron-worker` — read-only observer for scheduled jobs (C-13/C-14). The
  periodic status ping runs under this charter and must emit the JSON envelope.

## Usage

```bash
python3 assemble_charter.py application-executor   # build charter
# hand charter.<type>.<date>.md to the worker; worker returns JSON envelope
python3 ingest_envelope.py /path/to/envelope.json --dry-run   # validate first
python3 ingest_envelope.py /path/to/envelope.json             # ingest
python3 mine_learnings.py                  # mine draft learning proposals
python3 loop_watchdog.py --dry-run         # check evaluator-loop health
```

## Self-improvement loop (Loop 1)

- `mine_learnings.py` — clusters `edge-case-review.md` entries and recurring
  telemetry patterns (gates/errors since watermark) into draft proposals in
  `learning-proposals.md`. Watermark in `.mine-watermark`.
- `charter-evaluator-weekly` cron (Mon ~08:00 PT) — runs the miner, triages
  new proposals: PROMOTE (with exact new C-ID/E-ID text and destination) or
  DEFER. Promotion to a real constraint stays human-approved.
- `loop_watchdog.py` — tripwire on evaluator staleness/backlog; publishes
  one deduped blackboard finding per day when tripped.

Path convention: these modules live at the top-level `worker-charter/` dir
and resolve paths locally with a `$KEEL_HOME` fallback — they never reference
the private pipeline's workspace path.
