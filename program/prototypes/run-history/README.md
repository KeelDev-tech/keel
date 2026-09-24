# run-history prototype (G-1 / B-22)

**Status: PROTOTYPE — not wired to production. Do not point at live telemetry or claim as product.**

What it proves (6/6 tests green):
- Per-run history rows fold exactly from append-only telemetry — scans, promotions, confirmed/ambiguous/failed attempts, parked leads.
- Zero-activity runs render honestly ("0 promotions this run — no activity recorded") instead of vanishing.
- Ambiguous submissions never leak into confirmed counts.
- Pause/resume replays cannot double-count an attempt (dedupe on stable attempt_id — the F22 identity requirement).
- Unknown or malformed events are counted in `unmapped_events` and surfaced, never silently dropped.

What Build still owns before this becomes product (see specs/keel-05-gap-acceptance-criteria.md §G-1):
- Adapter from the live telemetry schema (this prototype uses a clean event schema; the live log's field mapping is Build's call).
- Provenance-per-number in the UI and the T2 unaided-first-run target.
- Coexistence with operator caps (user settings tighten only).
- The full acceptance checklist: 10 replay scenarios, no-double-run, zero-mismatch rows.

Run: `python3 test_run_history.py`
