# escape-hatch prototype (G-2 / B-23)

**Status: PROTOTYPE — not wired to production. Policy pins mirror standing rules; Build owns the live integration.**

What it proves (9/9 tests green):
- The router is total: 2,000 fuzzed leads land in exactly one visible state (AUTO / ASSISTED_ACTION / TRAY / SKIPPED), every non-AUTO outcome carrying its reason — no silent dead ends.
- Trent-only blockers (no-AI attestation, travel, essay, references, residence, recording consent) always route to TRAY; the hatch never offers a workaround for settled policy.
- CAPTCHA cap honored: the second failed auto attempt routes to the guided hand-off; automation never takes a third swing.
- Attribution is structural: receipts derive from per-step actors — auto_verified / assisted / user_only. A hand-finished submission can never masquerade as automated.

What Build still owns before this becomes product (see specs/keel-05-gap-acceptance-criteria.md §G-2):
- Adapter to live route detection (api_direct_loop verdicts, browser lane blocks).
- The guided hand-off UX itself (this prototype defines the state machine, not the screen).
- Wiring TRAY outcomes into the tray/inbox surface (G-3/B-24) with existing parking semantics.

Run: `python3 test_escape_hatch.py`
