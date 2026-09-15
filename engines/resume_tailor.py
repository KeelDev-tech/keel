#!/usr/bin/env python3
"""Keel resume tailor (public edition) — TEMPLATE.

Derives a role-tailored resume PDF from YOUR applicant profile. This is a
template: fill in applicant_profile.example.json (created by ./setup.sh),
adapt the EXPERIENCE bullets to your real history, and render.

TRUTHFULNESS RULES (non-negotiable — they are the product's differentiator):
  - Never invent dates, titles, metrics, degrees, credentials, tools, team
    sizes, quotas, or employer relationships.
  - Every claim must trace to the applicant profile or a verified source.
  - Year-only dates unless the profile documents exact dates.
  - If the role demands something the profile cannot support, the tailor
    must surface the gap (see qualification_gaps in the output), never
    bridge it with fiction.

Usage:
    python3 resume_tailor.py --profile applicant_profile.json \
        --role role_brief.json --out tailored_resume.pdf

Requires: reportlab (pip install reportlab)
"""
import argparse
import json
import os
import sys

try:
    from reportlab.lib.pagesizes import LETTER
    from reportlab.lib.units import inch
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.enums import TA_CENTER
    from reportlab.lib.colors import HexColor
    from reportlab.platypus import SimpleDocTemplate, Paragraph, HRFlowable, Spacer
except ImportError:
    sys.exit("reportlab is required: pip install reportlab")


ACCENT = HexColor("#1a1a1a")


def styles():
    return {
        "name": ParagraphStyle("name", fontName="Helvetica-Bold", fontSize=20,
                               alignment=TA_CENTER, spaceAfter=2),
        "contact": ParagraphStyle("contact", fontName="Helvetica", fontSize=9,
                                  alignment=TA_CENTER, spaceAfter=8,
                                  textColor=HexColor("#444444")),
        "headline": ParagraphStyle("headline", fontName="Helvetica-Bold", fontSize=11,
                                   alignment=TA_CENTER, spaceAfter=6),
        "head": ParagraphStyle("head", fontName="Helvetica-Bold", fontSize=11,
                               spaceBefore=10, spaceAfter=4, textColor=ACCENT),
        "body": ParagraphStyle("body", fontName="Helvetica", fontSize=9.5,
                               leading=13, spaceAfter=3),
        "bullet": ParagraphStyle("bullet", fontName="Helvetica", fontSize=9.5,
                                 leading=13, leftIndent=14, bulletIndent=6,
                                 spaceAfter=2),
        "job": ParagraphStyle("job", fontName="Helvetica-Bold", fontSize=10,
                              spaceBefore=6, spaceAfter=1),
        "sub": ParagraphStyle("sub", fontName="Helvetica-Oblique", fontSize=9.5,
                              spaceAfter=2, textColor=HexColor("#444444")),
    }


def esc(s):
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def truthfulness_check(profile, role):
    """Return a list of gaps: role requirements the profile cannot support.

    The tailor prints these and REFUSES to invent bridging copy. A gap is
    information for the applicant (skip the role, or supply real evidence),
    never a prompt to embellish.
    """
    gaps = []
    must_haves = (role.get("hard_requirements") or [])
    capabilities = set(profile.get("verified_capabilities") or [])
    for req in must_haves:
        name = req.get("name", "")
        status = req.get("status", "UNKNOWN")  # VERIFIED | PARTIAL | MISSING | UNKNOWN
        if status == "MISSING" and name not in capabilities:
            gaps.append(f"MISSING hard requirement with no profile support: {name}")
    return gaps


def build(profile, role, out_path):
    st = styles()
    doc = SimpleDocTemplate(out_path, pagesize=LETTER,
                            leftMargin=0.7 * inch, rightMargin=0.7 * inch,
                            topMargin=0.6 * inch, bottomMargin=0.6 * inch)
    story = []
    contact = profile.get("contact", {})
    contact_line = " &nbsp;|&nbsp; ".join(
        x for x in [contact.get("phone"), contact.get("email"),
                    contact.get("location"), contact.get("linkedin")]
        if x)
    story.append(Paragraph(esc(profile.get("name", "")), st["name"]))
    story.append(Paragraph(esc(contact_line), st["contact"]))
    if role.get("headline"):
        story.append(Paragraph(esc(role["headline"]), st["headline"]))
    story.append(HRFlowable(width="100%", thickness=0.5, color=HexColor("#999999")))

    story.append(Paragraph("Experience", st["head"]))
    for job in profile.get("experience", []):
        story.append(Paragraph(
            f"{esc(job.get('org',''))} — {esc(job.get('title',''))}", st["job"]))
        if job.get("dates"):
            story.append(Paragraph(esc(job["dates"]), st["sub"]))
        for b in job.get("bullets", []):
            story.append(Paragraph(esc(b), st["bullet"], bulletText="•"))

    if profile.get("credentials"):
        story.append(Paragraph("Credentials", st["head"]))
        for c in profile["credentials"]:
            story.append(Paragraph(
                f"{esc(c.get('name',''))} — {esc(c.get('issuer',''))}"
                + (f" ({esc(c.get('date',''))})" if c.get("date") else ""),
                st["bullet"], bulletText="•"))

    if profile.get("education"):
        story.append(Paragraph("Education", st["head"]))
        story.append(Paragraph(esc(profile["education"]), st["body"]))

    doc.build(story)
    print(f"Wrote {out_path}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", required=True, help="applicant profile JSON")
    ap.add_argument("--role", required=True, help="role brief JSON (title, headline, hard_requirements)")
    ap.add_argument("--out", required=True, help="output PDF path")
    args = ap.parse_args()

    profile = json.load(open(args.profile))
    role = json.load(open(args.role))

    gaps = truthfulness_check(profile, role)
    if gaps:
        print("QUALIFICATION GAPS (not bridged — tailor honestly or skip the role):")
        for g in gaps:
            print("  -", g)

    build(profile, role, args.out)


if __name__ == "__main__":
    main()
