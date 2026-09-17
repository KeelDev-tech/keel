#!/usr/bin/env python3
"""verify_cron.py — standing verify-cadence runner for the pending-verification pool.

Feeder-independent standing owner of the pending-verification pool: it
invokes verify_retry.py on a fixed cadence with cron-safe defaults and an
observable run record.

BOUNDARIES (inherited from verify_retry.py):
- HTTP-only worker; never touches the browser slot (safe to run alongside
  a live application — the one-at-a-time browser rule is unaffected).
- Dry-run by default (--live to apply). Dry runs touch NOTHING: no queue
  writes, no telemetry, no cursor movement (verify_retry's own convention).
- Respects verify_retry's 24h per-lead retry cooldown (last_verify_attempt)
  — the gate lives inside verify_retry; this script invokes it, never
  reimplements promotion logic.
- 429 / rate-limit: HTTP checks inside verify_retry fail closed to
  "ambiguous" (stays parked). This runner does not retry internally.
- Concurrency: a PID lockfile prevents overlap with another invocation of
  verify_retry.py or a previous cron tick (stale >2h locks are taken over,
  logged in the run record).

Usage:
    python3 verify_cron.py                 # dry-run tick: what WOULD happen
    python3 verify_cron.py --live           # apply (queue moves + telemetry)
    python3 verify_cron.py --live --limit 25
    python3 verify_cron.py --include-url-less   # include the URL-less
                                               # sub-pool (default tick is
                                               # --url-bearing-only: the
                                               # URL-less pool's enrichment
                                               # yield is low — weekly
                                               # full-scan slot only)
    python3 verify_cron.py --self-check     # decision audit against the
                                            # live pool. Read-only.

The cron spec (schedule enablement) is a parent-authorized step — enabling
it is not this module's job.
"""

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
from keel_paths import DATA  # noqa: E402

PDT = ZoneInfo("America/Los_Angeles")
HIDDEN = os.path.join(DATA, "hidden_files")
LOCK_PATH = os.path.join(HIDDEN, "verify_cron.lock")
RUNS_LOG = os.path.join(HIDDEN, "verify_cron_runs.jsonl")
CURSOR_PATH = os.path.join(HIDDEN, "verify_retry_cursor.json")
VERIFY_RETRY = os.path.join(_HERE, "verify_retry.py")

LOCK_STALE_S = 2 * 3600
DEFAULT_LIMIT = 40  # cursor rotation + 24h per-lead cooldown means one tick
                    # covers the eligible slice without waste.


def _now_iso():
    return datetime.now(PDT).isoformat()


def acquire_lock():
    """Exclusive lock. Returns True on acquisition, False if live holder."""
    try:
        with open(LOCK_PATH) as f:
            data = json.load(f)
        pid = data.get("pid")
        ts = data.get("ts", 0)
        alive = False
        if pid:
            try:
                os.kill(pid, 0)
                alive = True
            except (OSError, ProcessLookupError):
                alive = False
        if alive and (time.time() - ts) < LOCK_STALE_S:
            return False
        print(f"lock: previous holder {pid} "
              f"({'stale' if alive else 'dead'}) — taking over")
    except (FileNotFoundError, ValueError, OSError):
        pass
    os.makedirs(os.path.dirname(LOCK_PATH), exist_ok=True)
    json.dump({"pid": os.getpid(), "ts": time.time(),
               "started": _now_iso()},
              open(LOCK_PATH, "w"))
    return True


def release_lock():
    try:
        os.remove(LOCK_PATH)
    except OSError:
        pass


def load_cursor():
    try:
        return json.load(open(CURSOR_PATH))
    except Exception:
        return {}


def record_run(record):
    os.makedirs(os.path.dirname(RUNS_LOG), exist_ok=True)
    with open(RUNS_LOG, "a") as f:
        f.write(json.dumps(record) + "\n")


def parse_output(out):
    """Best-effort parse of verify_retry.py's human-readable output into a
    machine-readable counts dict. Wrapped in try/except by the caller —
    a parse failure must never fail the tick."""
    counts = {}
    m = re.search(r"candidates:\s*(\d+)\s*\|\s*queue dupes to reconcile:\s*(\d+)"
                  r"\s*\|\s*in cooldown:\s*(\d+)", out)
    if m:
        counts["candidates"] = int(m.group(1))
        counts["dupes"] = int(m.group(2))
        counts["cooldown_skipped"] = int(m.group(3))
    m = re.search(r"applied:\s*promoted=(\d+)\s*dead=(\d+)\s*held=(\d+)\s*"
                  r"reconciled=(\d+)\s*left_parked=(\d+)", out)
    if m:
        counts["promoted"] = int(m.group(1))
        counts["dead"] = int(m.group(2))
        counts["held"] = int(m.group(3))
        counts["reconciled"] = int(m.group(4))
        counts["left_parked"] = int(m.group(5))
    verdicts = re.findall(r"\] ([A-Za-z0-9-]+): (LIVE|DEAD|AMBIGUOUS|NO URL|ENRICHED)",
                          out)
    if verdicts:
        counts["verdicts"] = {rid: v for rid, v in verdicts}
    return counts


def self_check():
    """Decision audit (read-only): confirm the verify decision surface is
    healthy against the live pool.

    Checks (all derived from the live pool each run — composition drift
    shows up as FAIL, never a silent pass):
      E1. verify_retry loads a non-empty pending-verification pool and the
          decision helpers (is_verify_only, in_cooldown) evaluate without
          error.
      E2. Every currently scannable candidate is genuinely parked: no
          candidate that is verification-only and outside cooldown exists
          without a liveness verdict path (the pool is draining, not stuck).
      E3. The cursor file, when present, parses to an integer cursor.

    Returns 0 on pass, 1 on fail. Never writes anything.
    """
    import verify_retry as vr
    ok = True
    notes = []

    std, ni = vr.load(vr.STD_Q), vr.load(vr.NI_Q)
    std_cands = [e for e in std
                 if (e.get("status") or "").upper()
                 == "PARKED-PENDING-VERIFICATION"]
    ni_cands = [e for e in ni if vr.is_verify_only(e)]
    notes.append(f"pool: {len(std_cands)} pending-verification (standard), "
                 f"{len(ni_cands)} verify-only (needs_input)")
    if not std_cands and not ni_cands:
        ok = False
        notes.append("FAIL E1: pending pool is empty — verify cadence has "
                     "nothing to own")

    try:
        for e in std_cands + ni_cands:
            vr.in_cooldown(e)
        notes.append("decision helpers evaluate without error")
    except Exception as ex:
        ok = False
        notes.append(f"FAIL E1: decision helper raised: {ex}")

    cur = load_cursor()
    if cur and not isinstance(cur.get("cursor"), int):
        ok = False
        notes.append("FAIL E3: cursor file does not parse to an integer")
    else:
        notes.append(f"cursor: {cur.get('cursor', 0)}")

    for n in notes:
        print("  " + n)
    print("SELF-CHECK: " + ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


def run_tick(live, limit, include_url_less):
    if not acquire_lock():
        print("verify_cron: another verify run holds the lock — skipping tick")
        record_run({"ts": _now_iso(), "live": live, "limit": limit,
                    "status": "skipped_lock"})
        return 2
    cursor_before = load_cursor()
    cmd = [sys.executable, VERIFY_RETRY, "--limit", str(limit)]
    if not include_url_less:
        cmd.append("--url-bearing-only")
    if live:
        cmd.append("--live")
    print("verify_cron: " + " ".join(cmd))
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    except subprocess.TimeoutExpired:
        release_lock()
        print("verify_cron: verify_retry.py timed out (600s) — hard stop")
        record_run({"ts": _now_iso(), "live": live, "limit": limit,
                    "status": "timeout"})
        return 3
    finally:
        release_lock()
    out = r.stdout or ""
    err_tail = (r.stderr or "")[-400:]
    print(out[-2000:] if len(out) > 2000 else out)
    if err_tail:
        print(f"[stderr] {err_tail}", file=sys.stderr)

    try:
        counts = parse_output(out)
    except Exception as ex:
        counts = {"parse_error": str(ex)}

    cursor_after = load_cursor()
    cursor_moved = cursor_after.get("cursor") != cursor_before.get("cursor")
    if not live and cursor_moved:
        print("VERIFY-CRON INVARIANT VIOLATION: dry run moved the pool cursor!")
        counts["invariant_violation"] = "cursor_moved_on_dry_run"

    status = "ok" if r.returncode == 0 else f"exit_{r.returncode}"
    record = {"ts": _now_iso(), "live": live, "limit": limit,
              "include_url_less": include_url_less,
              "status": status, "counts": counts}
    record_run(record)

    # Observable run record in telemetry — --live only (dry runs emit
    # nothing, per verify_retry convention). Source is "verify-cron" (the
    # tick wrapper), NOT "verify-retry": verify_retry already logs its own
    # scan_summary, and downstream counters keying on source=verify-retry
    # must not double-count.
    if live and r.returncode == 0:
        try:
            import log_event
            log_event.log("verify_cron_run", role_id="", company="",
                          source="verify-cron",
                          details={"limit": limit,
                                   "include_url_less": include_url_less,
                                   "status": status,
                                   "counts": counts})
        except Exception as ex:
            print(f"telemetry: verify_cron_run emission failed: {ex}",
                  file=sys.stderr)

    summary_bits = []
    for k in ("candidates", "cooldown_skipped", "promoted", "held",
              "dead", "left_parked"):
        if k in counts:
            summary_bits.append(f"{k}={counts[k]}")
    print("verify_cron tick: " + (" ".join(summary_bits) or status)
          + f" (run record -> {RUNS_LOG})")
    return r.returncode


USAGE = """usage: verify_cron.py [--live] [--limit N] [--include-url-less] [--self-check] [--help]

Standing verify-cadence tick for the pending-verification pool.
  --live              apply queue moves + telemetry (default: dry-run, read-only)
  --limit N           cap candidates scanned per tick (default 40)
  --include-url-less  include the URL-less sub-pool (default: --url-bearing-only)
  --self-check        decision audit against the live pool; read-only
  --help, -h          this help
"""


def main():
    if "--help" in sys.argv or "-h" in sys.argv:
        print(USAGE)
        return 0
    if "--self-check" in sys.argv:
        return self_check()
    live = "--live" in sys.argv
    limit = DEFAULT_LIMIT
    if "--limit" in sys.argv:
        limit = int(sys.argv[sys.argv.index("--limit") + 1])
    include_url_less = "--include-url-less" in sys.argv
    return run_tick(live, limit, include_url_less)


if __name__ == "__main__":
    sys.exit(main())
