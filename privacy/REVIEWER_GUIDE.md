# Privacy Release Controller — Owner & Reviewer Guide (G1)

This controller prepares privacy-counsel review and enforces the decision.
It can NEVER grant G1 itself. This guide describes the human ceremony that
makes a decision real.

## The rule

Nothing leaves the private workspace for publication / offering / pricing /
sale without a **verified counsel decision**. "Verified" means ALL of:

1. The decision was recorded through `record_decision()` (never hand-written
   into the store — hand-written records break the seal chain and fail).
2. Its `authority_reference` names an authority in the registry, registered
   BEFORE the decision was made.
3. It binds the EXACT artifact SHA-256. Changed bytes => changed digest =>
   the old approval no longer matches. Re-review is required.
4. It has not expired (`expires_at` / `review_interval_days` are mandatory)
   and has not been revoked.

With an empty authority registry (the shipped default), `verify_decision()`
is False for everything and every egress check returns DENY.

## Owner ceremony: registering the counsel authority

Only the workspace owner (Trent) performs this, once per counsel engagement.
The `authority_reference` must come from the real engagement — the engagement
letter, contract, or the counsel's own hand. Never invent it.

```python
import sys, os
sys.path.insert(0, os.path.expanduser("~/workspace"))
from keel.privacy import register_authority

register_authority({
    "authority_reference": "ENG-2026-0042",   # from the engagement letter
    "authority_name": "Jane Counsel, Example Privacy LLP",
    "authority_type": "external_privacy_counsel",
    "scope_note": "G1 review for Keel public launch artifacts",
    "engagement_start": "2026-09-20T00:00:00Z",
    "engagement_end": "2026-12-31T23:59:59Z",
    "registered_by": "Name Example (owner ceremony, main chat 2026-09-20)",
    "registration_evidence": "engagement letter dated 2026-09-19, on file",
    # 64-hex Ed25519 public key, supplied OUT-OF-BAND by the reviewer.
    # The matching private key NEVER exists on this machine.
    "reviewer_public_key": "<64-hex public key from counsel>",
})
```

The registry is append-only and seal-chained. A reference can never be
re-registered; a typo needs a new reference recorded explicitly.

## Reviewer ceremony: signing a decision (reviewer-side, off this machine)

Decisions are unforgeable: each one carries an Ed25519 signature over its
binding fields, made with the reviewer's private key. The private key never
touches this machine — counsel signs on their own computer with the
reference snippet below (standard library only):

```python
import sys
sys.path.insert(0, "/path/to/a/copy/of/keel/privacy")  # for _ed25519.py
from _ed25519 import sign
from counsel_decision import decision_signing_payload  # canonical form

SECRET_SEED = bytes.fromhex("<counsel's 64-hex private seed, kept secret>")
payload = decision_signing_payload(
    release_id="PR-20260921-abcdef",
    artifact_digest="<64-hex sha256 of the reviewed bytes>",
    decision="approved_with_conditions",   # or approved | denied | needs_information
    actor="Jane Counsel",
    authority_reference="ENG-2026-0042",
    decided_at="2026-09-21T15:04:00Z",
    expires_at="2026-12-20T15:04:00Z",     # resolved expiry (no interval math here)
    conditions=["suppress buckets with n<5 before publishing"],
    packet_digest="<packet_digest from the reviewed packet>",
)
signature_hex = sign(SECRET_SEED, payload).hex()  # 128 hex chars
# The signature travels back with the decision fields (email, portal, etc.).
```

Without that signature, `record_decision()` refuses the decision and
`verify_decision()` returns False. No agent, LLM, orchestrator, or
deployment process on this machine can produce it.

## Reviewer flow: from packet to decision

1. The controller builds a review packet:
   `packet = prepare_packet(...)` — machine-readable, all 22 required fields.
2. The packet is handed to counsel OUT OF BAND (email, portal, print —
   never through an agent-controlled channel as the sole path).
3. Counsel returns a decision with the same fields they reviewed
   (release_id, artifact digests, packet_digest). The owner (or their
   explicitly delegated operator) records it:

```python
from keel.privacy import record_decision

record_decision({
    "release_id": packet["release_id"],
    "artifact_digest": packet["artifact_sha256"]["artifact-001"],
    "decision": "approved_with_conditions",  # or approved | denied | needs_information
    "actor": "Jane Counsel",
    "actor_contact": "jane@example-llp.example",
    "authority_reference": "ENG-2026-0042",
    "conditions": ["suppress buckets with n<5 before publishing"],
    "decided_at": "2026-09-21T15:04:00Z",
    "review_interval_days": 90,
    "packet_digest": packet["packet_digest"],
})
```

4. Egress code calls `check_egress(release_id, digest)` (or
   `assert_egress_allowed(...)`) BEFORE any external send. Allow only on
   `allowed=True`.

## Revocation

```python
from keel.privacy import revoke_decision
revoke_decision(release_id, digest, actor="Jane Counsel",
                authority_reference="ENG-2026-0042",
                reason="new re-identification analysis supersedes the approval")
```

Revocation is append-only and immediate: `verify_decision()` returns False
from that moment.

## What the controller refuses to do

- Mint an approval on its own (no such code path exists).
- Accept a decision from an unregistered authority.
- Accept a decision timestamped at or before the authority's registration
  (instant self-registration + approval is refused).
- Accept a decision with no expiry.
- Verify against a tampered ledger (any seal break => global fail-closed).
- Verify a digest that does not exactly match the recorded binding.

## Residual boundary

The controller guarantees structural integrity. It cannot prove the human
behind a registration is the real counsel — that is the owner's operational
control (this ceremony), performed outside the machine's reach.
