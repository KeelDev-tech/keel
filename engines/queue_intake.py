#!/usr/bin/env python3
"""queue_intake.py — intake validator for pipeline queue writes.

Single validation point for every entry written to the queue files.
Sweep/discovery workers MUST run validate_entry() on each entry before
writing; zero errors required (charter constraint C-17).

Root cause (2026-09-15): sweep23 wrote 66 entries with no `status` — invisible
to apply_loop and the feeder watchdog — plus APPLY-band leads below the 75
bar and entries missing company/title. queue_schema_repair.py did the
one-time triage; this module prevents recurrence.

ARM 83 (2026-09-15): PARKED-LOW-FIT writes require a `status_updated`
timestamp (J-20260915-1420-park-129); other PARKED-family statuses warn.
"""

import re

# ARM 51: discovery-side dedupe gate lives in dedupe_gate.py (canonical URL
# normalization + ledger SUBMITTED / standard-queue URL and employer+title
# checks). Imported lazily inside validate_batch to keep import side
# effects at zero for modules that only need the validator.

FIT_BAR = 75

KNOWN_STATUSES = {
    # standard queue lifecycle
    "READY", "READY-FOR-BROWSER", "IN-FLIGHT", "SUBMITTED",
    "PARKED-PENDING-VERIFICATION", "PARKED-AWAITING-MATERIALS",
    "PARKED-NEEDS-INPUT", "PARKED-ESSAY-GATE",
    "PARKED-ACCOUNT-GATE", "PARKED-LOCATION-GATED", "PARKED-LOW-FIT",
    "PARKED-UNVERIFIABLE", "PARKED-TRIAGE-DEFERRED",
    "PARKED", "REPOST-WATCH", "CLOSED-EXPIRED",
    "BLOCKED-FABRICATION-GATE", "BLOCKED-DAILYREMOTE-GATE",
    "PACKET-READY-NEEDS-APPLICANT",
    # strategic queue
    "GATED", "CLOSED", "NEEDS_INPUT", "SKIPPED", "FIRING", "UNVERIFIED",
    # legacy case variant (read-side tolerance; new writes use PARKED)
    "parked",
}

KNOWN_BANDS = {
    "APPLY", "APPLY (with caveat)", "PARKED", "LOW-FIT", "GATED",
    "STRATEGIC", "HOLD", "adjacent",
}

REQUIRED_FIELDS = ("role_id", "company", "title", "action_band", "status")

# ARM 83 (2026-09-15): J-20260915-1420-park-129 — 44/144 PARKED-LOW-FIT
# entries were parked without `status_updated`, breaking age-based sweeps
# (feeder stale checks, archive rules). Writers MUST stamp PARKED-LOW-FIT
# writes; other PARKED-family statuses warn (discovery's unscored "PARKED"
# path in ats_discovery/sweep.py does not stamp yet — its owner wires that
# separately; the warning keeps it visible without breaking supply).


def _parked_family(status):
    s = (status or "").strip()
    return s == "PARKED" or s.startswith("PARKED-") or s == "parked"


def _has_stamp(e):
    su = e.get("status_updated")
    return isinstance(su, str) and bool(su.strip())


# P2-7 (2026-09-16): canonical needs-input parking convention. The
# classifier (verify_retry.is_verify_only) reads ONLY these four fields;
# custom-only fields (the old park_reasons pattern) are invisible to it,
# and a stale status_reason can resurrect a genuinely input-blocked lead.
# Every parking writer (prescreen.park_lead, verify_retry's park path,
# one-off correction scripts) must put blocker words in `unresolved` and
# overwrite any stale verify-flavored `status_reason`.
PARKING_CONVENTION_FIELDS = frozenset(
    {"unresolved", "status_reason", "gate_note", "queue_notes"})

# Non-conventional parking-field names that have appeared in the wild and
# must never be written again (migrated out of the live queue 2026-09-16).
PARKING_NONCONVENTIONAL_FIELDS = frozenset(
    {"park_reasons", "parked_evidence", "parked_ts", "parked_at",
     "parking_reason", "parked_reason"})


def parking_convention_check(entry):
    """Return the list of non-conventional parking fields present on an
    entry. Empty means the entry follows the parking convention."""
    return sorted(k for k in entry
                  if k in PARKING_NONCONVENTIONAL_FIELDS)

# Optional field recorded by discovery workers when the board token is
# visible in the posting URL (ARM 35): "platform:token", e.g.
# "greenhouse:sofi", "ashby:headway", "lever:acme". Lets verify_retry's
# enrichment hit the real board directly instead of guessing tokens from
# the company name. Optional — a malformed value warns but never blocks
# intake; absent values keep the guessing fallback.


def _url(e):
    """Canonical URL-bearing field reader for intake (GR-2, ARM 63-A).

    Order: ats_url, application_url, posting_url, job_url — matches the
    GR-1 canonical posting_url() reader order (ARM 1, pulse-60). discovery
    captures non-canonical mirrors in posting_url/job_url; missing those
    misclassifies URL-bearing leads as no-URL.
    """
    return (e.get("ats_url") or e.get("application_url")
            or e.get("posting_url") or e.get("job_url") or "").strip()


def validate_entry(e):
    """Return (errors, warnings). Zero errors required before a queue write
    (charter C-17); warnings are advisory and do not block."""
    errors, warnings = [], []
    if not isinstance(e, dict):
        return (["entry is not a dict"], [])
    for f in REQUIRED_FIELDS:
        v = e.get(f)
        ok = bool(v.strip()) if isinstance(v, str) else (v is not None and v != "")
        if not ok:
            errors.append(f"missing required field: {f}")
    band = (e.get("action_band") or "").strip()
    status = (e.get("status") or "").strip()
    if band and band not in KNOWN_BANDS:
        errors.append(f"unknown action_band: {band!r}")
    if status and status not in KNOWN_STATUSES:
        errors.append(f"unknown status: {status!r}")
    fit = e.get("fit_score")
    if band == "APPLY":
        if not isinstance(fit, (int, float)):
            errors.append("APPLY band requires a numeric fit_score")
        elif fit < FIT_BAR:
            errors.append(f"APPLY band requires fit_score >= {FIT_BAR} "
                          f"(got {fit}); use PARKED-LOW-FIT")
        if not _url(e):
            errors.append("APPLY band requires a posting URL "
                          "(ats_url/application_url/posting_url/job_url)")
    if status == "READY" and band != "APPLY":
        errors.append(f"READY status requires action_band APPLY (got {band!r})")
    if status == "PARKED-LOW-FIT" and not _has_stamp(e):
        # J-20260915-1420-park-129: unstamped LOW-FIT parks are invisible to
        # age-based sweeps. Hard error — the writer must stamp.
        errors.append("PARKED-LOW-FIT requires status_updated timestamp "
                      "(age-based sweeps depend on it)")
    elif _parked_family(status) and not _has_stamp(e):
        warnings.append("PARKED status with no status_updated: stamp the park "
                        "time — age-based sweeps cannot see unstamped parks")
    if status == "PARKED-PENDING-VERIFICATION" and not _url(e):
        # GR-2 (ARM 63-A, 2026-09-15): C-17 intake guard against zero-yield
        # verify-pool sinks. A PV write with no URL-bearing field can never
        # be verified — it is a guaranteed sink (ARM 1, pulse-60, removed 5
        # such entries). Reject at intake with the exact guidance string.
        # Legacy queue entries are grandfathered (new writes only — this
        # module never rewrites history, same pattern as ARM 91's band
        # grandfathering).
        errors.append("PARKED-PENDING-VERIFICATION requires a URL-bearing "
                      "field: no URL-bearing field — resolve an individual "
                      "posting URL at discovery (employer-direct or "
                      "board-API), or log the lead to repost-watch / "
                      "rejected-queue with evidence instead of parking it "
                      "in the verify pool.")
    rid = e.get("role_id") or ""
    if rid and not re.match(r"^[A-Z0-9][A-Z0-9\-]*$", rid):
        warnings.append(f"role_id should be UPPER-KEBAB (got {rid!r})")
    # ARM 35: ats_board is optional and never blocks intake. A malformed
    # value warns (verify_retry will ignore it and fall back to guessing).
    board = e.get("ats_board")
    if board not in (None, ""):
        if not isinstance(board, str) or not re.match(
                r"^(greenhouse|lever|ashby):[a-z0-9][a-z0-9_-]*$",
                board.strip().lower()):
            warnings.append(f"ats_board should be 'platform:token' "
                            f"(greenhouse|lever|ashby), got {board!r}; "
                            f"enrichment will ignore it")
    return errors, warnings


def entry_is_queue_ready(e):
    """True when validate_entry reports zero errors."""
    errors, _ = validate_entry(e)
    return not errors


def validate_batch(entries, dedupe=True):
    """Validate a list; returns {role_id_or_index: (errors, warnings)} for
    entries with any errors or warnings, plus duplicate role_ids.

    dedupe (default True, ARM 51): run the discovery dedupe gate on every
    entry — hard "duplicate" verdicts (normalized posting URL already
    SUBMITTED in the ledger or already in the standard queue, or
    employer containment + title equality) become errors, so the entry
    must be logged as `gate_encountered` (duplicate_of_submitted) instead
    of `lead_discovered`. "suspect" verdicts (name or title matched
    alone) become warnings and never block intake. Pass dedupe=False only
    for historical re-validation sweeps that must not re-judge.
    """
    from dedupe_gate import check_candidate, _company, _url_of
    bad = {}
    seen = {}
    for i, e in enumerate(entries):
        errors, warnings = validate_entry(e)
        rid = e.get("role_id") if isinstance(e, dict) else None
        if rid:
            if rid in seen:
                errors.append(f"duplicate role_id in batch "
                              f"(also at index {seen[rid]})")
            else:
                seen[rid] = i
        if dedupe and isinstance(e, dict):
            verdict, evidence = check_candidate(
                _company(e), e.get("title"), _url_of(e))
            if verdict == "duplicate":
                errors.append(
                    f"duplicate_of_submitted: {evidence.get('match')} "
                    f"matches {evidence.get('ledger_role_id') or evidence.get('queue_role_id')} "
                    f"({evidence.get('kind')}) — log as gate_encountered, "
                    f"not lead_discovered")
            elif verdict == "suspect":
                warnings.append(
                    f"possible_duplicate: {evidence.get('ledger_role_id')} "
                    f"matched on name-or-title alone — human judgment")
        if errors or warnings:
            bad[rid or f"index-{i}"] = (errors, warnings)
    return bad
