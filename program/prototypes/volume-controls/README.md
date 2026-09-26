# volume-controls prototype (G-8 / B-25)

**Status: PROTOTYPE — not wired to production. Never pointed at live telemetry, the live engine, or the keel/ mirror.**

What it proves (11/11 tests green):
- **Tightening is honored, never punished:** user values below operator caps are kept exactly; nothing is clamped when nothing exceeds.
- **Exceeding a cap is clamped and recorded:** any user value above an operator cap is reduced to the cap, and the clamp is recorded as `(requested, applied)` per field — never silently exceeded, never silently accepted-as-set. This holds for per-run and per-day limits, the cadence floor (user may slow down but not speed up past the operator minimum), and aggression (user level may not exceed the operator max).
- **Hard ceilings hold under load:** a 500-attempt sequential stress run launched exactly 50 (daily cap binding across run boundaries) and 450 DENYs; a run-ceiling-only run launched exactly 7. Zero overruns — the limits are ceilings, not targets.
- **Saved-search policies are pure:** `apply_policy(saved, overrides, caps)` has precedence overrides > saved preset > defaults; the output is still clamp-checked against caps.
- **DENYs cite their reason:** every DENY carries a human-readable reason naming the ceiling and the numbers (e.g. "run limit reached: 20 attempts against max_submissions_per_run=20").
- **Guards at the edges:** non-int values, non-positive ints, and unknown aggression levels are rejected; `OperatorCaps` is frozen (immutable ceilings).

What Build still owns before this becomes product:
- The settings **UI** — the run object screen (G-1/B-22) must show the clamp receipts ("you asked for 999/run; the cap is 20") so clamps stay visible, never silent.
- The **live adapter** — operator caps wired to real operator config, `attempts_today` / `attempts_this_run` sourced from the append-only telemetry log (dedupe on stable F22 attempt IDs), `check_launch` called at every launch decision.
- Aggression pacing semantics (low=1, standard=3, bounded_max=5 launches per cadence tick) need product review — currently a prototype constant.
- The **T2 unaided target**: 8/10 first-run users configure their volume unaided within 15 minutes, per the gap acceptance criteria (specs/keel-05-gap-acceptance-criteria.md).

Run: `python3 test_volume_controls.py`
