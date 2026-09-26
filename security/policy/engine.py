"""Deterministic policy engine: the LLM requests; this engine decides.

evaluate(agent, action, resource, context) -> Evaluation with decision in
{ALLOW, DENY, REQUIRE_APPROVAL, QUARANTINE}.

Inputs weighed: identity, resource, action, environment, risk, provenance,
approval state, system state (safe mode). No LLM anywhere in this path.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from enum import Enum

from ..actions.classifier import ActionClass, classify
from ..errors import CapabilityDenied
from ..identity.capabilities import CapabilityManifest, required_capability
from ..identity.agent_identity import Identity, IdentityRegistry
from ..policy_version import POLICY_VERSION
from .risk import risk_score

_RULES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "rules", "base_rules.json")
_BLOCKER_RULES_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "rules", "blocker_resolution.json")


class Decision(Enum):
    ALLOW = "ALLOW"
    DENY = "DENY"
    REQUIRE_APPROVAL = "REQUIRE_APPROVAL"
    QUARANTINE = "QUARANTINE"


@dataclass
class Evaluation:
    decision: Decision
    reasons: list[str]
    risk_score: int
    risk_factors: list[str]
    policy_version: str
    action_class: ActionClass
    required_capability: str | None = None

    @property
    def allowed(self) -> bool:
        return self.decision == Decision.ALLOW

    def to_dict(self) -> dict:
        return {"decision": self.decision.value, "reasons": self.reasons,
                "risk_score": self.risk_score,
                "risk_factors": self.risk_factors,
                "policy_version": self.policy_version,
                "action_class": self.action_class.value,
                "required_capability": self.required_capability}


def _load_rules() -> dict:
    """Load and validate rules/base_rules.json against the schema contract.

    Hand-rolled validation (stdlib only): required keys, class decisions
    cover all ActionClasses, severity actions cover HIGH/MEDIUM/LOW,
    policy_version format. Any violation -> fail closed (raise).
    """
    with open(_RULES_PATH, "r", encoding="utf-8") as f:
        rules = json.load(f)
    required = ("policy_version", "class_decisions",
                "injection_severity_action", "risk_deny_threshold",
                "safe_mode_allowed_classes")
    missing = [k for k in required if k not in rules]
    if missing:
        raise ValueError(f"policy rules invalid: missing keys {missing}")
    if not re.match(r"^\d{4}-\d{2}-\d{2}\.\d+$",
                    str(rules["policy_version"])):
        raise ValueError("policy rules invalid: bad policy_version format")
    want_classes = {c.value for c in ActionClass}
    got_classes = set(rules["class_decisions"])
    if want_classes != got_classes:
        raise ValueError(
            "policy rules invalid: class_decisions must cover exactly "
            f"{sorted(want_classes)}; got {sorted(got_classes)}")
    want_sev = {"HIGH", "MEDIUM", "LOW"}
    if set(rules["injection_severity_action"]) != want_sev:
        raise ValueError("policy rules invalid: injection_severity_action "
                         "must cover HIGH/MEDIUM/LOW")
    allowed_decisions = {"allow_if_capability",
                         "allow_if_capability_and_evidence",
                         "require_approval", "deny_unless_approval", "deny"}
    bad = [k for k, v in rules["class_decisions"].items()
           if v not in allowed_decisions]
    if bad:
        raise ValueError(f"policy rules invalid: unknown class decisions "
                         f"for {bad}")
    if rules["policy_version"] != POLICY_VERSION:
        raise ValueError(
            f"policy rules version {rules['policy_version']} != "
            f"policy_version.py {POLICY_VERSION} — bump together")
    return rules


_RULES: dict | None = None
_BLOCKER_RULES: dict | None = None
_BLOCKER_MISSING = object()  # sentinel: file absent -> fallback deny


def get_rules() -> dict:
    global _RULES
    if _RULES is None:
        _RULES = _load_rules()
    return _RULES


def _reset_caches() -> None:
    """Test support only — never call in production paths."""
    global _RULES, _BLOCKER_RULES
    _RULES = None
    _BLOCKER_RULES = None


# ---------------------------------------------------------------------------
# Blocker Resolution Directive (§9, SECURITY-AGENT ENFORCEMENT)
#
# Six deterministic policies delivered as rules/blocker_resolution.json.
# This layer runs before all other rules (after the safe-mode kill switch)
# and can only DENY or abstain — it never grants. No LLM may override it.
# ---------------------------------------------------------------------------

# Known policy ids (Trent's Blocker Resolution Directive §9). Used ONLY as
# a fail-closed fallback when the rules file is absent: a missing directive
# file must not silently unblock these actions.
_BLOCKER_KNOWN_IDS = frozenset({
    "api_direct_write", "external_egress", "synthetic_to_live",
    "human_decision_impersonation", "publication", "keel_0_8_promotion",
})

_BLOCKER_EFFECTS = frozenset({"DENY", "DENY_ALWAYS", "CONDITIONAL_DENY"})

# Condition mini-language: "<fact> <op> <value>", one per string.
# Supported ops: == != > < >= <= . Values: true/false, numbers, quoted or
# bare strings (PASS, PRESENT, hex hashes — all exact match).
_COND_RE = re.compile(
    r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*(==|!=|>=|<=|>|<)\s*(.+?)\s*$")


def _blocker_matcher(policy_id: str, action_name: str,
                     action_class: ActionClass) -> bool:
    """Deterministic policy -> action matching.

    Category-style policies match by action class (per their scope);
    everything else matches by exact action name. Unknown future policy
    ids fall back to exact-name matching (fail closed).
    """
    if policy_id == "external_egress":
        # Scope: "any data egress to an external system or party".
        return action_class == ActionClass.EXTERNAL_COMMUNICATION
    if policy_id == "publication":
        return action_name == "publication" or \
            action_name.startswith("publish_")
    return action_name == policy_id


def _parse_condition_value(token: str):
    t = token.strip()
    if len(t) >= 2 and t[0] == t[-1] and t[0] in ("'", '"'):
        return t[1:-1]
    low = t.lower()
    if low == "true":
        return True
    if low == "false":
        return False
    try:
        return int(t)
    except ValueError:
        pass
    try:
        return float(t)
    except ValueError:
        pass
    return t  # bare token: PASS, PRESENT, hex hash, ...


def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _values_equal(actual, expected) -> bool:
    # Booleans are type-strict (True != 1 here — authorization flags are
    # never truthy-coerced).
    if isinstance(actual, bool) or isinstance(expected, bool):
        return type(actual) is type(expected) and actual == expected
    if _is_num(actual) and _is_num(expected):
        return float(actual) == float(expected)
    return actual == expected


def _eval_condition(cond_str: str, facts: dict) -> tuple[bool, str]:
    """Evaluate one condition string against caller-asserted facts."""
    m = _COND_RE.match(cond_str)
    if not m:
        return False, f"malformed condition {cond_str!r} — fail closed"
    fact, op, token = m.group(1), m.group(2), m.group(3)
    if fact not in facts:
        return False, f"fact {fact!r} not presented — fail closed"
    actual = facts[fact]
    expected = _parse_condition_value(token)
    if op == "==":
        ok = _values_equal(actual, expected)
    elif op == "!=":
        ok = not _values_equal(actual, expected)
    elif op in (">", "<", ">=", "<="):
        if not (_is_num(actual) and _is_num(expected)):
            return False, (f"condition {cond_str!r}: non-numeric "
                           "comparison — fail closed")
        a, e = float(actual), float(expected)
        ok = {">": a > e, "<": a < e, ">=": a >= e, "<=": a <= e}[op]
    else:
        return False, f"unsupported operator {op!r} — fail closed"
    return ok, (f"condition {cond_str!r}: "
                f"{'satisfied' if ok else 'NOT satisfied'}")


def _validate_blocker_rules(data: dict) -> dict:
    """Validate blocker_resolution.json against the schema contract.

    Hand-rolled (stdlib only), mirroring policy.schema.json's
    blockerResolution definition. Raises ValueError on any violation —
    an invalid directive file is loud, never silently downgraded.
    """
    if not isinstance(data, dict):
        raise ValueError("blocker rules invalid: top-level must be an object")
    for key in ("version", "source", "policies"):
        if key not in data:
            raise ValueError(f"blocker rules invalid: missing key {key!r}")
    policies = data["policies"]
    if not isinstance(policies, list) or not policies:
        raise ValueError("blocker rules invalid: 'policies' must be a "
                         "non-empty list")
    seen = set()
    for p in policies:
        if not isinstance(p, dict):
            raise ValueError("blocker rules invalid: each policy must be "
                             "an object")
        pid = p.get("id")
        if not pid or not isinstance(pid, str):
            raise ValueError("blocker rules invalid: policy missing "
                             "string 'id'")
        if pid in seen:
            raise ValueError(f"blocker rules invalid: duplicate id {pid!r}")
        seen.add(pid)
        effect = p.get("effect")
        if effect not in _BLOCKER_EFFECTS:
            raise ValueError(
                f"blocker rules invalid: policy {pid!r} has unknown effect "
                f"{effect!r}; known: {sorted(_BLOCKER_EFFECTS)}")
        conds = p.get("conditions", [])
        if not isinstance(conds, list) or \
                any(not isinstance(c, str) for c in conds):
            raise ValueError(f"blocker rules invalid: policy {pid!r} "
                             "'conditions' must be a list of strings")
        if effect == "CONDITIONAL_DENY" and not conds:
            raise ValueError(f"blocker rules invalid: policy {pid!r} is "
                             "CONDITIONAL_DENY with no conditions")
        if effect in ("DENY", "DENY_ALWAYS") and conds:
            raise ValueError(f"blocker rules invalid: policy {pid!r} is "
                             f"{effect} but carries conditions")
        for c in conds:
            if not _COND_RE.match(c):
                raise ValueError(
                    f"blocker rules invalid: policy {pid!r} has malformed "
                    f"condition {c!r}")
    return data


def _load_blocker_rules():
    """Load blocker_resolution.json. Returns the validated dict, or the
    _BLOCKER_MISSING sentinel when the file is absent (fallback deny).
    Raises ValueError when the file exists but is invalid."""
    if not os.path.exists(_BLOCKER_RULES_PATH):
        return _BLOCKER_MISSING
    with open(_BLOCKER_RULES_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    return _validate_blocker_rules(data)


def get_blocker_rules():
    global _BLOCKER_RULES
    if _BLOCKER_RULES is None:
        _BLOCKER_RULES = _load_blocker_rules()
    return _BLOCKER_RULES


def _apply_blocker_rules(action_name: str, action_class: ActionClass,
                         facts: dict) -> tuple[Evaluation | None, list[str]]:
    """Apply the Blocker Resolution Directive layer.

    Returns (deny_evaluation, reasons) when a policy denies, or
    (None, reasons) when every matching policy abstains (evaluation
    continues through the normal rules).
    """
    blocker = get_blocker_rules()
    reasons: list[str] = []
    if blocker is _BLOCKER_MISSING:
        # Fail closed: without the directive file, the six named actions
        # (and the egress class) cannot be verified — deny them.
        if action_name in _BLOCKER_KNOWN_IDS or \
                action_class == ActionClass.EXTERNAL_COMMUNICATION:
            return Evaluation(
                decision=Decision.DENY,
                reasons=[f"blocker-resolution: rules file absent — "
                         f"{action_name!r} fail-closed (directive §9)"],
                risk_score=100, risk_factors=["blocker_rules_absent"],
                policy_version=POLICY_VERSION, action_class=action_class,
                required_capability=None), []
        return None, ["blocker-resolution: rules file absent; action not in "
                      "directive scope — continuing"]
    for policy in blocker["policies"]:
        pid = policy["id"]
        if not _blocker_matcher(pid, action_name, action_class):
            continue
        effect = policy["effect"]
        if effect in ("DENY", "DENY_ALWAYS"):
            return Evaluation(
                decision=Decision.DENY,
                reasons=[f"blocker-resolution: policy {pid!r} "
                         f"({effect}) — {policy.get('description', '')}"],
                risk_score=100,
                risk_factors=[f"blocker_policy:{pid}"],
                policy_version=POLICY_VERSION, action_class=action_class,
                required_capability=None), []
        # CONDITIONAL_DENY: all conditions must hold, else DENY.
        cond_results = [_eval_condition(c, facts)
                        for c in policy["conditions"]]
        failed = [why for ok, why in cond_results if not ok]
        if failed:
            return Evaluation(
                decision=Decision.DENY,
                reasons=[f"blocker-resolution: policy {pid!r} "
                         f"(CONDITIONAL_DENY) denied — "
                         + "; ".join(failed)],
                risk_score=100,
                risk_factors=[f"blocker_policy:{pid}"],
                policy_version=POLICY_VERSION, action_class=action_class,
                required_capability=None), []
        reasons.append(f"blocker-resolution: policy {pid!r} conditions "
                       f"satisfied ({len(cond_results)}/{len(cond_results)}) "
                       "— abstaining")
    return None, reasons


def evaluate(agent: dict, action: dict, resource: dict | None,
             context: dict | None) -> Evaluation:
    """Deterministic authorization decision.

    agent:   {"identity": Identity, "manifest": CapabilityManifest,
              "delegation": Delegation | None}
    action:  {"name": str, "action_class": ActionClass | None}
    resource:{"type": str, "id": str, "sensitivity": str}
    context: {"safe_mode": bool, "injection_severity": str | None,
              "evidence": dict | None, "environment": str, ...}
    """
    rules = get_rules()
    context = context or {}
    resource = resource or {}
    reasons: list[str] = []

    action_name = action.get("name", "")
    action_class = action.get("action_class")
    if action_class is None:
        action_class, why = classify(action_name)
        reasons.append(f"class:{action_class.value} ({why})")
    else:
        reasons.append(f"class:{action_class.value} (explicit)")

    sensitivity = str(resource.get("sensitivity", "INTERNAL")).upper()
    injection_sev = context.get("injection_severity")
    if injection_sev:
        injection_sev = str(injection_sev).upper()

    canonical_class, _ = classify(action_name)
    if action_class != canonical_class:
        return _deny(reasons + ["action class differs from canonical classification"],
                     canonical_class, None, sensitivity, injection_sev,
                     bool(agent.get("delegation")), rules)

    identity = agent.get("identity")
    if (not isinstance(identity, Identity)
            or IdentityRegistry.get(identity.id) != identity
            or IdentityRegistry.is_revoked(identity.id)):
        return _deny(reasons + ["identity unregistered, altered, or revoked"],
                     action_class, None, sensitivity, injection_sev,
                     bool(agent.get("delegation")), rules)

    manifest: CapabilityManifest = agent.get("manifest")
    if manifest is None:
        return _deny(reasons + ["no capability manifest — default deny"],
                     action_class, None, sensitivity, injection_sev,
                     bool(agent.get("delegation")), rules)

    # 1. Safe mode: only allow-listed classes pass.
    if context.get("safe_mode"):
        allowed_classes = rules["safe_mode_allowed_classes"]
        if action_class.value not in allowed_classes:
            return _deny(
                reasons + [f"safe mode engaged: {action_class.value} not in "
                           f"{allowed_classes}"],
                action_class, None, sensitivity, injection_sev,
                bool(agent.get("delegation")), rules)

    # 1b. Blocker Resolution Directive (§9): deterministic deny-layer.
    # Runs after the kill switch, before everything else. It can only
    # DENY or abstain — never grant. No LLM may override these.
    blocker_deny, blocker_reasons = _apply_blocker_rules(
        action_name, action_class, context.get("facts") or {})
    reasons = blocker_reasons + reasons
    if blocker_deny is not None:
        blocker_deny.reasons = blocker_deny.reasons + reasons
        return blocker_deny

    # The exported generic action API has no trusted adapter binding an
    # authenticated approval to destination, current artifact bytes and
    # attempt. Caller booleans and ApprovalStore grants cannot supply that
    # missing boundary. Hold execution until the host adapter is reviewed.
    if (canonical_class == ActionClass.EXTERNAL_COMMUNICATION
            or action_name in {"browser_submit", "submit_application"}
            or _blocker_matcher("publication", action_name, canonical_class)):
        return _deny(reasons + ["trusted host execution/egress binding absent — held"],
                     action_class, None, sensitivity, injection_sev,
                     bool(agent.get("delegation")), rules)

    # 2. Delegation validity (when acting under delegation).
    delegation = agent.get("delegation")
    if delegation is not None:
        from ..identity.delegation import DelegationRegistry
        valid, why = DelegationRegistry.is_valid(delegation)
        if not valid:
            return _deny(reasons + [f"delegation invalid: {why}"],
                         action_class, None, sensitivity, injection_sev,
                         True, rules)
        if (delegation.delegate_id != identity.id or
                IdentityRegistry.get(delegation.delegator_id) is None or
                IdentityRegistry.is_revoked(delegation.delegator_id)):
            return _deny(reasons + ["delegation actor mismatch or delegator unavailable/revoked"],
                         action_class, None, sensitivity, injection_sev,
                         True, rules)
        reasons.append("delegation registered, actor-bound, unexpired and unrevoked")

    # 3. Capability check — default deny.
    cap = required_capability(action_name)
    if cap is None:
        return _deny(
            reasons + [f"action {action_name!r} maps to no known capability "
                       "— default deny"], action_class, None, sensitivity,
            injection_sev, bool(delegation), rules)
    if delegation is not None and cap not in delegation.capabilities:
        return _deny(reasons + ["required capability outside delegated subset"],
                     action_class, cap, sensitivity, injection_sev, True, rules)
    try:
        manifest.check(cap)
    except CapabilityDenied as e:
        return _deny(reasons + [str(e)], action_class, cap, sensitivity,
                     injection_sev, bool(delegation), rules)
    reasons.append(f"capability {cap!r} granted")

    # 4. Risk ceiling is a hard deny BEFORE any approval-producing branch.
    score, factors = risk_score(action_class, sensitivity, injection_sev,
                                bool(delegation))
    if score >= rules["risk_deny_threshold"]:
        return _deny(reasons + [f"risk ceiling: score {score} >= "
                                f"{rules['risk_deny_threshold']}"],
                     action_class, cap, sensitivity, injection_sev,
                     bool(delegation), rules, score=score, factors=factors)

    # 5. Injection severity gates.
    if injection_sev:
        sev_action = rules["injection_severity_action"][injection_sev]
        if sev_action == "quarantine":
            return _eval(Decision.QUARANTINE,
                         reasons + [f"injection {injection_sev}: quarantine "
                                    "per policy"], action_class, cap,
                         sensitivity, injection_sev, bool(delegation), rules)
        if sev_action == "deny":
            return _deny(reasons + [f"injection {injection_sev}: deny per "
                                    "policy"], action_class, cap,
                         sensitivity, injection_sev, bool(delegation), rules)
        if sev_action == "require_approval":
            return _eval(Decision.REQUIRE_APPROVAL,
                         reasons + [f"injection {injection_sev}: approval "
                                    "required per policy"], action_class, cap,
                         sensitivity, injection_sev, bool(delegation), rules)
        reasons.append(f"injection {injection_sev}: noted, proceeding")

    # 6. Class decision from policy rules.
    class_rule = rules["class_decisions"][action_class.value]
    if class_rule == "deny":
        return _deny(reasons + [f"class {action_class.value}: deny per "
                                "policy"], action_class, cap, sensitivity,
                     injection_sev, bool(delegation), rules,
                     score=score, factors=factors)
    if class_rule == "deny_unless_approval":
        return _eval(Decision.REQUIRE_APPROVAL,
                     reasons + [f"class {action_class.value}: human approval "
                                "required, never auto-permitted"],
                     action_class, cap, sensitivity, injection_sev,
                     bool(delegation), rules, score=score, factors=factors)
    if class_rule == "require_approval":
        return _eval(Decision.REQUIRE_APPROVAL,
                     reasons + [f"class {action_class.value}: approval "
                                "required per policy"], action_class, cap,
                     sensitivity, injection_sev, bool(delegation), rules,
                     score=score, factors=factors)
    if class_rule == "allow_if_capability_and_evidence":
        evidence = context.get("evidence")
        if not evidence:
            return _deny(
                reasons + [f"class {action_class.value}: no evidence "
                           "presented — fail closed"], action_class, cap,
                sensitivity, injection_sev, bool(delegation), rules,
                score=score, factors=factors)
        reasons.append("evidence presented")

    reasons.append(f"class {action_class.value}: allow (capability held)")
    return _eval(Decision.ALLOW, reasons, action_class, cap, sensitivity,
                 injection_sev, bool(delegation), rules,
                 score=score, factors=factors)


def _eval(decision: Decision, reasons: list[str], action_class: ActionClass,
          cap: str | None, sensitivity: str, injection_sev: str | None,
          delegated: bool, rules: dict,
          score: int | None = None,
          factors: list[str] | None = None) -> Evaluation:
    if score is None:
        score, factors = risk_score(action_class, sensitivity, injection_sev,
                                    delegated)
    return Evaluation(decision=decision, reasons=reasons, risk_score=score,
                      risk_factors=factors or [],
                      policy_version=rules["policy_version"],
                      action_class=action_class, required_capability=cap)


def _deny(reasons: list[str], action_class: ActionClass, cap: str | None,
          sensitivity: str, injection_sev: str | None, delegated: bool,
          rules: dict, score: int | None = None,
          factors: list[str] | None = None) -> Evaluation:
    return _eval(Decision.DENY, reasons, action_class, cap, sensitivity,
                 injection_sev, delegated, rules, score=score, factors=factors)
