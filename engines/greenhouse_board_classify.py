#!/usr/bin/env python3
"""Classify Greenhouse boards by reCAPTCHA Enterprise status (board-level).

Enterprise status is board/account-level: sampling ONE posting page per board
classifies the whole board. The registry written here feeds sweep targeting so
discovery aims at directly-submittable boards (see
discovery/_sweep-worker-prompt-snippet-apidirect.md).

Detection reuses api_direct_detect.is_enterprise_board() — the same
recaptcha-enterprise.js marker heuristic the capability-radar detection
layer relies on. No duplicated logic: import it, don't re-implement it.

MONTHLY RE-VERIFICATION rides the existing `ats-capability-radar-monthly`
cron — this script gets NO separate schedule. Suspected flips are reported
through the existing `edge_flip` gate path (engines/application-executor/
edge_probe.py); a FLIP never auto-wires into apply_loop.py.

Rate limits: polite delay between fetches; HTTP 429 = HARD STOP (exit 75,
nothing written). Boards that cannot be classified (dead postings, fetch
errors, no marker signal) are left OUT of the registry — fail closed, never
assert "clean" on missing evidence.

Usage: python3 greenhouse_board_classify.py [--limit N] [--boards a,b,c]
"""

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
from safe_http import urlopen as safe_urlopen
from safe_io import atomic_json, file_lock, read_json
import api_direct_detect as add  # noqa: E402  (shared Enterprise heuristic)

from keel_paths import HOME, DATA  # noqa: E402
REGISTRY = os.path.join(HOME, "hidden_files/greenhouse-board-enterprise.json")
BOARD_PAT = re.compile(r"(?:job-boards|boards)\.greenhouse\.io/([a-z0-9_\-]+)", re.I)
POSTING_PAT = re.compile(
    r"(?:job-boards|boards)\.greenhouse\.io/([a-z0-9_\-]+)/jobs/(\d+)", re.I)
DELAY = 2.0  # polite gap between board fetches


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def harvest_boards():
    """board -> one sample posting URL, from queue + ledger.

    Only boards with a real posting URL are harvested: the Enterprise
    marker lives on posting pages, so a board with no known posting has
    nothing classifiable to fetch."""
    boards = {}
    files = [os.path.join(DATA, "queues/standard-queue.json"),
             os.path.join(DATA, "queues/needs_input-queue.json"),
             os.path.join(DATA, "application-ledger.json")]
    for fp in files:
        if not os.path.exists(fp):
            continue
        try:
            data = json.load(open(fp))
        except Exception:
            continue
        ents = data if isinstance(data, list) else data.get("entries",
                                                            data.get("queue", []))
        for e in ents:
            blob = json.dumps(e)
            for m in POSTING_PAT.finditer(blob):
                url = m.group(0)
                if not url.startswith("http"):
                    url = "https://" + url
                boards.setdefault(m.group(1).lower(), url)
    return boards


def fetch_html(url):
    req = urllib.request.Request(url, headers=add.UA)
    with safe_urlopen(req, timeout=20) as r:
        return r.read().decode("utf-8", "replace")


def classify(board, posting_url):
    """Return True (Enterprise) / False (clean) / None (unclassifiable)."""
    try:
        html = fetch_html(posting_url)
    except urllib.error.HTTPError as e:
        if e.code == 429:
            raise SystemExit("429 rate limit — HARD STOP, nothing written")
        return None
    except Exception:
        return None
    # The marker check is the shared heuristic from api_direct_detect.
    return add.is_enterprise_board(html)


def main(argv):
    limit = None
    only = None
    for i, a in enumerate(argv):
        if a == "--limit" and i + 1 < len(argv):
            limit = int(argv[i + 1])
        if a == "--boards" and i + 1 < len(argv):
            only = {b.strip().lower() for b in argv[i + 1].split(",")}
    boards = harvest_boards()
    if only:
        boards = {b: u for b, u in boards.items() if b in only}
    names = sorted(boards)
    if limit:
        names = names[:limit]
    if not names:
        print("no boards to classify")
        return 0

    reg = {}
    if os.path.exists(REGISTRY):
        try:
            reg = json.load(open(REGISTRY))
        except Exception:
            reg = {}
    t0 = time.time()
    done, ent, clean, skip = 0, 0, 0, 0
    for b in names:
        verdict = classify(b, boards[b])
        ts = utcnow()
        if verdict is None:
            skip += 1
        elif verdict:
            reg[b] = {"enterprise": True, "checked_ts": ts}
            ent += 1
        else:
            reg[b] = {"enterprise": False, "checked_ts": ts}
            clean += 1
        done += 1
        if done < len(names):
            time.sleep(DELAY)
    with file_lock(REGISTRY + ".lock"):
        current = read_json(REGISTRY, missing={})
        if not isinstance(current, dict):
            raise ValueError("malformed board registry")
        # Only boards actually inspected in this pass may replace current data.
        for board in names:
            if board in reg and reg[board].get("checked_ts", "") >= current.get(board, {}).get("checked_ts", ""):
                current[board] = reg[board]
        atomic_json(REGISTRY, current)
        reg = current
    dt = time.time() - t0
    print(f"boards={done} enterprise={ent} clean={clean} skipped={skip} "
          f"registry={len(reg)} elapsed={dt:.1f}s -> {REGISTRY}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
