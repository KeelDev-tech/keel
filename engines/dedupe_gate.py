#!/usr/bin/env python3
"""dedupe_gate.py — permanent discovery-side dedupe gate (ARM 51).

Before discovery logs `lead_discovered` (or appends to a queue), every
candidate is checked against BOTH:
  1. ledger SUBMITTED rows — same normalized posting URL, or same
     employer + equivalent title
  2. the standard queue — same normalized posting URL, or same
     employer + equivalent title

Evidence:
  - 2026-09-15 YC steered arm re-logged already-SUBMITTED Shadeform as
    `lead_discovered` (re-harvest leak).
  - 2026-09-14/15 Source/Source Network double submission: company-name
    dedupe missed on "Source" vs "Source Network (Source Inc., DefraDB)";
    both rows share confirmation_url
    https://source.network/careers/chief-of-staff. URL matching is the
    canonical fix — names collide, URLs don't.

URL canonicalization (fail-closed, stronger than apply_loop._norm_url):
  - lowercase scheme+host, drop default ports
  - strip fragment, strip trailing slash
  - drop TRACKING params (utm_*, gclid, fbclid, msclkid, ...) but KEEP
    meaningful ones (gh_jid, job ids)
  - empty / javascript: / mailto: / "#" URLs canonicalize to "" (no URL)

Verdict levels:
  - "duplicate" (hard): normalized-URL match on the ledger (any of
    posting_url/application_url/confirmation_url/url) or the queue, OR
    company containment + normalized-title equality.
  - "suspect" (advisory only): company containment alone, or title
    equality alone. Never blocks intake — e.g. "Source" vs "Sourcegraph"
    is a different company.
  - "fresh": none of the above.

Backfilled-ledger company|employer mismatch is already fixed at the
consumer side (apply_loop.already_submitted checks company|employer and
normalized URLs since 2026-09-15); this gate is the discovery-side
permanent fix and deliberately re-checks company|employer itself.
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
              "confirmation_url", "url")

# Tracking params that never identify a posting. Everything else is kept.
_TRACKING_PAT = re.compile(
    r"^(utm_.+|gclid|fbclid|msclkid|mc_cid|mc_eid|igshid|vero_.+"
    r"|ref$|referrer|source$|campaign|cid$|sid$|trk|_hsenc|_hsmi)$",
    re.IGNORECASE)

_CORP_SUFFIX_PAT = re.compile(
    r"\b(inc|llc|corp|corporation|co|ltd|plc|gmbh|pty|technologies|labs"
    r"|company|group|holdings|ventures)\b")


def canonical_url(u):
    """Canonical posting-URL form for dedupe comparison."""
    u = (u or "").strip()
    if not u or u.startswith(("javascript:", "mailto:")) or u == "#":
        return ""
    try:
        p = urlparse(u if "://" in u else "https://" + u)
    except Exception:
        return ""
    host = (p.hostname or "").lower()
    if not host:
        return ""
    if p.port and not (p.scheme == "https" and p.port == 443
                       or p.scheme == "http" and p.port == 80):
        host = f"{host}:{p.port}"
    kept = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
            if not _TRACKING_PAT.match(k)]
    kept.sort()
    path = p.path.rstrip("/") or "/"
    return urlunparse((p.scheme.lower() or "https", host, path, "",
                       urlencode(kept), ""))


def _norm_name(s):
    s = (s or "").lower()
    s = _CORP_SUFFIX_PAT.sub(" ", s)
    return re.sub(r"[^a-z0-9]", "", s)


def _norm_title(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _company(e):
    return e.get("company") or e.get("employer") or ""


def _url_of(e):
    for k in URL_FIELDS:
        v = (e.get(k) or "").strip()
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
        v = (e.get(k) or "").strip()
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
    the ledger + queue JSON per call — same verdict ladder, O(1)-ish
    lookups. An explicit `index` (or explicit row lists) overrides.
    Note: the index covers ALL live queues, a deliberate superset of
    the legacy standard-queue-only scan.

    `urls` (optional): the candidate's URL fields as a list. When given
    it is checked across ALL fields; otherwise the single `url` arg is
    used, preserving the check_candidate(company, title, url) call
    shape for existing callers.
    """
    cand_urls = urls if urls is not None else ([url] if url else [])
    if ledger_rows is None and queue_entries is None:
        idx = index
        if idx is None:
            try:
                from dedupe_index import get_index
                idx = get_index()
            except Exception:
                idx = None
        if idx is not None:
            return idx.check_candidate(company, title, url)
        ledger_rows = [r for r in _load(LEDGER)
                       if r.get("status") == "SUBMITTED"]
        queue_entries = _load(STANDARD_QUEUE)
        return _check_rows(company, title, cand_urls, ledger_rows,
                           queue_entries)
    if ledger_rows is None:
        ledger_rows = [r for r in _load(LEDGER)
                       if r.get("status") == "SUBMITTED"]
    if queue_entries is None:
        queue_entries = _load(STANDARD_QUEUE)
    return _check_rows(company, title, cand_urls, ledger_rows, queue_entries)


def _check_rows(company, title, urls, ledger_rows, queue_entries):
    # Ledger-side filter: only SUBMITTED rows ever block as
    # duplicate_of_submitted. Explicit-ledger_rows callers (e.g. sweep)
    # pass the full unfiltered ledger, so the filter must live here —
    # a REJECTED row must not yield ("duplicate", kind=...) and block
    # legitimate re-discovery. Queue entries are deliberately NOT
    # status-filtered.
    ledger_rows = [r for r in (ledger_rows or [])
                   if isinstance(r, dict) and r.get("status") == "SUBMITTED"]
    # Candidate side: check across ALL url fields, matching the
    # ledger/queue row scan below.
    cand_urls = [cu for cu in dict.fromkeys(
        canonical_url(u) for u in (urls or [])) if cu]
    nc, nt = _norm_name(company), _norm_title(title)

    for cu in cand_urls:
        for r in ledger_rows:
            if isinstance(r, dict) and any(
                    canonical_url(r.get(k)) == cu for k in URL_FIELDS):
                return ("duplicate", {
                    "kind": "duplicate_of_submitted",
                    "match": "posting_url",
                    "ledger_role_id": r.get("role_id"),
                    "ledger_company": r.get("company") or r.get("employer"),
                    "url": cu})
        for e in queue_entries:
            if isinstance(e, dict) and any(
                    canonical_url(e.get(k)) == cu for k in URL_FIELDS):
                return ("duplicate", {
                    "kind": "duplicate_in_queue",
                    "match": "posting_url",
                    "queue_role_id": e.get("role_id"),
                    "queue_status": e.get("status"),
                    "url": cu})

    if nc and len(nc) >= 4:
        for r in ledger_rows:
            if not isinstance(r, dict):
                continue
            rc = _norm_name(r.get("company") or r.get("employer"))
            if not rc:
                continue
            contained = nc in rc or rc in nc
            title_eq = nt and _norm_title(r.get("title")) == nt
            if contained and title_eq:
                return ("duplicate", {
                    "kind": "duplicate_of_submitted",
                    "match": "employer_title",
                    "ledger_role_id": r.get("role_id"),
                    "ledger_company": r.get("company") or r.get("employer"),
                    "ledger_title": r.get("title")})
        for e in queue_entries:
            if not isinstance(e, dict):
                continue
            ec = _norm_name(_company(e))
            if not ec:
                continue
            contained = nc in ec or ec in nc
            title_eq = nt and _norm_title(e.get("title")) == nt
            if contained and title_eq:
                return ("duplicate", {
                    "kind": "duplicate_in_queue",
                    "match": "employer_title",
                    "queue_role_id": e.get("role_id"),
                    "queue_status": e.get("status")})
        # Advisory only: name containment without title equality, or
        # title equality without name containment.
        for r in ledger_rows:
            if not isinstance(r, dict):
                continue
            rc = _norm_name(r.get("company") or r.get("employer"))
            rt = _norm_title(r.get("title"))
            if (rc and (nc in rc or rc in nc)) or (nt and rt and rt == nt):
                return ("suspect", {
                    "kind": "possible_duplicate",
                    "ledger_role_id": r.get("role_id"),
                    "ledger_company": r.get("company") or r.get("employer"),
                    "note": "name or title matched alone — human judgment; "
                            "does not block intake"})
    return ("fresh", {})


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
            idx = get_index()
        except Exception:
            idx = None
    if not use_rows and idx is None:
        ledger_rows = [r for r in _load(LEDGER)
                       if r.get("status") == "SUBMITTED"]
        queue_entries = _load(STANDARD_QUEUE)
        use_rows = True
    fresh, dupes = [], []
    for e in entries:
        if not isinstance(e, dict):
            continue
        if use_rows:
            verdict, evidence = check_candidate(
                _company(e), e.get("title"), _url_of(e),
                ledger_rows=ledger_rows, queue_entries=queue_entries,
                urls=_urls_of(e))
        else:
            verdict, evidence = idx.check_candidate(
                _company(e), e.get("title"), _url_of(e),
                description=e.get("description"))
        if verdict == "duplicate":
            e = dict(e)
            e["dedupe_evidence"] = evidence
            dupes.append(e)
        else:
            fresh.append(e)
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
