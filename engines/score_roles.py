#!/usr/bin/env python3
"""Keel role scorer — TEMPLATE.

Scores discovered roles 0-100 using the fit-scoring model
(engines/fit-scoring-model.md) and emits queue-ready lead dicts.

Adapt the lane definitions and component weights to your search, then run:
    python3 score_roles.py --in discovered_roles.json --out scored_roles.json

Input: JSON list of {role_id, company, title, location, work_model,
compensation, posting_date, source, application_url, ats, hard_requirements[]}.
Output: same list with fit_score, score_breakdown, action_band,
recommended_action.
"""
import argparse
import json

COMPONENTS = [
    "experience_alignment",   # 25
    "transferable_skills",    # 15
    "hard_requirements",      # 20 (start at 20, deduct for gaps)
    "career_upside",          # 10
    "compensation",           # 10
    "founder_advantage",      # 5
    "industry_alignment",     # 5
    "location_work_model",    # 5
    "employer_quality",       # 5
]
MAXIMA = {
    "experience_alignment": 25, "transferable_skills": 15,
    "hard_requirements": 20, "career_upside": 10, "compensation": 10,
    "founder_advantage": 5, "industry_alignment": 5,
    "location_work_model": 5, "employer_quality": 5,
}


def band(score):
    if score >= 90:
        return "PRIORITY"
    if score >= 82:
        return "APPLY"
    if score >= 72:
        return "STRATEGIC"
    return "SKIP"


def score_role(role, profile):
    """Score one role. `profile` is the applicant profile dict.

    This template implements the HARD-REQUIREMENT classifier honestly and
    leaves the judgment-heavy components to you: fill in the component
    scores per the rubric in fit-scoring-model.md, or wire in your own
    scorer (human review, LLM judge with the rubric pasted in).
    """
    breakdown = {c: 0 for c in COMPONENTS}
    # Hard requirements: start at max, deduct for gaps.
    hr = MAXIMA["hard_requirements"]
    missing_mandatory = False
    for req in role.get("hard_requirements", []):
        status = (req.get("status") or "UNKNOWN").upper()
        if status == "MISSING":
            hr -= 12
            if req.get("mandatory", True):
                missing_mandatory = True
        elif status == "PARTIAL":
            hr -= 5
        elif status == "UNKNOWN":
            hr -= 2
    breakdown["hard_requirements"] = max(0, hr)
    # --- fill the remaining components per the rubric (fit-scoring-model.md) ---
    # breakdown["experience_alignment"] = ...
    total = sum(min(breakdown[c], MAXIMA[c]) for c in COMPONENTS)
    if missing_mandatory and total > 81:
        total = 81  # a clearly MISSING mandatory requirement caps at STRATEGIC
    action = band(total)
    return {
        **role,
        "fit_score": total,
        "score_breakdown": breakdown,
        "action_band": action,
        "recommended_action": {"PRIORITY": "APPLY", "APPLY": "APPLY",
                               "STRATEGIC": "HOLD", "SKIP": "SKIP"}[action],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--profile", default="applicant_profile.json")
    args = ap.parse_args()
    roles = json.load(open(args.inp))
    try:
        profile = json.load(open(args.profile))
    except FileNotFoundError:
        profile = {}
    scored = [score_role(r, profile) for r in roles]
    json.dump(scored, open(args.out, "w"), indent=2)
    for s in sorted(scored, key=lambda x: x["fit_score"], reverse=True):
        print(f'{s["fit_score"]:3d} {s["action_band"]:9s} {s["company"]} — {s["title"]}')


if __name__ == "__main__":
    main()
