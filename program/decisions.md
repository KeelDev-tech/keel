# Pending Trent Decisions — Program Office log

**Rule:** Trent decides; agents never do. Items here are blocked on his word only. Batched into the weekly digest; phase-gated items surface when their phase approaches, not before.

| # | Decision | From | Blocks | Status |
|---|---|---|---|---|
| PD-1 | Phase-4 adapter pilots: per-route approval + pilot scope (which boards first, how many live applications, success criteria) | Keel Build reconciliation design §10 | Phase 4 only — phases 0–3 proceed on design/test gates | LOGGED |
| PD-2 | Authenticated-decision threshold: who signs CONFIRMED_SUBMITTED during pilot — Trent per case, or a named trusted verifier? | Keel Build reconciliation design §10 | Pilot execution | LOGGED |
| PD-3 | Receipt archive location: confirm `hidden_files/` alongside submit-intents.json (retained with operational state) | Keel Build reconciliation design §10 | Archive implementation (phase 0–1) | LOGGED |
| PD-4 | Browser dispatch boundary: durable intent before browser submit | Keel Build reconciliation design §10 — **routed to main chat only** (Build will not implement from a side chat) | D3 green first | LOGGED — main-chat proposal pending |
| PD-5 | Embed-endpoint fallback per-employer contract (P2 from Greenhouse renderer audit) — needs implementation, tests, **and Trent approval** | Keel Build renderer audit | After P1 (verdict persistence + browser routing) | LOGGED |

**Cross-department hooks recorded:**
- Phase 3 (holds/UNKNOWN/READY) requires Keel Pipeline Performance sign-off before any behavior change. No ship without it.
- No browser dispatch work from side chats — main chat owns the lane exclusively.

Logged 2026-09-17 12:11 PDT.

## Declined / closed without action (no repeated prompting)
| # | Item | Record |
|---|---|---|
| D-1 | Release-on-refuse approval tap — Trent skipped it 2026-09-17 | Do not re-prompt. Path forward is the autonomous engine design (lock released when the acquiring loop itself refuses), with tests — register B-E1. |
