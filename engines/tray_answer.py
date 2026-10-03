#!/usr/bin/env python3
"""Apply one tray answer: bank it, retro-resolve every matching blocker.

The closing half of the draft-first HITL loop (LangGraph / OpenAI Agents SDK
approve-edit-reject, productized): the tray digest shows each card with an
optional bank draft; the applicant replies APPROVE / edits / answers fresh; this CLI
applies that decision:

  - one answer retro-clears EVERY lead on the card (LazyApply-style
    compounding: answer once, unblock N -- including leads parked since),
  - the answer is banked to answer_bank.json with provenance "the applicant's own
    words, <date> (tray reply)" so future applications never ask again,
  - resolved leads revive through the CANONICAL input_resolution +
    verify_retry path: fully-unblocked leads trigger an immediate targeted
    verify_retry --live --role-ids run (fail-safe; hourly cadence is the
    fallback) -- nothing is force-promoted here.

Usage:
  tray_answer.py --key <card-key> --answer "..."            # dry run (default)
  tray_answer.py --live --key <card-key> --answer "..."
  tray_answer.py --live --family travel_commitment/general --answer "..."
  tray_answer.py --live --key <k> --answer "..." --bank-new willing_overtime
  tray_answer.py --live --key <k> --answer "..." --bank-key existing_key
  tray_answer.py --qresolve-request request.json         # read-only validation
  tray_answer.py --live --qresolve-request request.json  # authorized bank reuse
  tray_answer.py --approve-qresolve <decision-id>        # read-only draft check
  tray_answer.py --live --approve-qresolve <decision-id> # approve exact saved draft
  tray_answer.py --live --key <k> --answer "..." --bank-new sms_x \
      --scope employer:Acme          # scoped (non-global) bank write

Banking is OPT-IN (--bank-new / --bank-key). Without it, blockers resolve on
the matched leads but no standing answer is created -- the human (relaying
the applicant) decides what becomes permanent. Never invents: the answer text comes
from the applicant's reply verbatim.

The separate --qresolve-request mode accepts only a current evidence-bound
FACT decision from qresolve.py. It never writes the answer bank or invents new
human provenance. It journals intent before changing queues, keeps posting
liveness pending, and records leads awaiting canonical preparation and
admission. It does not launch network verification or submissions.

The --approve-qresolve mode explicitly approves a current persisted FACT or
JUDGMENT draft for exactly its existing targets. Original bank provenance stays
unchanged; a separate approval receipt records the decision. Protected human-only
questions and structural routes are refused by this mode.

SAFETY (mirrors input_resolution/apply.py):
  - Backup of both queue files + answer bank BEFORE any write (--live only).
  - queue_io.queue_lock() held for the read-modify-write.
  - Crash-safe writes (tmp + os.replace).
  - Fail closed: fully-unblocked claims are verified with the REAL
    is_verify_only(); a no-match key changes nothing.
  - --dry-run (default) prints the planned mutation and writes nothing.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

ENGINES = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ENGINES)

import input_tray_digest as tray
from genuine_pat import is_verify_only
import queue_io
import safe_io
import bank_scope as bs
from qresolve_corpus import _read as _bounded_read, _open_parent
from qresolve_policy import fit_admission, identity_digest

PDT = ZoneInfo("America/Los_Angeles")
STAMP = datetime.now(PDT).strftime("%Y-%m-%d %H:%M PDT")

from keel_paths import HOME, DATA  # noqa: E402 — repo path convention
STD_Q = os.path.join(DATA, "queues", "standard-queue.json")
NI_Q = os.path.join(DATA, "queues", "needs_input-queue.json")
DEFAULT_BANK = os.path.join(DATA, "answer_bank.json")
ANSWERS_LOG = os.path.join(HOME, "hidden_files", "tray-answers.jsonl")


def load_list(path):
    q = queue_io.strict_loads(_bounded_read(path, 16 * 1024 * 1024)[0])
    return q if isinstance(q, list) else []


def save_list(path, rows):
    queue_io.atomic_write_json(path, rows)


def qn_with_notes(rec, notes):
    qn = rec.get("queue_notes")
    if isinstance(qn, list):
        return qn + notes
    return ((qn or "") + " | " + " | ".join(notes)).strip(" |")


def card_matches(text, key=None, family=None):
    fam = tray.family_of(text)
    norm = tray.normalize_question(text)
    if key:
        return tray.card_key_for(fam, norm) == key
    if family:
        return fam == family
    return False


def find_targets(key=None, family=None):
    """(card, [(entry, [matched raw blockers])]) for current queue state."""
    cards = tray.collect_cards()
    card = None
    if key:
        card = cards.get(key)
    elif family:
        cands = [c for c in cards.values() if c["family"] == family]
        card = cands[0] if len(cands) == 1 else None
        if len(cands) > 1:
            return None, [], f"family {family} matches {len(cands)} cards; use --key"
    if card is None:
        avail = sorted(
            ((c["family"] or "no-family", c["key"], c["unblock_leads"])
             for c in cards.values()),
        )
        hint = "\n".join(f"  {k}  [{f}] ({n} leads)" for f, k, n in avail[:15])
        return None, [], f"no tray card matches; available:\n{hint}"
    # Only current card members can be targeted. A matching prompt on a
    # below-floor or newly appearing sibling does not expand authorization.
    current_ids = {lead["role_id"] for lead in card["leads"]}
    targets = []
    for fname, path in (("needs_input", NI_Q), ("standard", STD_Q)):
        for e in load_list(path):
            if "NEEDS-INPUT" not in str(e.get("status", "")):
                continue
            if e.get("role_id") not in current_ids or not fit_admission(e)["eligible"]:
                continue
            matched = [t for _, t in tray.genuine_blockers(e)[1]
                       if card_matches(t, key=card["key"])]
            if matched:
                targets.append((fname, e, matched))
    return card, targets, ""


def resolve_unresolved(unresolved, card_key, matched_texts):
    """Split an `unresolved` list into (removed, kept) for a card answer.

    Reviewed aliases match the card identity. A merged prompt may clear only
    the exact raw fragments that generated its current matched text. Substring
    overlap is never authority to clear an additional qualified obligation.
    """
    mtexts = {str(m) for m in matched_texts}
    fragments = [u for text, raw in tray.blocker_groups({"unresolved": unresolved})
                 if text in mtexts and card_matches(text, key=card_key)
                 for u in raw]

    def _belongs(u):
        us = str(u)
        if card_matches(us, key=card_key):
            return True
        return any(u == fragment for fragment in fragments)

    removed, kept = [], []
    for u in (unresolved or []):
        (removed if _belongs(u) else kept).append(u)
    return removed, kept


def apply_bank_write(bank_path, banked_key, answer, scope_arg, card,
                     stamp, employers=None, question_variants=None):
    """Write one banked answer with governed scope (ADOPTION 1 of 5).

    Scope decision (bank_scope.decide_bank_scope): explicit --scope always
    wins; a consent/attestation-family card or an employer-named question
    without explicit scope infers AMBIGUOUS and is written to
    ``_quarantined`` (same shape as existing _quarantined entries) instead
    of ``answers`` -- a per-employer consent can NEVER land in global
    answers without the applicant's deliberate --scope.

    Returns (banked: bool, quarantined: bool, scope: str).

    Fail-safe: any scope-check failure quarantines the entry and never
    raises -- banking must never break unblock routing.
    """
    from answer_resolver import load_bank
    from packet_contract import answer_receipt
    import copy
    banked = False
    quarantined = True
    scope = bs.SCOPE_AMBIGUOUS
    # Never replace a corrupt bank while trying to quarantine a new capture.
    try:
        bank = load_bank(bank_path)
        if any(not isinstance(bank.get(key, {}), dict)
               for key in ("answers", "_provenance", "_quarantined", "_meta")):
            raise ValueError("malformed answer bank")
    except (OSError, ValueError):
        return False, True, scope
    original = copy.deepcopy(bank)
    try:
        scope, qreason = bs.decide_bank_scope(scope_arg, card, employers)
        quarantined = (scope == bs.SCOPE_AMBIGUOUS)
        prov = bank.setdefault("_provenance", {})
        prov[banked_key] = {
            "source": (f"the applicant's own words, {stamp} "
                       f"(tray reply; card {card['key']})"),
            "added": datetime.now(PDT).strftime("%Y-%m-%d"),
            "via": "tray_answer",
            "scope": scope,
        }
        if quarantined:
            bank.setdefault("_quarantined", {})[banked_key] = {
                "was": answer,
                "quarantined_at": datetime.now(timezone.utc).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"),
                "reason": (qreason or "ambiguous scope; held out of answers "
                           "pending the applicant's explicit scoped authorization"),
            }
        else:
            bank.setdefault("answers", {})[banked_key] = {
                "value": answer, "scope": scope,
                # 2026-09-17 tray-answer compounding: the exact question he
                # answered, so the prescreen can consume this answer via
                # _scoped_question_match instead of re-parking the lead on
                # the same question.
                "question": card.get("question") or card.get("norm") or "",
                "card_key": card.get("key") or "",
                # Alternate wrappings of the same question seen across the
                # matched leads (blind-pattern prefixes etc.) so the prescreen
                # keeps consuming this answer as harvest wording drifts.
                "question_variants": sorted({
                    str(m)[:300] for m in (question_variants or [])
                    if str(m).strip()
                })[:5],
            }
            # Bind the entire scoped entry, including question metadata. Only
            # this fresh applicant capture receives authority, never old values.
            entry = bank["answers"][banked_key]
            entry["provenance"] = prov[banked_key]["source"]
            prov[banked_key] = {**prov[banked_key],
                **answer_receipt(entry, entry["provenance"]), "answer_scope": scope}
            banked = True
        meta = bank.setdefault("_meta", {})
        meta["last_updated"] = datetime.now(PDT).strftime("%Y-%m-%d")
        queue_io.atomic_write_json(bank_path, bank)
    except Exception as e:  # scope-check failure: fail to quarantine + continue
        bank = original
        banked = False
        # Failed replacement must not revoke the original answer receipt.
        bank.setdefault("_quarantined", {})[banked_key] = {
            "was": answer,
            "quarantined_at": datetime.now(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"),
            "reason": (f"scope-check failed ({type(e).__name__}); "
                       "fail-closed to quarantine"),
        }
        try:
            queue_io.atomic_write_json(bank_path, bank)
        except Exception:
            pass
        quarantined, scope = True, bs.SCOPE_AMBIGUOUS
    return banked, quarantined, scope


def _structural_guard(card, targets):
    """A factual or human answer cannot repair an operational route blocker."""
    from qresolve_semantics import classify
    result = classify(card, contexts=[entry for _, entry, _ in targets])
    return result["class"] == "STRUCTURAL"


def _durable_backup(bank_path):
    """Create a unique fsynced pre-write snapshot while the queue lock is held."""
    parent = os.path.dirname(os.path.abspath(NI_Q))
    backup = tempfile.mkdtemp(prefix="_backup-tray-answer-", dir=parent)
    for path in (STD_Q, NI_Q, bank_path):
        if os.path.exists(path):
            value = queue_io.strict_loads(_bounded_read(path, 16 * 1024 * 1024)[0])
            queue_io.atomic_write_json(os.path.join(backup, os.path.basename(path)), value)
    queue_io._dir_fsync(backup)
    queue_io._dir_fsync(parent)
    return backup


def _qresolve_paths():
    hidden = os.path.dirname(ANSWERS_LOG)
    return (os.path.join(hidden, "qresolve-resolutions.jsonl"),
            os.path.join(hidden, "qresolve-resolved.json"))


def _qresolve_digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def _qresolve_journal(path):
    """Refuse damaged audit state instead of guessing after a partial write."""
    try:
        raw = _bounded_read(path, 16 * 1024 * 1024)[0]
    except FileNotFoundError:
        return []
    if raw and not raw.endswith("\n"):
        raise ValueError("incomplete_audit_record")
    rows = []
    for line in raw.splitlines():
        row = queue_io.strict_loads(line)
        if (not isinstance(row, dict) or row.get("action") not in {
                "INTENT", "auto_applied", "human_applied", "held",
                "recovered_applied", "cancelled_unwritten"}
                or not isinstance(row.get("decision_id"), str)):
            raise ValueError("invalid_audit_record")
        rows.append(row)
    return rows


def _qresolve_append(path, record):
    """Append one durable regular-file event through a pinned parent descriptor."""
    payload = (json.dumps(record, sort_keys=True, ensure_ascii=False,
                          allow_nan=False) + "\n").encode("utf-8")
    if len(payload) > 2 * 1024 * 1024:
        raise ValueError("audit_event_too_large")
    parent, name = _open_parent(path)
    fd = None
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK
        fd = os.open(name, flags, 0o600, dir_fd=parent)
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or info.st_size + len(payload) > 16 * 1024 * 1024):
            raise ValueError("unsafe_or_oversized_audit")
        remaining = memoryview(payload)
        while remaining:
            count = os.write(fd, remaining)
            if count <= 0:
                raise OSError("incomplete_audit_write")
            remaining = remaining[count:]
        os.fsync(fd)
        os.fsync(parent)
    finally:
        if fd is not None:
            os.close(fd)
        os.close(parent)


def _qresolve_receipt(status, decision_id=None, reason=None, **fields):
    value = {"schema": "keel.qresolve.apply.v1", "status": status,
             "decision_id": decision_id, "removed_blockers": 0,
             "changed_leads": 0, "fully_unblocked": 0, "role_ids": [],
             "ready_verified": False}
    if reason:
        value["reason"] = reason
    value.update(fields)
    print(json.dumps(value, sort_keys=True, allow_nan=False))
    return 0 if status in {"APPLIED", "NO_CHANGE"} else 3


def _qresolve_queues():
    queue_io._refuse_pending_transactions()
    queues = {}
    identities = set()
    # Identity is canonical across all four queues even though this actuator
    # may mutate only needs_input and standard. Missing queues cannot be
    # treated as empty evidence of uniqueness.
    qdir = Path(NI_Q).parent
    for path in (NI_Q, STD_Q, str(qdir / "strategic-queue.json"),
                 str(qdir / "rejected-queue.json")):
        rows = queue_io.strict_loads(_bounded_read(path, 16 * 1024 * 1024)[0])
        if not isinstance(rows, list):
            raise ValueError("invalid_queue_shape")
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError("invalid_queue_row")
            identity = row.get("role_id")
            if not isinstance(identity, str) or not identity.strip() or identity in identities:
                raise ValueError("missing_or_duplicate_role_id")
            identities.add(identity)
        if path in (NI_Q, STD_Q):
            queues[path] = rows
    queue_io._refuse_pending_transactions()
    return queues


def _qresolve_changes(card, targets, queues, decision, *, approval=None):
    """Prepare an exact, reversible mutation without claiming posting liveness."""
    expected = json.loads(json.dumps(queues))
    counts = {"removed_blockers": 0, "changed_leads": 0,
              "fully_unblocked": 0, "role_ids": []}
    seen = set()
    for fname, entry, matched in targets:
        if not fit_admission(entry)["eligible"]:
            raise ValueError("below_floor_or_invalid_fit")
        path = NI_Q if fname == "needs_input" else STD_Q
        rid = entry.get("role_id")
        if rid in seen:
            raise ValueError("duplicate_target")
        seen.add(rid)
        matches = [row for row in queues[path] if row.get("role_id") == rid]
        if len(matches) != 1 or matches[0] != entry:
            raise ValueError("target_snapshot_changed")
        row = matches[0]
        if not fit_admission(row)["eligible"]:
            raise ValueError("below_floor_or_invalid_fit")
        unresolved = row.get("unresolved")
        if not isinstance(unresolved, list) or not all(isinstance(u, str) for u in unresolved):
            raise ValueError("invalid_blockers")
        removed, kept = resolve_unresolved(unresolved, card["key"], matched)
        if not removed:
            raise ValueError("no_exact_blocker_removed")
        row["unresolved"] = kept
        action_note = ("explicit approval of existing bank quotation" if approval
                       else "reuse of approved bank evidence")
        row["queue_notes"] = qn_with_notes(row, [
            f"qresolve: blocker cleared by {action_note}; "
            f"decision {decision['decision_id']}; source answer_bank[{decision['bank_key']}]"
        ])
        row["status_updated"] = datetime.now(timezone.utc).isoformat()
        if not kept:
            trial = dict(row)
            trial["status_reason"] = (
                f"qresolve: all input blockers cleared; decision {decision['decision_id']}; "
                "liveness unverified — awaiting canonical preparation and admission")
            gate = row.get("gate_note") or ""
            if not isinstance(gate, str):
                raise ValueError("invalid_gate_note")
            trial["gate_note"] = "\n".join(
                line for line in gate.splitlines() if not any(text in line for text in removed)
            ).strip() or "qresolve: no open input blockers; liveness unverified"
            if is_verify_only(trial):
                row["status_reason"] = trial["status_reason"]
                row["gate_note"] = trial["gate_note"]
                row["last_verify_attempt"] = None
                counts["fully_unblocked"] += 1
        counts["removed_blockers"] += len(removed)
        counts["changed_leads"] += 1
        counts["role_ids"].append(rid)
    if not counts["removed_blockers"] or set(counts["role_ids"]) != set(decision["target_role_ids"]):
        raise ValueError("target_accounting_mismatch")
    counts["role_ids"].sort()
    return expected, counts


def _qresolve_fit_hold(request, queues):
    """A requested target must remain eligible under current intake policy."""
    target_ids = request.get("decision", {}).get("target_role_ids", [])
    if not isinstance(target_ids, list) or not all(isinstance(rid, str) for rid in target_ids):
        raise ValueError("invalid_target_role_ids")
    for rows in queues.values():
        for row in rows:
            if row.get("role_id") in target_ids:
                gate = fit_admission(row)
                if not gate["eligible"]:
                    return gate["reason"]
    return None


def _qresolve_resolution_state(queues, target_ids):
    """Capture the exact post-resolution state bound to the durable intent."""
    state = {}
    for rows in queues.values():
        for row in rows:
            if row.get("role_id") not in target_ids:
                continue
            gate = fit_admission(row)
            state[row["role_id"]] = {
                "fit_score": gate["score"], "fit_eligible": gate["eligible"],
                "fit_floor": gate["floor"],
                "remaining_blockers": len(row.get("unresolved") or []),
                "fully_unblocked": not row.get("unresolved") and is_verify_only(row),
                "status": row.get("status"), "status_updated": row.get("status_updated"),
                "identity_sha256": identity_digest(row),
            }
    return state


def apply_qresolve(request_path, bank_path, *, live=False, approval_id=None):
    """Only sanctioned QRESOLVE queue actuator; offline and bank-read-only.

    INTENT precedes canonical writes. A process crash can leave multiple files at
    different stages; pending intent blocks subsequent application until explicit
    reconciliation. This is a crash journal, not a multi-file transaction.
    """
    decision_id = None
    try:
        root = Path(NI_Q).absolute().parents[2]
        if Path(bank_path).absolute() != root / "data/answer_bank.json":
            raise ValueError("qresolve_requires_canonical_bank")
        if approval_id is not None:
            if request_path is not None:
                raise ValueError("conflicting_request_modes")
            from qresolve import _root
            root = _root()
            if Path(bank_path).absolute() != root / "data/answer_bank.json":
                raise ValueError("approval_requires_canonical_bank")
            from qresolve_approval import load_proposal, validate_approval
            request = None
            validator = validate_approval
            decision = {"decision_id": approval_id}
        else:
            request = queue_io.strict_loads(_bounded_read(request_path, 2 * 1024 * 1024)[0])
            from qresolve import validate_application
            validator = validate_application
            if (not isinstance(request, dict)
                    or request.get("schema") != "keel.qresolve.request.v1"
                    or not isinstance(request.get("decision"), dict)):
                raise ValueError("invalid_request")
            decision = request["decision"]
        decision_id = decision.get("decision_id")
        if (not isinstance(decision_id, str) or len(decision_id) != 64
                or any(char not in "0123456789abcdef" for char in decision_id)):
            raise ValueError("invalid_decision_id")
        card_key = decision.get("card_key")
        if approval_id is None and (not isinstance(card_key, str) or not card_key):
            raise ValueError("invalid_card_key")
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        return _qresolve_receipt("HOLD", reason="invalid_request")

    journal, resolved_path = _qresolve_paths()
    # Dry runs do not acquire queue_lock(): lock diagnostics themselves write.
    if not live:
        try:
            if approval_id:
                from qresolve_recovery import journal_state
                pending, completed = journal_state(_qresolve_journal(journal))
                if pending:
                    return _qresolve_receipt("HOLD", decision_id, "incomplete_prior_intent")
                if decision_id in completed:
                    return _qresolve_receipt("NO_CHANGE", decision_id, "already_recorded")
                request = load_proposal(approval_id, bank_path)
                card_key = request["decision"]["card_key"]
            queues = _qresolve_queues()
            fit_hold = _qresolve_fit_hold(request, queues)
            if fit_hold:
                return _qresolve_receipt("HOLD", decision_id, fit_hold)
            card, targets, _ = find_targets(key=card_key)
            if card is None:
                raise ValueError("card_missing")
            validator(request, card, targets, bank_path)
            if _structural_guard(card, targets):
                raise ValueError("structural_blocker")
        except (OSError, ValueError, TypeError, KeyError, RuntimeError):
            return _qresolve_receipt("HOLD", decision_id, "revalidation_failed")
        return _qresolve_receipt("NO_CHANGE", decision_id, "dry_run",
                                 proposed_target_count=len(targets))

    try:
        from qresolve_recovery import preflight_locks
        preflight_locks(root)
        # Queue writers and confirm-answer use different canonical locks. Hold
        # both, in queue -> bank order, through evidence validation and commit.
        with queue_io.queue_lock(owner="tray_answer:qresolve", recover=False), safe_io.file_lock(str(bank_path) + ".lock"):
            try:
                records = _qresolve_journal(journal)
                from qresolve_recovery import journal_state
                pending, completed = journal_state(records)
                if pending:
                    return _qresolve_receipt("HOLD", decision_id, "incomplete_prior_intent")
                if decision_id in completed:
                    return _qresolve_receipt("NO_CHANGE", decision_id, "already_recorded")
                if approval_id:
                    request = load_proposal(approval_id, bank_path)
                    card_key = request["decision"]["card_key"]
                queues = _qresolve_queues()
                fit_hold = _qresolve_fit_hold(request, queues)
                if fit_hold:
                    return _qresolve_receipt("HOLD", decision_id, fit_hold)
                card, targets, _ = find_targets(key=card_key)
                if card is None:
                    raise ValueError("card_missing")
                decision = validator(request, card, targets, bank_path)
                authorized = ((decision.get("class") in {"FACT", "JUDGMENT"}
                               and decision.get("action") == "draft") if approval_id else
                              (decision.get("class") == "FACT"
                               and decision.get("action") == "auto_apply"))
                if (not authorized
                        or not decision.get("evidence") or not decision.get("bank_key")
                        or not isinstance(decision.get("answer"), str)
                        or _structural_guard(card, targets)):
                    raise ValueError("application_not_authorized")
                approval = None
                if approval_id:
                    from qresolve_approval import approval_receipt
                    approval = approval_receipt(decision)
                try:
                    resolved = queue_io.strict_loads(_bounded_read(resolved_path, 16 * 1024 * 1024)[0])
                except FileNotFoundError:
                    resolved = {}
                if not isinstance(resolved, dict):
                    raise ValueError("invalid_resolved_map")
                expected, counts = _qresolve_changes(card, targets, queues, decision,
                                                     approval=approval)
                target_state = _qresolve_resolution_state(queues, counts["role_ids"])
                # Keep the evidence and exact removal plan in the durable intent.
                backup = _durable_backup(bank_path)
                intent = {"ts": datetime.now(timezone.utc).isoformat(),
                          "action": "INTENT", **decision,
                          "planned_counts": counts,
                          "target_resolution_state": target_state,
                          "backup": os.path.basename(backup),
                          "queue_before_sha256": _qresolve_digest(expected),
                          "queue_after_sha256": _qresolve_digest(queues)}
                intent["action"] = "INTENT"
                if approval:
                    intent["approval"] = approval
                _qresolve_append(journal, intent)
                # No writes outside sanctioned queue/bank paths; bank is never changed.
                for path, rows in queues.items():
                    if rows != expected[path]:
                        queue_io.atomic_write_json(path, rows)
                actual = _qresolve_queues()
                if actual != queues:
                    raise ValueError("postwrite_queue_changed")
                if sum(len(row.get("unresolved") or []) for rows in expected.values() for row in rows) - sum(
                        len(row.get("unresolved") or []) for rows in actual.values() for row in rows
                ) != counts["removed_blockers"]:
                    raise ValueError("postwrite_accounting_mismatch")
                snapshots = {row["role_id"]: _qresolve_digest(row)
                             for rows in actual.values() for row in rows
                             if row["role_id"] in counts["role_ids"]}
                resolved[decision_id] = {
                    "decision_id": decision_id, "fingerprint": decision["fingerprint"],
                    "card_key": card_key, "answer": decision["answer"],
                    "bank_key": decision["bank_key"], "evidence": decision["evidence"],
                    "context_sha256": decision["context_sha256"],
                    "evidence_sha256": decision["evidence_sha256"],
                    "config_sha256": decision["config_sha256"],
                    "target_role_ids": counts["role_ids"],
                    "target_post_sha256": snapshots,
                    "target_resolution_state": target_state,
                    "ts": datetime.now(timezone.utc).isoformat(),
                }
                if approval:
                    resolved[decision_id]["approval"] = approval
                queue_io.atomic_write_json(resolved_path, resolved)
                revival = ("awaiting_canonical_preparation_admission" if counts["fully_unblocked"]
                           else "remaining_blockers")
                _qresolve_append(journal, {
                    **resolved[decision_id],
                    "action": "human_applied" if approval else "auto_applied", **counts,
                    "class": decision["class"], "confidence": decision["confidence"],
                    "revival": revival, "ready_verified": False,
                })
                return _qresolve_receipt("APPLIED", decision_id, **counts, revival=revival)
            except (OSError, ValueError, TypeError, KeyError, RuntimeError):
                # A failed append can leave an ambiguous final line. Never retry it
                # in this process or claim a completed application without receipt.
                try:
                    _qresolve_append(journal, {
                        "ts": datetime.now(timezone.utc).isoformat(),
                        "decision_id": decision_id, "card_key": card_key,
                        "action": "held", "reason": "revalidation_or_write_failed",
                    })
                except (OSError, ValueError, TypeError):
                    pass
                return _qresolve_receipt("HOLD", decision_id, "revalidation_or_write_failed")
    except (OSError, ValueError, RuntimeError):
        return _qresolve_receipt("HOLD", decision_id, "lock_unavailable")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Apply one tray answer.")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--key", help="tray card key from the digest / input-tray.json")
    g.add_argument("--family", help="decision family, e.g. travel_commitment/general")
    g.add_argument("--qresolve-request", metavar="PATH",
                   help="revalidate and reuse an authorized evidence-bound FACT")
    g.add_argument("--approve-qresolve", metavar="DECISION_ID",
                   help="approve an exact persisted FACT/JUDGMENT draft for its current targets")
    ap.add_argument("--answer", help="the applicant's verbatim answer")
    ap.add_argument("--live", action="store_true", help="apply (default: dry run)")
    ap.add_argument("--bank-new", metavar="KEY",
                    help="also bank as new answer_bank key KEY")
    ap.add_argument("--bank-key", metavar="KEY",
                    help="also overwrite existing answer_bank key KEY")
    ap.add_argument("--scope", metavar="SCOPE",
                    help="explicit bank scope: 'global', 'employer:<Name>', "
                         "or 'ambiguous'. Overrides scope inference; a "
                         "consent/attestation or employer-named answer is "
                         "quarantined (never banked globally) without this.")
    ap.add_argument("--bank", metavar="PATH", default=DEFAULT_BANK,
                    help="answer-bank JSON path (default: <keel-home>/data/answer_bank.json)")
    args = ap.parse_args(argv)
    if args.qresolve_request or args.approve_qresolve:
        if args.answer is not None or args.bank_new or args.bank_key or args.scope:
            ap.error("QRESOLVE application cannot supply an answer, bank write, or scope")
        return apply_qresolve(args.qresolve_request, args.bank, live=args.live,
                              approval_id=args.approve_qresolve)
    if args.answer is None:
        ap.error("manual --key/--family requires --answer")

    card, targets, err = find_targets(key=args.key, family=args.family)
    if card is None:
        print(f"NO-MATCH: {err}")
        return 2

    # Keel 0.4 P3 (2026-09-17): banking guard. A the applicant answer is applied to
    # an employer-prior blocker ONLY when the question is confirmed
    # (authoritative form intel). An intel_gap card is an UNCONFIRMED prior
    # — the form may not even ask the question — so resolving or banking
    # against it is refused, fail closed. The promotion path
    # (tray_sources.promote_intel_gap) produces the confirmed genuine_missing
    # card the applicant may then answer. (Blind-safe keys per BLIND_SAFE_BANKED_KEYS
    # are a prescreen-layer concern and never reach the tray as parks.)
    try:
        from tray_sources import classify_source
        if classify_source(card.get('question', ''), card.get('norm', '')) == "intel_gap":
            print("REFUSED: card is SYSTEM-BLOCKED (intel-gap) — the question "
                  "is unconfirmed (no authoritative form intel). the applicant cannot "
                  "answer a question the form may not ask; route: "
                  "authoritative form probe first, then promote. Fail closed.")
            return 3
    except SystemExit:
        raise
    except Exception:
        print("REFUSED: blocker-source classification failed — fail closed.")
        return 3

    if _structural_guard(card, targets):
        print("REFUSED: structural blocker requires its designated route")
        return 3
    total_blockers = sum(len(m) for _, _, m in targets)
    print(f"card: [{card['family']}] {card['question'][:100]}")
    print(f"key: {card['key']}")
    print(f"answer: \"{args.answer[:120]}\"")
    print(f"would resolve {total_blockers} blockers across {len(targets)} leads")
    if args.bank_new or args.bank_key:
        print(f"would bank as: {args.bank_new or args.bank_key}")
    if not args.live:
        print("dry run -- nothing written (pass --live to apply)")
        return 0

    # ---- live path ----
    # Targets and pre-write snapshots must belong to the same locked read.
    with queue_io.queue_lock(owner="tray_answer:apply"):
        card, targets, err = find_targets(key=args.key, family=args.family)
        if card is None:
            print(f"NO-MATCH: {err}")
            return 2
        if _structural_guard(card, targets):
            print("REFUSED: structural blocker requires its designated route")
            return 3
        total_blockers = sum(len(m) for _, _, m in targets)
        bkdir = _durable_backup(args.bank)
        print(f"backup: {bkdir}")
        queues = {NI_Q: load_list(NI_Q), STD_Q: load_list(STD_Q)}
        changed = {"needs_input": 0, "standard": 0}
        fully = 0
        fully_rids = []
        for fname, rows in (("needs_input", queues[NI_Q]), ("standard", queues[STD_Q])):
            by_rid = {r.get("role_id"): r for r in rows}
            for qfname, entry, matched in targets:
                if qfname != fname:
                    continue
                rec = by_rid.get(entry.get("role_id"))
                if rec is None:
                    continue
                removed, kept = resolve_unresolved(
                    rec.get("unresolved"), card["key"], matched)
                new_unresolved = kept
                notes = [
                    f"tray-answer {STAMP}: blocker cleared — "
                    f"\"{m[:70]}\" -> answered \"{args.answer[:60]}\" "
                    f"(the applicant's words, tray reply; key {card['key']})"
                    for m in removed
                ]
                rec["unresolved"] = new_unresolved
                rec["queue_notes"] = qn_with_notes(rec, notes)
                # 2026-09-17 (pulse 250 / ARM 8): stamp hygiene -- a
                # material mutation (blockers cleared) moves status_updated
                # at second resolution. The P2 guard keys on
                # status_updated_park_ref, not this stamp, so the bump is
                # truthfulness for age-based sweeps, not guard logic.
                rec["status_updated"] = datetime.now(timezone.utc).isoformat()
                if not new_unresolved:
                    # Fully unblocked: mirror input_resolution/apply.py --
                    # clean conventional fields, verify with the REAL
                    # classifier, fail closed.
                    trial = dict(rec)
                    trial["unresolved"] = []
                    trial["status_reason"] = (
                        f"tray-answer {STAMP}: all input blockers cleared "
                        f"via tray answer (key {card['key']}); liveness "
                        f"unverified — queued for verification")
                    gn = rec.get("gate_note") or ""
                    kept = [ln for ln in str(gn).splitlines()
                            if not any(m in ln for m in removed)]
                    trial["gate_note"] = "\n".join(kept).strip() or (
                        f"tray-answer {STAMP}: no open input blockers")
                    trial["queue_notes"] = rec["queue_notes"]
                    if is_verify_only(trial):
                        rec["status_reason"] = trial["status_reason"]
                        rec["gate_note"] = trial["gate_note"]
                        # Immediate-verify fast path (2026-09-17): clear the
                        # retry-cooldown stamp so the targeted verify run
                        # below (and the hourly cadence as fallback) picks
                        # this lead up without waiting out a stale cooldown.
                        rec["last_verify_attempt"] = None
                        fully += 1
                        if rec.get("role_id"):
                            fully_rids.append(rec["role_id"])
                    else:
                        rec["queue_notes"] = qn_with_notes(rec, [(
                            f"tray-answer {STAMP}: unresolved cleared but "
                            f"classifier still sees input blockers — left for review")])
                changed[fname] += 1
            save_list(NI_Q if fname == "needs_input" else STD_Q, rows)

        banked_key = None
        banked_scope = None
        banked_quarantined = False
        if args.bank_new or args.bank_key:
            banked_key = args.bank_new or args.bank_key
            # Matched raw blocker texts across the answered leads become
            # question variants, so the prescreen keeps recognizing this
            # question as harvest wording drifts.
            _variants = [m for _, _, ms in targets for m in ms]
            banked, banked_quarantined, banked_scope = apply_bank_write(
                args.bank, banked_key, args.answer, args.scope, card, STAMP,
                question_variants=_variants)
            if banked_quarantined:
                print(f"QUARANTINED: answer_bank._quarantined[{banked_key}] "
                      f"-- scope ambiguous, held out of answers")
            # ECV-T3 gate (2026-09-16, the applicant-approved): a global bank
            # promotion is a T3 decision — log an auto-filled packet.
            # Fail-open (never-halt): emit() catches everything internally
            # and the hook is guarded, so a packet failure can never break
            # banking or unblock routing. Fires only on real answers
            # writes (never on quarantine).
            if banked:
                try:
                    import ecv_packet_gate
                    ecv_packet_gate.emit(
                        "global-bank-promotion",
                        subject=banked_key,
                        decision_summary=(
                            f"answer_bank[{banked_key}] banked with scope "
                            f"'{banked_scope}' from "
                            f"the applicant's own words (tray card {card['key']}, family "
                            f"{card['family']})"),
                        component="tray_answer")
                except Exception:
                    pass

    # Immediate-verify fast path (2026-09-17): fully-unblocked leads
    # re-enter verify_retry NOW via a targeted --role-ids run instead of
    # waiting for the next hourly cadence. Fail-safe by design (never-halt):
    # any failure here is logged and swallowed — banking and unblocking
    # above already succeeded. A skipped_lock contender simply means the
    # hourly cadence is running; the cleared cooldown stamps guarantee it
    # picks these leads up.
    if fully_rids:
        try:
            import subprocess
            vr = os.path.join(ENGINES, "verify_retry.py")
            proc = subprocess.run(
                [sys.executable, vr, "--live", "--role-ids",
                 ",".join(sorted(set(fully_rids)))],
                capture_output=True, text=True, timeout=600, cwd=ENGINES)
            tail = (proc.stdout or "").strip().splitlines()[-3:]
            print(f"immediate verify: exit={proc.returncode} "
                  f"for {len(fully_rids)} lead(s)")
            for line in tail:
                print(f"  verify: {line[:160]}")
            if proc.returncode != 0:
                print(f"  verify stderr: {(proc.stderr or '').strip()[:300]}")
        except Exception as exc:
            print(f"immediate verify skipped ({type(exc).__name__}: {exc}); "
                  f"hourly cadence will pick up {len(fully_rids)} lead(s)")

    # append-only answer log (telemetry, never secrets)
    os.makedirs(os.path.dirname(ANSWERS_LOG), exist_ok=True)
    with open(ANSWERS_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps({
            "ts": STAMP, "card_key": card["key"], "family": card["family"],
            "leads": len(targets), "blockers": total_blockers,
            "fully_unblocked": fully,
            "banked_key": banked_key,
            "banked_scope": banked_scope,
            "banked_quarantined": banked_quarantined,
            "answer_chars": len(args.answer),
        }) + "\n")

    print(f"applied: {len(targets)} leads updated "
          f"({changed['needs_input']} needs_input, {changed['standard']} standard), "
          f"{fully} fully unblocked -> immediate targeted verify above "
          f"(hourly cadence as fallback)")
    if banked_key:
        if banked_quarantined:
            print(f"QUARANTINED: answer_bank._quarantined[{banked_key}] "
                  f"(scope ambiguous -- held out of answers)")
        else:
            print(f"banked: answer_bank[{banked_key}] (scope {banked_scope})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
