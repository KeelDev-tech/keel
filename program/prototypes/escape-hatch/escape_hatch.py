"""Designed execution escape hatch (prototype, G-2/B-23).

When a lead's route is unsupported, the lead lands in exactly ONE honest,
visible state — never a silent dead end, never a policy violation, never
manufactured evidence.

States:
  AUTO            — automation proceeds (clean route).
  ASSISTED_ACTION — bounded user action with a one-sentence reason
                    (finish_in_browser | answer_one_question | skip_route).
  TRAY            — Trent-only blocker; routes to tray with parking semantics.
                    The escape hatch never re-opens settled policy.
  SKIPPED         — route skipped, reason recorded.

Provenance: every completed step records its actor (automation | user).
Receipt labels derive from the actor split:
  all automation -> "auto_verified"
  mixed          -> "assisted"      (never masquerades as auto_verified)
  all user       -> "user_only"

Standing policy encoded here (prototype pins it so tests guard it):
  - CAPTCHA: max two auto attempts; the second failure routes to the escape
    hatch (finish_in_browser) — automation never retries a third time.
  - Trent-only reasons ALWAYS go to TRAY, never to an assisted action.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

AUTO = "AUTO"
ASSISTED_ACTION = "ASSISTED_ACTION"
TRAY = "TRAY"
SKIPPED = "SKIPPED"

# Settled policy: these blockers belong to Trent alone. The escape hatch must
# never offer a user-side workaround for them.
TRENT_ONLY_REASONS = {
    "no_ai_attestation",
    "travel_commitment",
    "essay",
    "reference_contact",
    "residence_unknown",
    "recording_consent",
}

MAX_CAPTCHA_AUTO_ATTEMPTS = 2


@dataclass
class Lead:
    lead_id: str
    api_lane_available: bool = True
    browser_lane_blocked: Optional[str] = None  # None | "captcha" | "verification" | "unsupported_route"
    captcha_attempts: int = 0
    attestation_required: Optional[str] = None  # None or a reason string
    manual_step: Optional[str] = None           # None or description of a required manual step
    drop_reason: Optional[str] = None           # user/operator chose to skip the route


@dataclass
class HatchOutcome:
    lead_id: str
    state: str
    reason: str = ""
    action: Optional[Dict[str, Any]] = None
    steps: List[Dict[str, str]] = field(default_factory=list)  # {step, actor}


def route(lead: Lead) -> HatchOutcome:
    """Total function: every lead lands in exactly one visible state."""
    # 1. Explicit skip is recorded, not lost.
    if lead.drop_reason:
        return HatchOutcome(
            lead_id=lead.lead_id, state=SKIPPED, reason=lead.drop_reason
        )

    # 2. Trent-only blockers go to the tray. Never an assisted workaround.
    if lead.attestation_required in TRENT_ONLY_REASONS:
        return HatchOutcome(
            lead_id=lead.lead_id,
            state=TRAY,
            reason=f"Trent-only blocker: {lead.attestation_required} — parked with existing tray semantics",
        )

    # 3. CAPTCHA: two auto attempts max, then the hatch (guided handoff).
    if lead.browser_lane_blocked == "captcha" and lead.captcha_attempts >= MAX_CAPTCHA_AUTO_ATTEMPTS:
        return HatchOutcome(
            lead_id=lead.lead_id,
            state=ASSISTED_ACTION,
            reason="Browser blocked by CAPTCHA after 2 auto attempts — finish by hand",
            action={"kind": "finish_in_browser", "steps": ["open guided session", "complete CAPTCHA", "resume submission"]},
        )

    # 4. Unsupported routes / verification blocks the policy won't auto-handle.
    if lead.browser_lane_blocked in ("unsupported_route", "verification") and not lead.api_lane_available:
        if lead.manual_step:
            return HatchOutcome(
                lead_id=lead.lead_id,
                state=ASSISTED_ACTION,
                reason=f"No supported route — {lead.manual_step}",
                action={"kind": "answer_one_question", "question": lead.manual_step},
            )
        return HatchOutcome(
            lead_id=lead.lead_id,
            state=ASSISTED_ACTION,
            reason="No supported route for this lead — finish in browser or skip",
            action={"kind": "finish_in_browser", "steps": ["open guided session", "complete by hand"]},
        )

    # 5. Clean route: automation proceeds.
    return HatchOutcome(lead_id=lead.lead_id, state=AUTO, reason="supported route")


def complete_step(outcome: HatchOutcome, step: str, actor: str) -> None:
    """Record a completed step with its actor. Actor must be automation|user."""
    assert actor in ("automation", "user"), f"bad actor: {actor}"
    outcome.steps.append({"step": step, "actor": actor})


def receipt_label(outcome: HatchOutcome) -> str:
    """Derive the completion label from the actor split. Never invented."""
    actors = {s["actor"] for s in outcome.steps}
    if not actors:
        return "no_steps_recorded"
    if actors == {"automation"}:
        return "auto_verified"
    if actors == {"user"}:
        return "user_only"
    return "assisted"
