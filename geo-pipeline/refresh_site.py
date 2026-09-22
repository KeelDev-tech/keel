#!/usr/bin/env python3
"""Keel GEO pipeline — refresh the public site's proof figure.

Single-sources the site's *current* "verified submissions" statements from
docs/geo/stats.json (written by the canonical recount.py). The historical
"at launch" figure (55) is a canon and is never touched.

Why this exists: the site/ subtree shipped with a hardcoded 55 while the
README moved 211 -> 214, because README refreshes never regenerated the
site. Any future submission-count refresh must run:
    python3 geo-pipeline/recount.py && python3 geo-pipeline/refresh_site.py
then commit site/ + docs/geo/stats.json + README.md together.

Fail-closed: the count must be a positive int from stats.json; every file
must match at least one anchored current-count slot, otherwise no file is
written. Writes are atomic (temp file + rename). Idempotent.

Slots refreshed (current count only):
  - "<N> verified submissions. Zero lies."  (meta/og/JSON-LD/llms headers)
  - <div class="proof-number"><N></div>      (site/index.html hero)
  - "holds <strong><N> verified submissions</strong> in its ledger."
Slots deliberately untouched: any "... at launch" historical statement.
"""

import json
import os
import re
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
STATS_JSON = os.path.join(REPO, "docs", "geo", "stats.json")
SITE_DIR = os.path.join(REPO, "site")


def fail(msg):
    print(f"refresh_site: FATAL: {msg}", file=sys.stderr)
    sys.exit(1)


def load_count():
    try:
        with open(STATS_JSON, "r", encoding="utf-8") as f:
            stats = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        fail(f"cannot read {STATS_JSON}: {e}")
    n = stats.get("verified_submissions_now")
    if not isinstance(n, int) or n <= 0:
        fail(f"stats.json has no positive int verified_submissions_now: {n!r}")
    return n, stats.get("counted_at", "unknown")


# (pattern, replacement-template, minimum matches per file set)
# "Zero lies." suffix appears ONLY on current-count statements; the
# historical "at launch" statements never carry it, so they can't match.
RULES = [
    (re.compile(r"\d+ verified submissions\. Zero lies\."),
     lambda n: f"{n} verified submissions. Zero lies.",
     "zero-lies headers"),
    (re.compile(r'(<div class="proof-number">)\d+(</div>)'),
     lambda n: rf"\g<1>{n}\g<2>",
     "proof-number hero"),
    (re.compile(r"(holds <strong>)\d+( verified submissions</strong> in its ledger\.)"),
     lambda n: rf"\g<1>{n}\g<2>",
     "proof paragraph"),
]

FILES = ["index.html", "llms.txt", "llms-full.txt"]


def main():
    n, counted_at = load_count()
    print(f"refresh_site: count={n} (stats.json counted_at={counted_at})")
    pending = {}
    total_hits = 0
    for name in FILES:
        path = os.path.join(SITE_DIR, name)
        try:
            with open(path, "r", encoding="utf-8") as f:
                text = f.read()
        except OSError as e:
            fail(f"cannot read {path}: {e}")
        hits = 0
        for pattern, repl, label in RULES:
            text, k = pattern.subn(repl(n), text)
            hits += k
        if hits == 0:
            fail(f"{path}: no current-count slot matched; refusing partial update")
        pending[path] = text
        total_hits += hits
        print(f"refresh_site: {name}: {hits} slot(s) -> {n}")
    for path, text in pending.items():
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path),
                                   prefix=".site.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
            os.replace(tmp, path)
        except OSError as e:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            fail(f"cannot write {path}: {e}")
    print(f"refresh_site: done, {total_hits} replacement(s) across {len(pending)} file(s)")


if __name__ == "__main__":
    main()
