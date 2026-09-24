#!/usr/bin/env python3
"""Description-enrichment loop (Best-in-World follow-up, 2026-09-17).

For each thin-evidence PARKED-AWAITING-MATERIALS lead (lane-assigner
abstain: "no lane keyword hits in title or description -- insufficient
evidence to assign"), fetch the live posting description via the
verify_retry.py ATS-HTTP patterns and write it into the entry's
`description` field -- the ONLY field this script mutates (plus the
canonical dead-marking path).

Fetch path mirrors verify_retry.check_live's greenhouse branch exactly:
skip the resolve GET when _resolve_needed says the URL determines the
branch; parse greenhouse board/job tokens or fall back to the ordered
gh_jid board guesses (_gh_jid_guesses); fetch the public board-API job
record via ats._get_json -- the SAME primitive ats.greenhouse_job uses.
NOTE: the public wrapper drops the API's `content` field (fixed 2026-09-17:
verify_retry.fetch_posting_text now reads the raw record's `content` field
itself via ats._get_json); this loop reads the raw record's `content` field
and runs it through verify_retry._html_to_text. No new fetcher is hand-rolled.

Verdicts per lead:
  enriched           -- live record with >= MIN_TEXT chars of real text;
                        `description` written. Nothing invented: only text
                        actually fetched from the posting.
  dead               -- canonical dead signal per check_live conventions
                        (explicit 404/410 on the posting page / resolve).
                        NOTE: greenhouse board-API 404/410 on every guess
                        is NOT death evidence (J-20260916-0045-veri-440:
                        board API decoupled from page rendering) -> that
                        case is enrichment_blocked, never dead-marked.
  enrichment_blocked -- ambiguous / fetch failed / content too thin /
                        JS-gated (browser lane owns those). Entry left
                        byte-identical; reported, never silently dropped.
  skipped_race       -- another writer moved the entry while we worked;
                        fail closed, write skipped, reported.

Sanctioned path: pre-write backup of standard-queue.json (+
rejected-queue.json when the dead path fires), all writes under
queue_io.queue_lock, atomic save (tmp + os.replace), append-only
telemetry (lead_dead per dead-mark; one scan_summary for the run with
per-lead outcomes in details -- no new event types invented).
HTTP 429 at any point = HARD STOP: the fetch loop ends immediately;
already-fetched honest results are still persisted (no further HTTP),
the partial run is reported, exit code is nonzero.
"""
import json
import os
import re
import shutil
import sys
import time
import urllib.error
from collections import Counter
from datetime import datetime, timezone

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)  # repo engines dir — no private workspace path

import ats
import queue_io
import log_event
import html as _htmlmod
from verify_retry import (
    posting_url,
    _html_to_text,
    _resolve_needed,
    _gh_jid_guesses,
)

from keel_paths import HOME, DATA  # noqa: E402 — repo path convention
STD_Q = os.path.join(DATA, "queues", "standard-queue.json")
REJ_Q = os.path.join(DATA, "queues", "rejected-queue.json")
TS = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
SOURCE = "description-enrichment"
MIN_TEXT = 100          # min fetched-text length to count as a description
PACE_S = 0.5            # politeness pause between leads
HELD_STATUS = "PARKED-AWAITING-MATERIALS"
THIN_CLAUSE = ("no lane keyword hits in title or description "
               "— insufficient evidence to assign")

HTTP_STATS = Counter()


class RateLimited(Exception):
    """HTTP 429 seen: hard stop the fetch loop."""


def _bump(code):
    HTTP_STATS[code] += 1


def fetch_description(url):
    """Fetch posting description text via the ATS-API pattern.

    Returns (status, payload):
      ("ok", text)        -- real fetched description (>= MIN_TEXT chars)
      ("dead", detail)    -- canonical dead signal
      ("blocked", detail) -- ambiguous / failed / thin content / out of scope
    Raises RateLimited on HTTP 429. Never invents text.
    """
    if not url:
        return "blocked", "no posting URL"
    try:
        resolved = (url if not _resolve_needed(url)
                    else ats.resolve_final_url(url))
    except urllib.error.HTTPError as e:
        if e.code == 429:
            raise RateLimited(f"resolve 429 on {url[:60]}")
        _bump(f"resolve_http_{e.code}")
        return "blocked", f"resolve HTTP {e.code}"
    except Exception as e:
        _bump("resolve_error")
        return "blocked", f"resolve failed: {str(e)[:80]}"

    candidates = [url] if resolved == url else [url, resolved]
    detected = [(u, ats.detect_ats(u)) for u in candidates]
    final_url, which = next(
        ((u, a) for u, a in detected if a != "unknown"), detected[-1])
    if which == "unknown" and any(
            re.search(r"[?&]gh_jid=(\d+)", u) for u in candidates):
        which = "greenhouse"  # mirrors check_live's gh_jid branch
    if which != "greenhouse":
        # This loop's scope is the 30 greenhouse gh_jid leads. Other ATS /
        # JS-gated pages are enrichment_blocked; the browser lane owns them.
        return "blocked", f"ATS '{which}' out of enrichment scope"

    guesses = []
    for u in candidates:
        b, j = ats.parse_greenhouse(u)
        if b and j:
            guesses.append((b, j))
            break
    if not guesses:
        guesses = _gh_jid_guesses(candidates)
    if not guesses:
        _bump("no_tokens")
        return "blocked", "greenhouse URL but no board/job tokens"

    api_404 = None
    for board, job_id in guesses:
        api_url = ats.GREENHOUSE_BOARD_API.format(board=board,
                                                 job_id=job_id)
        try:
            rec = ats._get_json(api_url)
            _bump("board_api_200")
        except urllib.error.HTTPError as e:
            if e.code == 429:
                raise RateLimited(f"board API 429 {board}/{job_id}")
            _bump(f"board_api_http_{e.code}")
            if e.code in (404, 410):
                # J-20260916-0045-veri-440: board-API 404/410 is NOT
                # posting-death evidence. Try the next guess.
                api_404 = e.code
                continue
            return "blocked", f"board API HTTP {e.code} ({board}/{job_id})"
        except Exception as e:
            _bump("board_api_error")
            return "blocked", (f"board API error ({board}/{job_id}): "
                               f"{str(e)[:80]}")
        if not rec.get("title"):
            _bump("board_api_no_title")
            return "blocked", f"board API record has no title ({board}/{job_id})"
        text = _html_to_text(_htmlmod.unescape(rec.get("content") or ""))
        if len(text) < MIN_TEXT:
            _bump("board_api_thin_content")
            return "blocked", (f"record live ({rec.get('title')}) but "
                                f"content only {len(text)} chars")
        return "ok", text

    # Every guess failed closed on board-API 404/410: ambiguous per the
    # canonical convention -- enrichment_blocked, never dead-marked.
    _bump("board_api_all_404")
    return ("blocked",
            f"board API HTTP {api_404} on all {len(guesses)} guess(es) "
            f"-- not death evidence; stays parked")


def thin_evidence_leads(items):
    return [e for e in items
            if e.get("status") == HELD_STATUS
            and (e.get("status_reason") or "").endswith(THIN_CLAUSE)]


def main():
    raw = queue_io.load_json(STD_Q)
    items = raw if isinstance(raw, list) else raw.get("items", [])
    targets = thin_evidence_leads(items)
    print(f"thin-evidence targets: {len(targets)}")

    outcomes = {}   # role_id -> (status, payload)
    rate_limited = False
    for i, e in enumerate(targets):
        rid = e.get("role_id")
        try:
            st, payload = fetch_description(posting_url(e))
        except RateLimited as rl:
            print(f"HARD STOP: {rl}")
            rate_limited = True
            break
        outcomes[rid] = (st, payload)
        print(f"[{i+1}/{len(targets)}] {rid[:45]:45s} {st} "
              f"{(payload[:70] if st=='ok' else payload)[:70]}")
        time.sleep(PACE_S)

    # ---- sanctioned write phase -------------------------------------
    enriched, dead_marked, skipped_race = [], [], []
    backup_dir = f"queue/_backup-{TS}-description-enrichment"
    with queue_io.queue_lock(owner="description-enrichment"):
        os.makedirs(os.path.join(HOME, backup_dir), exist_ok=True)
        shutil.copy2(STD_Q, os.path.join(HOME, backup_dir,
                                         "standard-queue.json"))
        rej_backup = None
        if os.path.exists(REJ_Q):
            rej_backup = os.path.join(HOME, backup_dir,
                                      "rejected-queue.json")
            shutil.copy2(REJ_Q, rej_backup)
        print(f"backup: {backup_dir}/standard-queue.json"
              + (f", {rej_backup}" if rej_backup else ""))

        fresh = queue_io.load_json(STD_Q)
        fitems = (fresh if isinstance(fresh, list)
                  else fresh.get("items", []))
        by_id = {e.get("role_id"): e for e in fitems}
        rej = queue_io.load_json(REJ_Q) if os.path.exists(REJ_Q) else []
        if not isinstance(rej, list):
            rej = rej.get("items", [])

        for rid, (st, payload) in outcomes.items():
            cur = by_id.get(rid)
            if cur is None or cur.get("status") != HELD_STATUS:
                # Fail closed: another writer moved it; not our write.
                skipped_race.append(rid)
                continue
            if st == "ok":
                if (cur.get("description") or cur.get("posting_text")
                        or "").strip():
                    skipped_race.append(rid)  # another writer enriched it
                    continue
                cur["description"] = payload  # ONLY field mutated
                enriched.append(rid)
            elif st == "dead":
                log_event.log(
                    "lead_dead", role_id=rid,
                    company=cur.get("company", ""),
                    ats=cur.get("ats", ""), source=SOURCE,
                    details={"reason": (f"{SOURCE}: posting confirmed dead"),
                             "check_detail": payload})
                rej.append({"role_id": rid,
                            "role_key": (cur.get("role_key")
                                         or cur.get("company")),
                            "reason": f"{SOURCE}: posting confirmed dead",
                            "worker": SOURCE})
                dead_marked.append(rid)
                fitems.remove(cur)
                del by_id[rid]

        queue_io.atomic_write_json(STD_Q, fitems)
        queue_io.atomic_write_json(REJ_Q, rej)
        print(f"writes: enriched={len(enriched)} dead={len(dead_marked)} "
              f"skipped_race={len(skipped_race)}")

    # ---- append-only telemetry --------------------------------------
    blocked = {rid: p for rid, (st, p) in outcomes.items() if st == "blocked"}
    log_event.log(
        "scan_summary", role_id="", company="", source=SOURCE,
        details={"run": "description-enrichment",
                 "targets": len(targets),
                 "enriched": len(enriched),
                 "dead_marked": len(dead_marked),
                 "enrichment_blocked": len(blocked),
                 "skipped_race": len(skipped_race),
                 "unattempted_rate_limited": len(targets) - len(outcomes),
                 "http_stats": dict(HTTP_STATS),
                 "enriched_role_ids": enriched,
                 "dead_role_ids": dead_marked,
                 "blocked_reasons": {rid: p for rid, p in blocked.items()}})

    report = {"targets": len(targets), "enriched": enriched,
              "dead_marked": dead_marked, "blocked": blocked,
              "skipped_race": skipped_race,
              "unattempted": [e.get("role_id") for e in targets
                              if e.get("role_id") not in outcomes],
              "rate_limited": rate_limited,
              "http_stats": dict(HTTP_STATS)}
    with open(os.path.join(HOME, "hidden_files",
                           f"description-enrichment-{TS}.json"), "w") as f:
        json.dump(report, f, indent=2)
    print(json.dumps({k: (len(v) if isinstance(v, (list, dict)) else v)
                         for k, v in report.items()
                         if k != "blocked"}, indent=2))
    print("blocked reasons:")
    for rid, reason in sorted(blocked.items()):
        print(f"  {rid[:50]:50s} {reason[:90]}")
    return 2 if rate_limited else 0


if __name__ == "__main__":
    sys.exit(main())
