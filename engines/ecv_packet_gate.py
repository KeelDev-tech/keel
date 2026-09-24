"""ECV packet gate for T3 decisions — auto-filled, fail-open, never halts the lane.

the applicant-approved 2026-09-16 ("Yes test and exhaust all possibilities").

T3 decisions (consequential, hard to reverse):
  - "submission"              — application submitted (hooked in record_outcome)
  - "global-bank-promotion"   — answer banked globally (hooked in tray_answer)
  - "standing-authorization"  — new autopilot scope / standing rule (manual CLI)

Before (logically; the packet is written alongside, never before) any T3
decision is executed, emit() auto-fills a one-page ECV packet from live
telemetry, forces the challenge step (strongest counterargument), and logs
the packet alongside the decision record. Packets accumulate toward 20
live T3 packets for blind scoring in:
  ~/workspace/goals/run-my-western-application-pipeline/hidden_files/ecv-pilot/live-packets/

FAIL-OPEN CONTRACT (never-halt doctrine): emit() catches ALL exceptions.
On any failure it appends an `ecv_packet_missing` row to the packet log
and returns None. A packet-generation failure must never block a
submission or stall the pipeline. Callers additionally guard the call.

Usage:
    import ecv_packet_gate
    ecv_packet_gate.emit("submission", subject=role_id,
                        decision_summary="submitted via greenhouse/direct: ...")

CLI (for standing authorizations — human-authored config changes):
    python3 ecv_packet_gate.py --t3 standing-authorization \\
        --subject "autopilot-attestation-scope" \\
        --decision "added arbitration attestation to pre-authorized scope" \\
        --claim "the applicant authorized full autopilot 2026-09-16 | High | MEMORY.md" \\
        --counterargument "scope creep: a future attestation may not be standard"
"""

import argparse
import json
import os
import re
import sys
import traceback
from datetime import datetime, timezone

T3_KINDS = ("submission", "global-bank-promotion", "standing-authorization")

from keel_paths import TELEMETRY  # noqa: E402 — repo path convention
_DEFAULT_EVENTS = os.path.join(TELEMETRY, "events.jsonl")


def _packet_dir():
    return os.path.expanduser(os.environ.get(
        "ECV_PACKET_DIR",
        "~/workspace/goals/run-my-western-application-pipeline/"
        "hidden_files/ecv-pilot/live-packets"))


def _events_path():
    """Same precedence as log_event._events_path: explicit attr, env, default."""
    try:
        import log_event  # local import: same directory as callers
        return log_event._events_path()
    except Exception:
        return os.environ.get("JOB_PIPELINE_EVENTS_PATH") or _DEFAULT_EVENTS


def _utcnow():
    return datetime.now(timezone.utc)


def _slug(s):
    s = re.sub(r"[^A-Za-z0-9_-]+", "-", str(s or "unknown")).strip("-")
    return (s or "unknown")[:60]


def _load_events_for(subject, cap=20000):
    """Return recent telemetry events attributed to subject (role_id match)."""
    path = _events_path()
    subject = str(subject or "").strip()
    if not subject:
        return []
    hits = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                if str(row.get("role_id") or "").strip().lower() == subject.lower():
                    hits.append(row)
                    if len(hits) >= cap:
                        break
    except FileNotFoundError:
        return []
    return hits


def _fit_score(events):
    for ev in reversed(events):
        d = ev.get("details") or {}
        fs = d.get("fit_score")
        if isinstance(fs, (int, float)) and not isinstance(fs, bool):
            return fs
    return None


def _gates(events):
    cleared, blocked = [], []
    for ev in events:
        et = ev.get("event_type")
        d = ev.get("details") or {}
        g = d.get("gate")
        if et == "gate_cleared" and g:
            cleared.append(g)
        elif et == "gate_blocked" and g:
            blocked.append(g)
    return sorted(set(cleared)), sorted(set(blocked))


def _has_event(events, *types):
    want = set(types)
    return any(ev.get("event_type") in want for ev in events)


# Failure taxonomy from the 2026-09-16 ECV pilot (7 reversed/error cases).
# The forced challenge step names the check that later became the real fix.
_CHALLENGES = {
    "posting_text": (
        "Coverage assumption unchecked: the posting text itself may state "
        "travel %, office days/week, or degree requirements the form never "
        "asked — the form-label screen cannot see them (Addepar, HelloFresh, "
        "Lucid pattern). Was the POSTING TEXT checked, not just the form?"),
    "extractor": (
        "Extractor coverage gap: a question type the extractor does not "
        "cover (dropdowns without enumerated options, address lines, "
        "checkboxes mislabeled as text) may have bypassed the gate silently "
        "(Axon 79068290, McMaster-Carr pattern)."),
    "ownership": (
        "Lane ownership vs queue state: was the browser task verified LIVE, "
        "not inferred from queue markers? A stale IN-FLIGHT marker claims a "
        "submission that does not exist (OpenTable pattern)."),
    "provenance": (
        "Provenance gap: is every load-bearing fact traceable to a cited "
        "source — not inferred from an employer name or a banked answer "
        "without provenance (Axon experience-checkbox pattern)?"),
}


def _default_challenge(kind, events):
    if kind == "global-bank-promotion":
        return (
            "Portability risk: the 2026-09-16 SMS-consent quarantine proved "
            "a per-employer consent must NEVER be promoted to a standing "
            "global answer. Is this answer truly employer-portable, or is it "
            "scoped to one employer's wording/context?")
    if kind == "standing-authorization":
        return (
            "Scope creep: does this authorization stay inside the boundary "
            "the applicant actually drew (truthful, standard, reversible)? What "
            "future case inside this scope would he NOT have approved?")
    # submission: pick from evidence
    if not _has_event(events, "lead_verified"):
        return _CHALLENGES["posting_text"]
    cleared, _ = _gates(events)
    if not cleared:
        return _CHALLENGES["extractor"]
    return _CHALLENGES["posting_text"]


def _autofill_claims(kind, subject, events, decision_summary):
    claims = []  # (claim, confidence, evidence)
    if kind == "submission":
        fs = _fit_score(events)
        if fs is not None:
            claims.append((f"Fit score {fs}", "Med",
                           "telemetry fit detail for this role_id"))
        else:
            claims.append(("No fit score found in telemetry for this role_id",
                           "Low", "telemetry scan returned no fit_score"))
        cleared, blocked = _gates(events)
        if cleared:
            claims.append((f"Prescreen gates cleared: {', '.join(cleared)}",
                           "Med", "telemetry gate_cleared events"))
        if blocked:
            claims.append((f"Gate blocks seen: {', '.join(blocked)}", "Med",
                           "telemetry gate_blocked events"))
        if _has_event(events, "lead_verified"):
            claims.append(("Posting verified live", "Med",
                           "telemetry lead_verified event"))
        else:
            claims.append(("No lead_verified event found — liveness "
                           "attribution is weak", "Low", "telemetry scan"))
        if _has_event(events, "confirmation_received"):
            claims.append(("Confirmation text received", "High",
                           "telemetry confirmation_received event"))
        claims.append((decision_summary, "High", "record_outcome call args"))
    elif kind == "global-bank-promotion":
        claims.append((f"answer_bank[{subject}] banked globally", "High",
                       "tray_answer bank write"))
        claims.append(("Answer text is the applicant's own words", "High",
                       "answer_bank _provenance via tray_answer"))
        claims.append((decision_summary, "High", "tray_answer call context"))
        claims.append(("Scope is GLOBAL — applies to every future lead "
                       "matching this key", "High",
                       "answer_bank.json answers section"))
    else:  # standing-authorization
        claims.append((decision_summary, "Med",
                       "human-authored config change (see CLI args)"))
    return claims


def _render(kind, subject, decision_summary, claims, counterargument,
            verification_check, component):
    lines = []
    lines.append(f"# ECV Decision Packet — LIVE (T3: {kind})")
    lines.append("")
    lines.append(f"- **Subject:** {subject}")
    lines.append(f"- **Decision class:** T3-consequential")
    lines.append(f"- **Decision:** {decision_summary}")
    lines.append(f"- **Component:** {component}")
    lines.append(f"- **Packet generated:** {_utcnow().isoformat()}")
    lines.append(f"- **Outcome (scoring only, filled later):** HELD / REVERSED / UNKNOWN")
    lines.append("")
    lines.append("## Claims (generation)")
    lines.append("| # | Claim | Confidence | Evidence cited |")
    lines.append("|---|-------|-----------|----------------|")
    for i, (claim, conf, ev) in enumerate(claims, 1):
        claim = str(claim).replace("|", "/")
        ev = str(ev).replace("|", "/")
        lines.append(f"| {i} | {claim} | {conf} | {ev} |")
    lines.append("")
    lines.append("## Strongest counterargument (challenge)")
    lines.append(counterargument or "_not provided — CHALLENGE STEP SKIPPED_")
    lines.append("")
    lines.append("## Verification check (verification)")
    lines.append(verification_check or
                 "Not recorded. The counterargument above names the check "
                 "that should have run before the decision became irreversible.")
    lines.append("")
    lines.append("## Synthesis")
    n_unsupported = sum(1 for _, _, ev in claims
                        if "no " in str(ev).lower() and "found" in str(ev).lower())
    lines.append(f"Auto-filled packet. Unsupported-claim heuristic: "
                 f"{n_unsupported} claim(s) with weak/no evidence. "
                 f"Net judgment: proceed iff the counterargument's check was "
                 f"run or is inapplicable; otherwise park for verification.")
    lines.append("")
    return "\n".join(lines) + "\n"


def _log_row(row):
    d = _packet_dir()
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, "packets.jsonl")
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def emit(kind, subject, decision_summary, claims=None, counterargument=None,
         verification_check=None, component="unknown"):
    """Write an auto-filled ECV packet for a T3 decision. Fail-open: never raises."""
    try:
        return _emit_inner(kind, subject, decision_summary, claims,
                           counterargument, verification_check, component)
    except Exception as e:
        try:
            _log_row({"ts": _utcnow().isoformat(), "type": "ecv_packet_missing",
                      "kind": kind, "subject": str(subject),
                      "component": component,
                      "error": f"{type(e).__name__}: {e}"[:300]})
        except Exception:
            pass
        return None


def _emit_inner(kind, subject, decision_summary, claims, counterargument,
                verification_check, component):
    if kind not in T3_KINDS:
        raise ValueError(f"unknown T3 kind {kind!r}; valid: {T3_KINDS}")
    subject = str(subject or "unknown").strip() or "unknown"
    events = (_load_events_for(subject)
              if kind == "submission" else [])
    if claims is None:
        claims = _autofill_claims(kind, subject, events, decision_summary)
    if counterargument is None:
        counterargument = _default_challenge(kind, events)
    body = _render(kind, subject, decision_summary, claims, counterargument,
                   verification_check, component)
    d = _packet_dir()
    os.makedirs(d, exist_ok=True)
    stamp = _utcnow().strftime("%Y%m%dT%H%M%SZ")
    fname = f"packet-{stamp}-{kind}-{_slug(subject)}.md"
    tmp = os.path.join(d, fname + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(body)
    os.replace(tmp, os.path.join(d, fname))
    _log_row({"ts": _utcnow().isoformat(), "type": "ecv_packet",
              "kind": kind, "subject": subject, "packet_file": fname,
              "component": component,
              "decision_summary": str(decision_summary)[:300],
              "unsupported_heuristic": sum(
                  1 for _, _, ev in claims
                  if "no " in str(ev).lower() and "found" in str(ev).lower())})
    return fname


def main(argv=None):
    ap = argparse.ArgumentParser(description="ECV T3 packet gate (fail-open)")
    ap.add_argument("--t3", required=True, choices=T3_KINDS)
    ap.add_argument("--subject", required=True)
    ap.add_argument("--decision", required=True)
    ap.add_argument("--claim", action="append", default=[],
                    help="'claim | confidence | evidence'; repeatable")
    ap.add_argument("--counterargument", default=None)
    ap.add_argument("--verification", default=None)
    ap.add_argument("--component", default="manual-cli")
    a = ap.parse_args(argv)
    claims = None
    if a.claim:
        claims = []
        for c in a.claim:
            parts = [p.strip() for p in c.split("|")]
            while len(parts) < 3:
                parts.append("")
            claims.append((parts[0], parts[1] or "Med", parts[2] or "manual"))
    if a.t3 == "standing-authorization" and not a.counterargument:
        # The challenge step is forced on T3: refuse a packet without one.
        # Fail-open still holds — this only refuses the CLI packet, it
        # never blocks the underlying config change.
        print("refusing: --counterargument is required for T3 packets "
              "(challenge step is forced)", file=sys.stderr)
        return 2
    fname = emit(a.t3, a.subject, a.decision, claims=claims,
                 counterargument=a.counterargument,
                 verification_check=a.verification, component=a.component)
    print(f"packet: {fname}" if fname else "packet: MISSING (logged, continuing)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
