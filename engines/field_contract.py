#!/usr/bin/env python3
"""Producer/consumer field contract: parking writers <-> is_verify_only classifier.

The classifier (genuine_pat.is_verify_only) reads EXACTLY these queue-entry
fields: unresolved, status_reason, gate_note, queue_notes. Two production
incidents prove the failure class this contract pins down:

- 2026-09-14 park_reasons: blocker text written to a custom-only field the
  classifier never reads -> leads stuck parked forever, invisible to verify
  triage.
- 2026-09-14 stale status_reason: a genuinely input-blocked lead kept a
  stale "posting confirmed live... promoted READY" status_reason, which
  matches VERIFY_PAT -> verify_retry resurrected it to READY.

This module is the executable version of that contract (previously prose
only, in AGENTS.md and prescreen.park_lead's docstring):

- CLASSIFIER_READ_FIELDS: the registry. test_field_contract.py proves it
  stays in sync with genuine_pat.is_verify_only's body via single-field
  flip tests — edit the classifier's field reads without updating this
  tuple and the test fails.
- CLASSIFIER_BLIND_FIELDS: fields observed in the wild that the classifier
  does NOT read. Documentation for the audit; the contract test asserts
  none of them flip the classifier.
- classifier_visible_text(entry): the exact text the classifier scans
  (field reads only, no patterns). Mirrors is_verify_only's field reads
  byte-for-byte, minus the pattern strips (CLEARED_HISTORY_PAT /
  RESOLVED_STATE_PAT / TRENT_NOTE_PAT / FP-5 residue): those only REMOVE
  stale residue, so the contract is about field PLACEMENT, not pattern
  calibration.
- blockers_visible(entry, reasons): tripwire predicate. prescreen.park_lead
  calls it after the C-19 write verification; a park whose blocker text is
  not classifier-visible annotates queue_notes and emits a
  field_contract_violation telemetry event (non-fatal: the park itself
  still lands).
- audit_entries(entries): read-only scan for entries whose
  blocker-indicating text lives ONLY in blind fields.

CLI: field_contract.py audit  -> read-only scan of the three queue files.
"""
import json
import os
import re
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
from genuine_pat import _field_text, GENUINE_PAT  # noqa: E402

CLASSIFIER_READ_FIELDS = ("unresolved", "status_reason", "gate_note",
                          "queue_notes")

CLASSIFIER_BLIND_FIELDS = ("park_reasons", "last_activity", "fit",
                           "fit_score", "action_band", "ats_url",
                           "resume_lane", "status_updated",
                           "status_updated_park_ref",
                           "never_auto_submit_attestation")

_WORD_RE = re.compile(r"[a-z0-9]+")


def classifier_visible_text(entry):
    """The exact text genuine_pat.is_verify_only scans (field reads only)."""
    entry = entry or {}
    return (_field_text(entry.get("unresolved"), sep=" ") + " "
            + _field_text(entry.get("status_reason")) + " "
            + _field_text(entry.get("gate_note")) + " "
            + _field_text(entry.get("queue_notes")))


def _norm(s):
    return re.sub(r"\s+", " ", str(s or "")).strip().lower()


def _significant_tokens(s):
    return {t for t in _WORD_RE.findall(_norm(s)) if len(t) >= 4}


def blockers_visible(entry, reasons):
    """True iff every reason's blocker text is classifier-visible.

    Verbatim containment first, then significant-token cover (a paraphrased
    writer still counts as visible). Empty reasons are vacuously visible.
    """
    visible = _norm(classifier_visible_text(entry))
    vis_tokens = _significant_tokens(visible)
    for r in reasons or []:
        rn = _norm(r)
        if not rn:
            continue
        if rn in visible:
            continue
        toks = _significant_tokens(rn)
        if toks and toks <= vis_tokens:
            continue
        return False
    return True


def audit_entries(entries):
    """Read-only: (role_id, blind_fields_hit) for entries whose genuine
    blocker-indicating text lives ONLY in classifier-blind fields."""
    out = []
    for e in entries or []:
        if GENUINE_PAT.search(classifier_visible_text(e)):
            continue  # the classifier already sees a genuine blocker
        blind_hits = [f for f in CLASSIFIER_BLIND_FIELDS
                      if GENUINE_PAT.search(_field_text(e.get(f)))]
        if blind_hits:
            out.append((e.get("role_id"), blind_hits))
    return out


def _load_queue(path):
    try:
        with open(path) as f:
            data = json.load(f)
    except Exception as ex:
        print(f"audit: cannot read {path}: {ex}", file=sys.stderr)
        return []
    if isinstance(data, dict):
        for v in data.values():
            if isinstance(v, list):
                return v
        return []
    return data if isinstance(data, list) else []


def main_audit():
    from keel_paths import DATA  # noqa: E402 — repo path convention
    qdir = os.path.join(DATA, "queues")
    total, flagged = 0, []
    for name in ("standard-queue.json", "needs_input-queue.json",
                 "strategic-queue.json"):
        entries = _load_queue(os.path.join(qdir, name))
        total += len(entries)
        for role_id, blind in audit_entries(entries):
            flagged.append((name, role_id, blind))
    print(f"field_contract audit: scanned={total} "
          f"invisible_blocker_entries={len(flagged)}")
    for name, role_id, blind in flagged:
        print(f"  {name} {role_id}: blocker text only in blind fields "
              f"{blind}")
    return 0 if not flagged else 2


if __name__ == "__main__":
    if len(sys.argv) == 2 and sys.argv[1] == "audit":
        sys.exit(main_audit())
    sys.exit("usage: field_contract.py audit")
