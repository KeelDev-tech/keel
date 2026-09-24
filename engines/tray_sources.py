#!/usr/bin/env python3
"""Blocker-source taxonomy for the input tray (Keel 0.4 P3, 2026-09-17).

The tray used to charge four different kinds of blockage to the applicant as if
they were all "questions he must answer". This module labels every card
with its true source so the digest can route each kind to its real owner:

  genuine_missing -- the question is confirmed (authoritative form intel,
                     verified posting text, or the applicant-visible form state)
                     AND the answer is the applicant's own words/decision.
  intel_gap       -- the blocker depends on a fact only a lookup can
                     provide (e.g. Stripe's WhatsApp prior with no form
                     intel to rule it out). the applicant cannot answer a question
                     he cannot see; this routes to the intel pipeline,
                     never to his taps.
  software_error  -- exception-path parks (K06 prescreen-exception parks,
                     FRP hook failures, queue-write failures). Engineering
                     owns these; never a question.
  stale_packet    -- packet built_ts older than the materials/intel refresh
                     watermark for the role. Rebuild feeder owns it.

Everything here is pure (text in, label out) EXCEPT the optional
evidence-gated stale_packet parameters. The digest layer only; queue files
are never mutated to clean the digest. Fail closed: unknown or ambiguous
input is genuine_missing, never a downgraded system card.
"""
import re

# The closed label set. Every card in hidden_files/input-tray.json carries
# one of these in its "source" field (spec §5 acceptance criterion).
SOURCES = ("genuine_missing", "intel_gap", "software_error", "stale_packet")

# ---------------------------------------------------------------------------
# Rule 1 — software_error: K06 prescreen-exception parks and other
# unambiguous exception-path markers.
#
# K06 (Keel 0.3.1 port, apply_loop.py:2995): prescreen.screen_packet must
# NEVER coerce to CLEAN; the park reason is built as
#   "prescreen.screen_packet raised {Type}: {e} — packet not screened;
#    parked fail-closed (K06)"
# These strings are distinctive by construction — they name the exception
# path, never a question for the applicant.
# ---------------------------------------------------------------------------
_SOFTWARE_ERROR_RE = re.compile(
    r"prescreen\.screen_packet raised"        # K06 exception-park template
    r"|parked fail-closed \(K06\)"            # K06 canonical marker
    r"|FRP hook failed"                       # field-question protocol failure surfaced
    r"|queue write failed|queue-write failure",  # queue-write failure
    re.IGNORECASE)


# ---------------------------------------------------------------------------
# Rule 2 — verify-class intel_gap: verify_retry verdict "ambiguous" with a
# cooldown/transport message. BOTH must be present (fail closed): the bare
# word "ambiguous" also appears in D1 office-frequency cards ("exceed it or
# are ambiguous") which are genuine NEEDS-YOU questions and must never
# match here.
# ---------------------------------------------------------------------------
def _verify_ambiguous(text):
    if not re.search(r"\bambiguous\b", text, re.IGNORECASE):
        return False
    return bool(re.search(
        r"\b(cooldown|transport|verify_retry|resolve failed|host.{0,16}unavailable)\b",
        text, re.IGNORECASE))


# ---------------------------------------------------------------------------
# Rule 3 — blind-prior intel_gap (the Stripe WhatsApp pattern).
#
# prescreen._prior_reason(company, emp_key, blocker, blind=True)
# (prescreen.py:947-955) emits:
#   Employer form pattern for {company} with no form intel in packet to
#   rule it out -- needs the applicant's explicit input ({cls}): "{blocker}"
# (the template uses a double hyphen, not an em dash).
#
# The parenthetical ({cls}) is the _blocker_class hint vocabulary:
# "consent", "attest", "recording consent", "essay", "travel" (older rows
# carry compound hints like "attest/travel"). INTEGRITY CARVE-OUT (spec
# §3.3, the load-bearing input_tray_digest.py:176-180 comment): ONLY the
# pure preference/consent class routes to intel_gap. Any integrity hint
# (attest, recording consent, essay, travel) — or no hint at all — stays
# NEEDS-YOU (default-closed), because for integrity-class blockers the applicant's
# decision is needed regardless of exact wording.
#
# Regex caution (AGENTS.md lesson): short tokens need word boundaries —
# \btrip\b once matched "Stripe". Here we never substring-match the hint:
# the parenthetical is split on "/" and tokens are matched EXACTLY, so
# "recording consent" never collapses to "consent".
# ---------------------------------------------------------------------------
_BLIND_PRIOR_RE = re.compile(
    r"Employer form pattern for .+? with no form intel in packet to rule it out",
    re.IGNORECASE)

# Matches the "(cls)" suffix; tolerates straight or curly apostrophes.
_CLASS_HINT_RE = re.compile(
    r"needs the applicant['\u2019]s explicit input\s*\(([^)]*)\)", re.IGNORECASE)

# The ONLY hint token that routes a blind prior to intel_gap. Closed and
# default-closed: anything else (integrity hints, compound hints, unknown
# hints, no hint) is genuine_missing.
_PREFERENCE_CLASSES = frozenset({"consent"})


def _blind_prior_hint_tokens(text):
    m = _CLASS_HINT_RE.search(text)
    if not m:
        return set()
    return {t.strip().lower() for t in m.group(1).split("/") if t.strip()}


def classify_source(question, norm="", *, built_ts=None, refresh_watermark=None):
    """Return the blocker-source label for a card. Pure function.

    Args:
        question: the card's short question text.
        norm: the card's normalized full text (family header stripped).
        built_ts: packet built timestamp (epoch), when evidence is
            available. refresh_watermark: materials/intel refresh watermark
            (epoch) for the role. stale_packet is ONLY returned when both
            are supplied and built_ts < refresh_watermark — unparseable
            or absent evidence is not evidence (fail closed).
    """
    text = f"{question} {norm or ''}"

    # software_error always wins: an exception path is never a question.
    if _SOFTWARE_ERROR_RE.search(text):
        return "software_error"

    # stale_packet: evidence-gated only.
    if built_ts is not None and refresh_watermark is not None:
        try:
            if float(built_ts) < float(refresh_watermark):
                return "stale_packet"
        except (TypeError, ValueError):
            pass  # unparseable evidence is not evidence — fall through

    # verify-class: ambiguous verdict with cooldown/transport framing.
    if _verify_ambiguous(text):
        return "intel_gap"

    # blind prior: no form intel to rule the employer pattern out.
    if _BLIND_PRIOR_RE.search(text):
        hints = _blind_prior_hint_tokens(text)
        if hints and hints <= _PREFERENCE_CLASSES:
            return "intel_gap"
        # integrity carve-out + default-closed: no hint, or any
        # non-preference hint, stays NEEDS-YOU.
        return "genuine_missing"

    # everything else: confirmed question + the applicant's own words/decision.
    return "genuine_missing"


# ---------------------------------------------------------------------------
# Rule 4 — advisory-only form intel (K49 passive_intel) never gates the tray.
# The blind=True template fires exactly when prescreen finds no form
# questions for an employer with priors, which is the advisory/unknown-intel
# case — but this helper answers the payload-level question directly for
# the promotion path and regression tests.
# ---------------------------------------------------------------------------
def intel_gap_from_form_intel(intel, employer_has_priors):
    """True when the blocker depends on advisory-only form intel for a role
    whose employer has confirmed blocker priors. Lazy form_intel import:
    no import-cycle risk with the digest layer."""
    try:
        from form_intel import is_authoritative_intel
    except Exception:
        return False  # cannot evaluate intel — fail closed, not intel_gap
    return bool(employer_has_priors) and not is_authoritative_intel(intel)


# ---------------------------------------------------------------------------
# Promotion path (spec §3.2): intel_gap -> genuine_missing.
#
# When authoritative intel later arrives for a role with an open intel_gap
# card: the prior is checked against the CONFIRMED questions. Prior
# confirmed -> new genuine_missing card with the confirmed question text,
# same key/family grouping (the 38 Stripe leads collapse to one card).
# Prior ruled out -> the park is voided at the prescreen layer (the lead
# re-flows through screen_packet; no tray action). Intel still absent ->
# stays intel_gap forever: a card NEVER degrades into a blind question.
# ---------------------------------------------------------------------------

# Retry bound for the authoritative form probe on an intel_gap card. After
# this many probe attempts with no authoritative intel, the card stays
# intel_gap permanently (spec §3.1: "Intel still absent after retry budget
# → stays intel_gap forever (never degrades into a blind question)").
# The spec's honest fallback — surfacing the unverified prior to the applicant as
# an explicitly *unverified* question — is a tray-surface decision made by
# the intel pipeline when it invokes promote_intel_gap, not an automatic
# decay here.
INTEL_PROBE_RETRY_BUDGET = 3


def intel_probe_exhausted(attempts):
    """True when the probe retry budget is spent."""
    try:
        return int(attempts) >= INTEL_PROBE_RETRY_BUDGET
    except (TypeError, ValueError):
        return False


# Human-readable reasons for the taxonomy SYSTEM-BLOCKED surface. These
# are the digest-layer reasons carried in input-tray.json's "blocked_reason"
# for intel_gap / software_error / stale_packet cards.
SYSTEM_REASONS = {
    "intel_gap": ("intel-gap: form intel unreadable — the employer prior "
                  "cannot be confirmed or ruled out; queued for "
                  "authoritative form probe"),
    "software_error": ("software-error: exception-path park — queued for "
                       "engineering (Program Office register)"),
    "stale_packet": ("stale-packet: packet older than the materials/intel "
                     "refresh watermark — queued for rebuild"),
}


def system_line(card):
    """One-line system-section rendering for a taxonomy-blocked card
    (spec §3.4). Pure; used by the digest renderer."""
    source = (card or {}).get("source")
    question = (card or {}).get("question") or ""
    leads = (card or {}).get("leads") or []
    employer = leads[0].get("employer") if leads else ""
    n = (card or {}).get("unblock_leads") or len(leads)
    plural = "" if n == 1 else "s"
    key = (card or {}).get("key") or ""
    if source == "intel_gap":
        prior = _prior_quoted_text(card)
        return (f"SYSTEM-BLOCKED (intel-gap): {employer} form unreadable — "
                f"'{prior}' prior cannot be confirmed or ruled out; "
                f"{n} lead{plural} held. Queued for authoritative form probe. "
                f"(key {key})")
    if source == "software_error":
        return (f"SYSTEM-BLOCKED (engineering): {question[:110]} — "
                f"{n} lead{plural} held. Queued for engineering. (key {key})")
    if source == "stale_packet":
        return (f"SYSTEM-BLOCKED (stale-packet): {question[:110]} — "
                f"{n} lead{plural} held. Queued for rebuild. (key {key})")
    return ""


def _content_words(s):
    return {w for w in re.findall(r"[a-z]{4,}", (s or "").lower())}


def _prior_quoted_text(card):
    """The employer-pattern wording the blind template quotes, e.g.
    "WhatsApp recruiting opt-in". Searches the norm first, then the
    question (digest payload cards carry only 'question')."""
    text = (card or {}).get("norm") or (card or {}).get("question") or ""
    m = re.search(r'"([^"]{8,200})"', text)
    if m:
        return m.group(1).strip()
    return text.strip()


def _confirming_question(prior_text, questions):
    """Return the confirmed question that is about the same thing the prior
    was about, or None. Distinctive long tokens from the prior must appear
    word-bounded in the confirmed question label/text (e.g. "WhatsApp" in
    "Want WhatsApp recruiting updates?"). Fail closed: no distinctive
    overlap, no confirmation — a weak match would bank a fabricated link."""
    toks = [t for t in re.findall(r"[A-Za-z]{6,}", prior_text)]
    for q in questions or []:
        if not isinstance(q, dict):
            continue
        qtext = str(q.get("label", "")) + " " + str(q.get("text", ""))
        for t in toks:
            if re.search(r"\b" + re.escape(t) + r"\b", qtext, re.IGNORECASE):
                return q
    return None


def promote_intel_gap(card, authoritative_intel):
    """Promote/void/stay decision for an intel_gap card once intel arrives.

    Returns (decision, payload):
      ("stay", None)    — no authoritative intel yet; card stays intel_gap
                          (never degrades into a blind question).
      ("promote", card) — prior CONFIRMED: new card with source
                          genuine_missing, the confirmed question text, and
                          the same key/family grouping. the applicant's answer is
                          applied only to this confirmed question —
                          no blind banking (provenance rule).
      ("void", None)    — prior ruled out: the park is voided at the
                          prescreen layer (lead re-flows through
                          screen_packet); no tray action needed.
    """
    try:
        from form_intel import authoritative_questions
        questions = authoritative_questions(authoritative_intel)
    except Exception:
        return ("stay", None)  # intel unevaluable — never degrade blind
    if not questions:
        return ("stay", None)
    prior_text = _prior_quoted_text(card)
    match = _confirming_question(prior_text, questions)
    if match is None:
        return ("void", None)
    label = str(match.get("label") or match.get("text") or "").strip()
    new_card = dict(card)
    new_card["source"] = "genuine_missing"
    new_card["status"] = "NEEDS-YOU"
    new_card["blocked_reason"] = None
    if label:
        new_card["question"] = label[:160]
        new_card["norm"] = label
    new_card["promoted_from"] = "intel_gap"
    return ("promote", new_card)
