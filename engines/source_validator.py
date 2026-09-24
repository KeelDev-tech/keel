"""Forward-rule validator for submission `source` attribution.

J-20260917-2027-sour-1626 (source-attribution backfill + forward rule).

Rule: every NEW submission record must carry a non-empty `source` (a
discovery source) or an explicit `source_unknown` with a reason string.
On ambiguity the validator FAILS CLOSED: the record is flagged
(source="source_unknown" + source_unknown_reason), never silently
defaulted — and the submission itself is NEVER blocked. This function is
pure (no I/O) and never raises: a defensive fallback inside guarantees
a flagged stamp even on internal error, so it is safe to call on the
hot submission path.

A blank/unknown source is an attribution fact, never a guess: the
flagged record routes to the periodic source-backfill pass, which may
later link it to a documented discovery record or leave it
source_unknown. Writers must NOT substitute a transport or apply-route
value ("browser_task", "Indeed Apply", ...) for a discovery source —
exact transport tokens are flagged, not accepted.
"""
import re

SOURCE_UNKNOWN = "source_unknown"

# Exact transport values observed in the ledger that are NOT discovery
# sources. A new submission stamped with one of these is flagged with
# the reason naming the token — the ledger keeps the fact that the
# attribution was attempted-and-flagged, never a silent guess.
# (Re-measured 2026-09-17: 17/192 submitted rows carried transport or
# transport-variant values: browser_task x9, browser-task x3,
# live-browser (...) x5.)
TRANSPORT_TOKENS = frozenset({"browser_task", "browser-task"})

_REASON_NO_SOURCE = "queue_entry_carried_no_source"
_REASON_NO_REASON = "source_unknown_stamped_without_reason"
_REASON_TRANSPORT = "transport_value_is_not_a_discovery_source"
_REASON_INTERNAL = "validator_internal_error_never_block"


def _nonempty_str(v):
    return v if isinstance(v, str) and v.strip() else None


def is_transport_token(value):
    """True when value is an exact known transport token, or a
    "live-browser (...)" transport annotation (the 5 historical variants
    all share that prefix). Exact/prefix only — no fuzzy matching, so a
    genuine discovery source can never be misflagged by substring."""
    v = _nonempty_str(value)
    if v is None:
        return False
    if v in TRANSPORT_TOKENS:
        return True
    return v.startswith("live-browser")


def stamp_source(raw_source, existing_reason=None, role_id=""):
    """Stamp a submission's source field. Returns a dict:

        {"source": str, "source_unknown_reason": str|None, "flagged": bool}

    - missing/blank/non-string raw_source -> flagged source_unknown
      (reason queue_entry_carried_no_source)
    - raw_source == "source_unknown" WITH an explicit non-empty reason ->
      passed through, not flagged
    - raw_source == "source_unknown" WITHOUT a reason -> flagged
      (reason source_unknown_stamped_without_reason)
    - raw_source is a known transport token -> flagged source_unknown
      (reason transport_value_is_not_a_discovery_source:<token>)
    - any other non-empty string -> accepted as-is (stripped), not flagged
      (the validator does not guess: junk-classification of novel values
      belongs to the periodic backfill audit, not the submission path)
    - never raises: any internal error degrades to a flagged
      source_unknown (reason validator_internal_error_never_block) so the
      submission record is never blocked by the validator itself.
    """
    try:
        v = _nonempty_str(raw_source)
        if v is None:
            return {"source": SOURCE_UNKNOWN,
                    "source_unknown_reason": _REASON_NO_SOURCE,
                    "flagged": True}
        v = v.strip()
        if v == SOURCE_UNKNOWN:
            reason = _nonempty_str(existing_reason)
            if reason is not None:
                return {"source": SOURCE_UNKNOWN,
                        "source_unknown_reason": reason,
                        "flagged": False}
            return {"source": SOURCE_UNKNOWN,
                    "source_unknown_reason": _REASON_NO_REASON,
                    "flagged": True}
        if is_transport_token(v):
            return {"source": SOURCE_UNKNOWN,
                    "source_unknown_reason":
                        f"{_REASON_TRANSPORT}:{v}",
                    "flagged": True}
        return {"source": v, "source_unknown_reason": None, "flagged": False}
    except Exception:
        return {"source": SOURCE_UNKNOWN,
                "source_unknown_reason": _REASON_INTERNAL,
                "flagged": True}


def row_fields(raw_source, existing_reason=None, role_id=""):
    """Ledger-row field fragment from stamp_source: always includes
    "source"; includes "source_unknown_reason" whenever one is carried
    (flagged stamps always carry one; a passed-through source_unknown
    keeps its explicit reason)."""
    stamp = stamp_source(raw_source, existing_reason, role_id)
    fields = {"source": stamp["source"]}
    if stamp["source_unknown_reason"] is not None:
        fields["source_unknown_reason"] = stamp["source_unknown_reason"]
    return fields
