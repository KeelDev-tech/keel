# blocker-inbox prototype (G-3 / B-24)

**Status: PROTOTYPE — not wired to production. Policy pins mirror standing rules; Build owns the live integration.**

What it proves (8/8 tests green):
- One card per distinct question across leads (fuzzy dedupe: spacing variants collapse), each with gate citation, recurrence count, and lead set.
- Machine-suggested answers can never enter the bank and never fill a packet — the resolver abstains until a human confirms.
- User-confirmed answers bank with provenance (his_words | user_edited) and trace to the exact confirming message; answers without a source message are refused.
- Retro-resolution unblocks exactly the blocked set (3 leads on the shared essay question; the 2 Salesforce blockers untouched) with honest before/after counts.
- Rejected suggestions are recorded and leave the blocker open.

What Build still owns before this becomes product (see specs/keel-05-gap-acceptance-criteria.md §G-3):
- Adapter from the live answer_bank.json schema (needs question_hash / expires_at / scope / source_ref / attestation_key per B-20 prerequisites).
- The inbox UI itself (this prototype defines the state machine, not the screen).
- Wiring TRAY outcomes from the escape hatch (G-2/B-23) into inbox cards with existing tray parking semantics.

Run: `python3 test_blocker_inbox.py`
