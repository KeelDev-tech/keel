"""Pipeline tools: status summary + the agentic composite workflow.

keel_pipeline_status summarizes the bundled sample telemetry/ledger.
keel_run_pipeline chains the stages — discover -> score -> prescreen — in one
call, demonstrating the agentic orchestration the Alexa+ track rewards.
Optional live URL verification is off by default (fixture URLs are not real).
"""
from __future__ import annotations

from collections import Counter

import keel_bridge
from . import discovery as _discovery
from . import scoring as _scoring
from . import prescreen_tool as _prescreen
from . import verification as _verification


def keel_pipeline_status() -> dict:
    """Summarize pipeline telemetry and the submission ledger (sample fixtures)."""
    events = keel_bridge.load_fixture("sample_telemetry.jsonl")
    ledger = keel_bridge.load_fixture("sample_ledger.json")
    by_type = Counter(e.get("type", "?") for e in events)
    by_status = Counter(r.get("status", "?") for r in ledger)
    return {
        "fixture": True,
        "events_total": len(events),
        "events_by_type": dict(by_type),
        "ledger_total": len(ledger),
        "ledger_by_status": dict(by_status),
        "note": "Sample fixtures. Production telemetry is Keel's append-only event log.",
    }


def keel_answer_lookup(key: str) -> dict:
    """Look up a key in the EXAMPLE answer bank (synthetic fixtures only).

    The real answer bank is never readable here — assert_safe_basename refuses
    answer_bank.json outright; only the shipped example copy is served.
    """
    bank = keel_bridge.load_fixture("answer_bank.example.json")
    answers = bank.get("answers", {})
    if key not in answers:
        return {"key": key, "found": False, "fixture": True,
                "note": "Key not present in the example bank."}
    return {"key": key, "found": True, "value": answers[key], "fixture": True}


def keel_run_pipeline(query: str, work_model: str = "", location: str = "",
                      limit: int = 5, verify_urls: bool = False,
                      profile: dict | None = None) -> dict:
    """Run Keel's full triage workflow in one call: discover -> score -> prescreen.

    Each discovered role is fit-scored with the production scorer; roles that
    have sufficient fit evidence and no requirement holds get a prescreen
    dry-run against a synthetic
    packet brief. This is the agentic-orchestration surface: one call drives
    the whole pipeline, and anything needing a human is PARKed, never faked.
    Args:
        query / work_model / location / limit: passed to keel_search_roles.
        profile: trusted applicant profile with structured scoring evidence;
            omitted uses a placeholder profile, which establishes no fit.
        verify_urls: when true, also live-check each posting URL (slower;
            fixture URLs will report ambiguous/dead — they are not real).
    """
    found = _discovery.keel_search_roles(query, work_model, location, limit)
    stages = []
    for role in found["roles"]:
        scored = _scoring.keel_score_role(dict(role), profile)
        step = {
            "role_id": role.get("role_id"),
            "title": role.get("title"),
            "company": role.get("company"),
            "fit_score": scored.get("fit_score"),
            "action_band": scored.get("action_band"),
            "recommended_action": scored.get("recommended_action"),
            "display_band": scored["display_band"],
            "fit_score_upper": scored["fit_score_upper"],
            "score_coverage_percent": scored["score_coverage_percent"],
            "fit_eligible": scored["fit_eligible"],
            "action_eligibility": scored["action_eligibility"],
            "execution_authorized": False,
        }
        if scored["fit_eligible"]:
            brief = (
                f"Submit a job application for Applicant to {role.get('company')} "
                f"for the role {role.get('title')}.\nFORM INTEL\n"
                f"- [text] Why do you want to work at {role.get('company')}?*\n"
                f"- [dropdown] Are you authorized to work in the US?*\n"
                f"\nSTEP 3: build packet\n"
            )
            screen = _prescreen.keel_prescreen_packet(brief, role.get("company", ""))
            step["prescreen"] = screen
        if verify_urls:
            v = _verification.keel_verify_posting(role.get("application_url", ""))
            step["verification"] = v
        stages.append(step)
    parked = sum(1 for s in stages
                 if s["action_band"] == "HOLD" or
                 isinstance(s.get("prescreen"), dict) and s["prescreen"].get("verdict") == "PARK")
    return {
        "query": query,
        "fixture": True,
        "execution_authorized": False,
        "stages": stages,
        "summary": {
            "discovered": len(stages),
            "apply_band": sum(1 for s in stages if s.get("recommended_action") == "APPLY"),
            "parked_for_human": parked,
        },
    }
