"""Read-only READY admission and scoped legacy launch-packet integrity.

READY is an inventory label, not proof of an admissible application. These
checks never answer questions, clear holds, move queues, or authorize a submit.
Legacy packets without the scoped manifest must be rebuilt; review-only modern
packets remain review-only. Hashes detect changes, not truth or authenticity.
"""
from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from pathlib import Path

import answer_resolver
from safe_io import contained_path, digest, file_digest, fresh
from keel_paths import HOME
from fit_policy import main_floor

FIT_FLOOR = main_floor()  # compatibility alias; admission reads current policy
READY_STATES = {"READY", "READY-FOR-BROWSER"}
PACKET_MAX_AGE_SECONDS = 12 * 3600
HOLD_FIELDS = (
    "holds", "human_hold", "manual_hold", "operator_hold", "revoked",
    "permanent_hold", "permanent_exclusion", "structurally_blocked",
    "d1_office_exclusion", "never_auto_submit_attestation", "applicant_only",
    "personal_takeover_required", "no_ai", "no_ai_required",
    "no_ai_unaided_writing", "unaided_writing_required",
    "no_ai_attestation", "no_ai_or_unaided_work",
)
QUESTION_FIELDS = ("unresolved", "open_questions", "unanswered_questions")
LEDGER_HOLD_STATES = frozenset({"SUBMITTED", "SUBMISSION_CLAIMED", "IN-FLIGHT",
    "APPLYING", "UNKNOWN_OUTCOME", "REJECTED", "DEAD", "CANCELLED"})


def _identity_label(value):
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError("invalid identity label")
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", "", value.lower())).strip()


def _posting_key(entry):
    from posting_identity import identity
    url = next((entry.get(key) for key in ("ats_url", "application_url", "posting_url", "job_url", "apply_url", "url")
                if entry.get(key)), "")
    try:
        return identity(url)
    except (TypeError, ValueError):
        return None


def ledger_holds(entry, ledger_rows):
    """Pure exact identity/twin hold check; raises on malformed ledger rows."""
    from safe_io import rows
    records = rows(ledger_rows)
    rid = entry.get("role_id")
    company = _identity_label(entry.get("company"))
    title = _identity_label(entry.get("role_title") or entry.get("title"))
    posting = _posting_key(entry)
    for record in records:
        status = record.get("status")
        if not isinstance(status, str) or not status.strip():
            raise ValueError("ledger outcome is missing or malformed")
        other_company = _identity_label(record.get("company"))
        other_title = _identity_label(record.get("role_title") or record.get("title"))
        exact = (bool(rid and record.get("role_id") == rid)
                 or posting is not None and posting == _posting_key(record)
                 or bool(company and title and company == other_company and title == other_title))
        # This ledger records application outcomes. An unfamiliar outcome for
        # the same identity is unresolved history, not affirmative clearance.
        if exact:
            return True
    return False


def blocked_employers(path):
    """Read exact names from the setup format or legacy bold-name section."""
    with open(path, "rb") as stream:
        body = stream.read(65537)
    if len(body) > 65536:
        raise ValueError("employer blocklist exceeds size limit")
    content = body.decode("utf-8")
    if "\x00" in content:
        raise ValueError("invalid employer blocklist text")
    lines = content.splitlines()
    has_section = any(re.fullmatch(r"\s*#{1,6}\s*Blocked employers\s*", line, re.I)
                      for line in lines)
    active, names = not has_section, []
    for line in lines:
        stripped = line.strip()
        heading = re.match(r"#{1,6}\s*(.+)", stripped)
        if heading:
            if has_section:
                active = heading.group(1).strip().casefold() == "blocked employers"
            continue
        if not active or not stripped or stripped.startswith(("#", "//")):
            continue
        bold = re.findall(r"\*\*([^*]+)\*\*", stripped)
        candidates = bold or [re.sub(r"^(?:[-*+]\s+|\d+[.)]\s+)", "", stripped).split(" #", 1)[0].strip()]
        for name in candidates:
            normalized = answer_resolver.normalize_employer(name)
            if normalized:
                names.append(normalized)
    return sorted(set(names))


def employer_blocklisted(company, workspace, *, path=None):
    """Canonical static policy check. Unreadable policy is an unknown gate."""
    if not isinstance(company, str) or not company.strip():
        raise ValueError("employer identity required for blocklist check")
    names = blocked_employers(path or Path(workspace) / "data/employer-blocklist.md")
    return answer_resolver.normalize_employer(company) in names


def _context(entry):
    return {"role_id": entry.get("role_id"), "company": entry.get("company"),
            "title": entry.get("title"),
            "ats_url": entry.get("ats_url") or entry.get("application_url"),
            "materials": entry.get("materials"),
            "resume_version": entry.get("resume_version")}


def entry_admission(entry, origin="standard", *, require_ready=True, now=None, workspace=None):
    """Cheap static checks; an allowed result still requires packet/live checks.

    Missing legacy posting observations are delegated to the existing live
    check. A present malformed, stale, ambiguous, or failed observation cannot
    be ignored in favor of a READY label. Historical notes are not questions.
    Durable office exclusions require an explicit upstream clear; no text or
    guessed office-days fact clears them here.
    """
    now = now or datetime.now(timezone.utc)
    reasons, codes = [], []

    def deny(code, reason):
        codes.append(code); reasons.append(reason)

    if not isinstance(entry, dict):
        return {"allowed": False, "reasons": ["invalid queue entry"],
                "reason_codes": ["invalid_entry"]}
    if require_ready and entry.get("status") not in READY_STATES:
        deny("not_ready", "not READY")
    rid = entry.get("role_id")
    if not isinstance(rid, str) or not rid.strip():
        deny("missing_role_id", "role_id required")
    score = entry.get("fit_score")
    if (isinstance(score, bool) or not isinstance(score, (int, float))
            or not math.isfinite(score)):
        deny("unknown_fit", "finite fit_score required")
    elif score < main_floor():
        deny("below_fit_floor", f"fit_score below {main_floor()}")
    band = entry.get("action_band")
    band_ok = (band in (None, "APPLY") or
               isinstance(band, str) and band.startswith("STRATEGIC")) if origin == "strategic" else band == "APPLY"
    if not band_ok:
        deny("not_apply_band", f"not APPLY band ({band})")
    if workspace is not None:
        try:
            if employer_blocklisted(entry.get("company"), workspace):
                deny("blocklisted_employer", "blocklisted employer")
        except (OSError, ValueError, TypeError):
            deny("blocklist_unconfirmed", "employer blocklist is unconfirmed")
    for field in HOLD_FIELDS:
        # Nonempty malformed flag/hold values fail closed, rather than being
        # coerced into an affirmative clearance.
        if entry.get(field):
            deny("explicit_hold:" + field, "explicit hold: " + field)
    for field in QUESTION_FIELDS:
        questions = entry.get(field)
        if questions not in (None, [], ""):
            deny("unanswered_questions:" + field, "unanswered questions: " + field)
    gates = entry.get("gates")
    if gates is not None:
        if not isinstance(gates, dict):
            deny("invalid_gates", "gate state is malformed")
        else:
            for field in HOLD_FIELDS:
                if gates.get(field):
                    deny("explicit_gate:" + field, "explicit gate: " + field)
    if "posting_verification" in entry or "verification_attempt" in entry:
        # Import lazily: pipeline_service can use this module for diagnostics
        # without introducing an import cycle or any network action.
        try:
            from pipeline_service import posting_is_current
            if not posting_is_current(entry, now=now):
                deny("posting_not_current", "fresh exact posting verification required")
        except (ImportError, ValueError, TypeError, KeyError):
            deny("posting_verification_unconfirmed", "posting verification is unconfirmed")
    return {"allowed": not reasons, "reasons": reasons, "reason_codes": codes}


def scoped_answers(entry, bank):
    """Values for this employer/role and a value-free authority manifest."""
    if not isinstance(bank, dict) or not isinstance(bank.get("answers", {}), dict):
        raise ValueError("answer bank must contain an answers object")
    values, resolved, abstained = {}, [], []
    # Modern value-bound applicant receipts can establish authority for plain
    # scalar values. Embedded scoped values still go through the resolver;
    # a receipt must never broaden their employer/role scope.
    import packet_contract
    confirmed, _problems = packet_contract.confirmed_answers(
        {**bank, "answers": bank.get("answers", {})}, role_id=entry.get("role_id"),
        employer=entry.get("company"), role_context=entry)
    for key, raw in sorted(bank.get("answers", {}).items()):
        if not isinstance(key, str):
            raise ValueError("answer keys must be strings")
        candidate = raw
        if not isinstance(raw, dict) and key in confirmed:
            receipt = bank["_provenance"][key]
            candidate = {"value": confirmed[key], "scope": "global",
                         "source": receipt.get("source"),
                         "expires_at": receipt.get("expires_at")}
        result = answer_resolver.resolve(key, candidate, employer=entry.get("company"),
                                         role_context=entry,
                                         registry={entry.get("company")} if entry.get("company") else set())
        version = digest({"entry": raw, "receipt": (bank.get("_provenance") or {}).get(key)
                          if isinstance(bank.get("_provenance"), dict) else None})
        if result.status == answer_resolver.STATUS_RESOLVED:
            values[key] = result.value
            resolved.append({"key": key, "scope": result.scope,
                             "answer_version_sha256": version,
                             "source_sha256": digest(result.source),
                             "expiry": result.expiry})
        else:
            # Do not publish the rejected value, including legacy values.
            abstained.append({"key": key, "status": result.status})
    authority = {"resolved_keys": resolved, "abstained_keys": abstained,
                 "rules_sha256": digest({"banded_questions": bank.get("banded_questions", {}),
                                         "gates": bank.get("gates", {})})}
    return values, authority


def _attachment_versions(packet, workspace):
    uploads = packet.get("upload_files")
    if not isinstance(uploads, list) or not uploads:
        raise ValueError("packet requires prepared materials")
    if len(uploads) > 2:
        raise ValueError("unexpected material count")
    versions = []
    for value in uploads:
        path = contained_path(workspace, value)
        versions.append({"path_sha256": digest(str(path)), **file_digest(path)})
    return versions


def seal_packet(packet, entry, bank, *, now=None, workspace=None):
    """Bind a newly built legacy packet to current context and scoped answers."""
    _values, authority = scoped_answers(entry, bank)
    packet["ready_manifest"] = {"schema_version": 1,
        "created_at": (now or datetime.now(timezone.utc)).isoformat(),
        "context_sha256": digest(_context(entry)),
        "material_versions": _attachment_versions(packet, workspace or HOME), **authority}
    packet["launch_integrity_sha256"] = digest(
        {k: v for k, v in packet.items() if k != "launch_integrity_sha256"})
    return packet


def packet_admission(packet, entry, bank, *, now=None, workspace=None, for_execution=True):
    """Validate artifact integrity and, by default, require execution authority.

    Preparation consumers explicitly pass for_execution=False. Neither mode
    supplies authority or proves a runtime owner's live task/approval checks.
    An absent execution flag never grants execution permission.
    """
    reasons, codes = [], []

    def deny(code, reason):
        codes.append(code); reasons.append(reason)

    try:
        if not isinstance(packet, dict):
            raise ValueError("packet must be an object")
        if for_execution and (packet.get("scope") == "preparation_only"
                              or packet.get("execution_authorized") is not True):
            deny("review_only_packet", "packet requires review; no execution authorization")
        for key in ("role_id", "company", "title", "brief", "ats_url"):
            if not isinstance(packet.get(key), str) or not packet[key].strip():
                deny("missing_packet_field:" + key, "packet missing field: " + key)
        if any(packet.get(key) != _context(entry).get(key)
               for key in ("role_id", "company", "title", "ats_url")):
            deny("packet_context_mismatch", "packet target differs from current queue context")
        manifest = packet.get("ready_manifest")
        if not isinstance(manifest, dict) or type(manifest.get("schema_version")) is not int or manifest.get("schema_version") != 1:
            deny("missing_manifest", "legacy packet has no scoped manifest; rebuild required")
        else:
            if not fresh(manifest.get("created_at"), PACKET_MAX_AGE_SECONDS, now=now):
                deny("expired_packet", "packet manifest expired or future dated")
            if manifest.get("context_sha256") != digest(_context(entry)):
                deny("packet_context_mismatch", "packet context changed; rebuild required")
            if manifest.get("material_versions") != _attachment_versions(packet, workspace or HOME):
                deny("materials_changed", "packet material bytes changed; rebuild required")
            _values, authority = scoped_answers(entry, bank)
            if any(manifest.get(key) != value for key, value in authority.items()):
                deny("answer_authority_changed", "answer scope, value, authority or rules changed; rebuild required")
            expected = digest({k: v for k, v in packet.items() if k != "launch_integrity_sha256"})
            if packet.get("launch_integrity_sha256") != expected:
                deny("packet_integrity_mismatch", "packet digest mismatch")
    except (ValueError, TypeError, KeyError, OverflowError, OSError):
        deny("packet_integrity_unconfirmed", "packet integrity unconfirmed")
    return {"allowed": not reasons, "reasons": reasons, "reason_codes": codes}
