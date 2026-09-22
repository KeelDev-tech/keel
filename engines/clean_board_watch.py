#!/usr/bin/env python3
"""clean_board_watch.py — source-inversion discovery: watch the 10 known-clean
Greenhouse boards as a PRIMARY lead source (human-approved 2026-09-15).

WHY: the api-direct transport starves because discovery finds postings first
and checks boards second. These 10 boards are classified clean (no reCAPTCHA
Enterprise) in hidden_files/greenhouse-board-enterprise.json. Watching them
directly inverts the flow — every new posting here is an api-direct
candidate, fileable in ~1 min vs ~15 min browser.

FETCH MECHANISM DECISION (evidence-documented):
- Draft P-2026-09-15-arm51-2 ("keyless public JSON board API") covers
  Workable's www.workable.com/api/accounts/<slug> endpoint ONLY and is
  DRAFT/PENDING TRIAGE — NOT approved for use, and not about Greenhouse.
- edge_case_registry.json carries no Greenhouse JSON-board-API entry
  (greenhouse verdicts: browser_only / viable_direct, via HTML).
- boards-api.greenhouse.io would be a NEW unapproved mechanism -> NOT used.
- Therefore: fetch the HTML board listing pages exactly like
  greenhouse_board_classify.py does (same UA, polite delay, 429 = hard
  stop, fail closed).
- Empirical 2026-09-15: all 10 clean boards' listing pages are JS-rendered
  with ZERO server-rendered /jobs/<id> hrefs. gigs embeds job JSON
  (absolute_url + numeric id + title) in the page; stripe (Next.js) embeds
  nothing enumerable. The parser handles (1) classic /jobs/<id> hrefs and
  (2) embedded JSON job objects. Boards with no enumerable postings are
  logged as board_not_enumerable and skipped — fail closed, never invent.
- 2026-09-15 dept2-supply parser expansion (same sanctioned HTML fetch, new
  shapes only): stripe's __NEXT_DATA__ carries 605 listings
  (props.pageProps.jobIndexData.listings[{greenhouseId,title,slug,
  locationIndices}]) -> posting URL https://stripe.com/jobs/search?gh_jid=<id>
  (shape proven live by verify_retry enrichment 2026-09-15); samsara's page
  server-renders 272 listing links (/company/careers/roles/<id>?gh_jid=<gid>
  + h4 title + data-location); embedded-JSON split generalized to
  absolute_url in any key position. 7 boards (abnormalsecurity, databricks,
  fastly, mongodb, salesloft, coinbase, securly13) verified JS-only over
  plain HTTP (no job-data markers; JSON-LD is Organization-only) — they stay
  polled as SSR-flip detectors, logged board_not_enumerable.
- 2026-09-15/16 JSON API productionization (human-authorized "Push them",
  blackboard J-20260916-0012-sour-423): the 7 JS-only boards are now
  enumerated by greenhouse_json_enumerate.py via boards-api.greenhouse.io
  (read-only trial 2026-09-16 00:12 UTC: 1,662 postings, zero 429/403).
  This module runs it as a subprocess phase FIRST each run (its own seen
  file, yield rows, staging, ingest, metrics); the HTML loop skips those 7
  boards. A 429 from the JSON phase propagates as exit 75 before this
  module writes anything.
- 2026-09-16 Ashby/Lever JSON phase (human-approved P-2026-09-16-discovery-1):
  registry-clean Ashby/Lever boards (hidden_files/ashby-lever-board-registry.json,
  built from the census3x universe, each board's JSON API validated live)
  are enumerated by ashby_lever_json_enumerate.py via api.ashbyhq.com
  posting-api + api.lever.co v0/postings — same polite UA, 2s delay,
  429 = hard stop (exit 75). Runs as a subprocess phase AFTER the
  Greenhouse JSON phase, BEFORE the HTML loop; same seen-diff +
  dedupe_gate + staging_ingest contract, yield rows in the shared
  clean-board-watch-yield.jsonl schema.

FLOW (live mode):
  poll 10 clean boards -> diff vs clean-board-watch-seen.json
  for each NEW posting:
    - employer-blocklist check (employer-blocklist.md)
    - dedupe_gate.check_candidate(company, title, url): "duplicate" -> skip;
      "suspect" is advisory only -> stage it (canonical gate semantics)
    - build entry: action_band PARKED, status PARKED-PENDING-VERIFICATION,
      NO fit_score (never invented), ats_board=greenhouse:<board>
    - role_id embeds the Greenhouse numeric job id
      (make_watch_role_id): N.make_role_id's 4-word title truncation
      collapsed 12 distinct gigs postings into 8 role_ids on 2026-09-15
      (ingest rejected them as 'duplicate role_id in batch' — real postings
      silently lost). The jid discriminator keeps UPPER-KEBAB shape.
    - queue_intake.validate_entry -> ZERO errors required (C-17)
  write hidden_files/discovery-staging/clean-board-watch-<ts>-leads.json
  append per-board {ts, board, postings_seen, new_postings} rows to
    hidden_files/clean-board-watch-yield.jsonl  (feeds board-yield ranking)
  then run staging_ingest.py --live --only <the file just written> — the
    sanctioned intake path (validate -> dedupe -> BACKUP -> write ->
    telemetry -> archive). The file just written is complete (atomic
    write), so the settle wait is waived for exactly that file; every
    other staging file keeps the 300s settle window (2026-09-21 fix — the
    old blanket --settle 0 waived the window for the whole staging dir).
    This also drains other settled staging files the pulse would
    otherwise flag stale.

MODES:
  --dry-run   poll + diff + report only; no staging write, no ingest, no
              state/yield writes. Default is dry-run (fail closed).
  --live      full flow above.
  --merge-report board,enterprise
              fold a worker-reported board status (from the retargeted
              discovery snippet's enterprise_board output) into the registry.
              Freshness layering: worker reports only FILL boards missing
              from the registry or previously worker-reported; they NEVER
              overwrite classifier-verified entries (the classifier's HTTP
              check is authoritative). Atomic merge, never clobber.

Rate limits: 2s polite delay between board fetches; HTTP 429 = HARD STOP
(exit 75, nothing written — no state, no yield, no staging).
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
import greenhouse_json_enumerate as gje  # noqa: E402 (JSON phase, additive)
import ashby_lever_json_enumerate as ale  # noqa: E402 (Ashby/Lever phase)
from log_event import emit_429_halt  # noqa: E402 (COV-http_429_halt telemetry)

from keel_paths import HOME, DATA  # noqa: E402
REGISTRY = os.path.join(HOME, "hidden_files/greenhouse-board-enterprise.json")
SEEN = os.path.join(HOME, "hidden_files/clean-board-watch-seen.json")
YIELD_LOG = os.path.join(HOME, "hidden_files/clean-board-watch-yield.jsonl")
STAGED_DIR = os.path.join(HOME, "hidden_files/discovery-staging")
BLOCKLIST = os.path.join(DATA, "employer-blocklist.md")
DELAY = 2.0

PDT = ZoneInfo("America/Los_Angeles")

# classic server-rendered board template
HREF_PAT = re.compile(r'href="([^"]*?/jobs/(\d+)[^"]*)"[^>]*>([^<]{3,120})<', re.I)
# embedded JSON job objects (gigs-style): {"absolute_url":"...","id":123,...,"title":"..."}
# 2026-09-15 dept2-supply: also match when absolute_url is NOT the first key
# (samsara-shape: ...,"absolute_url":"...").
EMBED_SPLITS = ('{"absolute_url"', ',"absolute_url"')
ID_PAT = re.compile(r'"id":(\d{5,})')
TITLE_PAT = re.compile(r'"title":"((?:[^"\\]|\\.){3,160})"')
# stripe-shape: Next.js __NEXT_DATA__ with props.pageProps.jobIndexData.listings[]
# each {greenhouseId, title, slug, locationIndices[]}; locations resolved via
# jobIndexData.filters.locations[]. Canonical posting URL (proven live by
# verify_retry enrichment 2026-09-15): https://stripe.com/jobs/search?gh_jid=<id>
NEXT_DATA_PAT = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S)
# samsara-shape: server-rendered Vue listing links on the (proxied) careers page:
# <li data-location="..."><a href="/company/careers/roles/<id>?gh_jid=<gid>..."><h4>TITLE</h4>
SAMSARA_LINK_PAT = re.compile(
    r'data-location="([^"]{0,120})"[^>]*>\s*'
    r'<a href="/company/careers/roles/(\d+)\?gh_jid=(\d+)[^"]*"[^>]*>'
    r'.*?<h4[^>]*>([^<]{3,220})</h4>', re.S)
# board token -> canonical company name (page <title> on employer-proxied
# boards is marketing copy, not the company name)
BOARD_COMPANY = {"stripe": "Stripe", "samsara": "Samsara"}


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def pdt_stamp():
    return datetime.now(PDT).strftime("%Y-%m-%d %H:%M PDT")


def load_registry_clean():
    """board -> checked_ts for registry-clean boards."""
    try:
        reg = json.load(open(REGISTRY))
    except Exception:
        return {}
    return {b: v.get("checked_ts", "") for b, v in reg.items()
            if not v.get("enterprise")}


def fetch_html(url):
    req = urllib.request.Request(url, headers=add.UA)
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read().decode("utf-8", "replace")


def parse_stripe_next_data(html):
    """Stripe-shape: Next.js __NEXT_DATA__ jobIndexData.listings.

    Returns [(gid, title, location, url)]. Never invents: only listings with
    a numeric greenhouseId actually present in the blob."""
    out = []
    m = NEXT_DATA_PAT.search(html or "")
    if not m:
        return out
    try:
        d = json.loads(m.group(1))
        jid = d["props"]["pageProps"]["jobIndexData"]
        loc_table = [loc.get("name", "") for loc in
                     jid.get("filters", {}).get("locations", [])]
        listings = jid.get("listings", [])
    except (ValueError, KeyError, TypeError, AttributeError):
        return out
    seen = set()
    for it in listings:
        try:
            gid = str(int(it.get("greenhouseId", 0)))
        except (ValueError, TypeError):
            continue
        title = (it.get("title") or "").strip()
        if not gid or gid == "0" or gid in seen or not title:
            continue
        seen.add(gid)
        locs = [loc_table[i] for i in it.get("locationIndices", [])
                if isinstance(i, int) and 0 <= i < len(loc_table)
                and loc_table[i]]
        url = f"https://stripe.com/jobs/search?gh_jid={gid}"
        out.append((gid, title, ", ".join(locs), url))
    return out


def parse_samsara_ssr(html):
    """Samsara-shape: server-rendered listing links with gh_jid + h4 title.

    Returns [(jid, title, location, url)]. Never invents: only links with a
    numeric role id actually present in the fetched HTML."""
    out = []
    seen = set()
    for loc, rid, gid, title in SAMSARA_LINK_PAT.findall(html or ""):
        jid = gid or rid
        if not jid or jid in seen:
            continue
        seen.add(jid)
        url = (f"https://www.samsara.com/company/careers/roles/"
               f"{rid}?gh_jid={gid}")
        out.append((jid, title.strip(), loc.strip(), url))
    return out


def parse_board_listings(board, html):
    """Return [(job_id, title, location, url)] enumerated from a board page.

    Never invents: only returns postings with a numeric job id actually
    present in the fetched HTML. Shapes handled: (1) classic /jobs/<id>
    hrefs, (2) embedded JSON job objects (gigs-style, absolute_url in any
    key position), (3) stripe-shape __NEXT_DATA__ listings, (4) samsara-shape
    SSR listing links."""
    out = []
    seen_ids = set()

    def add(jid, title, location, url):
        if not jid or jid in seen_ids:
            return
        seen_ids.add(jid)
        title = htmlmod.unescape(title).strip()
        out.append((jid, title, location.strip(), url))

    for m in HREF_PAT.finditer(html):
        jid, title = m.group(2), m.group(1)
        add(jid, title, "",
            f"https://job-boards.greenhouse.io/{board}/jobs/{jid}")
    for split in EMBED_SPLITS:
        if split in html:
            for chunk in html.split(split)[1:]:
                seg = chunk[:1200]
                im = ID_PAT.search(seg)
                tm = TITLE_PAT.search(seg)
                if not im or not tm:
                    continue
                jid = im.group(1)
                title = tm.group(1).encode("utf-8").decode(
                    "unicode_escape", errors="replace")
                lm = re.search(r'"location":\{"name":"([^"]+)"', seg)
                add(jid, title, lm.group(1) if lm else "",
                    f"https://job-boards.greenhouse.io/{board}/jobs/{jid}")
    for gid, title, location, url in parse_stripe_next_data(html):
        add(gid, title, location, url)
    for jid, title, location, url in parse_samsara_ssr(html):
        add(jid, title, location, url)
    return out


def make_watch_role_id(company, title, jid, date_tag):
    """Collision-proof role_id for clean-board-watch leads.

    N.make_role_id truncates titles to 4 words and ignores source_job_id,
    which collapsed 12 distinct gigs postings into 8 shared role_ids on
    2026-09-15 — rejected at ingest as 'duplicate role_id in batch', so 12
    real postings were silently lost (and marked seen, never resurfacing).
    Greenhouse numeric job ids are stable, board-scoped, and embedded in
    the canonical posting URL, so they are a safe discriminator for THIS
    producer. Format stays UPPER-KEBAB so queue_intake validation passes.
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


def board_company(html, board):
    if board in BOARD_COMPANY:
        return N.norm_company(BOARD_COMPANY[board])
    m = re.search(r"<title>([^<]+)</title>", html or "", re.I)
    if m:
        t = m.group(1)
        t = re.sub(r"(?i)^current job openings\s*\|\s*", "", t).strip()
        if t:
            return N.norm_company(t)
    return N.norm_company(board)


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


def build_entry(board, company, jid, title, location, url, reg_ts):
    date_tag = datetime.now(PDT).strftime("%Y%m%d")
    entry = {
        "role_id": make_watch_role_id(company, title, jid, date_tag),
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
        "source": "clean-board-watch",
        "date_discovered": datetime.now(PDT).strftime("%Y-%m-%d"),
        "status_updated": pdt_stamp(),
        "queue_notes": (
            f"clean-board-watch: new posting on registry-clean board "
            f"{board} (registry checked {reg_ts}); board is marker-clean "
            f"but per-posting submittability still gated by "
            f"api_direct_detect at verify/apply time. Unscored — "
            f"fit-scoring pass required; no fit_score invented."
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


def merge_report(board, enterprise):
    """Fold a worker-reported board status into the registry.

    Freshness layering: only fills boards missing from the registry or
    previously worker-reported; NEVER overwrites classifier-verified
    entries (the classifier's HTTP check is authoritative)."""
    board = board.strip().lower()
    enterprise = enterprise.strip().lower() in ("true", "1", "yes",
                                                "enterprise")
    try:
        reg = json.load(open(REGISTRY))
    except Exception:
        reg = {}
    cur = reg.get(board)
    if cur and cur.get("source") != "worker-report":
        print(f"merge-report: {board} already classifier-verified "
              f"(enterprise={cur.get('enterprise')}) — worker report "
              f"not applied")
        return 0
    reg[board] = {"enterprise": enterprise, "checked_ts": utcnow(),
                  "source": "worker-report"}
    atomic_write_json(REGISTRY, reg)
    print(f"merge-report: {board} -> enterprise={enterprise} "
          f"(source=worker-report)")
    return 0


def main(argv):
    live = "--live" in argv
    if any(a == "--merge-report" for a in argv):
        i = argv.index("--merge-report")
        try:
            b, e = argv[i + 1].split(",", 1)
        except (IndexError, ValueError):
            print("usage: --merge-report board,enterprise", file=sys.stderr)
            return 2
        return merge_report(b, e)

    clean = load_registry_clean()
    if not clean:
        print("no clean boards in registry — nothing to watch")
        return 0
    boards = sorted(clean)

    # JSON API phase (additive, 2026-09-15): the 7 JS-only boards are
    # enumerated by greenhouse_json_enumerate.py (human-authorized,
    # J-20260916-0012-sour-423) — its own seen file, yield rows, staging,
    # ingest, and metrics. Runs FIRST so a 429 hard-stop (exit 75)
    # propagates before this module writes any state.
    json_boards = sorted(set(boards) & set(gje.JSON_BOARDS))
    html_boards = [b for b in boards if b not in json_boards]
    if json_boards:
        jp = subprocess.run(
            [sys.executable, os.path.join(BASE,
                                          "greenhouse_json_enumerate.py"),
             "--live" if live else "--dry-run"],
            capture_output=True, text=True, timeout=900)
        print(jp.stdout[-1500:])
        if jp.returncode == 75:
            # COV-http_429_halt (2026-09-18): the child enumerator already
            # emitted its own http_429_halt row; this row marks the
            # clean-board-watch halt itself (return 75, no HTML-phase
            # writes). Halt behavior unchanged.
            emit_429_halt("clean-board-watch",
                          {"halt": "429 hard stop propagated — exiting, "
                                  "nothing written",
                           "exit_code": 75,
                           "propagated_from": "greenhouse-json-enumerate",
                           "live": live})
            print("JSON enumerator hit 429 — HARD STOP, nothing written",
                  file=sys.stderr)
            return 75
        if jp.returncode != 0:
            print(f"JSON enumerator exited {jp.returncode}: "
                  f"{jp.stderr[-500:]}", file=sys.stderr)

    # Ashby/Lever JSON phase (additive, 2026-09-16, human-approved
    # P-2026-09-16-discovery-1): registry-clean Ashby/Lever boards are
    # enumerated by ashby_lever_json_enumerate.py via the vendors' public
    # board JSON APIs (api.ashbyhq.com posting-api + api.lever.co
    # v0/postings — both already exercised by the census3x biweekly
    # universe, no new transport). Own registry, seen file, yield rows
    # (same clean-board-watch-yield.jsonl schema, board namespaced
    # ashby:<slug>/lever:<org>), staging, ingest, and metrics. A 429
    # propagates as exit 75 before any HTML-phase writes below.
    al_boards = ale.load_registry_boards()
    if al_boards:
        alp = subprocess.run(
            [sys.executable, os.path.join(
                BASE, "ashby_lever_json_enumerate.py"),
             "--live" if live else "--dry-run"],
            capture_output=True, text=True, timeout=900)
        print(alp.stdout[-1500:])
        if alp.returncode == 75:
            # COV-http_429_halt (2026-09-18): the child enumerator already
            # emitted its own http_429_halt row; this row marks the
            # clean-board-watch halt itself (return 75, no HTML-phase
            # writes). Halt behavior unchanged.
            emit_429_halt("clean-board-watch",
                          {"halt": "429 hard stop propagated — exiting, "
                                  "nothing written",
                           "exit_code": 75,
                           "propagated_from": "ashby-lever-json-enumerate",
                           "live": live})
            print("Ashby/Lever enumerator hit 429 — HARD STOP, "
                  "nothing written", file=sys.stderr)
            return 75
        if alp.returncode != 0:
            print(f"Ashby/Lever enumerator exited {alp.returncode}: "
                  f"{alp.stderr[-500:]}", file=sys.stderr)
    else:
        print("no api_ok boards in ashby-lever registry — phase skipped")

    try:
        seen = json.load(open(SEEN))
    except Exception:
        seen = {}
    blocked_names = load_blocklist()

    results = []          # per-board yield rows
    new_entries = []      # staged leads
    skipped_dup = 0
    skipped_blocked = 0
    not_enumerable = []

    for bi, board in enumerate(html_boards):
        try:
            html = fetch_html(f"https://job-boards.greenhouse.io/{board}")
        except urllib.error.HTTPError as e:
            if e.code == 429:
                # COV-http_429_halt (2026-09-18): the 429 hard stop
                # previously emitted to stdout/stderr only — invisible to
                # the safety coverage map. One http_429_halt telemetry row
                # per halted run. Halt behavior (return 75, nothing
                # written) is unchanged.
                emit_429_halt("clean-board-watch",
                              {"halt": "429 hard stop — exiting, nothing "
                                      "written",
                               "exit_code": 75,
                               "phase": "html",
                               "board": board,
                               "live": live})
                print("429 rate limit — HARD STOP, nothing written",
                      file=sys.stderr)
                return 75
            results.append({"ts": utcnow(), "board": board,
                            "postings_seen": 0, "new_postings": 0,
                            "error": f"http_{e.code}"})
            continue
        except Exception as ex:
            results.append({"ts": utcnow(), "board": board,
                            "postings_seen": 0, "new_postings": 0,
                            "error": type(ex).__name__})
            continue
        postings = parse_board_listings(board, html)
        company = board_company(html, board)
        if not postings:
            not_enumerable.append(board)
        board_seen = seen.setdefault(board, {})
        fresh = 0
        for jid, title, location, url in postings:
            if url in board_seen:
                continue
            if is_blocked(company, blocked_names):
                skipped_blocked += 1
                board_seen[url] = utcnow()  # don't resurface a blocked lead
                continue
            verdict, _ev = dedupe_gate.check_candidate(company, title, url)
            if verdict == "duplicate":
                skipped_dup += 1
                board_seen[url] = utcnow()  # known — don't resurface
                continue
            # "suspect" is advisory only per the gate's contract: stage it.
            entry = build_entry(board, company, jid, title, location,
                                url, clean[board])
            errors, _w = queue_intake.validate_entry(entry)
            if errors:
                print(f"VALIDATION REFUSED {entry['role_id']}: {errors}",
                      file=sys.stderr)
                continue
            new_entries.append(entry)
            board_seen[url] = utcnow()
            fresh += 1
        results.append({"ts": utcnow(), "board": board,
                        "postings_seen": len(postings),
                        "new_postings": fresh})
        if bi < len(html_boards) - 1:
            time.sleep(DELAY)

    total_seen = sum(r["postings_seen"] for r in results)
    total_new = sum(r["new_postings"] for r in results)
    print(f"html_boards={len(html_boards)} json_boards={len(json_boards)} "
          f"(JSON phase above) postings_seen={total_seen} "
          f"new={total_new} dup_skipped={skipped_dup} "
          f"blocked_skipped={skipped_blocked}")
    if not_enumerable:
        print(f"board_not_enumerable (JS-only listings, skipped fail-closed): "
              f"{', '.join(not_enumerable)}")
    for r in results:
        print(f"  {r['board']}: seen={r['postings_seen']} "
              f"new={r['new_postings']}"
              f"{' err=' + r['error'] if r.get('error') else ''}")

    if not live:
        print(f"DRY-RUN: would stage {len(new_entries)} lead(s); "
              f"nothing written.")
        for e in new_entries[:10]:
            print(f"  WOULD-STAGE {e['role_id']} | {e['company']} | "
                  f"{e['title'][:60]}")
        return 0

    # live: persist state + yield, write staging file, run sanctioned ingest
    atomic_write_json(SEEN, seen)
    with open(YIELD_LOG, "a") as f:
        for r in results:
            f.write(json.dumps(r, sort_keys=True) + "\n")
    staged_path = None
    if new_entries:
        # J-20260916-0425-sour-562: emission precheck — schema-valid,
        # canonical role_id, intra-batch dedupe BEFORE the staging write.
        # Invalid stubs and batch-internal dupes die here with one summary
        # line instead of becoming staged_rejected noise at ingest
        # (measured 2412/24h, ~95% avoidable). Fail-closed: withheld
        # entries are dropped from the batch, never rewritten.
        from staging_ingest import emission_precheck  # noqa: E402
        new_entries, _pc_withheld, pc_report = emission_precheck(new_entries)
        print(f"emission_precheck: {pc_report['fresh']} fresh, "
              f"{pc_report['withheld']} withheld from staging: "
              f"{pc_report['withheld_reason_counts']}")
        if not new_entries:
            print("emission precheck withheld the whole batch — "
                  "no staging file written")
    if new_entries:
        os.makedirs(STAGED_DIR, exist_ok=True)
        batch = datetime.now(PDT).strftime("%Y%m%d-%H%M%S")
        staged_path = os.path.join(
            STAGED_DIR, f"clean-board-watch-{batch}-leads.json")
        atomic_write_json(staged_path, new_entries)
        print(f"staged {len(new_entries)} lead(s) -> {staged_path}")
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
        capture_output=True, text=True, timeout=600)
    print(proc.stdout[-2000:])
    if proc.returncode != 0:
        print(proc.stderr[-1000:], file=sys.stderr)
    return proc.returncode


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
