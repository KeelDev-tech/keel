#!/usr/bin/env python3
"""Answer-consistency guard (2026-09-16, item 2/10).

Prevents another Zipline sponsorship inversion: a finalized launch packet
once carried future-sponsorship=Yes while answer_bank.json's verified
truthful answer is No.

Pure, deterministic, no LLM calls in the hot path: extracts the final
answer set from the packet's rendered brief (the "Fill the form with
these truthful values" section — exactly what the browser task answers
from per the packet's answer-scope discipline) and compares every
material answer against the canonical answer_bank.json value after
normalization.

Public API:
    check_packet(packet, bank) -> {
        "verdict": "CLEAN" | "PARK",
        "mismatches": [ {"key", "category", "kind", "bank", "packet"} ],
        "checked": int,      # material keys compared
        "skipped": [keys],   # material keys with no factual bank answer
    }
    mismatch_reasons(result) -> [str]  # park_lead-ready reason strings

Hook point: apply_loop._buffer_lead, immediately after
prescreen.screen_packet(packet, bank) returns CLEAN and before the
packet is buffered/marked ready. That is the last in-process point
before the packet can reach the browser lane, so it is the
packet-finalization gate.

Fail-closed: ANY mismatch (missing key, polarity inversion, numeric
drift, or any other normalized difference on a material key) -> PARK.
The caller parks via prescreen.park_lead (C-19 verified queue write)
and archives the packet; a mismatched packet never proceeds.

C-11: this module never imports or calls the telemetry-logging module.
The gate event is emitted through the single established engine path
for packet-screen parks — prescreen.park_lead's internal gate_blocked
emission (reached only via the standard park call,
gate="answer_mismatch"). The worker-envelope ingestion CLI is the
browser-task-attempt ingestion path, not a pre-launch gate emitter;
routing a pre-launch guard park through it would fabricate an
application-attempt record in the outcome pipeline, so it is
deliberately not used here.
"""

import ast
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import answer_resolver  # F34: scope-aware comparison of bank vs packet

# Trailing "  [...]" markers the F34 brief renderer appends ([scope: ...],
# [LEGACY — ...], [explicit launch value — ...], [role-explicit value]).
# Stripped before comparison so the marker never reads as value drift.
_F34_MARKER_RE = re.compile(r"\s{2}\[[^\]\n]*\]\s*$")

# Telemetry gate value for guard parks. Registered additively in the
# telemetry module's GATE_TYPES set (2026-09-16); never renamed, never reused.
GATE_VALUE = "answer_mismatch"

# Material answers: key -> category. These are the answers whose
# inversion or drift would misrepresent the applicant on a live application
# (the Zipline class). Everything else in the bank (contact details,
# heard-about, banded questions, essays) is out of scope for this guard.
MATERIAL_KEYS = {
    # work authorization / sponsorship
    "needs_sponsorship": "sponsorship",
    "us_work_auth": "work_authorization",
    "us_citizen_resident": "work_authorization",
    # clearance
    "clearance": "clearance",
    # tenure numbers (verified numeric figures)
    "ai_industry_years": "tenure",
    "total_professional_years": "tenure",
    # education
    "education": "education",
    "bachelors_degree": "education",
    # location / relocation / office commitments (D1 policy)
    "location": "location",
    "relocation_willingness": "relocation",
    "office_location_policy": "office",
    "work_location_intent": "location",
    "remote_work_intent_yesno": "location",
    "work_intent": "location",
    # attestations — full-autopilot scope is pre-authorized, which is
    # exactly why the banked value must match verbatim; a drifted
    # attestation answer is an integrity violation, not a convenience.
    "arbitration_agreement": "attestation",
    "at_will_acknowledgment": "attestation",
    "background_check_consent": "attestation",
    "data_privacy_consent": "attestation",
    "information_truthfulness_attestation": "attestation",
    "ai_evaluation_consent": "attestation",
    "noncompete": "attestation",
    # hard-boundary attestation classes (never-attest) — the banked
    # answer (PARK / DO NOT ATTEST) must survive into the packet intact.
    "personally_completed_certification": "attestation",
}

# --- normalization -------------------------------------------------------

_WS_RE = re.compile(r"\s+")
_ANSWERS_HEADER_RE = re.compile(r"Fill the form with these truthful values")
_ANSWER_LINE_RE = re.compile(r"^\s*-\s*([A-Za-z0-9_]+)\s*:\s*(.*?)\s*$")
_INT_RE = re.compile(r"\d+")
# Canonical section terminator (brief_builder.py): the truthful-values
# answer list is always followed by a blank line and this header. The
# guard must not read past it — the BANDED-QUESTION RULES and HARD GATES
# sections below reuse the same "  - key: value" rendering for rule text
# that is not applicant answers.
_ANSWERS_END_RE = re.compile(r"^BANDED-QUESTION RULES")

# Canonical NEEDS_INPUT deferral annotation rendered by brief_builder for
# abstained keys ("  - {key}: NEEDS_INPUT — no banked answer authorized
# for {employer} ({reason}); do not invent"). An honest deferral to
# the applicant's input is NOT an answer — the abstain branch treats it as absent.
_NEEDS_INPUT_DEFER_RE = re.compile(
    r"^NEEDS_INPUT\s+—\s+no banked answer authorized for\b", re.I)


def _is_deferral_annotation(value):
    """True when the rendered packet value is the brief's canonical
    NEEDS_INPUT launch-guidance annotation, not an answer.

    2026-09-17 (Option A, gate-burst triage arm01-gate-burst-triage.md):
    the brief renders every abstained material key as
    `NEEDS_INPUT — no banked answer authorized for …; do not invent`.
    A recent extract_packet_answers fix made the guard see these deferral
    annotations, and the abstain branch parked every honest packet as an
    "unauthorized answer leak" (143 answer_mismatch × 144 packet_shelved
    on personally_completed_certification in 9 min). Match the canonical
    render shape only — anything else (including a crafted value that
    merely starts with NEEDS_INPUT) still fails closed to out_of_scope.
    """
    return bool(_NEEDS_INPUT_DEFER_RE.match(str(value or "").strip()))

_YES_TOKENS = frozenset({
    "yes", "y", "true", "1", "agree", "agreed", "acknowledge",
    "acknowledged", "consent", "consented", "affirmative",
})
_NO_TOKENS = frozenset({
    "no", "n", "false", "0", "negative", "decline", "declined",
})
# Two-word affirmative openers ("I agree", "I consent", "I acknowledge").
_YES_BIGRAMS = frozenset({"i agree", "i consent", "i acknowledge"})


def _normalize(text):
    """Lowercase, collapse whitespace, strip. The comparison baseline."""
    return _WS_RE.sub(" ", str(text or "").strip().lower())


def _polarity(normalized):
    """YES / NO / None from the leading token(s) of a normalized answer."""
    if not normalized:
        return None
    first = normalized.split(" ", 1)[0]
    if first in _YES_TOKENS:
        return "YES"
    if first in _NO_TOKENS:
        return "NO"
    for bigram in _YES_BIGRAMS:
        if normalized.startswith(bigram):
            return "YES"
    return None


def _leading_int(normalized):
    """First integer in the text, or None (tenure drift detection)."""
    m = _INT_RE.search(normalized or "")
    return int(m.group(0)) if m else None


def _coerce_packet_value(raw):
    """Undo brief_builder's rendering of dict-form bank answers.

    The brief renders dict answers as their Python repr; ast.literal_eval
    recovers the dict so ["answer"] can be compared against the bank's
    canonical answer. F34: trailing "  [...]" scope/legacy markers are
    stripped so marker text never reads as value drift. Anything else
    passes through as a string.
    """
    s = str(raw or "").strip()
    if s.startswith("{") and s.endswith("}"):
        try:
            parsed = ast.literal_eval(s)
        except Exception:
            return s
        if isinstance(parsed, dict) and "answer" in parsed:
            return parsed.get("answer", "")
    while True:
        stripped = _F34_MARKER_RE.sub("", s)
        if stripped == s:
            return s
        s = stripped


# --- extraction ----------------------------------------------------------

def extract_packet_answers(packet):
    """Final answer set from a launch packet, as the browser task sees it.

    Parses the brief's "Fill the form with these truthful values" section
    (key: value lines). Multi-line values (essay answers render across
    several lines and blank-line-separated paragraphs) are accumulated
    into the current key — the old break-on-blank-line behavior silently
    dropped every material key rendered after the first multi-line essay
    (2026-09-17: 26 false ANSWER-GUARD-PARKs on attestation keys that were
    present in the brief but invisible to the parser). The section ends at
    the canonical BANDED-QUESTION RULES header. Returns {} when the section
    is absent — the guard then fails closed on every material key.
    """
    brief = (packet or {}).get("brief", "") or ""
    answers = {}
    in_section = False
    current_key = None
    for line in brief.splitlines():
        if not in_section:
            if _ANSWERS_HEADER_RE.search(line):
                in_section = True
            continue
        if _ANSWERS_END_RE.match(line):
            break
        m = _ANSWER_LINE_RE.match(line)
        if m:
            current_key = m.group(1)
            answers[current_key] = m.group(2)
            continue
        if current_key is not None:
            answers[current_key] = (answers[current_key] + " " +
                                    line.strip()).strip()
    return answers


# --- packet hygiene (pre-gate strip) -------------------------------------

def strip_unanswerable_answers(packet, bank):
    """Drop rendered answer lines for keys the resolver abstains on.

    Packet-assembly hygiene, run BEFORE check_packet (2026-09-18,
    J-20260918-0900-gate-1990): a bank key the resolver abstains on for
    this employer/role -- no authorized banked answer, including
    personal-takeover refusal values (the resolver abstains on those per
    the ARM1 adjudication; a refusal is not a resolution) -- must not
    appear in the packet's rendered answer list at all. Neither as a
    value (an unanswerable attestation rendered as answered burns a
    full brief-build + consistency-gate cycle per lead) nor as a
    NEEDS_INPUT deferral line (honest guidance, but not an answer; the
    browser's answer-scope discipline reads only truthful values).

    Only bank keys are judged: role-explicit lines (compensation) and
    packet-explicit launch values are not bank reads and are never
    stripped. A resolver error fails closed to KEEP the line -- a key
    the strip cannot judge stays in the packet for the (unchanged) gate
    to decide.

    Pure: returns (stripped_packet, stripped_keys). The input packet
    dict is never mutated. check_packet itself is unchanged -- an
    abstained key absent from the packet is skipped by its abstain
    branch, so the gate's fail-closed character is preserved.
    """
    brief = (packet or {}).get("brief", "") or ""
    bank_answers = (bank or {}).get("answers", {}) or {}
    if not brief or not bank_answers:
        return packet, []
    lines = brief.splitlines()
    start = end = None
    for i, ln in enumerate(lines):
        if start is None:
            if _ANSWERS_HEADER_RE.search(ln):
                start = i
            continue
        if _ANSWERS_END_RE.match(ln):
            end = i
            break
    if start is None:
        return packet, []
    if end is None:
        end = len(lines)
    try:
        employer = (packet or {}).get("company") or ""
        role_ctx = {"role_id": (packet or {}).get("role_id"),
                    "company": employer}
    except Exception:
        employer, role_ctx = "", {}
    stripped = set()
    kept = []
    i = start + 1
    while i < end:
        ln = lines[i]
        m = _ANSWER_LINE_RE.match(ln)
        if m and m.group(1) in bank_answers:
            key = m.group(1)
            try:
                res = answer_resolver.resolve(
                    key, bank_answers.get(key), employer=employer,
                    role_context=role_ctx)
            except Exception:
                res = None
            if res is not None and res.status == answer_resolver.STATUS_ABSTAIN:
                stripped.add(key)
                i += 1
                # Swallow the dropped key's continuation lines (multi-line
                # values accumulate into the key per extract_packet_answers).
                while i < end and not _ANSWER_LINE_RE.match(lines[i]):
                    i += 1
                continue
        kept.append(ln)
        i += 1
    if not stripped:
        return packet, []
    new_packet = dict(packet)
    new_packet["brief"] = "\n".join(lines[:start + 1] + kept + lines[end:])
    if isinstance(new_packet.get("brief_chars"), int):
        new_packet["brief_chars"] = len(new_packet["brief"])
    return new_packet, sorted(stripped)


# --- comparison ----------------------------------------------------------

def _compare_one(key, bank_resolved_value, packet_raw):
    """Compare one material key. Returns (ok, kind, bank_shown, packet_shown).

    bank_resolved_value is the resolver's authorized value for this
    employer/role (F34) — never the raw bank entry, so scope filtering
    cannot be bypassed at comparison time.

    kind is one of: match | skipped | missing | polarity_inversion |
    numeric_drift | value_drift.
    """
    bank_canonical = bank_resolved_value
    if bank_canonical is None or _normalize(bank_canonical) == "":
        return True, "skipped", "", ""
    packet_value = _coerce_packet_value(packet_raw)
    bn = _normalize(bank_canonical)
    pn = _normalize(packet_value)
    if bn == pn:
        return True, "match", bn, pn
    if pn == "":
        return False, "missing", str(bank_canonical), ""
    pol_b, pol_p = _polarity(bn), _polarity(pn)
    if pol_b and pol_p:
        # Both sides are boolean-polarity answers: same polarity is a
        # match after yes/no normalization ("Y" == "Yes", "N" == "No",
        # "TRUE" == "Yes", "I agree" == "Agree"); opposite polarity is
        # the Zipline-class inversion and fails closed.
        if pol_b != pol_p:
            return False, "polarity_inversion", str(bank_canonical), str(packet_value)
        return True, "match", bn, pn
    ib, ip = _leading_int(bn), _leading_int(pn)
    if ib is not None and ip is not None:
        # Numeric equivalence: the material numeric fact agrees and only
        # the rendering differs ("5" vs "5 years"). A differing number
        # is drift and fails closed.
        if ib != ip:
            return False, "numeric_drift", str(bank_canonical), str(packet_value)
        return True, "match", bn, pn
    return False, "value_drift", str(bank_canonical), str(packet_value)


def check_packet(packet, bank):
    """Cross-check a finalized packet against answer_bank.json.

    Never raises on malformed input — any structural problem fails
    closed to PARK (a packet the guard cannot read is a packet the
    guard cannot clear).
    """
    try:
        return _check_packet_inner(packet, bank)
    except Exception as ex:
        return {
            "verdict": "PARK",
            "mismatches": [{
                "key": "<guard-error>",
                "category": "guard",
                "kind": "guard_error",
                "bank": "",
                "packet": "%s: %s" % (type(ex).__name__, ex),
            }],
            "checked": 0,
            "skipped": [],
        }


def _check_packet_inner(packet, bank):
    bank_answers = (bank or {}).get("answers", {}) or {}
    if not bank_answers:
        # Fail-closed: no bank baseline means no material key can be
        # verified — a packet the guard cannot check against the bank is
        # a packet the guard cannot clear.
        return {
            "verdict": "PARK",
            "mismatches": [{
                "key": "<guard-error>",
                "category": "guard",
                "kind": "bank_unreadable",
                "bank": "",
                "packet": "",
            }],
            "checked": 0,
            "skipped": [],
        }
    packet_answers = extract_packet_answers(packet)
    mismatches = []
    skipped = []
    checked = 0
    # F34: the guard compares through the same resolver the brief renders
    # with. Red-team round 9: a key the resolver abstains on for this
    # employer is DELIBERATELY absent from the brief — an abstained key
    # that is ABSENT from the packet is skipped, but an abstained key that
    # APPEARS in the packet with a value is an out-of-scope answer leaking
    # into the packet, and the guard PARKs it (kind "out_of_scope").
    # Resolved values compare against the brief's rendered line (F34
    # markers stripped by _coerce_packet_value); flagged-legacy compares
    # against its marked value, never silently broadened.
    # Option A (2026-09-17): the brief ALSO renders abstained keys as
    # NEEDS_INPUT launch-guidance annotations (brief_builder.py) — the
    # canonical annotation is an honest deferral, treated as absent via
    # _is_deferral_annotation, never as a leak.
    try:
        employer = (packet or {}).get("company") or ""
        # The packet's company rides as the canonical employer binding
        # (red-team round 9): a legacy free-text scope only authorizes when
        # the role context independently corroborates the requester.
        role_ctx = {"role_id": (packet or {}).get("role_id"),
                    "company": employer}
    except Exception:
        employer, role_ctx = "", {}
    for key, category in MATERIAL_KEYS.items():
        _res = answer_resolver.resolve(
            key, bank_answers.get(key), employer=employer,
            role_context=role_ctx)
        if _res.status == "abstain":
            _pkt_val = packet_answers.get(key)
            # Option A (2026-09-17): the brief's canonical NEEDS_INPUT
            # launch-guidance annotation is an honest deferral to the applicant's
            # input, not an answer leak — treat it as absent (skipped),
            # never as out_of_scope. The lead still parks via prescreen's
            # own NEEDS_INPUT → needs_input path with honest framing.
            if (_pkt_val is not None and str(_pkt_val).strip() != ""
                    and not _is_deferral_annotation(
                        _coerce_packet_value(_pkt_val))):
                checked += 1
                mismatches.append({
                    "key": key,
                    "category": category,
                    "kind": "out_of_scope",
                    "bank": "",
                    "packet": "",
                })
            else:
                skipped.append(key)
            continue
        ok, kind, bank_shown, packet_shown = _compare_one(
            key, _res.value, packet_answers.get(key))
        if kind == "skipped":
            skipped.append(key)
            continue
        checked += 1
        if not ok:
            mismatches.append({
                "key": key,
                "category": category,
                "kind": kind,
                "bank": bank_shown,
                "packet": packet_shown,
            })
    return {
        "verdict": "PARK" if mismatches else "CLEAN",
        "mismatches": mismatches,
        "checked": checked,
        "skipped": skipped,
    }


# --- park reasons ----------------------------------------------------------

_KIND_TEXT = {
    "missing": "absent from the packet's answer list",
    "out_of_scope": "answer not authorized for this employer/role "
                    "(resolver abstained) but present in the packet",
    "polarity_inversion": "polarity inversion (yes/no flip)",
    "numeric_drift": "numeric value drift",
    "value_drift": "value drift from the banked answer",
    "guard_error": "guard could not read the packet",
    "bank_unreadable": "answer bank unreadable or empty — no baseline to compare",
}


def _kind_fact(m):
    """Compact, computed divergence fact for the reason string.

    Deliberately value-free: raw bank/packet quotes can carry
    VERIFY_PAT substrings (e.g. education's "verified Coursera
    completions"), which would make verify_retry misread the park as
    verification-only and resurrect the lead. Full values ride in the
    gate telemetry details instead (telemetry_details).
    """
    kind = m.get("kind")
    if kind == "polarity_inversion":
        pb = _polarity(_normalize(m.get("bank", ""))) or "?"
        pp = _polarity(_normalize(m.get("packet", ""))) or "?"
        return "packet says %s, bank says %s" % (pp, pb)
    if kind == "numeric_drift":
        ib = _leading_int(_normalize(m.get("bank", "")))
        ip = _leading_int(_normalize(m.get("packet", "")))
        return "packet has %s, bank has %s" % (ip, ib)
    return _KIND_TEXT.get(kind, kind)


def mismatch_reasons(result):
    """Render guard mismatches as park_lead-ready reason strings.

    Value-free by design (see _kind_fact): no raw bank/packet quotes,
    so the conventional queue fields can never trip VERIFY_PAT and the
    park is never mistaken for a verification-only hold. Also free of
    GENUINE_PAT genuine-input framing — this is an integrity defect
    for agent repair, not a question for the applicant.
    """
    reasons = []
    for m in result.get("mismatches", []):
        reasons.append(
            "answer-consistency guard: packet answer for '%s' (%s) "
            "diverges from answer_bank.json [%s: %s]; packet blocked "
            "before submission — agent repair required before rebuild "
            "(bank-vs-packet values in gate telemetry details)" % (
                m["key"], m["category"], m["kind"], _kind_fact(m)))
    return reasons


def _mismatch_question(key, bank):
    """Attestation/form wording for a mismatched bank key, or "".

    The bank entry carries the exact wording the applicant authorized ("question",
    written by tray_answer.apply_bank_write); plain-string standing answers
    carry none. Telemetry details are NOT scanned by
    verify_retry.is_verify_only, so quoting the wording here is safe.
    """
    try:
        entry = ((bank or {}).get("answers") or {}).get(key)
        if isinstance(entry, dict):
            return str(entry.get("question") or "")
    except Exception:
        pass
    return ""


def telemetry_details(result, bank=None):
    """Full mismatch records for the gate event details dict.

    Carries the raw bank-vs-packet values the repair agent needs.
    Telemetry details are NOT scanned by verify_retry.is_verify_only,
    so quoting values here is safe.

    2026-09-17 (dev-support-deep-sweep run 130,
    J-20260918-0354-gate-1840): every mismatch now carries "question"
    (the bank entry's authorized wording, "" when the entry is a
    plain-string standing answer) so the gate-clustering analysis can
    cluster answer_mismatch parks by question text without joining to
    answer_bank.json. Additive only; callers that pass no bank get "".
    """
    return {
        "answer_consistency_guard": True,
        "answer_mismatches": [
            {"key": m["key"], "category": m["category"], "kind": m["kind"],
             "question": _mismatch_question(m["key"], bank),
             "bank": m["bank"][:300], "packet": m["packet"][:300]}
            for m in result.get("mismatches", [])
        ],
        "answer_keys_checked": result.get("checked", 0),
    }
