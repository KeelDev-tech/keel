#!/usr/bin/env python3
"""Offer surfacing for the pipeline ping digest.

Mirrors invite_surface.py's exactly-once pattern for the offer playbook:
on_offer() builds the terms pack and prints a SURFACE: line, but no
channel consumes it. This module is the sanctioned delivery path — the
30-min pipeline ping calls it as step (7) of the full ping.

Exactly-once: offer identities are recorded in
~/hooks/state/pipeline-ping-gate-surfaced-offers.json (a SEPARATE file
from the gate snapshot AND from the invite surfaced-ids file — the gate
poll script rebuilds the snapshot from scratch and would drop any
foreign key). An offer surfaces in exactly one digest; repeated pings
never re-page the applicant.

Digest-claim honesty: the block is rendered from the telemetry event's
own fields plus the terms-summary.md header — nothing invented, every
unverifiable field labeled. No outreach, no negotiation: drafts are
only pointed at. Agents never negotiate, accept, or decline.
"""

import argparse
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

PT = ZoneInfo("America/Los_Angeles")

_TRACK_DIR = os.path.dirname(os.path.abspath(__file__))
_ENGINES = os.path.dirname(_TRACK_DIR)
for _p in (_TRACK_DIR, _ENGINES):
    if _p not in sys.path:
        sys.path.insert(0, _p)
from keel_paths import DATA, TELEMETRY  # noqa: E402

try:
    import log_event as _log_event
except Exception:  # pragma: no cover - standalone fallback
    _log_event = None

SURFACED_DEFAULT = os.path.join(
    DATA, "state", "pipeline-ping-gate-surfaced-offers.json")
EVENTS_FALLBACK = os.path.join(TELEMETRY, "events.jsonl")

EVENT_TYPE = "offer_terms_extracted"
_PACK_HEADER_RE = re.compile(
    r"^#\s+Terms summary\s+[—–-]\s+(.*?)\s+/\s+(.*?)\s*$")


def default_events_path():
    if _log_event is not None:
        try:
            return _log_event._events_path()
        except Exception:
            pass
    return os.environ.get("JOB_PIPELINE_EVENTS_PATH") or EVENTS_FALLBACK


def _parse_ts(ts):
    try:
        d = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _pt_str(ts):
    d = _parse_ts(ts)
    if d is None:
        return "unverified"
    return d.astimezone(PT).strftime("%Y-%m-%d %H:%M PDT")


def iter_offer_events(events_path=None):
    """All offer_terms_extracted events, oldest first. Malformed lines are
    skipped silently — a broken line must never block surfacing."""
    events_path = events_path or default_events_path()
    out = []
    try:
        with open(events_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except Exception:
                    continue
                if isinstance(e, dict) and e.get("event_type") == EVENT_TYPE:
                    out.append(e)
    except FileNotFoundError:
        pass
    out.sort(key=lambda e: (_parse_ts(e.get("ts")) or
                           datetime.min.replace(tzinfo=timezone.utc),
                           str(e.get("role_id"))))
    return out


def offer_id(event):
    """Stable identity for exactly-once: event ts + role_id."""
    return "%s|%s" % (event.get("ts", ""), event.get("role_id", ""))


def load_surfaced(surfaced_path=None):
    surfaced_path = surfaced_path or SURFACED_DEFAULT
    try:
        data = json.load(open(surfaced_path))
        return set(data) if isinstance(data, list) else set()
    except (FileNotFoundError, ValueError):
        return set()


def pending_offers(events_path=None, surfaced_path=None):
    """(offer_id, event) pairs not yet surfaced, oldest first."""
    surfaced = load_surfaced(surfaced_path)
    return [(offer_id(e), e)
            for e in iter_offer_events(events_path)
            if offer_id(e) not in surfaced]


def mark_surfaced(surfaced_path, ids):
    """Atomically record offer ids as surfaced (union, no dupes)."""
    surfaced_path = surfaced_path or SURFACED_DEFAULT
    cur = load_surfaced(surfaced_path)
    cur |= set(ids)
    parent = os.path.dirname(surfaced_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=parent or ".",
                               prefix=".surfaced-offers.")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(sorted(cur), f, indent=1)
        os.replace(tmp, surfaced_path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return sorted(cur)


def _pack_title(offer_dir):
    """Parse 'COMPANY / TITLE' from the terms-summary.md header. Returns
    (company_or_None, title_or_None) — never guesses."""
    if not offer_dir:
        return None, None
    pack = os.path.join(offer_dir, "terms-summary.md")
    try:
        with open(pack) as f:
            first = f.readline()
    except (FileNotFoundError, OSError):
        return None, None
    m = _PACK_HEADER_RE.match(first.strip())
    if not m:
        return None, None
    return m.group(1).strip() or None, m.group(2).strip() or None


def render_block(event):
    """Top-priority digest block for one offer. Fields come only from the
    event + the terms-summary header; anything unverifiable is labeled.
    Never invents terms, never negotiates."""
    details = event.get("details") or {}
    offer_dir = details.get("offer_dir") or ""
    pack_company, pack_title = _pack_title(offer_dir)
    company = (pack_company or event.get("company") or "unknown employer")
    title = pack_title or (event.get("role_id") or "unknown role")
    msg_date = details.get("message_date") or ""
    msg_date_str = (msg_date if msg_date
                    else "unverified — check the employer message")
    terms = details.get("terms") or {}
    deadline = terms.get("deadline") or "not stated in message"
    complete = details.get("terms_complete") or "unverified"
    competing = details.get("competing_offers") or 0
    competing_line = (f"- COMPETING OFFERS: {competing} open — "
                      f"comparison is the applicant's call, agents never rank offers"
                      if competing else "")
    pack_line = (f"Terms pack: {offer_dir} (drafts/ inside — "
                 f"for the applicant to read themselves; agents never negotiate, "
                 f"accept, or decline)"
                 if offer_dir and offer_dir != "build_failed"
                 else "Terms pack: BUILD FAILED — see hook stderr; "
                      "read the employer message directly")
    lines = [
        "TIME-SENSITIVE — JOB OFFER (surfaces once):",
        f"- {company} — {title}",
        f"- Offer logged: {_pt_str(event.get('ts'))}",
        f"- Employer message date: {msg_date_str}",
        f"- Decision deadline: {deadline}",
        f"- Terms extracted: {complete} fields with source quotes "
        f"(unquoted fields read \"not stated\")",
    ]
    if competing_line:
        lines.append(competing_line)
    lines.append(f"- {pack_line}")
    return "\n".join(lines)


def pending_payload(events_path=None, surfaced_path=None):
    """JSON-serializable payload for the ping worker: rendered blocks +
    the ids to mark surfaced after the digest carries them."""
    items = []
    for oid, e in pending_offers(events_path, surfaced_path):
        details = e.get("details") or {}
        items.append({
            "id": oid,
            "ts": e.get("ts", ""),
            "role_id": e.get("role_id", ""),
            "company": e.get("company", ""),
            "message_date": details.get("message_date") or "",
            "offer_dir": details.get("offer_dir") or "",
            "block": render_block(e),
        })
    return {"count": len(items), "offers": items}


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Surface unsurfaced job offers for the ping.")
    ap.add_argument("--events", default=None,
                    help="telemetry events.jsonl (default: canonical path)")
    ap.add_argument("--surfaced", default=None,
                    help="surfaced-ids file (default: hook state file)")
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--pending", action="store_true",
                       help="print JSON payload of unsurfaced offers")
    group.add_argument("--block", action="store_true",
                       help="print the rendered digest block(s) as text")
    group.add_argument("--mark", nargs="+", metavar="ID",
                       help="record offer ids as surfaced (exactly-once)")
    args = ap.parse_args(argv)

    if args.mark:
        ids = mark_surfaced(args.surfaced, args.mark)
        print(json.dumps({"surfaced_total": len(ids)}))
        return 0
    payload = pending_payload(args.events, args.surfaced)
    if args.block:
        blocks = [i["block"] for i in payload["offers"]]
        print(("\n\n" + "-" * 40 + "\n\n").join(blocks) if blocks else "")
        return 0
    print(json.dumps(payload, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
