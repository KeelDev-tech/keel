#!/usr/bin/env python3
"""Contract-first validation for the FORM INTEL payload (ADOPTION 3 of 5).

The FORM INTEL section ("FORM INTEL -- VERIFIED PRE-LAUNCH") is produced by
prescreen.extract_form_intel(brief) and consumed by the question extractors
(extract_required_text_questions / extract_option_questions) and the
per-question D1 numeric scan inside prescreen.screen_packet. This module is
the contract between producer and consumers: it names the exact shape the
payload must have, and fails CLOSED on the payload (never on the lane).

Historical incidents this contract catches (the rules map 1:1):
  - dropdown/checkbox/radio questions rendered without enumerated OPTIONS,
    silently skipped by extract_option_questions (C3X-AXON-79068290: an
    explicit "up to 30% travel" dropdown screened CLEAN because its OPTIONS
    never reached question_mappable)
  - checkboxes mislabeled as [text] (CSSMER sessions: experience-assertion
    checkboxes rendered as [text] lines -- the field-type mismatch changed
    the answer contract and the park reason quoted the wrong blockers)
  - question lines carrying an unknown field_type marker, which both
    extractors silently ignore

The contract is asserted at the call site where the intel enters the launch
path (apply_loop, before prescreen.screen_packet), never inside
prescreen.extract_form_intel's body. On ContractViolation the caller logs a
`contract_violation` telemetry event and treats the intel as unavailable
("") -- the same shape as Ashby packets with no per-job intel, which the
screen already handles. The archived packet is untouched; the loop keeps
moving.
"""

import re

FORM_INTEL_CONTRACT_VERSION = 1

# Typed field types the FORM INTEL producer may render. brief_builder emits
# "- [type] label" lines from the probe's question types; any line carrying
# a marker outside this set is a producer bug (or a new type the consumers
# were never taught) and must not flow silently into the screen.
FIELD_TYPES = frozenset({
    "text", "dropdown", "checkbox", "radio",
    "textarea", "number", "date", "file", "select",
})

# Field types that MUST enumerate their options inline as
# "OPTIONS: a | b | c". Without the enumeration the option extractor and
# the per-question screens silently skip the question.
OPTION_FIELD_TYPES = frozenset({"dropdown", "checkbox", "radio", "select"})

# Producer-sanctioned non-question marker lines. prescreen parses these with
# its own markers (e.g. CAPTCHA_MARKER_RE for the Lever hCaptcha carrier);
# they are not form questions and the typed contract does not apply to them.
MARKER_TYPES = frozenset({"captcha"})

_QUESTION_RE = re.compile(r"^\s*-\s*\[([^\]]*)\]\s*(.*)$")
_NEED_RENDERED_HEAD_RE = re.compile(r"rendered-option read", re.I)


class ContractViolation(Exception):
    """Raised by assert_form_intel when the payload breaks the contract."""


def _norm_label(label):
    """Normalize a question label for cross-referencing (strip the trailing
    required `*` the producer appends to required questions)."""
    return (label or "").strip().rstrip("*").strip()


def _need_rendered_labels(intel):
    """Labels the producer declared as "enumerate on arrival".

    The Greenhouse embed probe renders dropdowns with no OPTIONS and lists
    them under the "Dropdowns still needing rendered-option read" block --
    their options are enumerated by the browser at launch, so the OPTIONS
    rule is waived for exactly these labels (0 false flags on real
    Greenhouse intel).
    """
    labels = set()
    in_block = False
    for raw in (intel or "").splitlines():
        s = raw.strip()
        if _NEED_RENDERED_HEAD_RE.search(s):
            in_block = True
            continue
        if in_block:
            if s.startswith("*"):
                lab = _norm_label(s.lstrip("*"))
                if lab:
                    labels.add(lab)
                continue
            if s:
                break  # first non-bullet, non-blank line ends the block
    return labels


def validate_form_intel(intel):
    """Return the list of contract violations in the FORM INTEL payload.

    Empty list = valid. Empty/blank intel is valid (no intel to screen --
    the Ashby shape). Each violation string names the violated rule and
    quotes the offending line.
    """
    violations = []
    if not intel or not str(intel).strip():
        return violations
    need_rendered = _need_rendered_labels(intel)
    for lineno, raw in enumerate(str(intel).splitlines(), 1):
        m = _QUESTION_RE.match(raw)
        if not m:
            continue  # not a question line (prose, ATS-API dumps, etc.)
        ftype = (m.group(1) or "").strip().lower()
        rest = m.group(2) or ""
        line = raw.strip()
        if ftype in MARKER_TYPES:
            continue  # producer-sanctioned marker, not a question
        if ftype not in FIELD_TYPES:
            violations.append(
                "rule=field_type_enum: line %d carries unknown field type "
                "[%s] (expected one of %s): %r"
                % (lineno, m.group(1).strip(),
                   "|".join(sorted(FIELD_TYPES)), line))
            continue
        has_options_marker = "OPTIONS:" in rest
        if ftype in OPTION_FIELD_TYPES:
            label = _norm_label(rest.split("OPTIONS:", 1)[0])
            if not has_options_marker:
                if label in need_rendered:
                    continue  # options enumerated on arrival, per producer
                violations.append(
                    "rule=options_required: line %d is [%s] but carries no "
                    "OPTIONS: list (option extractors skip it silently): %r"
                    % (lineno, ftype, line))
                continue
            opts = [o.strip()
                    for o in rest.split("OPTIONS:", 1)[1].split("|")
                    if o.strip()]
            if not opts:
                violations.append(
                    "rule=options_required: line %d has OPTIONS: but zero "
                    "options: %r" % (lineno, line))
        else:
            # Scalar field types must not smuggle option enumerations.
            if has_options_marker:
                opts = [o.strip()
                        for o in rest.split("OPTIONS:", 1)[1].split("|")
                        if o.strip()]
                if opts:
                    violations.append(
                        "rule=options_on_scalar_field: line %d is [%s] but "
                        "carries an OPTIONS: list (checkbox mislabeled as "
                        "[%s]?): %r" % (lineno, ftype, ftype, line))
            else:
                segs = [s.strip() for s in rest.split("|") if s.strip()]
                if len(segs) >= 2:
                    violations.append(
                        "rule=text_with_option_enumeration: line %d is "
                        "[%s] but contains |-separated options "
                        "(mis-typed option question?): %r"
                        % (lineno, ftype, line))
    return violations


def assert_form_intel(intel):
    """Raise ContractViolation naming the contract version, the violated
    rule(s), and the offending line(s). No-op when the payload is valid."""
    violations = validate_form_intel(intel)
    if violations:
        raise ContractViolation(
            "form_intel_contract v%d: %d violation(s): %s"
            % (FORM_INTEL_CONTRACT_VERSION, len(violations),
               " | ".join(violations)))


def strip_intel_section(brief, intel):
    """Return the brief with the extracted FORM INTEL section removed.

    Used by the fail-closed path: the screen proceeds with the intel
    treated as unavailable, exactly the Ashby no-intel shape.
    """
    if brief and intel:
        return brief.replace(intel, "", 1)
    return brief or ""
