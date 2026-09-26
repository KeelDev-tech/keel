"""User-facing volume controls (prototype, G-8/B-25).

Standing rule encoded here: the user configures bounded aggression, but user
settings can only TIGHTEN relative to operator caps — never exceed them.
Limits are hard ceilings: `check_launch` DENYs anything over the effective
policy, and the effective policy never exceeds the operator caps.

Clamps are never silent: every clamp is recorded in `clamped_fields`
(field -> (requested, applied)) so the UI can show "you asked for X, cap is Y".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

AGGRESSION_LEVELS = ("low", "standard", "bounded_max")

# Aggression maps to per-run pacing: how many launches the run object may
# attempt per cadence tick before pausing for the next tick.
AGGRESSION_PACING = {
    "low": 1,
    "standard": 3,
    "bounded_max": 5,
}


@dataclass(frozen=True)
class OperatorCaps:
    """Immutable operator-set ceilings. User settings never exceed these."""
    max_submissions_per_run: int = 20
    max_per_day: int = 50
    cadence_minutes: int = 30  # minimum minutes between run ticks
    max_aggression: str = "bounded_max"


@dataclass
class VolumePolicy:
    max_submissions_per_run: int = 10
    max_per_day: int = 25
    cadence_minutes: int = 60
    aggression: str = "standard"
    # Populated by effective_policy: field -> (requested, applied)
    clamped_fields: Dict[str, Tuple[int, int]] = field(default_factory=dict)


@dataclass
class EffectivePolicy:
    policy: VolumePolicy
    clamped_fields: Dict[str, Tuple[Any, Any]]
    pacing_per_tick: int

    def was_clamped(self, name: str) -> bool:
        return name in self.clamped_fields


def _validate_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an int, got {value!r}")
    if value < 1:
        raise ValueError(f"{name} must be >= 1, got {value}")
    return value


def effective_policy(user_policy: VolumePolicy, caps: OperatorCaps) -> EffectivePolicy:
    """Merge a user policy against operator caps.

    Anything the user set above a cap is CLAMPED to the cap and recorded —
    never silently exceeded, never silently accepted-as-set.
    Anything the user tightened (below the cap) is kept as-is.
    """
    clamped: Dict[str, Tuple[Any, Any]] = {}

    def clamp_int(requested: int, cap: int, name: str) -> int:
        requested = _validate_int(requested, name)
        if requested > cap:
            clamped[name] = (requested, cap)
            return cap
        return requested

    merged = VolumePolicy(
        max_submissions_per_run=clamp_int(
            user_policy.max_submissions_per_run, caps.max_submissions_per_run,
            "max_submissions_per_run",
        ),
        max_per_day=clamp_int(
            user_policy.max_per_day, caps.max_per_day, "max_per_day",
        ),
        # Cadence: user may slow down (larger minutes) but may not speed up
        # past the operator floor.
        cadence_minutes=clamp_int(
            user_policy.cadence_minutes, 10 ** 9, "cadence_minutes",
        ),
    )
    # Cadence tighten-check: below the operator minimum -> clamp up + record.
    if merged.cadence_minutes < caps.cadence_minutes:
        clamped["cadence_minutes"] = (user_policy.cadence_minutes, caps.cadence_minutes)
        merged.cadence_minutes = caps.cadence_minutes

    # Aggression: user may only pick a level at or below the operator max.
    aggression = user_policy.aggression
    if aggression not in AGGRESSION_LEVELS:
        raise ValueError(f"aggression must be one of {AGGRESSION_LEVELS}, got {aggression!r}")
    if AGGRESSION_LEVELS.index(aggression) > AGGRESSION_LEVELS.index(caps.max_aggression):
        clamped["aggression"] = (aggression, caps.max_aggression)
        aggression = caps.max_aggression
    merged.aggression = aggression

    merged.clamped_fields = dict(clamped)
    return EffectivePolicy(
        policy=merged,
        clamped_fields=clamped,
        pacing_per_tick=AGGRESSION_PACING[merged.aggression],
    )


@dataclass(frozen=True)
class SavedSearchPolicy:
    """A named, reusable policy preset. Stored as a value, applied purely."""
    name: str
    filters: Dict[str, Any] = field(default_factory=dict)  # search filters, opaque to volume logic
    aggression: str = "standard"
    max_submissions_per_run: Optional[int] = None
    max_per_day: Optional[int] = None
    cadence_minutes: Optional[int] = None


def apply_policy(
    saved: SavedSearchPolicy,
    user_overrides: Dict[str, Any],
    caps: OperatorCaps,
) -> EffectivePolicy:
    """Pure function: saved preset + user overrides + caps -> effective policy.

    Precedence: user_overrides > saved preset > defaults. Caps always win;
    everything exceeding a cap is clamped and recorded.
    """
    merged_kwargs: Dict[str, Any] = {}
    for name in ("max_submissions_per_run", "max_per_day", "cadence_minutes", "aggression"):
        saved_val = getattr(saved, name)
        if name in user_overrides:
            merged_kwargs[name] = user_overrides[name]
        elif saved_val is not None:
            merged_kwargs[name] = saved_val
    # Defaults for anything still unset.
    defaults = {
        "max_submissions_per_run": 10,
        "max_per_day": 25,
        "cadence_minutes": 60,
        "aggression": "standard",
    }
    for name, default in defaults.items():
        merged_kwargs.setdefault(name, default)
    return effective_policy(VolumePolicy(**merged_kwargs), caps)


@dataclass(frozen=True)
class LaunchDecision:
    allowed: bool
    reason: str
    ceiling: Optional[str] = None  # which ceiling fired on DENY


def check_launch(
    eff: EffectivePolicy,
    attempts_today: int,
    attempts_this_run: int,
) -> LaunchDecision:
    """Hard-ceiling gate: returns DENY with a cited reason the moment any
    limit would be exceeded. Limits are ceilings, not targets."""
    p = eff.policy
    if attempts_this_run >= p.max_submissions_per_run:
        return LaunchDecision(
            allowed=False,
            reason=(
                f"run limit reached: {attempts_this_run} attempts against "
                f"max_submissions_per_run={p.max_submissions_per_run}"
            ),
            ceiling="max_submissions_per_run",
        )
    if attempts_today >= p.max_per_day:
        return LaunchDecision(
            allowed=False,
            reason=(
                f"daily limit reached: {attempts_today} attempts against "
                f"max_per_day={p.max_per_day}"
            ),
            ceiling="max_per_day",
        )
    return LaunchDecision(allowed=True, reason="within effective policy")
