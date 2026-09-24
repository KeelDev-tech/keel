#!/usr/bin/env python3
"""Regression tests for workstream D (Vercel/funding state).

Covers KEEL-BLOCKER-RESOLUTION-DIRECTIVE-2026-09-18 §4:

1. Tracker states read back exactly:
   - VERCEL_STARTUPS == HOLD_ELIGIBILITY
     (reason REQUIRED_ELIGIBILITY_EVIDENCE_NOT_VERIFIED)
   - VERCEL_OSS == PREPARE_NEXT_AVAILABLE_WINDOW
   - every opportunity carries an explicit type (financial vs infra_credit),
     and infra_credit entries never carry cash-funding language.
2. Forbidden invented-claim scan over all funding/vercel_oss/ materials:
   every hit on an affiliation/investment/round/customer/traction/revenue/
   adoption pattern must sit on a line that also carries an evidence marker
   (UNKNOWN / Evidence) or an explicit denial (no / not / none / never).
3. application_packet.json is submittable-shape but NOT submitted.

Run: python3 -m pytest tests/test_vercel_funding_state.py -v
"""

import json
import re
from pathlib import Path

KEEL_ROOT = Path(__file__).resolve().parent.parent
FUNDING_DIR = KEEL_ROOT / "funding"
TRACKER_PATH = FUNDING_DIR / "funding-tracker.json"
VERCEL_OSS_DIR = FUNDING_DIR / "vercel_oss"
PACKET_PATH = VERCEL_OSS_DIR / "application_packet.json"

EXPECTED_MATERIALS = [
    "project_summary.md",
    "technical_architecture.md",
    "security_model.md",
    "roadmap.md",
    "open_source_impact.md",
    "hosting_plan.md",
    "code_of_conduct_check.md",
    "community_metrics.json",
    "claim_evidence.json",
    "application_packet.json",
]

# Patterns for invented claims the workstream is forbidden from making.
# (Affiliation / investment / funding round / customers / traction /
#  revenue / adoption metrics asserted as facts.)
FORBIDDEN_PATTERNS = [
    r"\braised\s+\$",
    r"\b(?:seed|series\s+[a-d]|pre-seed)\s+(?:round|funding)\b",
    r"\bfunding\s+rounds?\b",
    r"\binvestment\b",
    r"\b\d[\d,]*\s+(?:paying\s+)?customers?\b",
    r"\brevenue\b",
    r"\bARR\b",
    r"\bMRR\b",
    r"\btraction\b",
    r"\b\d[\d,]*\s+(?:monthly\s+)?active\s+users?\b",
    r"\b\d[\d,]*\+?\s*downloads\b",
    r"\badoption\b",
    r"\bpartner(?:ship|ed)\s+with\b",
    r"\baccelerator\b",
]

# A forbidden-pattern hit is only acceptable on a line that also carries
# an evidence marker or an explicit denial.
EVIDENCE_MARKER = re.compile(r"\b(unknown|evidence)\b", re.IGNORECASE)
DENIAL_MARKER = re.compile(r"\b(no|not|none|never|n/?a)\b", re.IGNORECASE)

# Cash-funding language that must never appear on an infra_credit entry,
# outside of explicit denials ("NOT cash funding", "non-cash", ...).
CASH_LANGUAGE = re.compile(
    r"cash funding|funding received|investment received|\$\d", re.IGNORECASE
)
CASH_DENIAL = re.compile(r"\bnot\s+cash funding\b|\bnon-cash\b", re.IGNORECASE)


def strip_cash_denials(blob):
    return CASH_DENIAL.sub("", blob)


def load_tracker():
    with open(TRACKER_PATH, encoding="utf-8") as f:
        return json.load(f)


def opportunities_by_id(tracker):
    return {o["id"]: o for o in tracker["opportunities"]}


def test_tracker_file_exists():
    assert TRACKER_PATH.is_file(), f"tracker missing: {TRACKER_PATH}"


def test_vercel_startups_hold_eligibility():
    opps = opportunities_by_id(load_tracker())
    assert "VERCEL_STARTUPS" in opps, "VERCEL_STARTUPS missing from tracker"
    opp = opps["VERCEL_STARTUPS"]
    assert opp["status"] == "HOLD_ELIGIBILITY", opp["status"]
    assert opp["reason"] == "REQUIRED_ELIGIBILITY_EVIDENCE_NOT_VERIFIED", opp["reason"]
    assert opp["type"] == "infra_credit", opp["type"]


def test_vercel_oss_prepare_next_window():
    opps = opportunities_by_id(load_tracker())
    assert "VERCEL_OSS" in opps, "VERCEL_OSS missing from tracker"
    opp = opps["VERCEL_OSS"]
    assert opp["status"] == "PREPARE_NEXT_AVAILABLE_WINDOW", opp["status"]
    assert opp["type"] == "infra_credit", opp["type"]


def test_every_opportunity_has_explicit_type():
    tracker = load_tracker()
    assert tracker["type_taxonomy"]["financial"]
    assert tracker["type_taxonomy"]["infra_credit"]
    assert tracker["type_separation_rule"]
    for opp in tracker["opportunities"]:
        assert opp.get("type") in ("financial", "infra_credit"), (
            f"{opp.get('id')}: missing or invalid type: {opp.get('type')!r}"
        )


def test_type_separation_exercised_both_sides():
    opps = opportunities_by_id(load_tracker())
    types = {o["type"] for o in opps.values()}
    assert "financial" in types, "no financial-type opportunity present"
    assert "infra_credit" in types, "no infra_credit-type opportunity present"


def test_infra_credit_never_described_as_cash_funding():
    opps = opportunities_by_id(load_tracker())
    for oid, opp in opps.items():
        if opp["type"] != "infra_credit":
            continue
        blob = " ".join(
            str(opp.get(k, "")) for k in ("value_description", "name", "notes", "next_step")
        )
        blob = strip_cash_denials(blob)
        assert not CASH_LANGUAGE.search(blob), (
            f"{oid}: infra_credit entry carries cash-funding language: {blob!r}"
        )
        assert "amount_usd" not in opp, f"{oid}: cash amount field on infra_credit"


def test_all_material_files_exist():
    missing = [m for m in EXPECTED_MATERIALS if not (VERCEL_OSS_DIR / m).is_file()]
    assert not missing, f"missing materials: {missing}"


def test_each_prose_material_has_evidence_section():
    for name in EXPECTED_MATERIALS:
        if not name.endswith(".md"):
            continue
        text = (VERCEL_OSS_DIR / name).read_text(encoding="utf-8")
        assert re.search(r"^## Evidence\b", text, re.MULTILINE), (
            f"{name}: no '## Evidence' section"
        )


def test_forbidden_claims_have_evidence_or_denial():
    # Paragraph-aware: markdown hard-wraps sentences across lines, so unwrap
    # each blank-line-separated paragraph before scanning. A forbidden hit
    # is only acceptable when the same paragraph carries an evidence marker
    # (UNKNOWN / Evidence) or an explicit denial (no / not / none / never).
    violations = []
    for name in EXPECTED_MATERIALS:
        text = (VERCEL_OSS_DIR / name).read_text(encoding="utf-8")
        # JSON has no sentence-wrapping: scan line-by-line (each line is one
        # key/value). Markdown hard-wraps sentences: scan unwrapped paragraphs.
        units = text.splitlines() if name.endswith(".json") else [
            " ".join(p.split()) for p in re.split(r"\n\s*\n", text)
        ]
        for unit in units:
            for pat in FORBIDDEN_PATTERNS:
                if re.search(pat, unit):
                    if not (EVIDENCE_MARKER.search(unit) or DENIAL_MARKER.search(unit)):
                        violations.append(f"{name}: {unit[:140]}")
    assert not violations, (
        "forbidden invented-claim pattern without evidence marker or denial:\n"
        + "\n".join(violations)
    )


def test_packet_submittable_shape_but_not_submitted():
    with open(PACKET_PATH, encoding="utf-8") as f:
        packet = json.load(f)
    # Shape: the fields a submission form would need.
    assert packet["program"] == "Vercel OSS Program"
    project = packet["project"]
    for field in ("name", "description", "repository_url", "license", "hosting_use"):
        assert project.get(field), f"project.{field} missing"
    materials = packet["materials"]
    material_paths = [v for v in materials.values() if isinstance(v, str) and v.endswith((".md", ".json"))]
    for name in EXPECTED_MATERIALS:
        assert any(p.endswith(name) for p in material_paths), f"materials missing {name}"
        assert (VERCEL_OSS_DIR / name).is_file()
    # NOT submitted.
    submission = packet["submission"]
    assert submission["submitted"] is False
    assert submission["submission_status"].startswith("NOT_SUBMITTED")
    assert submission["confirmation_record"] is None
    assert "authorization" in submission["authorization_required"].lower()


def test_packet_unknowns_not_invented():
    with open(PACKET_PATH, encoding="utf-8") as f:
        packet = json.load(f)
    facts = packet["project_facts"]
    # Affiliation / investment / round / customer / traction / revenue /
    # adoption facts must be explicit non-claims, never invented values.
    assert "none" in facts["accelerator_affiliation"]
    assert "none" in facts["investment"]
    assert "none" in facts["funding_rounds"]
    assert "none" in facts["customers"]
    assert "none" in facts["traction_metrics"]
    assert "none" in facts["revenue"]
    assert "UNKNOWN" in facts["user_counts"]
    assert "UNKNOWN" in packet["applicant"]["legal_entity"]
    assert "UNKNOWN" in packet["project"]["repository_visibility"]


def test_claim_evidence_registry_covers_prose_materials():
    with open(VERCEL_OSS_DIR / "claim_evidence.json", encoding="utf-8") as f:
        registry = json.load(f)
    covered = {c["file"] for c in registry["claims"]}
    prose = [m for m in EXPECTED_MATERIALS if m.endswith(".md")]
    missing = [m for m in prose if m not in covered]
    assert not missing, f"claim_evidence.json missing coverage for: {missing}"
    for claim in registry["claims"]:
        assert claim["status"] in ("VERIFIED_LOCAL", "UNKNOWN", "NON_CLAIM"), claim
        assert claim["evidence"], f"claim without evidence: {claim['claim'][:60]}"
