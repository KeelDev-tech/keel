#!/usr/bin/env python3
"""Discovery deduplication using exact posting identities and bounded snapshots.

Names, title similarity and company containment are advisory only. Durable
coverage is restricted to explicitly configured sources. Missing/corrupt/stale
coverage yields suspect, never fresh. This gate does not authorize submission.
"""

import argparse
import json
import os
import re
import sys
from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
from keel_paths import HOME as PIPE, DATA  # noqa: E402 — repo root; never the private pipeline path
LEDGER = os.path.join(DATA, "application-ledger.json")
STANDARD_QUEUE = os.path.join(PIPE, "data", "queues", "standard-queue.json")

URL_FIELDS = ("ats_url", "application_url", "posting_url",
              "confirmation_url", "url", "job_url", "jobUrl", "absolute_url")

# Tracking params that never identify a posting. Everything else is kept.
_TRACKING_PAT = re.compile(
    r"^(utm_.+|gclid|fbclid|msclkid|mc_cid|mc_eid|igshid|vero_.+"
    r"|_hsenc|_hsmi)$",
    re.IGNORECASE)

_CORP_SUFFIX_PAT = re.compile(
    r"\b(inc|llc|corp|corporation|co|ltd|plc|gmbh|pty|technologies|labs"
    r"|company|group|holdings|ventures)\b")


def canonical_url(u):
    """Conservative URL key; retain meaningful queries and case-sensitive paths."""
    if not isinstance(u, str) or len(u) > 8192 or any(ord(c) < 32 or ord(c) == 127 for c in u):
        return ""
    u = u.strip()
    if not u or u == "#" or any(ord(c) <= 32 or ord(c) == 127 for c in u) or "\\" in u:
        return ""
    try:
        p = urlparse(u if "://" in u else "https://" + u)
        if p.scheme.lower() not in {"http", "https"} or p.username is not None or p.password is not None:
            return ""
        host = (p.hostname or "").encode("idna").decode("ascii").lower()
        port = p.port
        if not host:
            return ""
        host = "[" + host + "]" if ":" in host else host
        if port is not None and not (p.scheme.lower() == "https" and port == 443 or p.scheme.lower() == "http" and port == 80):
            host = f"{host}:{port}"
        kept = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True, errors="strict")
                if not _TRACKING_PAT.match(k)]
        if len({k for k, v in kept}) == len(kept):
            kept.sort()
        path = p.path.rstrip("/") or "/"
        return urlunparse((p.scheme.lower(), host, path, p.params, urlencode(kept), p.fragment))
    except (ValueError, UnicodeError):
        return ""


def _norm_name(s):
    s = s.lower() if isinstance(s, str) else ""
    s = _CORP_SUFFIX_PAT.sub(" ", s)
    return re.sub(r"[^a-z0-9]", "", s)


def _norm_title(s):
    return re.sub(r"[^a-z0-9]", "", s.lower() if isinstance(s, str) else "")


def _company(e):
    return e.get("company") or e.get("employer") or ""


def _url_of(e):
    for k in URL_FIELDS:
        v = e.get(k).strip() if isinstance(e.get(k), str) else ""
        if v:
            return v
    return ""


def _urls_of(e):
    """All non-empty candidate URL fields, in URL_FIELDS order.

    The ledger/queue side scans every URL field of a row; the candidate
    side must too — otherwise a candidate whose application_url matches a
    SUBMITTED row but whose ats_url is fresh verifies "fresh".
    """
    urls = []
    for k in URL_FIELDS:
        v = e.get(k).strip() if isinstance(e.get(k), str) else ""
        if v:
            urls.append(v)
    return urls


def _load(path):
    try:
        d = json.load(open(path))
    except (FileNotFoundError, json.JSONDecodeError):
        return []
    return d if isinstance(d, list) else d.get("rows", d.get("entries", []))


def _index_url(rows):
    """canonical_url -> list of row descriptors (fail-safe: skip unparseable)."""
    idx = {}
    for r in rows:
        if not isinstance(r, dict):
            continue
        for k in URL_FIELDS:
            cu = canonical_url(r.get(k))
            if cu:
                idx.setdefault(cu, []).append(r)
    return idx


def check_candidate(company, title, url, ledger_rows=None,
                    queue_entries=None, index=None, urls=None):
    """Return (verdict, evidence): verdict in
    {"duplicate", "suspect", "fresh"}; evidence is a dict with
    kind + the matching row/entry identifiers, or {} when fresh.

    When ledger_rows/queue_entries are not passed, the persistent
    dedupe index (dedupe_index.get_index) is used instead of reloading
    the ledger + queue JSON per call. Identity lookup is indexed, while
    bounded source hashing verifies freshness. An explicit `index` (or explicit row lists) overrides.
    The index covers every explicitly configured queue; it never discovers
    backup queues by globbing.

    `urls` (optional): the candidate's URL fields as a list. When given
    it is checked across ALL fields; otherwise the single `url` arg is
    used, preserving the check_candidate(company, title, url) call
    shape for existing callers.
    """
    cand_urls = urls if urls is not None else ([url] if url else [])
    if ledger_rows is None and queue_entries is None:
        try:
            if index is None:
                from dedupe_index import get_index
                index = get_index(ledger_path=LEDGER, queue_path=STANDARD_QUEUE)
            return index.check_candidate(company, title, url, urls=cand_urls)
        except Exception as exc:
            return "suspect", {"kind": "index_unavailable", "error": type(exc).__name__,
                               "execution_authorized": False}
    # Explicit snapshots retain the legacy call shape. Partial omitted inputs
    # are read conservatively rather than silently treating an unreadable file
    # as an empty source.
    try:
        if ledger_rows is None:
            ledger_rows = _load_strict(LEDGER)
        if queue_entries is None:
            queue_entries = _load_strict(STANDARD_QUEUE)
    except (OSError, ValueError, TypeError):
        return "suspect", {"kind": "source_unavailable", "execution_authorized": False}
    return _check_rows(company, title, cand_urls, ledger_rows, queue_entries)


def _load_strict(path):
    from dedupe_index import _regular_bytes, _decode
    data = _decode(_regular_bytes(path))
    if isinstance(data, dict):
        fields = [k for k in ("rows", "entries", "items", "leads") if k in data]
        if len(fields) != 1:
            raise ValueError("ambiguous snapshot")
        data = data[fields[0]]
    if not isinstance(data, list) or any(not isinstance(r, dict) for r in data):
        raise ValueError("invalid row list")
    return data


def _check_rows(company, title, urls, ledger_rows, queue_entries):
    from dedupe_index import check_rows
    return check_rows(company, title, urls, ledger_rows, queue_entries)


def filter_batch(entries, ledger_rows=None, queue_entries=None, index=None):
    """Split a discovery batch into (fresh, duplicates).

    Duplicates are returned with a `dedupe_evidence` field attached so the
    worker can log them as `gate_encountered` with
    `details.gate: duplicate_of_submitted` instead of `lead_discovered`.
    When row lists are not passed, the persistent dedupe index is
    resolved once for the whole batch (no per-candidate disk reload).
    """
    idx = index
    use_rows = not (ledger_rows is None and queue_entries is None)
    if not use_rows and idx is None:
        try:
            from dedupe_index import get_index
            idx = get_index(ledger_path=LEDGER, queue_path=STANDARD_QUEUE)
        except Exception:
            idx = None
    entries = [e for e in entries if isinstance(e, dict)]
    if not use_rows and idx is not None and callable(getattr(idx, "check_batch", None)):
        results = idx.check_batch(entries)
    else:
        results = [check_candidate(_company(e), e.get("title"), _url_of(e),
            ledger_rows=ledger_rows, queue_entries=queue_entries, index=idx, urls=e) for e in entries]
    from dedupe_index import keys_for_urls
    fresh, dupes, seen = [], [], set()
    for entry, (verdict, evidence) in zip(entries, results):
        keys, conflict = keys_for_urls(entry)
        if not conflict and keys & seen:
            verdict, evidence = "duplicate", {"kind": "duplicate_in_batch", "match": "posting_identity",
                                               "execution_authorized": False}
        if verdict == "duplicate":
            entry = dict(entry)
            entry["dedupe_evidence"] = evidence
            dupes.append(entry)
        else:
            # Keep advisory/coverage evidence visible without blocking intake.
            if verdict == "suspect":
                entry = dict(entry)
                entry["dedupe_advisory"] = evidence
            fresh.append(entry)
            if not conflict:
                seen.update(keys)
    return fresh, dupes


def main(argv=None):
    ap = argparse.ArgumentParser(description="discovery dedupe gate")
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("check", help="check one candidate")
    c.add_argument("--company", default="")
    c.add_argument("--title", default="")
    c.add_argument("--url", default="")

    b = sub.add_parser("batch", help="filter a JSON batch file")
    b.add_argument("batch", help="JSON list of entry dicts")
    b.add_argument("--fresh-out", default="")
    b.add_argument("--dupes-out", default="")

    a = ap.parse_args(argv)
    if a.cmd == "check":
        verdict, evidence = check_candidate(a.company, a.title, a.url)
        print(json.dumps({"verdict": verdict, "evidence": evidence},
                         indent=2))
        return 0 if verdict == "fresh" else 1
    entries = _load(a.batch)
    fresh, dupes = filter_batch(entries)
    print(json.dumps({
        "fresh": len(fresh), "duplicates": len(dupes),
        "dupes": [{"role_id": d.get("role_id"),
                   "evidence": d.get("dedupe_evidence")} for d in dupes]},
        indent=2))
    if a.fresh_out:
        json.dump(fresh, open(a.fresh_out, "w"), indent=2)
    if a.dupes_out:
        json.dump(dupes, open(a.dupes_out, "w"), indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
