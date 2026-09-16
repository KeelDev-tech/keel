#!/usr/bin/env python3
"""genuine_pat.py — the is_verify_only classification logic for the
pending-verification pool.

OWNERSHIP (2026-09-16 restructure, full-potential coordinator): this module
owns the verification-vs-genuine-input classifier — the compiled patterns
and `is_verify_only(entry)`. verify_retry.py owns the surrounding run/lock
machinery (scan, HTTP verify, apply, cursor, cooldowns) and imports this
module; scheduled verify workers consume the classifier through
verify_retry's re-exports.

Contract: `is_verify_only(entry)` is True when the entry's blockers are
verification-state only (posting liveness unchecked) — i.e. the verify
worker may spend HTTP budget on it. False when any conventional field
(unresolved / status_reason / gate_note / queue_notes) carries a genuine
needs-applicant-input blocker (essays, attestations, travel/commitment
questions, account-creation walls, references, ...).

Calibration rule (AGENTS.md): a false positive at promotion (genuinely
blocked lead classified verify-only) costs one prescreen PARK — defense in
depth, prescreen re-screens commitment gates on raw fields (C-15). A false
negative (verification-only lead classified blocked) costs the lead
forever — it can never promote. Both directions are pinned by tests in
tests/test_verify_retry_genuine_pat*.py.

Hard boundaries: pure classifier logic. No HTTP, no queue/ledger writes.

Note on wording convention: this module keys on the word "applicant" where
the production pipeline keyed on the operator's name — queue notes in this
repo use "the applicant" phrasing for applicant-input blockers (e.g.
"needs the applicant's explicit input", "the applicant to decide"). Benign
prose mentioning "the applicant" in a conventional field can false-positive
the bare-\bapplicant\b arm; prefer the explicit blocker phrasings below
when writing notes.
"""

import re

VERIFY_PAT = re.compile(
    r"verif|mirror|employer-direct|repost-watch|unverified|aggregat|liveness"
    r"|promote only after|individual posting|individual job page", re.I)
# GENUINE_PAT: genuine-input blockers only. False-positive classes
# excluded because they are NOT blockers:
#  (1) negated facts — "no degree requirement", "no travel required"
#      (lookbehind (?<!no )); [2026-09-15]
#  (2) internal fit annotations — "(applicant: 12 yrs)" (stripped by
#      APPLICANT_NOTE_PAT before matching; genuine applicant-mentions are
#      possessive/imperative: "the applicant's input", "the applicant must
#      decide"); [2026-09-15]
#  (3) "SF location weighs on score" (fit annotation, not a question),
#      "account creation standing-authorized" (authorized action, not a
#      wall); [2026-09-15]
#  (4) colon-fact readouts — "location: 100% remote, US",
#      "salary: competitive up to $165K" are observed field:value facts, not
#      blockers (genuine blockers never arrive as field + concrete value);
#      word-boundary bleed — "accountability" ([A-Za-z] guard); [2026-09-15]
#  (5) resolved-state annotations — stripped by RESOLVED_STATE_PAT before
#      matching: "relocation authorized" (resolved relocation-clearance
#      annotation), "office-stated (...)" posting facts, "<x> blocker
#      resolved per the applicant <ts>" records, "degree required,
#      unverifiable" scorer facts; [2026-09-16 v2]
#  (6) job titles — "Account Executive/Manager" (title-word lookahead);
#      "location: hybrid - X" colon-fact hybrid (location: lookbehind).
#      [2026-09-16 v2]
# Evidence: 2026-09-15 the 9 verified-live fire-test leads were stuck parked
# because "no degree requirement" / "(applicant: 12)" matched the old pattern.
# ARM 109 (2026-09-15): pulse-72 found 2 APPLY Securly leads (fit 82/76,
# URL-bearing, verification-only) parked indefinitely behind colon-fact
# readouts; the [A-Za-z] guard stops "accountability" while genuine
# "account creation required" still matches.
# J-20260916-0319-veri-517 (2026-09-16): 240/277 PV APPLY leads condemned to
# noop by hits on resolved annotations — "relocation authorized" x92,
# "office-stated (<X> Locations)" x94, "Account Executive/Manager" titles
# x43, "blocker resolved per the applicant" records. Classes (5)/(6)
# unblock them; genuine blockers ("Required office/relocation/travel
# commitment question needs the applicant's explicit answer",
# "account creation required", "needs the applicant's explicit input
# (travel)", "hybrid-onsite") still match.
APPLICANT_NOTE_PAT = re.compile(r"\(applicant:[^)]*\)", re.I)
# Cleared-history strip (2026-09-16): queue_notes is append-only, so it
# accumulates stale mentions of genuine blockers that were already cleared
# ("Answer banked; blocker #1 cleared", "the applicant volunteered:
# YES ...", "attestation answered YES and banked. Zero blockers"). That is
# history, not a current blocker — without this strip, a lead whose
# blockers were genuinely cleared can never read as verification-only
# (false negative; the ClickHouse/Drata/Airtable revival cases, all
# verified LIVE but refused promotion because old notes mention the
# applicant). Line-level, and only queue_notes is stripped:
# unresolved/status_reason/gate_note are current-state fields and still
# fail closed on any genuine-blocker mention, so a truly applicant-blocked
# lead cannot slip through here.
CLEARED_HISTORY_PAT = re.compile(
    r"(?m)^[^\n]*(?:\bcleared\b|\bbanked\b|zero\s+blockers?|"
    r"answered\s+yes|\bvolunteered\b)[^\n]*\n?")
# RESOLVED_STATE_PAT (2026-09-16, J-20260916-0319-veri-517): resolved-state
# annotations are the opposite of blockers. Four classes observed
# condemning 240/277 PV APPLY leads to noop on the 2026-09-16 live pool:
#  (a) "relocation authorized" — a resolved relocation-clearance
#      annotation recording the applicant's relocation decision (92 hits);
#  (b) "office-stated (<X> Locations)" — fit-scorer observed posting-fact
#      annotation, never a question (94 "location" hits);
#  (c) "<x/y/z> blocker resolved per the applicant <ts>" — re-screen
#      resolution records, not open blockers (travel/relocation/applicant
#      hits);
#  (d) "degree required, unverifiable" — fit-scorer scored fact; the
#      genuine degree class is the UNREAD one ("degree or travel
#      requirement needs the applicant's adjudication", "gates unchecked"),
#      which never arrives in this scorer-signature shape.
# Strip is classifier-input only: prescreen reads the RAW queue fields for
# commitment gates (C-15), so defense-in-depth is unchanged. Genuine
# blockers never arrive inside these shapes — pinned by
# tests/test_verify_retry_genuine_pat_v2.py.
# FP-3 (2026-09-16, pulse #189 ARM 2): three bare-\bapplicant\b
# false-positive classes condemning ~26 APPLY-band (fit>=75) verify-only
# leads to noop on the live pool. All are resolution/negation records, the
# opposite of blockers:
#  (e) "input blockers cleared by the applicant <ts>" — input-resolution
#      release records in gate_note/status_reason (CLEARED_HISTORY_PAT is
#      queue_notes-only by design);
#  (f) "(applicant explicit)" / "(applicant's words)" — parenthesized
#      provenance of a banked tray answer ("Tray answer banked 2026-09-16
#      (applicant explicit)"), not a demand;
#  (g) "not the applicant's input" / "No applicant judgment/input needed"
#      — explicit negations ("original park reason was agent verification
#      work, not the applicant's input").
# Genuine possessive/imperative mentions ("needs the applicant's
# selections", "the applicant must judge", "the applicant's actual GPA",
# "Only the applicant can approve") never arrive inside these shapes —
# pinned by tests/test_verify_retry_genuine_pat_fp3.py.
# FP-4 (2026-09-16, pulse #192 trace): false-positive re-adjudication
# clearing records. "false-positive re-adjudication <ts>: <blocker> park
# contradicted by live page" is a documented CLEARING under C-15 extended
# (live-page evidence), but the quoted blocker words ("no-AI attest",
# "pledge") re-tripped GENUINE_PAT and condemned 4 verified-live
# fit>=75 APPLY leads (3x Coinbase, Goodfire) to perpetual noop:
#   (h) the clearing declaration itself — "<blocker> park contradicted
#       by live page" is evidence OF CLEARING, not a live blocker;
#   (i) zero-hit evidence parentheticals — "(... all 0 hits)" records
#       absence of the patterns, the opposite of a blocker.
# Strip is classifier-input only (same as arms (a)-(g)); prescreen reads
# raw fields for commitment gates, so defense-in-depth is unchanged.
# Genuine blockers never arrive inside these shapes — pinned by
# tests/test_verify_retry_genuine_pat_fp4.py.
RESOLVED_STATE_PAT = re.compile(
    r"relocation\s+(?:authorized|approved|cleared|ok\b)"
    r"|office-stated\s*\([^)]*\)"
    r"|(?:[\w/]+\s+)*blocker\s+resolved\s+per\s+the\s+applicant[^;|\n]*"
    r"|degree\s+required,\s+unverifiable"
    r"|blockers?\s+cleared\s+by\s+the\s+applicant\b"
    r"|\(\s*applicant(?:'s)?\s+(?:explicit|words)\s*\)"
    r"|(?:not|no)\s+(?:the\s+)?applicant(?:'s)?\s+(?:input|judg?ment)\b"
    r"|\bno\s+applicant\s+input\s+needed\b"
    r"|false-positive\s+re-adjudication[^.]*?contradicted\s+by\s+live\s+page\.?"
    r"|\([^)]*all 0 hits[^)]*\)"
    #   (j) earlier-park-cleared records — "The earlier <ts>
    #       essay/APPLICANT-ONLY park ... was already gate_cleared <ts> per
    #       telemetry; no genuine blocker remains" documents a CLEARING; the
    #       quoted blocker words ("essay", "APPLICANT-ONLY") are history,
    #       not live blockers.
    r"|the earlier [^.]*? park [^.]*? gate_cleared [^.]*\."
    #   (k) negated gate enumerations — "No degree/travel/office/visa gates"
    #       documents the ABSENCE of blockers (re-adjudication evidence).
    #       The (?<!no ) lookbehinds in GENUINE_PAT only cover the singular
    #       "no <blocker>"; the slash-enumeration shape needs its own arm.
    #       A genuine blocker never arrives as "no <list> gates".
    r"|\bno\s+[\w/]+\s+gates?\b", re.I)
GENUINE_PAT = re.compile(
    r"essay|wording|(?<!zero )attest|reference|captcha|login"
    r"|(?<!no )(?:travel|degree|onsite|relocation)"
    r"|salary(?!\s*:\s*(?:competitive|up\s+to|\$))"
    # v2: "location: hybrid - X" is a colon-fact readout of the observed
    # location (same class as the 2026-09-15 salary/location colon facts).
    # The genuine hybrid class ("hybrid-onsite", MintMCP) never arrives as
    # a location: value.
    r"|(?<!no )(?<!location:)(?<!location: )hybrid(?!\s+likely)"
    r"|location(?!\s+weighs?)(?!\s*:)"
    # v2: "Account <Title>" ("Account Executive" x41, "Account Manager" x2
    # on the live pool) is a job title, not an account-creation wall. The
    # genuine class is always "account creation ..." ("account creation
    # required", "requires new account creation"), which still matches.
    r"|account(?!(?:\s+creation\s+standing-authorized)"
    r"|\s+(?:executive|manager|director|representative)|[A-Za-z])"
    # ARM 67 (2026-09-15): required interview-recording consent is the
    # applicant's own decision. Bigram-only so generic consent ("I consent
    # to the privacy policy") does not false-positive.
    r"|consent(?:-|\s)?to(?:-|\s)?(?:be\s+)?record(?:ing|ed)?"
    r"|(?:interview[-\s]?)?recording[-\s]?consent"
    # Gap-bridge 2026-09-15: authorization annotations are the opposite of
    # blockers. "applicant-authorized"/"applicant-approved" (enumeration
    # provenance, e.g. "(applicant-authorized 2026-09-15, proposal
    # J-...)") must not trip \bapplicant\b — it parked 7 verified-live
    # fit>=75 APPLY leads plus 106 triage-deferred fit>=75 leads
    # indefinitely. Genuine mentions stay possessive/imperative ("the
    # applicant must", "the applicant's input").
    r"|\bapplicant\b(?!-(?:authorized|approved))", re.I)


def _field_text(value, sep="; "):
    """Coerce a conventional queue field to plain text for pattern matching.

    Sweep workers sometimes write `queue_notes`/`status_reason`/`gate_note`
    as lists; joining them defensively keeps the classifier from raising
    TypeError (2026-09-15 pre-mortem: 4 standard-queue entries with
    list-typed queue_notes would crash is_verify_only mid-apply). String
    inputs pass through byte-identically, so the VERIFY_PAT/GENUINE_PAT
    calibration from 2026-09-15 is unchanged; None -> "".
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        return sep.join(str(v) for v in value if v is not None)
    return str(value)


def unresolved_text(e):
    return _field_text(e.get("unresolved"), sep=" ")


def is_verify_only(entry):
    """True when the entry's blockers are verification-state only."""
    qn = CLEARED_HISTORY_PAT.sub("", _field_text(entry.get("queue_notes")))
    t = unresolved_text(entry) + " " + _field_text(entry.get("status_reason")) \
        + " " + _field_text(entry.get("gate_note")) \
        + " " + qn
    t = RESOLVED_STATE_PAT.sub("", t)  # resolved-state annotations are not blockers
    t = APPLICANT_NOTE_PAT.sub("", t)  # internal (applicant: ...) fit annotations are not blockers
    return bool(VERIFY_PAT.search(t)) and not GENUINE_PAT.search(t)
