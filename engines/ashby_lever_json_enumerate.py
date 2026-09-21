#!/usr/bin/env python3
"""ashby_lever_json_enumerate.py — Ashby + Lever JSON board API enumeration for
registry-clean boards (human-approved 2026-09-16 ~12:55 PDT, proposal
P-2026-09-16-discovery-1: hourly Ashby/Lever source-inversion JSON watch).

SANCTION: both transports are already exercised by the census3x biweekly
universe (api.ashbyhq.com posting-api + api.lever.co v0/postings) with zero
429s. This module productionizes them as a continuous hourly watch — the
same clean-board-watch pattern as greenhouse_json_enumerate.py (seen-diff +
dedupe_gate + staging_ingest), called as a subprocess phase by
clean_board_watch.py AFTER the Greenhouse JSON phase (a 429 here propagates
as exit 75 before any HTML-phase writes).

MECHANISM:
  Ashby: GET https://api.ashbyhq.com/posting-api/job-board/<board>
         payload {"jobs": [{"id" (uuid), "title", "location", "jobUrl",
         "isListed", ...}]} — only isListed jobs are enumerated.
  Lever: GET https://api.lever.co/v0/postings/<org>?mode=json
         payload [{"id", "text" (title), "categories": {"location"},
         "hostedUrl", ...}]
Same polite UA as the rest of the discovery stack, 2s delay between boards,
HTTP 429 = HARD STOP (exit 75, nothing written — no state, no staging, no
yield, no metrics).

FLOW (live mode):
  poll registry-clean Ashby/Lever boards -> diff job ids vs
    ashby-lever-json-seen.json
  for each NEW posting:
    - employer-blocklist check (employer-blocklist.md)
    - dedupe_gate.check_candidate(company, title, url): "duplicate" -> skip;
      "suspect" is advisory only -> stage it (canonical gate semantics)
    - intra-batch employer+title dedupe (same 4b rationale as
      greenhouse_json_enumerate)
    - build entry: action_band PARKED, status PARKED-PENDING-VERIFICATION,
      NO fit_score (never invented), ats_board=ashby:<board>/lever:<org>
    - role_id embeds a job-id discriminator (Ashby UUID / Lever id) so
      N.make_role_id's title truncation can never collapse distinct postings
    - queue_intake.validate_entry -> ZERO errors required (C-17)
  write hidden_files/discovery-staging/ashby-lever-json-<ts>-leads.json
  append per-board {ts, board, postings_seen, new_postings} rows to
    hidden_files/clean-board-watch-yield.jsonl (SAME schema as the Greenhouse
    phases — board namespaced "ashby:<slug>"/"lever:<org>", feeds the
    board-yield ranking that auto-prioritizes verify budget)
  append one metrics row to hidden_files/ashby-lever-json-metrics.jsonl:
    {ts, producer, per_board: {board: {postings_seen, new_postings,
     duplicates_skipped}}, staged, ingested, validation_errors, duration_s,
     http_429s}
  then run staging_ingest.py --live --settle 0 — the sanctioned intake path
    (validate -> dedupe -> BACKUP -> write -> telemetry -> archive).

MODES:
  --dry-run   poll + diff + report only; no staging write, no ingest, no
              state/yield/metrics writes. Default is dry-run (fail closed).
  --live      full flow above.

Fail closed everywhere: unparseable board response -> board skipped, logged,
never invented. Titles/URLs/ids come only from the API payload.
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
from ats_discovery import normalize as N  # noqa: E402
import dedupe_gate  # noqa: E402
import queue_intake  # noqa: E402
import title_triage  # noqa: E402

from keel_paths import HOME, DATA  # noqa: E402
REGISTRY = os.path.join(HOME, "hidden_files/ashby-lever-board-registry.json")
SEEN = os.path.join(HOME, "hidden_files/ashby-lever-json-seen.json")
YIELD_LOG = os.path.join(HOME, "hidden_files/clean-board-watch-yield.jsonl")
METRICS_LOG = os.path.join(HOME, "hidden_files/ashby-lever-json-metrics.jsonl")
STAGED_DIR = os.path.join(HOME, "hidden_files/discovery-staging")
BLOCKLIST = os.path.join(DATA, "employer-blocklist.md")
DELAY = 2.0

ASHBY_API = "https://api.ashbyhq.com/posting-api/job-board/{board}"
LEVER_API = "https://api.lever.co/v0/postings/{board}?mode=json"

PDT = ZoneInfo("America/Los_Angeles")


class RateLimited(Exception):
    """HTTP 429 from a board API — hard stop, nothing written."""
    pass


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def pdt_stamp():
    return datetime.now(PDT).strftime("%Y-%m-%d %H:%M PDT")


def load_registry_boards():
    """[(platform, board, company)] for registry-validated boards.

    The registry (hidden_files/ashby-lever-board-registry.json) is built by
    validating each candidate board's JSON API responds with parseable
    listings — never invented, never hand-typed into the watch.
    """
    try:
        reg = json.load(open(REGISTRY))
    except Exception:
        return []
    out = []
    for board, v in reg.items():
        if not v.get("api_ok"):
            continue
        plat = (v.get("platform") or "").lower()
        if plat not in ("ashby", "lever"):
            continue
        out.append((plat, board, v.get("company") or board))
    return sorted(out)


def fetch_board_json(platform, board):
    """GET the board's jobs JSON. Raises RateLimited on 429."""
    api = ASHBY_API if platform == "ashby" else LEVER_API
    req = urllib.request.Request(api.format(board=board), headers=add.UA)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 429:
            raise RateLimited(f"{platform}:{board}")
        raise


def _looks_http(u):
    return isinstance(u, str) and re.match(r"^https?://[^\s/]+/", u or "")


def parse_ashby(board, payload):
    """Return [(job_id, title, location, url)] from an Ashby board payload.

    Never invents: only isListed jobs with a non-empty id and title actually
    present in the payload. Canonical posting URL prefers the API's jobUrl
    when it is a real http(s) URL, else jobs.ashbyhq.com/<board>/<id>.
    """
    out = []
    seen_ids = set()
    if not isinstance(payload, dict):
        return out
    jobs = payload.get("jobs")
    if not isinstance(jobs, list):
        return out
    for job in jobs:
        if not isinstance(job, dict) or not job.get("isListed"):
            continue
        jid = str(job.get("id") or "").strip()
        title = (job.get("title") or "").strip()
        if not jid or not title or jid in seen_ids:
            continue
        location = (job.get("location") or "").strip()
        job_url = (job.get("jobUrl") or "").strip()
        url = (job_url if _looks_http(job_url)
               else f"https://jobs.ashbyhq.com/{board}/{jid}")
        seen_ids.add(jid)
        out.append((jid, htmlmod.unescape(title).strip(), location, url))
    return out


def parse_lever(board, payload):
    """Return [(job_id, title, location, url)] from a Lever postings payload.

    Never invents: only postings with a non-empty id and title ("text")
    actually present in the payload. Canonical posting URL prefers the API's
    hostedUrl, else jobs.lever.co/<org>/<id>.
    """
    out = []
    seen_ids = set()
    if not isinstance(payload, list):
        return out
    for job in payload:
        if not isinstance(job, dict):
            continue
        jid = str(job.get("id") or "").strip()
        title = (job.get("text") or "").strip()
        if not jid or not title or jid in seen_ids:
            continue
        cats = job.get("categories") or {}
        location = (cats.get("location") or "").strip() if isinstance(
            cats, dict) else ""
        hosted = (job.get("hostedUrl") or "").strip()
        url = (hosted if _looks_http(hosted)
               else f"https://jobs.lever.co/{board}/{jid}")
        seen_ids.add(jid)
        out.append((jid, htmlmod.unescape(title).strip(), location, url))
    return out


def job_discriminator(jid):
    """Short stable discriminator for role_id from a board job id.

    Ashby UUIDs and Lever ids are hex/uuid strings; digits are preferred,
    falling back to the first 12 alnum chars. Fail closed: empty string
    when nothing usable (caller falls back to the base role_id).
    """
    digits = re.sub(r"\D", "", str(jid) or "")
    if digits:
        return "J" + digits[:24]
    alnum = re.sub(r"[^a-zA-Z0-9]", "", str(jid) or "")
    return ("J" + alnum[:12]) if alnum else ""


def make_json_role_id(platform, company, title, jid, date_tag):
    """Collision-proof role_id for Ashby/Lever JSON-enumerated leads.

    Self-contained mirror of clean_board_watch.make_watch_role_id (kept
    local to avoid an import cycle — clean_board_watch.py calls this module
    as a subprocess phase). The board job id (Ashby UUID / Lever id) is a
    stable discriminator; format stays UPPER-KEBAB so queue_intake
    validation passes.
    """
    base = N.make_role_id(platform, company, title, jid, date_tag)
    disc = job_discriminator(jid)
    if not disc:
        return base  # fail closed: never invent a discriminator
    room = 90 - len(disc) - 1
    head = base if len(base) <= room else base[:room].rstrip("-")
    rid = re.sub(r"-{2,}", "-", f"{head}-{disc}").strip("-")
    return rid


def board_company(company):
    return N.norm_company(company)


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


def build_json_entry(platform, board, company, jid, title, location, url):
    date_tag = datetime.now(PDT).strftime("%Y%m%d")
    ns = f"{platform}:{board}"
    entry = {
        "role_id": make_json_role_id(platform, company, title, jid,
                                     date_tag),
        "company": company,
        "title": N.norm_title(title),
        "location": location,
        "action_band": "PARKED",
        "status": "PARKED-PENDING-VERIFICATION",
        "application_url": url,
        "posting_url": url,
        "ats": platform,
        "ats_board": ns,
        "discovery_platform": platform,
        "discovery_slug": board,
        "source_job_id": jid,
        "source": "ashby-lever-json-enumerate",
        "date_discovered": datetime.now(PDT).strftime("%Y-%m-%d"),
        "status_updated": pdt_stamp(),
        "queue_notes": (
            f"ashby-lever-json-enumerate: new posting from the "
            f"{platform} board JSON API for registry board {board} "
            f"(human-approved 2026-09-16, proposal "
            f"P-2026-09-16-discovery-1). Direct posting URL is enumerable "
            f"and verifiable by verify_retry. Unscored — fit-scoring pass "
            f"required; no fit_score invented."
        ),
    }
    # Staging title triage (same hardened build as the Greenhouse phases):
    # deferred leads are TAGGED, never dropped.
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
    """Fetch + diff + build entries for the given (platform, board, company).

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
    # Intra-batch dedupe (same 4b rationale as greenhouse_json_enumerate):
    # the persistent-index check cannot see batch-internal collisions.
    batch_url_keys = set()
    batch_emp_titles = set()
    skipped_batch_dup = 0

    for bi, (platform, board, company) in enumerate(boards):
        ns = f"{platform}:{board}"
        company = board_company(company)
        try:
            payload = fetch_board_json(platform, board)
        except RateLimited:
            raise
        except Exception as ex:
            results.append({"ts": utcnow(), "board": ns,
                            "postings_seen": 0, "new_postings": 0,
                            "error": type(ex).__name__})
            per_board[ns] = {"postings_seen": 0, "new_postings": 0,
                             "duplicates_skipped": 0}
            continue
        postings = (parse_ashby(board, payload) if platform == "ashby"
                    else parse_lever(board, payload))
        board_seen = seen.setdefault(ns, {})
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
            url_key = dedupe_gate.canonical_url(url)
            emp_key = (dedupe_gate._norm_name(company),
                       dedupe_gate._norm_title(title))
            if url_key and url_key in batch_url_keys:
                skipped_batch_dup += 1
                if live:
                    board_seen[jid] = utcnow()
                continue
            if emp_key[0] and emp_key[1] and emp_key in batch_emp_titles:
                skipped_batch_dup += 1
                if live:
                    board_seen[jid] = utcnow()
                continue
            entry = build_json_entry(platform, board, company, jid, title,
                                     location, url)
            errors, _w = queue_intake.validate_entry(entry)
            if errors:
                validation_errors += 1
                print(f"VALIDATION REFUSED {entry['role_id']}: {errors}",
                      file=sys.stderr)
                continue  # fail closed: not marked seen, retried next run
            new_entries.append(entry)
            if url_key:
                batch_url_keys.add(url_key)
            if emp_key[0] and emp_key[1]:
                batch_emp_titles.add(emp_key)
            if live:
                board_seen[jid] = utcnow()
            fresh += 1
        results.append({"ts": utcnow(), "board": ns,
                        "postings_seen": len(postings),
                        "new_postings": fresh})
        per_board[ns] = {"postings_seen": len(postings),
                         "new_postings": fresh,
                         "duplicates_skipped": dups}
        if bi < len(boards) - 1:
            time.sleep(DELAY)

    if live:
        atomic_write_json(SEEN, seen)
        with open(YIELD_LOG, "a") as f:
            for r in results:
                f.write(json.dumps(r, sort_keys=True) + "\n")

    return {"entries": new_entries, "results": results,
            "per_board": per_board,
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
    boards = load_registry_boards()
    if not boards:
        print("no api_ok boards in ashby-lever registry — nothing to watch")
        return 0

    try:
        res = enumerate_boards(boards, live=live)
    except RateLimited:
        print("429 rate limit — HARD STOP, nothing written",
              file=sys.stderr)
        return 75

    total_seen = sum(r["postings_seen"] for r in res["results"])
    total_new = sum(r["new_postings"] for r in res["results"])
    print(f"boards={len(boards)} postings_seen={total_seen} "
          f"new={total_new} dup_skipped={res['skipped_dup']} "
          f"blocked_skipped={res['skipped_blocked']} "
          f"batch_dup_skipped={res['skipped_batch_dup']} "
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
    if res["entries"]:
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
            STAGED_DIR, f"ashby-lever-json-{batch}-leads.json")
        atomic_write_json(staged_path, res["entries"])
        print(f"staged {len(res['entries'])} lead(s) -> {staged_path}")
    else:
        print("no new postings — no staging file written")
    # sanctioned intake: validate -> dedupe -> BACKUP -> write -> telemetry
    proc = subprocess.run(
        [sys.executable, os.path.join(BASE, "staging_ingest.py"),
         "--live", "--settle", "0"],
        capture_output=True, text=True, timeout=900)
    print(proc.stdout[-2000:])
    if proc.returncode != 0:
        print(proc.stderr[-1000:], file=sys.stderr)
    ingested = extract_ingested(proc.stdout)

    metrics_row = {
        "ts": utcnow(),
        "producer": "ashby-lever-json-enumerate",
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
