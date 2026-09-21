#!/usr/bin/env python3
"""Record application outcomes (Keel public edition).

Usage:
    python3 record_outcome.py <ats> "<technique>" <submitted|blocked> "<note>"
        [--role-id ID] [--company NAME] [--source SRC] [--fit-score N]
        [--resume-lane LANE] [--lane A|B] [--date YYYY-MM-DD]
        [--timestamp-basis BASIS]
        [--experiment-variant VID] [--no-telemetry]

Validated, append-only outcome recording:

  - Canonical ATS-key enforcement: the ats key must come from
    ats.ATS_PATTERNS (the single source of truth for platform keys) or the
    LEGACY_ATS_KEYS set. Anything else — notably role_id-shaped strings
    from misordered CLI invocations — is refused, never written.
  - Placeholder refusal: literal 'tech'/'note' smoke-test values are
    refused before any telemetry emission.
  - Every accepted record emits a submitted / gate_blocked telemetry event
    (via log_event.py) so the append-only event log is the durable
    evidence base. --no-telemetry suppresses the event for honest
    backfills of rows whose telemetry already exists (no double-counting);
    --date backfills the evidence entry's date (validated YYYY-MM-DD).
  - Occurred-vs-observed separation (gap-plan item b): every emitted
    details dict carries BOTH occurred_at (the validated evidence date —
    when the outcome happened, per the caller's evidence) and observed_at
    (the UTC emission time — when this writer saw it), plus timestamp_basis
    (the CLOSED vocabulary naming where occurred_at came from: ats_receipt,
    ats_api, browser_confirmation, user_statement, backfill, unknown) and
    source_watermark (WRITER_REV). The basis is NEVER inferred — --date
    without --timestamp-basis leaves basis "unknown", and rows whose basis
    is unknown are excluded from latency estimates via latency_eligible().

Private-layer boundary (see SPLIT.md): the technique library — per-ATS
form-commit/event-sequencing methods — is part of the private execution
layer and is deliberately not written to here. The telemetry event is the
public learning loop's evidence base; blocked attempts carry a gate value
(recording_consent via prescreen._primary_gate, otherwise
technique_blocked) so consent parks and technique parks share one
vocabulary with the rest of the pipeline.
"""

import json
import os
import re
import sys
from datetime import datetime, timezone

BASE = os.path.dirname(os.path.abspath(__file__))

sys.path.insert(0, BASE)
import log_event  # noqa: E402
# Canonical ATS key enforcement: ats.ATS_PATTERNS is the single source of
# truth for platform keys; LEGACY_ATS_KEYS covers real keys that predate
# the pattern registry (wellfound/jazzhr/ycombinator/cursor-native) plus
# the explicit 'unknown' bucket and edge-registry transport variants.
from ats import ATS_PATTERNS  # noqa: E402
LEGACY_ATS_KEYS = frozenset({
    "wellfound", "jazzhr", "ycombinator", "cursor-native", "unknown",
    "greenhouse_legacy_embed", "greenhouse_modern", "harri", "successfactors",
})
VALID_ATS = frozenset(ATS_PATTERNS) | LEGACY_ATS_KEYS

try:
    # prescreen._primary_gate is the canonical gate classifier (see
    # GATE_TYPES in log_event.py). record_outcome re-exports ONLY its
    # "recording_consent" verdict so consent parks share one gate value
    # with prescreen. Every other blocked case keeps technique_blocked.
    from prescreen import _primary_gate as _canonical_primary_gate  # noqa: E402
except Exception:  # fail closed: without the classifier, consent parks
    _canonical_primary_gate = None  # read as technique_blocked


# --- Keel security authority choke point (Phase 1) ---
# SUBMISSION-class actions route through the deterministic security
# subsystem before anything is recorded:
#   IDENTITY/CAPABILITY -> INJECTION CHECK -> POLICY ENGINE -> LEDGER.
# The LLM requests; the policy engine decides. Fail closed: if the
# security authority cannot be loaded, no submission is recorded.
try:
    _SECURITY_PARENT = os.path.dirname(BASE)
    if _SECURITY_PARENT not in sys.path:
        sys.path.insert(0, _SECURITY_PARENT)
    from security.actions.interceptor import (  # noqa: E402
        request_submission_authorization as _sec_authorize_submission)
    _SECURITY_AVAILABLE = True
except Exception:
    _sec_authorize_submission = None
    _SECURITY_AVAILABLE = False


# --- Occurred-vs-observed separation (gap-plan item b) ---
# Module revision watermark stamped on every emitted telemetry row so
# consumers can distinguish rows written under this writer generation.
WRITER_REV = "record_outcome/1"
# Closed vocabulary for timestamp_basis: the provenance of the occurred_at
# value. Never inferred — the caller must name it; the default is
# "unknown" and unknown-basis rows are excluded from latency estimates
# (see latency_eligible).
TIMESTAMP_BASES = frozenset({
    "ats_receipt",         # ATS receipt page / confirmation email time
    "ats_api",             # ATS API record timestamp
    "browser_confirmation",  # live browser confirmation quote time
    "user_statement",      # the user's own stated time
    "backfill",            # honest backfill of a previously recorded row
    "unknown",             # provenance not recorded — fail closed
})
TIMESTAMP_BASIS_DEFAULT = "unknown"
_OCCURRED_AT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def latency_eligible(details) -> bool:
    """True iff this outcome row may be used for latency estimation.

    Latency consumers MUST call this before using occurred_at: a row whose
    timestamp_basis is "unknown" (or whose occurred_at is missing or
    unparseable) carries no trustworthy event time, so it must be excluded
    from every latency estimate. Only rows with a known basis and a
    well-formed occurred_at are eligible.
    """
    if not isinstance(details, dict):
        return False
    # Missing basis key is equivalent to "unknown"; a value outside the
    # closed vocabulary is also ineligible — never trust an unprovenanced
    # or unrecognized timestamp in a latency estimate.
    basis = details.get("timestamp_basis", TIMESTAMP_BASIS_DEFAULT)
    if basis not in TIMESTAMP_BASES or basis == TIMESTAMP_BASIS_DEFAULT:
        return False
    occurred = details.get("occurred_at")
    if not isinstance(occurred, str) or not _OCCURRED_AT_RE.match(occurred):
        return False
    try:
        datetime.strptime(occurred, "%Y-%m-%d")
    except ValueError:
        return False
    return True


def _num_score(v):
    """Return v if it's a real number, else None. Excludes bools."""
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def record(ats: str, technique: str, outcome: str, note: str,
           role_id: str = "", company: str = "", source: str = "",
           fit_score=None, resume_lane: str = "unknown", lane: str = "",
           date: str = None, emit_telemetry: bool = True,
           experiment_variant: str = "", event_id: str = None,
           timestamp_basis: str = TIMESTAMP_BASIS_DEFAULT):
    # Fail closed on non-canonical ATS keys. Garbage keys (role_ids,
    # --flags) previously created phantom library sections and poisoned
    # technique hit-rate measurement.
    if ats not in VALID_ATS:
        raise ValueError(
            f"refusing to record outcome: unknown ats key {ats!r}. "
            f"Valid keys: {sorted(VALID_ATS)}")
    # Fail closed on literal placeholder values. A smoke test once invoked
    # the CLI as `record_outcome.py greenhouse tech blocked note` and wrote
    # a gate_blocked telemetry row with technique="tech", note="note" —
    # pure noise. The placeholder pair is never a real outcome record.
    if ((technique or "").strip().lower() == "tech"
            and (note or "").strip().lower() == "note"):
        raise ValueError(
            "refusing to record outcome: technique/note are placeholder "
            "literals ('tech'/'note') — pass the real technique name and "
            "note.")
    if outcome == "submitted":
        # SECURITY CHOKE POINT: the security authority must ALLOW this
        # submission before anything is recorded. The note carries the
        # confirmation quote (ATS-derived, therefore untrusted) and is
        # injection-scanned; the caller must hold the record_submission
        # capability; every decision lands in the hash-chained security
        # ledger. Refusal raises PolicyDenied (a ValueError) — the same
        # fail-closed contract as the refusals above.
        if not _SECURITY_AVAILABLE or _sec_authorize_submission is None:
            raise ValueError(
                "refusing to record outcome: security authority "
                "unavailable — fail closed")
        _sec_authorize_submission(
            agent_hint="keel-application-engine", ats=ats,
            technique=technique, note=note, role_id=role_id,
            company=company)
    # Backfill support: `date` overrides the evidence entry's date for
    # honest backfills of outcomes recorded in queue/ledger but never
    # passed through this writer. Validated YYYY-MM-DD; defaults to today.
    # `emit_telemetry=False` records without emitting a telemetry event —
    # for rows whose submitted telemetry already exists, so the backfill
    # doesn't double-count.
    ev_date = date or datetime.now().strftime("%Y-%m-%d")
    if not isinstance(ev_date, str) or not re.match(
            r"^\d{4}-\d{2}-\d{2}$", ev_date):
        raise ValueError(
            f"refusing to record outcome: bad date {ev_date!r} — "
            "use YYYY-MM-DD")
    # Fail closed on unknown timestamp bases: the vocabulary is closed and
    # the basis is never inferred. A --date backfill without an explicit
    # --timestamp-basis stays "unknown" (never upgraded here).
    if timestamp_basis not in TIMESTAMP_BASES:
        raise ValueError(
            f"refusing to record outcome: unknown timestamp_basis "
            f"{timestamp_basis!r} — valid: {sorted(TIMESTAMP_BASES)}")
    print(f"recorded {outcome} for {ats}/{technique} "
          f"(telemetry only; technique library is private per SPLIT.md)")
    if not emit_telemetry:
        return
    # Telemetry: mirror the outcome into the append-only event log. Every
    # event carries ats (top level) and resume_lane (fail-closed to
    # 'unknown' when the caller lacks it) so ATS/lane conversion analysis
    # has attribution going forward.
    ev_type = "submitted" if outcome == "submitted" else "gate_blocked"
    # Occurred-vs-observed separation: occurred_at is the validated
    # evidence date (when the outcome happened); observed_at is the UTC
    # emission time (when this writer saw it). evidence_date is kept as a
    # backward-compat alias with an identical value. timestamp_basis names
    # the provenance of occurred_at and is never inferred; source_watermark
    # identifies this writer generation.
    details = {"technique": technique, "note": note,
               "resume_lane": resume_lane or "unknown",
               "occurred_at": ev_date,
               "evidence_date": ev_date,
               "observed_at": datetime.now(timezone.utc).isoformat(),
               "timestamp_basis": timestamp_basis,
               "source_watermark": WRITER_REV}
    if lane:  # two-lane tests: every event carries details.lane
        details["lane"] = lane
    if experiment_variant:  # per-variant outcome tracking. Omitted (never
        # a placeholder) when the caller has none.
        details["experiment_variant"] = experiment_variant
    fs = _num_score(fit_score)
    if fs is not None:
        details["fit_score"] = fs
    if outcome == "blocked":
        # recording-consent language gets the canonical recording_consent
        # gate (per prescreen._primary_gate). Any other blocked case stays
        # technique_blocked — no re-classification.
        if _canonical_primary_gate is not None and \
                _canonical_primary_gate([note]) == "recording_consent":
            details["gate"] = "recording_consent"
        else:
            details["gate"] = "technique_blocked"
    log_event.log(ev_type, role_id=role_id, company=company, ats=ats,
                  source=source or "record_outcome", details=details,
                  event_id=event_id)


USAGE = ("usage: record_outcome.py <ats> <technique> <submitted|blocked> <note> "
         "[--role-id ID] [--company NAME] [--source SRC] [--fit-score N] "
         "[--resume-lane LANE] [--lane A|B] [--date YYYY-MM-DD] "
         "[--timestamp-basis BASIS] "
         "[--experiment-variant VID] "
         "[--no-telemetry]  (flags may appear in any position; --date "
         "backfills the evidence entry's date, --timestamp-basis names the "
         "provenance of that date (ats_receipt|ats_api|browser_confirmation|"
         "user_statement|backfill|unknown; default unknown, never inferred), "
         "--no-telemetry skips the "
         "telemetry event for honest backfills, --experiment-variant tags "
         "the telemetry details for per-variant outcome tracking)")

# Flags the CLI recognizes anywhere in argv. Extracting them
# position-independently + requiring exactly four positionals fail-closes
# the misordered invocations that previously wrote malformed telemetry
# (ats="--role-id", technique=<role_id>, note="submitted").
_FLAG_TAKES_VALUE = {
    "--role-id", "--company", "--source", "--fit-score", "--resume-lane",
    "--lane", "--date", "--timestamp-basis", "--experiment-variant",
}
_FLAG_NO_VALUE = {"--no-telemetry"}


def parse_cli_args(argv):
    """Parse CLI args -> dict for record(). Fail-closed on misordering.

    argv[0] is the script name. Known flags are pulled out from ANY position;
    whatever remains must be exactly the four positionals
    (ats, technique, outcome, note) in order. Any unknown --flag (or a known
    flag with no value) raises SystemExit with usage, so no malformed
    telemetry can be written.
    """
    role_id = company = source = ""
    fit_score = None
    resume_lane = "unknown"
    lane = ""
    date = None
    emit_telemetry = True
    experiment_variant = ""
    timestamp_basis = TIMESTAMP_BASIS_DEFAULT
    positionals = []
    i = 1
    while i < len(argv):
        tok = argv[i]
        if tok in _FLAG_NO_VALUE:
            if tok == "--no-telemetry":
                emit_telemetry = False
            i += 1
        elif tok in _FLAG_TAKES_VALUE and i + 1 < len(argv):
            val = argv[i + 1]
            if tok == "--role-id":
                role_id = val
            elif tok == "--company":
                company = val
            elif tok == "--source":
                source = val
            elif tok == "--resume-lane":
                resume_lane = val or "unknown"
            elif tok == "--lane":
                lane = val or ""
            elif tok == "--date":
                date = val or None
            elif tok == "--timestamp-basis":
                # Closed vocabulary, never inferred: an unrecognized basis
                # fails closed with usage, exactly like an unknown flag.
                if val not in TIMESTAMP_BASES:
                    raise SystemExit(USAGE)
                timestamp_basis = val
            elif tok == "--experiment-variant":
                experiment_variant = val or ""
            elif tok == "--fit-score":
                try:
                    fit_score = float(val)
                except ValueError:
                    fit_score = None
            i += 2
        elif tok.startswith("--"):
            # Fail closed on UNKNOWN flags: an invented flag like
            # --outcome previously became ats="--outcome" and wrote garbage
            # keys. Any --token that is not a known flag (or is a known
            # flag with no value left) is rejected with usage.
            raise SystemExit(USAGE)
        else:
            positionals.append(tok)
            i += 1
    if len(positionals) != 4:
        raise SystemExit(USAGE)
    ats, technique, outcome, note = positionals
    return {"ats": ats, "technique": technique, "outcome": outcome,
            "note": note, "role_id": role_id, "company": company,
            "source": source, "fit_score": fit_score,
            "resume_lane": resume_lane, "lane": lane, "date": date,
            "emit_telemetry": emit_telemetry,
            "experiment_variant": experiment_variant,
            "timestamp_basis": timestamp_basis}


def main(argv):
    parsed = parse_cli_args(argv)
    try:
        record(parsed["ats"], parsed["technique"], parsed["outcome"],
               parsed["note"], role_id=parsed["role_id"],
               company=parsed["company"], source=parsed["source"],
               fit_score=parsed["fit_score"],
               resume_lane=parsed["resume_lane"], lane=parsed["lane"],
               date=parsed["date"],
               emit_telemetry=parsed["emit_telemetry"],
               experiment_variant=parsed["experiment_variant"],
               timestamp_basis=parsed["timestamp_basis"])
    except ValueError as ex:
        # Unknown ATS key / placeholder literals / bad date — refuse loudly,
        # never write a phantom record.
        print(f"record_outcome: {ex}", file=sys.stderr)
        raise SystemExit(2)


if __name__ == "__main__":
    if len(sys.argv) < 5:
        sys.exit(USAGE)
    main(sys.argv)
