"""title_triage.py — staging-side title-family triage for clean-board-watch intake.

Purpose: keep the verify HTTP budget focused on leads with real promotion
potential. Provenance: the RAMP-1A/1B/1C sweeps (9,241 postings screened ->
48 staged, 100% URL-bearing), hardened 2026-09-15 against 1,196 real
greenhouse-json-enumerated leads (measured: 93.6% defer, 6.4% keep,
14/14 false-positive/junk regression cases green).

Contract (non-negotiable):
- PURE title/location evidence, plus a read-only fit override. This is NOT
  fit scoring: no fit_score is invented, inferred, or written here. When a
  staging entry already carries an evidence-scored fit_score >= 75
  (APPLY band, from fit_push_score), the location-signal deferral is waived
  — an empty/unparseable location is an emitter defect, not evidence
  against the lead (2026-09-16 Kitsch recovery). Title-family,
  excluded-keyword, and location-over-cap gates always apply.
- DEFERRED IS NOT DROPPED. A deferred lead is ingested into the queue with
  status PARKED-TRIAGE-DEFERRED, a stable reason code, and a status_updated
  stamp — visible, countable, recoverable, auditable. Nothing here deletes,
  hides, or silently discards a lead.
- Deferred leads are excluded from verify_retry's pending-verification pool
  (it selects PARKED-PENDING-VERIFICATION only), so they cost zero HTTP
  budget. Recovery path: re-triage pass or individual revival moves them
  back to PARKED-PENDING-VERIFICATION; the reason code says why they were
  deferred in the first place.
- Zero engine/policy/threshold/transport changes. Callers (build_entry in
  clean_board_watch.py, build_json_entry in greenhouse_json_enumerate.py)
  annotate at build time, mirroring the existing dedupe-gate pattern.

Blackboard: J-20260916-0112-veri-457 (superseded by hardened build,
operator-authorized 2026-09-15).
"""

import re

TRIAGE_DEFERRED_STATUS = "PARKED-TRIAGE-DEFERRED"

# Keep families: RAMP-proven base + measured additions (2026-09-15 hardening).
# Additions vs the RAMP base: program-management family ("program manager",
# "technical program", "program lead", "project manager") and business-systems
# family ("business systems", "systems analyst") — the RAMP base missed 7-9
# clear on-lane false positives there (TPM AI & Data Systems, GFCO Program
# Manager, Strategic Program Lead, Sr. Systems Analyst Finance/Enterprise Apps).
# Deliberately NOT included (measured junk they would keep): bare "growth"
# (keeps Growth Account Executive sales roles), bare "strategy"/"strategic"
# (keeps Strategic Finance Manager), bare "automation" (keeps Senior IT
# Automation Engineer / Senior SWE Backend Core AI Automation).
# Hospitality-operations expansion (2026-09-18, gate-vocab-review):
# measured on 16 employer-direct hospitality leads. "general manager" is
# explicitly claimed by the GM-Ops lane profile (general management, team
# leadership, labor/inventory/cash controls); "director of outlets" is
# the hospitality analog of the covered "director of operations". Both are
# distinctive multi-word compounds with zero junk-title collision risk.
# Deliberately NOT added (each scores 0 on every lane profile,
# measured 2026-09-18): event titles (no lane master), culinary/facilities
# engineering ("chef", "engineer" — the latter also a TRIAGE_MISS keyword),
# service-specialist titles ("maitre d"), wellness/housekeeping/guest-services
# and F&B shift-supervisor titles, and pipeline/future postings. Keeping
# them would burn verify HTTP on leads that cannot reach materials.
# Implementation-family expansion (2026-09-20, ARM 569-2, blackboard
# J-20260920-0232-meth-3340): measured on 174 title-family-miss rejects
# from the 2026-09-19 LinkedIn staging sweep — 12 were implementation-family
# and 11 genuine on-lane roles (Rippling x2, DailyPay, Canary, Courier
# Health, Exiger, FIS, Profound, Moveworks, PreSales Collective,
# RemoteHunter) were parked PARKED-TRIAGE-DEFERRED on a title that no lane
# family recognized. The bare token's junk-collision risk is negligible:
# "Implementation Engineer"-class engineering titles still defer on the
# excluded-keyword gate (TRIAGE_MISS runs after HITS) — the same fail-safe
# the other families rely on.
TRIAGE_HITS = re.compile(
    r"(operations|revenue ops|sales ops|business ops|product ops|program ops|"
    r"go-to-market|gtm|chief of staff|ai tooling|ai operations|trust & safety|"
    r"ai safety|ai governance|solutions architect|strategy & operations|bizops|"
    r"enablement|"
    r"program manager|technical program|program lead|project manager|"
    r"business systems|systems analyst|"
    r"general manager|director of outlets|"
    r"implementation)",
    re.I)

# Exclusion keywords: RAMP-proven base + "talent" (measured: keeps Senior
# Talent Program Manager, an HR role, out of the ops lane).
TRIAGE_MISS = re.compile(
    r"(engineer|engineering|developer|scientist|clinical|nurse|physician|"
    r"counsel|attorney|legal|paralegal|intern|internship|talent)",
    re.I)

# Location gate: RAMP-proven. Measured on 1,196 leads: 102 location defers,
# all genuinely off-lane postings, zero empty-location false cuts.
# 2026-09-16: + comma-anchored US state abbreviations ("Austin, TX",
# "New York, NY"). The comma anchor matters: bare abbreviations like IN/OR
# are also English words, so only the "City, ST" shape counts.
TRIAGE_LOC = re.compile(
    r"(remote|bay area|san francisco|napa|california|united states|"
    r"\bUS\b|anywhere|"
    r",\s*\b(AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|IA|ID|IL|IN|KS|KY|LA|MA|MD|ME|"
    r"MI|MN|MO|MS|MT|NC|ND|NE|NH|NJ|NM|NV|NY|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|"
    r"VA|VT|WA|WI|WV|WY|DC)\b)",
    re.I)

# Stable reason codes for audit/recovery.
REASON_TITLE_FAMILY_MISS = "title-family-miss"
REASON_EXCLUDED_KEYWORD = "excluded-keyword"
# Location/travel policy (operator-configured): this triage sees only the
# location string, so it acts ONLY on EXPLICIT over-cap signals in that
# string (e.g. 4-5 days/week, full-time on-site — the explicit over-cap
# shapes in TRIAGE_OVER_CAP). Ambiguous cases (bare "hybrid",
# bare "on-site", undefined frequency, travel mentions) stay for the
# prescreen step with full posting text — title triage is pure evidence,
# never inference. The full office/travel/relocation evaluation happens
# downstream with complete posting text.
TRIAGE_OVER_CAP = re.compile(
    r"(\b5\s*days?\b|\b4\s*[-/]?\s*5\s*days?\b|\bfull[\s-]?time\s+on[\s-]?site\b|"
    r"\bon[\s-]?site\s+(5|full[\s-]?time)\b|\b5x\b|\b4x\b|\b100%\s+on[\s-]?site\b)",
    re.I)

REASON_LOCATION_OVER_CAP = "location-over-cap"
REASON_LOCATION = "location"
REASON_KEEP = "title-family-hit"

# Evidence-scored fit override (2026-09-16, Kitsch recovery): a lead
# carrying fit_score >= FIT_OVERRIDE_THRESHOLD was scored from real posting
# evidence by fit_push_score. A location string with no keep-signal is an
# emitter defect (empty/unparseable location), not evidence against the
# lead — deferring it wastes the scoring work already done and silently
# hides APPLY-band leads (measured: 56 high-fit leads deferred on empty
# location, incl. a fit-88 that had to be hand-recovered). The override
# skips ONLY the location-signal deferral: the title-family,
# excluded-keyword, and location-over-cap (explicit over-cap violation) gates
# still apply. Nothing is invented here — the score is read, never written
# or inferred — and the configured policy stays enforced downstream:
# prescreen evaluates
# office/travel/relocation with full posting text before any browser
# launch, so an over-cap or foreign posting still parks there.
FIT_OVERRIDE_THRESHOLD = 75


def triage_lead(title, location, fit_score=None):
    """Pure decision: (decision, reason_code).

    decision is "keep" or "defer". reason_code is one of the REASON_*
    constants. No I/O, no scoring, no invention. fit_score, when supplied,
    is read-only evidence from a prior evidence-scored fit run; it only
    waives the location-signal deferral, never any other gate.
    """
    t = title or ""
    loc = location or ""
    if not TRIAGE_HITS.search(t):
        return "defer", REASON_TITLE_FAMILY_MISS
    if TRIAGE_MISS.search(t):
        return "defer", REASON_EXCLUDED_KEYWORD
    # P2-6: policy recalibration — explicit over-cap office frequency in the
    # location string defers with its own reason code (prescreen still owns
    # the full office/travel evaluation with posting text).
    if TRIAGE_OVER_CAP.search(loc):
        return "defer", REASON_LOCATION_OVER_CAP
    if not TRIAGE_LOC.search(loc):
        if (fit_score is not None
                and fit_score >= FIT_OVERRIDE_THRESHOLD):
            return "keep", REASON_KEEP
        return "defer", REASON_LOCATION
    return "keep", REASON_KEEP


def annotate_deferred(entry, reason_code, stamp):
    """Tag a built queue entry as triage-deferred. Mutates and returns entry.

    Sets status PARKED-TRIAGE-DEFERRED (in queue_intake.KNOWN_STATUSES),
    keeps action_band PARKED, refreshes status_updated, and appends the
    reason code to queue_notes so the deferral is visible and auditable.
    The lead stays fully recoverable: nothing is deleted or hidden.
    """
    entry["status"] = TRIAGE_DEFERRED_STATUS
    entry["action_band"] = "PARKED"
    entry["status_updated"] = stamp
    note = (f"title-triage: deferred at staging ({reason_code}) — "
            f"excluded from verify HTTP budget, visible in "
            f"{TRIAGE_DEFERRED_STATUS} band; recoverable via re-triage. "
            f"No fit_score invented or inferred.")
    prev = entry.get("queue_notes") or ""
    if isinstance(prev, list):
        prev = " ".join(str(p) for p in prev if p)
    entry["queue_notes"] = (prev + " " + note).strip() if prev else note
    entry["triage_reason"] = reason_code
    return entry


# ---------------------------------------------------------------------------
# P-2026-09-16-titlegate-1 (operator-approved 2026-09-16 ~12:55 PDT): upstream
# pre-staging title gate. Same regex classes as triage_lead, verbatim —
# but (a) the read-only fit>=75 waiver extends to ALL defers (title-family,
# excluded-keyword, location-signal), not just the location-signal deferral
# (measured 2026-09-16: 13 conversions saved vs 3 high-fit false negatives;
# the Kitsch guard: an evidence-scored fit>=75 is stronger evidence than a
# title regex), and (b) the verdict is a hard keep/reject for the
# staging_ingest choke point instead of tag-and-ingest. The configured
# policy is intact: explicit
# over-cap signals (4-5x/week, full-time on-site) reject regardless of fit;
# ambiguous office/travel/location stays for prescreen with full posting
# text — the gate never infers. Rejects go to the reject ledger
# (role_id, title, reason, stamp) — visible, auditable, recoverable —
# honoring the "deferred is not dropped" contract without queue clutter.
# Zero HTTP: pure regex over title/location + read-only fit_score.
# ---------------------------------------------------------------------------

def _fit_waives(fit_score):
    """Read-only waiver check. fit_score is evidence already in hand
    (evidence-scored by fit_push_score); never invented or inferred here."""
    return (fit_score is not None
            and isinstance(fit_score, (int, float))
            and not isinstance(fit_score, bool)
            and fit_score >= FIT_OVERRIDE_THRESHOLD)


def pre_staging_title_gate(title, location, fit_score=None):
    """Upstream choke-point gate. Returns (keep: bool, reason_code).

    keep=True  -> ingest normally.
    keep=False -> reject to the titlegate reject ledger (recoverable),
                  never ingested into any queue.
    Pure function: no I/O, no scoring, no invention.
    """
    t = title or ""
    loc = location or ""
    waived = _fit_waives(fit_score)
    # Explicit over-cap office signals reject regardless of fit.
    if TRIAGE_OVER_CAP.search(loc):
        return False, REASON_LOCATION_OVER_CAP
    if not TRIAGE_HITS.search(t):
        if waived:
            return True, REASON_KEEP
        return False, REASON_TITLE_FAMILY_MISS
    if TRIAGE_MISS.search(t):
        if waived:
            return True, REASON_KEEP
        return False, REASON_EXCLUDED_KEYWORD
    if not TRIAGE_LOC.search(loc):
        if waived:
            return True, REASON_KEEP
        return False, REASON_LOCATION
    return True, REASON_KEEP
