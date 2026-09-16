#!/usr/bin/env python3
"""Interview-invite surfacing for the pipeline ping digest.

Closes the downstream-hooks gap: on_interview_invite() builds the prep
pack and prints a SURFACE: line, but no channel consumes it. This module
is the sanctioned delivery path — the 30-min pipeline ping calls it as
step (6) of the full ping.

Exactly-once: invite identities are recorded in
~/hooks/state/pipeline-ping-gate-surfaced-invites.json (a SEPARATE file
from the gate snapshot, because the gate poll script rebuilds the
snapshot from scratch and would drop any foreign key). An invite
surfaces in exactly one digest; repeated pings never re-page the applicant.

Digest-claim honesty: the block is rendered from the telemetry event's
own fields plus the prep-pack.md header — nothing invented, everything
unverifiable marked as such. No outreach: drafts are only pointed at.
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
    DATA, "state", "pipeline-ping-gate-surfaced-invites.json")
EVENTS_FALLBACK = os.path.join(TELEMETRY, "events.jsonl")

EVENT_TYPE = "interview_prep_built"
_PACK_HEADER_RE = re.compile(
    r"^#\s+Interview prep pack\s+[—–-]\s+(.*?)\s+/\s+(.*?)\s*$")


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


def iter_prep_built(events_path=None):
    """All interview_prep_built events, oldest first. Malformed lines are
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


def invite_id(event):
    """Stable identity for exactly-once: event ts + role_id."""
    return "%s|%s" % (event.get("ts", ""), event.get("role_id", ""))


def load_surfaced(surfaced_path=None):
    surfaced_path = surfaced_path or SURFACED_DEFAULT
    try:
        data = json.load(open(surfaced_path))
        return set(data) if isinstance(data, list) else set()
    except (FileNotFoundError, ValueError):
        return set()


def pending_invites(events_path=None, surfaced_path=None):
    """(invite_id, event) pairs not yet surfaced, oldest first."""
    surfaced = load_surfaced(surfaced_path)
    return [(invite_id(e), e)
            for e in iter_prep_built(events_path)
            if invite_id(e) not in surfaced]


def mark_surfaced(surfaced_path, ids):
    """Atomically record invite ids as surfaced (union, no dupes)."""
    surfaced_path = surfaced_path or SURFACED_DEFAULT
    cur = load_surfaced(surfaced_path)
    cur |= set(ids)
    parent = os.path.dirname(surfaced_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=parent or ".",
                               prefix=".surfaced-invites.")
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


def _pack_title(prep_dir):
    """Parse 'COMPANY / TITLE' from the prep-pack.md header. Returns
    (company_or_None, title_or_None) — never guesses."""
    if not prep_dir:
        return None, None
    pack = os.path.join(prep_dir, "prep-pack.md")
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
    """Top-priority digest block for one invite. Fields come only from
    the event + the prep-pack header; anything unverifiable is labeled."""
    details = event.get("details") or {}
    prep_dir = details.get("prep_dir") or ""
    pack_company, pack_title = _pack_title(prep_dir)
    company = (pack_company or event.get("company") or "unknown employer")
    title = pack_title or (event.get("role_id") or "unknown role")
    msg_date = details.get("message_date") or ""
    msg_date_str = (msg_date if msg_date
                    else "unverified — check the employer message")
    pack_line = (f"Prep pack: {prep_dir} (drafts/ inside — "
                 f"for the applicant to send themselves, never sent by agents)"
                 if prep_dir and prep_dir != "build_failed"
                 else "Prep pack: BUILD FAILED — see hook stderr; "
                      "check the employer message directly")
    return (
        "TIME-SENSITIVE — INTERVIEW INVITE (surfaces once):\n"
        f"- {company} — {title}\n"
        f"- Invite logged: {_pt_str(event.get('ts'))}\n"
        f"- Employer message date: {msg_date_str}\n"
        f"- Deadline: in the employer message — reply windows are short\n"
        f"- {pack_line}"
    )


def pending_payload(events_path=None, surfaced_path=None):
    """JSON-serializable payload for the ping worker: rendered blocks +
    the ids to mark surfaced after the digest carries them."""
    items = []
    for iid, e in pending_invites(events_path, surfaced_path):
        details = e.get("details") or {}
        items.append({
            "id": iid,
            "ts": e.get("ts", ""),
            "role_id": e.get("role_id", ""),
            "company": e.get("company", ""),
            "message_date": details.get("message_date") or "",
            "prep_dir": details.get("prep_dir") or "",
            "block": render_block(e),
        })
    return {"count": len(items), "invites": items}


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Surface unsurfaced interview invites for the ping.")
    ap.add_argument("--events", default=None,
                    help="telemetry events.jsonl (default: canonical path)")
    ap.add_argument("--surfaced", default=None,
                    help="surfaced-ids file (default: hook state file)")
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--pending", action="store_true",
                       help="print JSON payload of unsurfaced invites")
    group.add_argument("--block", action="store_true",
                       help="print the rendered digest block(s) as text")
    group.add_argument("--mark", nargs="+", metavar="ID",
                       help="record invite ids as surfaced (exactly-once)")
    args = ap.parse_args(argv)

    if args.mark:
        ids = mark_surfaced(args.surfaced, args.mark)
        print(json.dumps({"surfaced_total": len(ids)}))
        return 0
    payload = pending_payload(args.events, args.surfaced)
    if args.block:
        blocks = [i["block"] for i in payload["invites"]]
        print(("\n\n" + "-" * 40 + "\n\n").join(blocks) if blocks else "")
        return 0
    print(json.dumps(payload, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
