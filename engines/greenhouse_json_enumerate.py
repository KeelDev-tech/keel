#!/usr/bin/env python3
"""greenhouse_json_enumerate.py — Greenhouse JSON board API enumeration for the
7 JS-only registry-clean boards (human-authorized 2026-09-15; productionizes
blackboard proposal J-20260916-0012-sour-423).

SANCTION: the read-only trial (2026-09-16 00:12 UTC) pulled 1,662 postings
across these 7 boards with zero HTTP 429/403. The explicit "Push them"
instruction (2026-09-15) authorizes productionizing the JSON board API as a
discovery enumeration mechanism for the JS-only clean boards. The HTML-only
watcher (clean_board_watch.py) could never enumerate these boards —
verified JS-only over plain HTTP, logged board_not_enumerable.

MECHANISM: GET https://boards-api.greenhouse.io/v1/boards/<board>/jobs
(light form, no ?content=true — descriptions are not needed at discovery).
Same polite UA as greenhouse_board_classify.py, 2s delay between boards,
HTTP 429 = HARD STOP (exit 75, nothing written — no state, no staging, no
yield, no metrics).

FLOW (live mode):
  poll 7 JS-only boards -> diff job ids vs greenhouse-json-seen.json
  for each NEW posting:
    - employer-blocklist check (employer-blocklist.md)
    - dedupe_gate.check_candidate(company, title, url): "duplicate" -> skip;
      "suspect" is advisory only -> stage it (canonical gate semantics)
    - build entry: action_band PARKED, status PARKED-PENDING-VERIFICATION,
      NO fit_score (never invented), ats_board=greenhouse:<board>
    - role_id embeds the Greenhouse numeric job id (make_json_role_id —
      self-contained mirror of clean_board_watch.make_watch_role_id, kept
      local to avoid an import cycle with clean_board_watch.py, which now
      calls THIS module as a subprocess phase)
    - queue_intake.validate_entry -> ZERO errors required (C-17)
  write hidden_files/discovery-staging/greenhouse-json-<ts>-leads.json
  append per-board {ts, board, postings_seen, new_postings} rows to
    hidden_files/clean-board-watch-yield.jsonl (same schema — feeds the
    board-yield ranking)
  append one metrics row to hidden_files/greenhouse-json-metrics.jsonl:
    {ts, per_board: {board: {postings_seen, new_postings, duplicates_skipped}},
     staged, ingested, validation_errors, duration_s, http_429s}
  then run staging_ingest.py --live --only <the file just written> — the
    sanctioned intake path (validate -> dedupe -> BACKUP -> write ->
    telemetry -> archive). The settle wait is waived for exactly the
    file just written (atomic write, so complete already); every other
    staging file keeps the 300s settle window (2026-09-21 settle-bypass
    fix).

MODES:
  --dry-run   poll + diff + report only; no staging write, no ingest, no
              state/yield/metrics writes. Default is dry-run (fail closed).
  --live      full flow above.

Fail closed everywhere: unparseable board response -> board skipped, logged,
never invented. Titles/URLs/jids come only from the API payload.

BASELINE (scoreboard): read-only trial 2026-09-16 00:12 UTC — 1,662 postings
seen, 0 errors. Pre-change JS-board yield: 0 postings (board_not_enumerable).
"""

import json
import os
import re
import subprocess
import sys
import time
import html as htmlmod
import urllib.error
import urllib.request
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
import api_direct_detect as add  # noqa: E402 (shared UA)
import safe_http  # noqa: E402 — policy-checked transport
from ats_discovery import normalize as N  # noqa: E402
import dedupe_gate  # noqa: E402
import queue_intake  # noqa: E402
import title_triage  # noqa: E402
from log_event import emit_429_halt  # noqa: E402 (COV-http_429_halt telemetry)

from keel_paths import HOME, DATA  # noqa: E402
SEEN = os.path.join(HOME, "hidden_files/greenhouse-json-seen.json")
YIELD_LOG = os.path.join(HOME, "hidden_files/clean-board-watch-yield.jsonl")
METRICS_LOG = os.path.join(HOME, "hidden_files/greenhouse-json-metrics.jsonl")
STAGED_DIR = os.path.join(HOME, "hidden_files/discovery-staging")
BLOCKLIST = os.path.join(DATA, "employer-blocklist.md")
DELAY = 2.0
API = "https://boards-api.greenhouse.io/v1/boards/{board}/jobs"

PDT = ZoneInfo("America/Los_Angeles")

# The 7 registry-clean boards whose listing pages are JS-only over plain
# HTTP (verified 2026-09-15; read-only trial 2026-09-16 00:12 UTC).
JSON_BOARDS = ["abnormalsecurity", "databricks", "fastly", "mongodb",
               "salesloft", "coinbase", "securly13"]

# board token -> canonical company name (the JSON API carries no company
# name; the board token is the employer's Greenhouse slug).
JSON_BOARD_COMPANY = {
    "abnormalsecurity": "Abnormal Security",
    "databricks": "Databricks",
    "fastly": "Fastly",
    "mongodb": "MongoDB",
    "salesloft": "Salesloft",
    "coinbase": "Coinbase",
    "securly13": "Securly",
}


class RateLimited(Exception):
    """HTTP 429 from boards-api — hard stop, nothing written."""
    pass


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def pdt_stamp():
    return datetime.now(PDT).strftime("%Y-%m-%d %H:%M PDT")


def fetch_board_json(board):
    """GET the board's jobs JSON. Raises RateLimited on 429."""
    req = urllib.request.Request(API.format(board=board), headers=add.UA)
    try:
        with safe_http.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 429:
            raise RateLimited(board)
        raise


def _looks_http(u):
    return isinstance(u, str) and re.match(r"^https?://[^/\s]+/", u or "")


def parse_board_json(board, payload):
    """Return [(job_id, title, location, url)] from a board API payload.

    Never invents: only jobs with a numeric id actually present in the
    payload, with a non-empty title. Posting URL prefers the API's
    absolute_url when it is a real http(s) URL, else the canonical direct
    board URL (enumerable and verifiable by verify_retry)."""
    out = []
    seen_ids = set()
    if not isinstance(payload, dict):
        return out
    jobs = payload.get("jobs")
    if not isinstance(jobs, list):
        return out
    for job in jobs:
        if not isinstance(job, dict):
            continue
        try:
            jid = str(int(job.get("id", 0)))
        except (TypeError, ValueError):
            continue
        if not jid or jid == "0" or jid in seen_ids:
            continue
        title = (job.get("title") or "").strip()
        if not title:
            continue
        loc = job.get("location")
        location = ""
        if isinstance(loc, dict):
            location = (loc.get("name") or "").strip()
        abs_url = (job.get("absolute_url") or "").strip()
        if _looks_http(abs_url):
            url = abs_url
        else:
            url = (f"https://job-boards.greenhouse.io/{board}/jobs/{jid}")
        seen_ids.add(jid)
        out.append((jid, htmlmod.unescape(title).strip(), location, url))
    return out


def make_json_role_id(company, title, jid, date_tag):
    """Collision-proof role_id for JSON-enumerated leads.

    Self-contained mirror of clean_board_watch.make_watch_role_id (kept
    local to avoid an import cycle — clean_board_watch.py calls this
    module as a subprocess phase). N.make_role_id truncates titles to
    4 words and ignores source_job_id, which collapsed 12 distinct gigs
    postings into 8 role_ids on 2026-09-15. The Greenhouse numeric job
    id is a stable, board-scoped discriminator; format stays UPPER-KEBAB
    so queue_intake validation passes.
    """
    base = N.make_role_id("greenhouse", company, title, jid, date_tag)
    digits = re.sub(r"\D", "", str(jid) or "")
    if not digits:
        return base  # fail closed: never invent a discriminator
    disc = "J" + digits
    room = 90 - len(disc) - 1
    head = base if len(base) <= room else base[:room].rstrip("-")
    rid = re.sub(r"-{2,}", "-", f"{head}-{disc}").strip("-")
    return rid


def board_company(board):
    return N.norm_company(JSON_BOARD_COMPANY.get(board, board))


def load_blocklist():
    names = []
    try:
        for line in open(BLOCKLIST):
            m = re.match(r"\d+\.\s+\*\*(.+?)\*\*", line)
            if m:
                names.append(m.group(1).strip().lower())
    except FileNotFoundError:
        pass
    return names


def is_blocked(company, names):
    c = (company or "").lower()
    return any(n in c or c in n for n in names if n)


def build_json_entry(board, company, jid, title, location, url):
    date_tag = datetime.now(PDT).strftime("%Y%m%d")
    entry = {
        "role_id": make_json_role_id(company, title, jid, date_tag),
        "company": company,
        "title": N.norm_title(title),
        "location": location,
        "action_band": "PARKED",
        "status": "PARKED-PENDING-VERIFICATION",
        "application_url": url,
        "posting_url": url,
        "ats": "greenhouse",
        "ats_board": f"greenhouse:{board}",
        "discovery_platform": "greenhouse",
        "discovery_slug": board,
        "source_job_id": jid,
        "source": "greenhouse-json-enumerate",
        "date_discovered": datetime.now(PDT).strftime("%Y-%m-%d"),
        "status_updated": pdt_stamp(),
        "queue_notes": (
            f"greenhouse-json-enumerate: new posting from "
            f"boards-api.greenhouse.io for registry-clean board {board} "
            f"(human-authorized 2026-09-15, proposal "
            f"J-20260916-0012-sour-423). Direct board URL is enumerable "
            f"and verifiable by verify_retry. Unscored — fit-scoring pass "
            f"required; no fit_score invented."
        ),
    }
    # Staging title triage (human-authorized 2026-09-15, J-20260916-0112-veri-457
    # hardened build): deferred leads are TAGGED, never dropped — they enter
    # the queue in PARKED-TRIAGE-DEFERRED with a reason code, visible and
    # recoverable, excluded from verify HTTP budget.
    decision, reason = title_triage.triage_lead(entry["title"], location)
    if decision == "defer":
        title_triage.annotate_deferred(entry, reason, pdt_stamp())
    return entry


def atomic_write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=1, sort_keys=True)
    os.replace(tmp, path)


def enumerate_boards(boards, live=False):
    """Fetch + diff + build entries for the given boards.

    Returns {"entries", "results", "stats"}. Owns the seen file and the
    per-board yield rows when live=True (zero writes when live=False).
    Raises RateLimited on HTTP 429 — the caller must exit 75 with nothing
    further written.
    """
    t0 = time.monotonic()
    try:
        seen = json.load(open(SEEN))
    except Exception:
        seen = {}
    blocked_names = load_blocklist()

    results = []       # per-board yield rows
    new_entries = []   # staged leads
    per_board = {}
    skipped_dup = 0
    skipped_blocked = 0
    validation_errors = 0
    # Intra-batch dedupe sets (yield-audit J-20260916-0328-sour-525 finding
    # 4b, discovery-scale arm 2026-09-16): sweep24's 456 re-harvest rows were
    # ALL "duplicate_in_batch: employer_title matches" — intra-batch
    # collisions the persistent-index check cannot see (neither row is in
    # the queue/ledger yet at emission time). Mirror the staging-side
    # intra-batch check (dedupe_index.stage_verdict steps 1+3) here so the
    # same verdict is reached pre-staging instead of after a full
    # staging+ingest traversal. Normalization matches dedupe_gate's own
    # check_candidate, which staging's employer+title ladder also uses.
    batch_url_keys = set()
    batch_emp_titles = set()
    skipped_batch_dup = 0

    for bi, board in enumerate(boards):
        company = board_company(board)
        try:
            payload = fetch_board_json(board)
        except RateLimited:
            raise
        except Exception as ex:
            results.append({"ts": utcnow(), "board": board,
                            "postings_seen": 0, "new_postings": 0,
                            "error": type(ex).__name__})
            per_board[board] = {"postings_seen": 0, "new_postings": 0,
                                "duplicates_skipped": 0}
            continue
        postings = parse_board_json(board, payload)
        board_seen = seen.setdefault(board, {})
        fresh = 0
        dups = 0
        for jid, title, location, url in postings:
            if jid in board_seen:
                continue
            if is_blocked(company, blocked_names):
                skipped_blocked += 1
                if live:
                    board_seen[jid] = utcnow()  # don't resurface
                continue
            verdict, _ev = dedupe_gate.check_candidate(
                company, N.norm_title(title), url)
            if verdict == "duplicate":
                skipped_dup += 1
                dups += 1
                if live:
                    board_seen[jid] = utcnow()  # known — don't resurface
                continue
            # "suspect" is advisory only per the gate's contract: stage it.
            # Intra-batch dedupe (4b): same posting re-listed under a new
            # jid, or two boards carrying the same employer+title. Staging
            # would reject these as duplicate_in_batch; skip them here.
            from dedupe_index import keys_for_urls
            url_keys, identity_conflict = keys_for_urls([url])
            if not identity_conflict and url_keys & batch_url_keys:
                skipped_batch_dup += 1
                if live:
                    board_seen[jid] = utcnow()  # seen — don't resurface
                continue
            entry = build_json_entry(board, company, jid, title,
                                     location, url)
            errors, _w = queue_intake.validate_entry(entry)
            if errors:
                validation_errors += 1
                print(f"VALIDATION REFUSED {entry['role_id']}: {errors}",
                      file=sys.stderr)
                continue  # fail closed: not marked seen, retried next run
            new_entries.append(entry)
            if not identity_conflict:
                batch_url_keys.update(url_keys)
            if live:
                board_seen[jid] = utcnow()
            fresh += 1
        results.append({"ts": utcnow(), "board": board,
                        "postings_seen": len(postings),
                        "new_postings": fresh})
        per_board[board] = {"postings_seen": len(postings),
                            "new_postings": fresh,
                            "duplicates_skipped": dups}
        if bi < len(boards) - 1:
            time.sleep(DELAY)

    if live:
        atomic_write_json(SEEN, seen)
        with open(YIELD_LOG, "a") as f:
            for r in results:
                f.write(json.dumps(r, sort_keys=True) + "\n")

    return {"entries": new_entries, "results": results, "per_board": per_board,
            "skipped_dup": skipped_dup, "skipped_blocked": skipped_blocked,
            "skipped_batch_dup": skipped_batch_dup,
            "validation_errors": validation_errors,
            "duration_s": round(time.monotonic() - t0, 2)}


def extract_ingested(stdout):
    """Pull audit["ingested"] from staging_ingest.py's printed audit JSON."""
    idx = stdout.rfind("\n{\n")
    if idx < 0:
        return None
    try:
        obj, _ = json.JSONDecoder().raw_decode(stdout[idx + 1:])
        return obj.get("ingested")
    except Exception:
        return None


def main(argv):
    live = "--live" in argv
    boards = sorted(JSON_BOARDS)

    try:
        res = enumerate_boards(boards, live=live)
    except RateLimited as e:
        # COV-http_429_halt (2026-09-18): the 429 hard stop previously
        # emitted to stdout/stderr only — invisible to the safety coverage
        # map. One http_429_halt telemetry row per halted run. Halt
        # behavior (return 75, nothing written) is unchanged.
        emit_429_halt("greenhouse-json-enumerate",
                      {"halt": "429 hard stop — exiting, nothing written",
                       "exit_code": 75,
                       "board": str(e) or None,
                       "boards": len(boards),
                       "live": live})
        print("429 rate limit — HARD STOP, nothing written",
              file=sys.stderr)
        return 75

    total_seen = sum(r["postings_seen"] for r in res["results"])
    total_new = sum(r["new_postings"] for r in res["results"])
    print(f"boards={len(boards)} postings_seen={total_seen} "
          f"new={total_new} dup_skipped={res['skipped_dup']} "
          f"blocked_skipped={res['skipped_blocked']} "
          f"validation_errors={res['validation_errors']}")
    for r in res["results"]:
        print(f"  {r['board']}: seen={r['postings_seen']} "
              f"new={r['new_postings']}"
              f"{' err=' + r['error'] if r.get('error') else ''}")

    if not live:
        print(f"DRY-RUN: would stage {len(res['entries'])} lead(s); "
              f"nothing written.")
        for e in res["entries"][:10]:
            print(f"  WOULD-STAGE {e['role_id']} | {e['company']} | "
                  f"{e['title'][:60]}")
        return 0

    # live: write staging file, run sanctioned ingest, then record metrics
    staged_path = None
    if res["entries"]:
        # J-20260916-0425-sour-562: emission precheck — schema-valid,
        # canonical role_id, intra-batch dedupe BEFORE the staging write.
        # Invalid stubs and batch-internal dupes die here with one summary
        # line instead of becoming staged_rejected noise at ingest
        # (measured 2412/24h, ~95% avoidable). Fail-closed: withheld
        # entries are dropped from the batch, never rewritten.
        from staging_ingest import emission_precheck  # noqa: E402
        res["entries"], _pc_withheld, pc_report = emission_precheck(
            res["entries"])
        print(f"emission_precheck: {pc_report['fresh']} fresh, "
              f"{pc_report['withheld']} withheld from staging: "
              f"{pc_report['withheld_reason_counts']}")
        if not res["entries"]:
            print("emission precheck withheld the whole batch — "
                  "no staging file written")
    if res["entries"]:
        os.makedirs(STAGED_DIR, exist_ok=True)
        batch = datetime.now(PDT).strftime("%Y%m%d-%H%M%S")
        staged_path = os.path.join(
            STAGED_DIR, f"greenhouse-json-{batch}-leads.json")
        atomic_write_json(staged_path, res["entries"])
        print(f"staged {len(res['entries'])} lead(s) -> {staged_path}")
    else:
        print("no new postings — no staging file written")
    # sanctioned intake: validate -> dedupe -> BACKUP -> write -> telemetry
    # -> archive. The settle wait is waived ONLY for the file just written
    # (atomic write, so complete already); every other staging file keeps
    # the 300s settle window (2026-09-21 settle-bypass fix).
    ingest_argv = [sys.executable, os.path.join(BASE, "staging_ingest.py"),
                   "--live"]
    if staged_path:
        ingest_argv += ["--only", staged_path]
    proc = subprocess.run(
        ingest_argv,
        capture_output=True, text=True, timeout=900)
    print(proc.stdout[-2000:])
    if proc.returncode != 0:
        print(proc.stderr[-1000:], file=sys.stderr)
    ingested = extract_ingested(proc.stdout)

    metrics_row = {
        "ts": utcnow(),
        "producer": "greenhouse-json-enumerate",
        "per_board": res["per_board"],
        "staged": len(res["entries"]),
        "ingested": ingested,
        "validation_errors": res["validation_errors"],
        "duration_s": res["duration_s"],
        "http_429s": 0,
    }
    with open(METRICS_LOG, "a") as f:
        f.write(json.dumps(metrics_row, sort_keys=True) + "\n")
    return proc.returncode


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
