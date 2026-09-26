#!/usr/bin/env python3
"""Deterministic, evidence-referenced fit assessment; never launch authority.

See docs/SCORING_CONTRACT.md. Only explicit structured applicant facts and
posting criteria are compared. Free text, incoming scores, and a posting's
claim that a candidate is VERIFIED cannot establish applicant qualifications.
"""
from __future__ import annotations

import argparse
import json
import math
from fractions import Fraction

MAXIMA = {
    "experience_alignment": 25, "transferable_skills": 15,
    "hard_requirements": 20, "career_upside": 10, "compensation": 10,
    "founder_advantage": 5, "industry_alignment": 5,
    "location_work_model": 5, "employer_quality": 5,
}
COMPONENTS = list(MAXIMA)
SUBJECTIVE = {"career_upside", "founder_advantage", "industry_alignment", "employer_quality"}
VERSION = "keel-evidence-fit-v1"
MAX_ITEMS = 128


def _require(ok, message):
    if not ok:
        raise ValueError(message)


def _text(value, label):
    _require(type(value) is str and bool(value.strip()) and len(value) <= 2048,
             f"{label} must be nonempty text of at most 2048 characters")
    return value.strip()


def _number(value, label, minimum=0, maximum=1_000_000_000):
    _require(type(value) in (int, float) and minimum <= value <= maximum
             and math.isfinite(value), f"{label} must be a finite number in [{minimum}, {maximum}]")
    return value


def _items(value, label):
    _require(type(value) is list and len(value) <= MAX_ITEMS, f"{label} must be a list of at most {MAX_ITEMS} items")
    return value


def _refs(value, label):
    refs = [_text(ref, label) for ref in _items(value, label)]
    _require(bool(refs) and len(refs) == len(set(refs)), f"{label} needs unique evidence references")
    return refs


def _value(value, label):
    if type(value) is bool:
        return value
    if type(value) in (int, float):
        return _number(value, label, minimum=-1_000_000_000)
    if type(value) is str:
        return _text(value, label)
    if type(value) is list:
        values = [_text(item, label) for item in _items(value, label)]
        normalized = [item.casefold() for item in values]
        _require(len(normalized) == len(set(normalized)), f"{label} contains duplicate values")
        return values
    raise ValueError(f"{label} has an unsupported fact type")


def _bounded_mapping(value, label):
    _require(type(value) is dict, f"{label} must be an object")
    try:
        serialized = json.dumps(value, allow_nan=False)
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValueError(f"{label} must be finite JSON") from exc
    _require(len(serialized) <= 262144, f"{label} exceeds 256 KiB input limit")


def band(score):
    """Display band only. This function grants no action permission."""
    _number(score, "score", maximum=100)
    return _display_band(score)


def _display_band(score):
    if score >= 90:
        return "PRIORITY"
    if score >= 82:
        return "APPLY"
    if score >= 72:
        return "STRATEGIC"
    return "SKIP"


def _facts(profile):
    facts = profile.get("scoring_facts", {})
    _require(type(facts) is dict and len(facts) <= MAX_ITEMS, "scoring_facts must be a bounded object")
    result = {}
    for key, fact in facts.items():
        _text(key, "fact key")
        _require(type(fact) is dict and set(fact) == {"value", "evidence_refs"}, "fact needs value and evidence_refs only")
        result[key] = {"value": _value(fact["value"], key), "evidence_refs": _refs(fact["evidence_refs"], key)}
    return result


def _criterion(item, facts, label, index):
    _require(type(item) is dict, f"{label} criterion must be an object")
    allowed = {"id", "name", "fact", "operator", "value", "source_ref", "weight", "mandatory", "status"}
    _require(not set(item) - allowed, f"{label} criterion contains unsupported keys")
    identifier = _text(item.get("id", item.get("name", f"{label}-{index}")), "criterion id")
    weight = _number(item.get("weight", 1), "criterion weight", minimum=0.000001, maximum=100)
    # Legacy labels can preserve a hold but cannot certify qualifications.
    legacy = item.get("status", "UNKNOWN")
    _require(type(legacy) is str and legacy in {"VERIFIED", "MISSING", "PARTIAL", "UNKNOWN"}, "unsupported requirement status")
    mandatory = item.get("mandatory", True)
    _require(type(mandatory) is bool, "mandatory must be a boolean")
    record = {"id": identifier, "weight": weight, "mandatory": mandatory,
              "status": "UNKNOWN", "fraction": None, "profile_evidence_refs": [],
              "posting_source_ref": None, "reason": "structured_criterion_missing"}
    if not any(key in item for key in ("fact", "operator", "value", "source_ref")):
        # Uncorroborated negative claims are conservatively reported separately.
        if legacy in {"MISSING", "PARTIAL"}:
            record["legacy_hold"] = legacy
        return record
    fact_key = _text(item.get("fact"), "criterion fact")
    source = _text(item.get("source_ref"), "posting source_ref")
    op = item.get("operator")
    _require(type(op) is str and op in {"eq", "gte", "lte", "contains", "all_of", "any_of"}, "unsupported comparison operator")
    wanted = _value(item.get("value"), "criterion value")
    if op in {"gte", "lte"}:
        _number(wanted, "numeric criterion", minimum=-1_000_000_000)
    elif op == "contains":
        _text(wanted, "contains criterion")
    elif op in {"all_of", "any_of"}:
        _require(type(wanted) is list and bool(wanted), "set criterion needs a nonempty string list")
    record.update({"fact": fact_key, "operator": op, "posting_source_ref": source,
                   "reason": "applicant_fact_missing"})
    if fact_key not in facts:
        return record
    fact = facts[fact_key]
    actual = fact["value"]
    record["profile_evidence_refs"] = fact["evidence_refs"]
    if op in {"gte", "lte"}:
        _number(actual, "numeric applicant fact", minimum=-1_000_000_000)
        fraction = float(actual >= wanted if op == "gte" else actual <= wanted)
    elif op in {"contains", "all_of", "any_of"}:
        _require(type(actual) is list, "set comparison requires an applicant string list")
        found = {item.casefold() for item in actual}
        expected = {wanted.casefold()} if op == "contains" else {item.casefold() for item in wanted}
        hits = len(found & expected)
        fraction = (float(bool(hits)) if op == "any_of" else hits / len(expected))
    else:
        # Equality never coerces bool to int or matches a substring.
        _require(type(actual) is type(wanted) or
                 type(actual) in (int, float) and type(wanted) in (int, float), "equality fact type mismatch")
        left = actual.casefold() if type(actual) is str else actual
        right = wanted.casefold() if type(wanted) is str else wanted
        fraction = float(left == right)
    record.update({"status": "VERIFIED" if fraction == 1 else "MISSING" if fraction == 0 else "PARTIAL",
                   "fraction": fraction, "reason": "explicit_fact_comparison",
                   "credit_numerator": hits if op == "all_of" else int(fraction),
                   "credit_denominator": len(expected) if op == "all_of" else 1})
    if legacy in {"MISSING", "PARTIAL"}:
        # A structured match must not silently erase an existing adverse finding.
        record["legacy_hold"] = legacy
    return record


def _assessment(profile, role_id, component):
    assessments = profile.get("role_assessments", [])
    _items(assessments, "role_assessments")
    found = None
    for row in assessments:
        _require(type(row) is dict and set(row) == {"role_id", "component", "fraction", "reviewer", "rationale", "evidence_refs"},
                 "human assessment has unsupported or missing keys")
        _text(row["role_id"], "assessment role_id")
        _require(type(row["component"]) is str and row["component"] in SUBJECTIVE, "human assessments only support subjective components")
        _require(type(row["reviewer"]) is dict and set(row["reviewer"]) == {"kind", "id"}
                 and row["reviewer"]["kind"] == "human", "assessment requires a human reviewer record")
        _text(row["reviewer"]["id"], "reviewer id")
        _text(row["rationale"], "assessment rationale")
        _number(row["fraction"], "assessment fraction", maximum=1)
        _refs(row["evidence_refs"], "assessment evidence_refs")
        if row["role_id"] == role_id and row["component"] == component:
            _require(found is None, "duplicate role component assessment")
            found = row
    return found


def score_role(role, profile):
    """Return conservative score bounds and fit eligibility, never authorization.

    The caller supplies a trusted profile. Evidence references are auditable
    links, not cryptographic verification or instructions to fetch content.
    """
    _bounded_mapping(role, "role")
    _bounded_mapping(profile, "profile")
    facts = _facts(profile)
    criteria = role.get("scoring_criteria", {})
    _require(type(criteria) is dict and not set(criteria) - (set(COMPONENTS) - {"hard_requirements"}),
             "scoring_criteria contains unsupported components")
    requirements = _items(role.get("hard_requirements", []), "hard_requirements")
    reviewed = role.get("requirements_reviewed", False)
    _require(type(reviewed) is bool, "requirements_reviewed must be a boolean")
    requirements_source = role.get("requirements_source_ref")
    if reviewed:
        _text(requirements_source, "reviewed requirements_source_ref")
    holds = [_text(hold, "hold") for hold in _items(role.get("holds", []), "holds")]
    blocks = ["canonical_hold_present"] if holds else []
    if not reviewed:
        blocks.append("requirements_not_reviewed")
    evidence, breakdown, bounds = {}, {}, {}
    exact_lower = exact_upper = exact_known = Fraction(0)
    role_id = role.get("role_id")
    for component, maximum in MAXIMA.items():
        entries = requirements if component == "hard_requirements" else _items(criteria.get(component, []), component)
        records = [_criterion(item, facts, component, index) for index, item in enumerate(entries)]
        ids = [entry["id"] for entry in records]
        _require(len(ids) == len(set(ids)), f"duplicate {component} criterion id")
        review = _assessment(profile, role_id, component)
        _require(not (review and records), "component cannot combine criteria and a human assessment")
        if component == "hard_requirements":
            for record in records:
                if record["mandatory"] and (record["status"] != "VERIFIED" or "legacy_hold" in record):
                    blocks.append(f"mandatory_requirement:{record['id']}:{record.get('legacy_hold', record['status'])}")
        if review:
            low = high = maximum * Fraction(str(review["fraction"]))
            known = maximum
            evidence[component] = {"method": "human_assessment", "assessment": review}
        elif records:
            total_weight = sum(Fraction(str(record["weight"])) for record in records)
            low = maximum * sum(
                Fraction(str(record["weight"])) * Fraction(record["credit_numerator"], record["credit_denominator"])
                for record in records if record["fraction"] is not None) / total_weight
            unknown = maximum * sum(Fraction(str(record["weight"])) for record in records
                                    if record["fraction"] is None) / total_weight
            high, known = low + unknown, maximum - unknown
            evidence[component] = {"method": "explicit_fact_comparison", "criteria": records}
        elif component == "hard_requirements" and reviewed:
            low = high = known = maximum
            evidence[component] = {"method": "reviewed_no_requirements", "posting_source_ref": requirements_source}
        else:
            low, high, known = 0, maximum, 0
            evidence[component] = {"method": "unknown", "reason": "no_supported_criteria_or_assessment"}
        exact_lower += low
        exact_upper += high
        exact_known += known
        # Outward display rounding must never turn a lower bound into an
        # unsupported higher score; eligibility uses exact fractions below.
        bounds[component] = {"lower": math.floor(low * 1_000_000) / 1_000_000,
                             "upper": math.ceil(high * 1_000_000) / 1_000_000,
                             "known_weight": math.floor(known * 1_000_000) / 1_000_000}
        breakdown[component] = bounds[component]["lower"] if known else None
    lower = math.floor(exact_lower * 10_000) / 10_000
    upper = math.ceil(exact_upper * 10_000) / 10_000
    coverage = math.floor(exact_known * 10_000) / 10_000
    if exact_lower < 82:
        blocks.append("fit_below_apply_threshold" if exact_upper < 82 else "fit_evidence_incomplete")
    eligible = not blocks
    canonical = "APPLY" if eligible else "LOW-FIT" if exact_upper < 72 and not holds else "HOLD"
    display = _display_band(exact_lower)
    # Operational fields from a discovery row must not make scorer output READY.
    return {
        **role,
        "status": "PARKED", "approval_valid": False,
        "fit_score": lower, "fit_score_upper": upper,
        "score_breakdown": breakdown, "score_bounds": bounds,
        "score_evidence": evidence, "score_coverage_percent": coverage,
        "score_state": "COMPLETE" if exact_known == 100 else "INCOMPLETE",
        "scoring_version": VERSION, "display_band": display,
        "action_band": canonical,
        "recommended_action": "APPLY" if eligible else "SKIP" if canonical == "LOW-FIT" else "HOLD",
        "fit_eligible": eligible, "execution_authorized": False,
        "action_eligibility": {"fit_eligible": eligible, "execution_authorized": False,
                               "blocked_reasons": sorted(set(blocks)),
                               "required_gates": ["canonical_readiness", "current_policy", "human_approval"]},
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--in", dest="inp", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--profile", default="applicant_profile.json")
    args = ap.parse_args()
    with open(args.inp, encoding="utf-8") as handle:
        roles = json.load(handle)
    _require(type(roles) is list and len(roles) <= 10000, "input must be a list of at most 10000 roles")
    with open(args.profile, encoding="utf-8") as handle:
        profile = json.load(handle)
    scored = [score_role(role, profile) for role in roles]
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(scored, handle, indent=2, allow_nan=False)
        handle.write("\n")
    for row in sorted(scored, key=lambda item: item["fit_score"], reverse=True):
        print(f'{row["fit_score"]:6.2f}–{row["fit_score_upper"]:6.2f} {row["action_band"]:8s} {row.get("company", "?")} — {row.get("title", "?")}')


if __name__ == "__main__":
    main()
