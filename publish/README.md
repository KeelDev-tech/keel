# Publication control — Keel Blocker Resolution Directive §3

**Publication is currently DISABLED.** G1 (privacy-counsel authorization) is unresolved.
`publication_enabled` defaults to `False`; the machine refuses to enter
`PUBLICATION_AUTHORIZED`/`PUBLISHED` while disabled or while the G1 decision
cannot be verified. Do not enable it without the coordinator.

## Layout

- `state_machine.py` — the gate sequence (stdlib only, no credentials/network).
- `tests/test_state_machine.py` — 32 regression tests (stdlib unittest).

Run: `python3 tests/test_state_machine.py`

## Integration contract (for the coordinator)

1. Construct: `PublicationStateMachine(release_id, counsel_verifier=<callable>, publication_enabled=False)`.
2. Wire `counsel_verifier` to workstream B's `keel/privacy/counsel_decision.py`
   verification function. Signature: `(release_id: str, artifact_digest: str) -> bool`.
   `None` / non-True / raising all deny.
3. Drive the public flow methods; each enforces its current state and evidence gate:
   `record_evidence(...)` → `advance_to_tested()` → `advance_to_security_reviewed()`
   → `advance_to_privacy_review_ready()` → `freeze_release(bytes)` →
   `record_g1_decision(...)` → `advance_to_g1_approved()` →
   `authorize_publication()` → `publish(bytes)`.
4. Call `check_mutation(current_bytes)` whenever release bytes might have changed
   after freezing — mutation resets to `PRIVACY_REVIEW_READY` (no exceptions).
5. Persist with `snapshot()` / `PublicationStateMachine.restore(snap, counsel_verifier=...)`.
6. This module never writes to `keel/privacy/` or `keel/security/` — sibling workstreams.
