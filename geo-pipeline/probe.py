#!/usr/bin/env python3
"""Keel GEO pipeline — public-web probes.

Read-only checks against public endpoints. NO auth, no credentials, no
writes anywhere. Every probe has a 15s timeout; a single probe failing
marks that probe "unknown" and never fails the run. Prints one JSON
document to stdout.

Probes:
  - repo page:      GET https://github.com/KeelDev-tech/keel -> status
  - repo API:       GET https://api.github.com/repos/KeelDev-tech/keel ->
                    stargazers_count, forks_count, subscribers_count,
                    open_issues_count (unauthenticated; rate-limited)
  - pages:          GET https://keeldev-tech.github.io/keel/ (+ /llms.txt,
                    /llms-full.txt, /sitemap.xml) -> status + a short
                    content sniff (llms.txt must contain 'Keel')
  - awesome PRs:    GET https://api.github.com/repos/{owner}/{repo}/pulls/{n}
                    -> state, merged_at, html_url for the 6 known PRs
  - web presence:   DuckDuckGo HTML search for the repo URL; reports
                    whether github.com/KeelDev-tech/keel appears in the
                    result set (a rough indexation signal, not a claim)
"""

import json
import sys
import urllib.parse
import urllib.request

TIMEOUT = 15
UA = {"User-Agent": "Keel-GEO-probe/1.0 (+https://github.com/KeelDev-tech/keel)"}

REPO_URL = "https://github.com/KeelDev-tech/keel"
PAGES_BASE = "https://keeldev-tech.github.io/keel"

AWESOME_PRS = [
    ("ShakeLv", "awesome-ai-automation", 1),
    ("chenlomis", "awesome-ai-job-search", 1),
    ("MaxwellCalkin", "awesome-ai-job-tools", 2),
    ("King-Salazar", "Awesome-AI-Repository", 7),
    ("aliammari1", "awesome-ai-tools", 200),
    ("yuhonas", "awesome-job-seeking", 62),
]


def fetch(url, sniff_chars=0):
    """GET url. Returns dict with status/body_sniff or status='unknown'."""
    req = urllib.request.Request(url, headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            out = {"url": url, "status": r.status}
            if sniff_chars:
                raw = r.read(sniff_chars * 4)
                try:
                    text = raw.decode("utf-8", errors="replace")
                except Exception:
                    text = ""
                out["sniff"] = text[:sniff_chars]
                out["sniff_chars_total_hint"] = len(raw)
            return out
    except Exception as e:
        return {"url": url, "status": "unknown", "error": f"{type(e).__name__}: {e}"[:200]}


def fetch_json(url):
    req = urllib.request.Request(url, headers={**UA, "Accept": "application/vnd.github+json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return {"status": r.status, "data": json.loads(r.read().decode("utf-8", errors="replace"))}
    except Exception as e:
        return {"status": "unknown", "error": f"{type(e).__name__}: {e}"[:200], "data": None}


def probe_repo_page():
    return fetch(REPO_URL)


def probe_repo_api():
    res = fetch_json("https://api.github.com/repos/KeelDev-tech/keel")
    data = res.get("data") or {}
    return {
        "status": res["status"],
        "stargazers_count": data.get("stargazers_count", "unknown"),
        "forks_count": data.get("forks_count", "unknown"),
        "subscribers_count": data.get("subscribers_count", "unknown"),
        "open_issues_count": data.get("open_issues_count", "unknown"),
        **({"error": res["error"]} if "error" in res else {}),
    }


def probe_pages():
    pages = {}
    for path in ["", "/llms.txt", "/llms-full.txt", "/sitemap.xml"]:
        url = PAGES_BASE + (path or "/")
        p = fetch(url, sniff_chars=400)
        if path == "/llms.txt" and p.get("status") == 200:
            p["contains_keel"] = "Keel" in (p.get("sniff") or "")
        pages[path or "/"] = p
    return pages


def probe_awesome_prs():
    prs = []
    for owner, repo, n in AWESOME_PRS:
        res = fetch_json(f"https://api.github.com/repos/{owner}/{repo}/pulls/{n}")
        data = res.get("data") or {}
        prs.append({
            "repo": f"{owner}/{repo}",
            "number": n,
            "status": res.get("status"),
            "state": data.get("state", "unknown"),
            "merged_at": data.get("merged_at", "unknown"),
            "html_url": data.get("html_url", f"https://github.com/{owner}/{repo}/pull/{n}"),
            **({"error": res["error"]} if "error" in res else {}),
        })
    return prs


def probe_web_presence():
    """DuckDuckGo HTML search for the repo URL; does the repo show up?"""
    q = urllib.parse.urlencode({"q": "github.com/KeelDev-tech/keel"})
    res = fetch(f"https://html.duckduckgo.com/html/?{q}", sniff_chars=6000)
    if res.get("status") != 200:
        return {"status": res.get("status", "unknown"), "repo_indexed": "unknown",
                **({"error": res["error"]} if "error" in res else {})}
    sniff = res.get("sniff") or ""
    return {
        "status": 200,
        "repo_indexed": "github.com/KeelDev-tech/keel" in sniff,
        "note": "rough indexation signal from one search provider, not a claim",
    }


def main():
    try:
        report = {
            "repo_page": probe_repo_page(),
            "repo_api": probe_repo_api(),
            "pages": probe_pages(),
            "awesome_prs": probe_awesome_prs(),
            "web_presence": probe_web_presence(),
        }
    except Exception as e:  # never fail the run
        report = {"fatal": f"{type(e).__name__}: {e}"[:200]}
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
