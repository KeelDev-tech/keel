#!/usr/bin/env python3
"""Packet starvation watchdog: catches launch packets nobody picks up.

apply_loop writes launch packets and marks leads IN-FLIGHT; the executor
lane is supposed to log a browser_launched claim the moment a real browser
task spawns. If the claim never arrives, the packet is starving: it
suppresses apply_loop's in-flight accounting and the feeder watchdog's
"browser busy" window while the browser actually idles.

What this script checks (read-only first):
  (a) IN-FLIGHT queue entries (standard + needs_input queues) with NO
      browser_launched claim older than STARVE_AFTER_MIN minutes ->
      gate_encountered(packet_starved), deduped per gate (6h), so the
      status ping surfaces it. The operator then inspects and either
      launches the browser task (logging the claim) or clears the marker.
  (b) Hygiene report on stdout only (no events): orphaned root packets
      (packet file, no IN-FLIGHT queue entry — parked/discarded leads
      whose packets were never archived), IN-FLIGHT markers with no
      packet file, and browser_launched claims in the last 24h whose
      role_id is neither IN-FLIGHT nor SUBMITTED (the browser worked a
      lead the queue doesn't track).

What it does NOT do:
  - Stale IN-FLIGHT >2h (claimed or not) stays feeder_watchdog's job
    (gate=stale_inflight) — this script never re-emits it.
  - Never auto-resets queue state, never archives packets, never invents
    leads, never touches the ledger, never rewrites telemetry.
  - Never auto-launches browser tasks: the operator owns pickup.

Usage:
    python3 packet_watchdog.py            # check; emit one deduped event
    python3 packet_watchdog.py --dry-run  # report only, no event writes
    python3 packet_watchdog.py --explain <role_id> [--note <text>]
                                         # mark a claim-without-marker as
                                         # investigated (stops re-flagging)

STARVE_AFTER_MIN = 45: a real browser spawn logs its claim immediately,
so 45 minutes is generous pickup latency while still catching true
starvation inside one pulse cycle. INFLIGHT_CAP and the one-at-a-time
browser rule are the operator's decisions — this script never changes them.
"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
from keel_paths import DATA  # noqa: E402 — repo path convention
import log_event  # noqa: E402 — append-only telemetry

try:
    from status_matchers import is_inflight_status  # noqa
except ImportError:
    try:
        from feeder_watchdog import is_inflight_status  # noqa
    except ImportError:
        def is_inflight_status(status):  # fallback: canonical statuses
            return str(status or "").upper() in ("IN-FLIGHT", "INFLIGHT")

QUEUES = [os.path.join(DATA, "queues", "standard-queue.json"),
          os.path.join(DATA, "queues", "needs_input-queue.json")]
EVENTS = log_event.EVENTS  # canonical telemetry path (data/telemetry/events.jsonl)
PACKETS = os.path.join(DATA, "launch-packets")

STARVE_AFTER_MIN = 45
DEDUPE_HOURS = 6
GATE = "packet_starved"

# Explained-claims seen-list. The claim-without-marker hygiene line
# re-flagged the same investigated claim every run; claims that were
# investigated and explained are recorded here (role_id -> {reason,
# added_at}) and the watchdog stops re-flagging them. The underlying
# browser_launched events stay in telemetry, queryable — only the stdout
# re-flag is suppressed. Nothing about starvation detection changes.
SEEN_CLAIMS_PATH = os.path.join(DATA, ".state",
                                "packet-watchdog-seen-claims.json")


def load_seen_claims():
    """Explained claims: role_id -> {"reason": str, "added_at": iso}.
    Missing/corrupt file -> {} (fail-open: everything gets flagged,
    nothing is silently suppressed)."""
    try:
        with open(SEEN_CLAIMS_PATH) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def explain_claim(role_id, reason):
    """Mark a claim-without-marker as investigated and explained.
    Persists to the seen-list; future watchdog runs stop re-flagging it.
    Returns True."""
    seen = load_seen_claims()
    seen[role_id] = {"reason": reason,
                     "added_at": datetime.now(timezone.utc).isoformat()}
    os.makedirs(os.path.dirname(SEEN_CLAIMS_PATH), exist_ok=True)
    json.dump(seen, open(SEEN_CLAIMS_PATH, "w"), indent=1)
    return True


def load_queues():
    items = []
    for q in QUEUES:
        try:
            d = json.load(open(q))
        except (FileNotFoundError, json.JSONDecodeError):
            continue
        items.extend(d if isinstance(d, list)
                     else d.get("entries", d.get("items", [])))
    return items


def parse_ts(s):
    """Parse '2026-09-14 21:25 PDT' (or ISO) into an aware datetime."""
    if not s:
        return None
    s = str(s).strip()
    try:
        if s.endswith(" PDT"):
            dt = datetime.strptime(s[:-4], "%Y-%m-%d %H:%M")
            return dt.replace(tzinfo=timezone(timedelta(hours=-7)))
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def claimed_role_ids():
    """Role ids with a browser_launched claim event. Fail-open on read
    errors (empty set -> everything reads unclaimed and the watcher fires
    honestly) rather than dying mid-run."""
    claimed = set()
    try:
        with open(EVENTS) as f:
            for line in f:
                if "browser_launched" not in line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                rid = ev.get("role_id")
                if rid:
                    claimed.add(rid)
    except (FileNotFoundError, OSError) as ex:
        print(f"  warn: claim check could not read telemetry ({ex}); "
              f"treating all IN-FLIGHT as unclaimed", file=sys.stderr)
    return claimed


def recent_gate_event(gate, within_hours):
    """Gate-level dedupe: True if a gate_encountered/gate_blocked event
    with this gate name was logged within the window."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=within_hours)
    try:
        with open(EVENTS) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if ev.get("event_type") not in ("gate_encountered",
                                                "gate_blocked"):
                    continue
                if (ev.get("details") or {}).get("gate") != gate:
                    continue
                try:
                    ts = datetime.fromisoformat(ev["ts"])
                except (KeyError, ValueError):
                    continue
                if ts >= cutoff:
                    return True
    except FileNotFoundError:
        pass
    return False


def main():
    dry_run = "--dry-run" in sys.argv
    if "--explain" in sys.argv:
        idx = sys.argv.index("--explain")
        try:
            rid = sys.argv[idx + 1]
        except IndexError:
            print("usage: packet_watchdog.py --explain <role_id> "
                  "[--note <text>]", file=sys.stderr)
            return 2
        note = ""
        if "--note" in sys.argv:
            nidx = sys.argv.index("--note")
            if nidx + 1 < len(sys.argv):
                note = sys.argv[nidx + 1]
        explain_claim(rid, note)
        print(f"explained: {rid} (added to seen-list)")
        return 0
    now = datetime.now(timezone.utc)
    items = load_queues()
    inflight = [e for e in items if is_inflight_status(e.get("status"))]
    claimed = claimed_role_ids()

    starving, fresh_unclaimed = [], []
    for e in inflight:
        rid = e.get("role_id", "")
        if rid in claimed:
            continue
        ts = parse_ts(e.get("in_flight_at")) or parse_ts(e.get("status_updated"))
        if ts and now - ts >= timedelta(minutes=STARVE_AFTER_MIN):
            starving.append((e, ts))
        else:
            fresh_unclaimed.append((e, ts))

    # Hygiene: orphaned root packets (file on disk, no IN-FLIGHT entry) and
    # IN-FLIGHT markers with no packet file. Stdout only — no events.
    # (Buffered prefetch packets live in launch-packets/buffer/ — NOT the
    # root — so they never appear here.)
    packet_files = {}
    try:
        for fn in os.listdir(PACKETS):
            if fn.endswith(".json") and os.path.isfile(os.path.join(PACKETS, fn)):
                packet_files[fn[:-5]] = os.path.join(PACKETS, fn)
    except FileNotFoundError:
        pass
    inflight_ids = {e.get("role_id") for e in inflight}
    orphaned = sorted(rid for rid in packet_files if rid not in inflight_ids)
    marker_no_packet = sorted(rid for rid in inflight_ids
                              if rid and rid not in packet_files)

    # Reverse desync (stdout only): browser_launched claims in the last 24h
    # for role_ids that are neither IN-FLIGHT nor SUBMITTED — the browser
    # worked a lead the queue doesn't track.
    ledger_submitted = set()
    try:
        rows = json.load(open(os.path.join(DATA, "application-ledger.json")))
        rows = rows if isinstance(rows, list) else rows.get("rows", [])
        ledger_submitted = {r.get("role_id") for r in rows
                            if str(r.get("status", "")).upper() == "SUBMITTED"}
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    cutoff = now - timedelta(hours=24)
    claim_no_marker = set()
    try:
        with open(EVENTS) as f:
            for line in f:
                if "browser_launched" not in line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                rid = ev.get("role_id")
                if not rid:
                    continue
                try:
                    ts = datetime.fromisoformat(ev.get("ts", ""))
                except (ValueError, TypeError):
                    continue
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                if (ts >= cutoff and rid not in inflight_ids
                        and rid not in ledger_submitted):
                    claim_no_marker.add(rid)
    except (FileNotFoundError, OSError):
        pass

    # Explained claims (seen-list) are not re-flagged. The claim stays in
    # telemetry, queryable — only the per-run stdout re-flag is suppressed.
    seen = load_seen_claims()
    unexplained = sorted(rid for rid in claim_no_marker if rid not in seen)
    explained_n = len(claim_no_marker) - len(unexplained)

    print(f"IN-FLIGHT: {len(inflight)} "
          f"(claimed {len(inflight) - len(starving) - len(fresh_unclaimed)}, "
          f"fresh-unclaimed {len(fresh_unclaimed)}, "
          f"starving {len(starving)}) | "
          f"orphaned packets: {len(orphaned)} | "
          f"markers w/o packet: {len(marker_no_packet)} | "
          f"claims w/o marker (24h): {len(unexplained)}"
          + (f" | explained (seen, not re-flagged): {explained_n}"
             if explained_n else ""))
    for e, ts in starving:
        age_min = int((now - ts).total_seconds() // 60)
        print(f"  STARVING: {e.get('role_id')} "
              f"(marked {age_min} min ago, no browser_launched claim)")
    for rid in unexplained:
        print(f"  CLAIM-WITHOUT-MARKER: {rid} "
              f"(browser_launched in last 24h, not IN-FLIGHT, not submitted)")

    if not starving:
        print("no packet starvation — silent")
        return 0

    details = {"gate": GATE,
               "starved_role_ids": [e.get("role_id") for e, _ in starving][:10],
               "starved_count": len(starving),
               "starve_after_min": STARVE_AFTER_MIN,
               "note": ("launch packets marked IN-FLIGHT but never picked up "
                        "by the browser lane; operator should launch "
                        "(logging the claim) or clear the markers")}
    if dry_run:
        print("dry-run: would emit packet_starved gate_encountered")
        return 0
    if recent_gate_event(GATE, DEDUPE_HOURS):
        print(f"packet_starved event already logged within {DEDUPE_HOURS}h "
              f"— deduped, not re-emitting")
        return 0
    log_event.log("gate_encountered", role_id=details["starved_role_ids"][0],
                  company="", ats="", source="packet_watchdog",
                  details=details)
    print("emitted packet_starved gate_encountered")
    return 0


if __name__ == "__main__":
    sys.exit(main())
