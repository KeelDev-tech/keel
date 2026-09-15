#!/usr/bin/env python3
"""ATS capability radar — edge-case probe harness.

Read-only HTTP/dry-run re-verification of the transport-layer verdicts in
edge_case_registry.json. Keeps the pipeline on the edge of emerging ATS
technology: platforms change their defenses (new CAPTCHA types, API changes,
new vendors) and this harness detects verdict FLIPs instead of letting the
detection rules rot.

HARD RULES:
  - Probes are READ-ONLY. Never live-submit, never create accounts, never
    solve CAPTCHAs. Only GET requests against posting/apply pages and the
    documented public endpoints.
  - Never invent URLs. Entries without sample_urls report UNTESTABLE.
  - A FLIP is never auto-wired into apply_loop.py. The harness logs an
    edge_flip telemetry event, updates the registry verdict (usually to
    "unknown" + a supervised-re-verification note), and surfaces it in the
    report. Transport wiring changes require supervised verification.

Per entry:
  CONFIRMED    observed verdict == recorded verdict (last_retested updated)
  FLIP         observed verdict differs decisively -> telemetry + registry update
  INCONCLUSIVE probe could not decide (fetch failure, dead posting, partial
               walls) — no flip, no confirmation, noted in the report
  UNTESTABLE   no sample URLs in the registry entry
  NEEDS_REVIEW unknown-probe findings suggest a possible direct path — stays
               "unknown", flagged for a human/supervised pass

scan_new_ats() sweeps queue + ledger URLs for ATS values absent from the
registry (via ats.detect_ats, plus repeated unrecognized hostnames). A new
one logs a new_ats_detected telemetry event and gets an "unknown" registry
entry with probe findings — the monthly cron re-runs this, so anything the
discovery snippet missed is still caught.

Usage:
  python3 edge_probe.py            # full run: probes + new-ATS scan, reports
  python3 edge_probe.py --monthly  # same, but prints one summary line for cron
  python3 edge_probe.py --no-scan  # probes only
  python3 edge_probe.py --scan-only

Reports: hidden_files/edge-probes/edge-probe-<ts>.json + .md
"""

import glob
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

import ats as ats_mod  # noqa: E402
ENTERPRISE_JS_PAT = re.compile(r'recaptcha[^"\']*enterprise\.js', re.I)
import log_event as telemetry  # noqa: E402

from keel_paths import HOME as PIPE  # noqa: E402
REGISTRY_PATH = os.path.join(BASE, "edge_case_registry.json")
REPORT_DIR = os.path.join(PIPE, "hidden_files", "edge-probes")

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/126.0.0.0 Safari/537.36"}

MAX_SAMPLES_PER_ENTRY = 3
REQUEST_DELAY = 1.0  # polite pacing between live probes

LEVER_HCAPTCHA_MARKERS = ("hcaptcha", "e33f87f8-88ec-4e1a-9a13-df9bbb1d8120")
CAPTCHA_MARKERS = ("recaptcha", "hcaptcha", "turnstile", "enterprise.js",
                   "captcha")

# Hostnames that are job boards / aggregators / classifieds, not ATS
# platforms — never auto-onboard these as "new ATS".
SKIP_HOSTS = {
    "jobicy.com", "www.jobicy.com", "indeed.com", "www.indeed.com",
    "linkedin.com", "www.linkedin.com", "talent.com", "www.talent.com",
    "winebusiness.com", "www.winebusiness.com", "hcareers.com",
    "www.hcareers.com", "ziprecruiter.com", "www.ziprecruiter.com",
    "glassdoor.com", "www.glassdoor.com",
}


class FetchError(Exception):
    pass


class Fetcher:
    """Injectable HTTP GET. Real network lives only in RealFetcher."""

    def get(self, url):
        """Return (status:int, body:str). Raise FetchError on any failure."""
        raise NotImplementedError


class RealFetcher(Fetcher):
    def __init__(self, delay=REQUEST_DELAY, timeout=15):
        self.delay = delay
        self.timeout = timeout

    def get(self, url):
        time.sleep(self.delay)
        req = urllib.request.Request(url, headers=UA)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return r.status, r.read().decode("utf-8", "replace")
        except Exception as e:
            raise FetchError(f"{type(e).__name__}: {e}")


def today():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Probe protocols
# ---------------------------------------------------------------------------

def _inconclusive(reason):
    return {"observed": None, "status": "INCONCLUSIVE", "note": reason}


def parse_board_url(url):
    """Parse a direct Greenhouse board URL into (board, job_id).

    Public-edition replacement for the private greenhouse_direct helper.
    Raises ValueError when the URL is not a direct board URL.
    """
    m = re.search(r"(?:job-boards|boards)\.greenhouse\.io/([a-z0-9_\-]+)/jobs/(\d+)",
                  url or "", re.I)
    if not m:
        raise ValueError("not a direct Greenhouse board URL: %r" % (url,))
    return m.group(1), m.group(2)


def probe_greenhouse(entry, fetch):
    """Re-run the Enterprise-marker check on direct board URLs."""
    verdicts = []
    for url in entry.get("sample_urls", [])[:MAX_SAMPLES_PER_ENTRY]:
        try:
            board, job_id = parse_board_url(url)
        except ValueError:
            return _inconclusive(f"not a direct board URL: {url[:60]}")
        canonical = f"https://job-boards.greenhouse.io/{board}/jobs/{job_id}"
        try:
            status, html = fetch.get(canonical)
        except FetchError as e:
            return _inconclusive(f"fetch failed ({e}) — cannot judge")
        if status != 200:
            return _inconclusive(f"HTTP {status} — posting may be dead, "
                                 "not a platform signal")
        verdicts.append("browser_only" if ENTERPRISE_JS_PAT.search(html or "")
                        else "viable_direct")
    if not verdicts:
        return _inconclusive("no fetchable samples")
    if len(set(verdicts)) != 1:
        return _inconclusive(f"samples disagree: {verdicts}")
    return {"observed": verdicts[0], "status": None,
            "note": f"Enterprise marker {'present' if verdicts[0] == 'browser_only' else 'absent'} "
                    f"on {len(verdicts)} sample(s)"}


def _parse_lever(url):
    m = re.search(r"jobs\.lever\.co/([^/?#]+)/([0-9a-f-]{8,})", url, re.I)
    return (m.group(1), m.group(2)) if m else (None, None)


def probe_lever(entry, fetch):
    """Re-probe both Lever walls: API-key requirement + hCaptcha on apply.

    Dead samples (posting 404s on both endpoints) are skipped — a dead
    posting is not a platform signal. INCONCLUSIVE only when no sample
    yields any wall data.
    """
    downs, ups, unknowns = [], [], []
    judged = 0
    for url in entry.get("sample_urls", [])[:MAX_SAMPLES_PER_ENTRY]:
        org, jid = _parse_lever(url)
        if not org:
            return _inconclusive(f"unparseable Lever URL: {url[:60]}")
        sample_ups, sample_downs = [], []
        try:
            status, body = fetch.get(
                f"https://api.lever.co/v0/postings/{org}/{jid}")
            if status == 403 and "api key" in (body or "").lower():
                sample_ups.append("api_key_wall")
            elif status == 200:
                sample_downs.append("api_key_wall")
            elif status == 404:
                pass  # dead posting — not a wall signal
            else:
                unknowns.append(f"api_key_wall:HTTP{status}")
        except FetchError as e:
            unknowns.append(f"api_key_wall:fetch({e})")
        try:
            status2, html = fetch.get(f"https://jobs.lever.co/{org}/{jid}/apply")
            low = (html or "").lower()
            if status2 == 200 and any(m in low for m in LEVER_HCAPTCHA_MARKERS):
                sample_ups.append("hcaptcha_wall")
            elif status2 == 200:
                sample_downs.append("hcaptcha_wall")
            elif status2 == 404:
                pass  # dead posting — not a wall signal
            else:
                unknowns.append(f"hcaptcha_wall:HTTP{status2}")
        except FetchError as e:
            unknowns.append(f"hcaptcha_wall:fetch({e})")
        if sample_ups or sample_downs:
            judged += 1
            ups.extend(sample_ups)
            downs.extend(sample_downs)
    if downs:
        return {"observed": "unknown", "status": None,
                "note": "WALL DOWN: " + ", ".join(sorted(set(downs))) +
                        " — supervised re-verification required before any "
                        "transport wiring"}
    if ups:
        return {"observed": "refused", "status": None,
                "note": f"both walls intact on {judged} sample(s): " +
                        ", ".join(sorted(set(ups)))}
    return _inconclusive("no live sample yielded wall data" +
                         (": " + ", ".join(sorted(set(unknowns)))
                          if unknowns else ""))


def probe_http_render(entry, fetch):
    """Ashby-style: is the page still un-renderable over plain HTTP?"""
    for url in entry.get("sample_urls", [])[:MAX_SAMPLES_PER_ENTRY]:
        try:
            status, html = fetch.get(url)
        except FetchError as e:
            return _inconclusive(f"fetch failed ({e}) — cannot judge")
        if status != 200:
            return _inconclusive(f"HTTP {status} — posting may be dead")
        low = (html or "").lower()
        has_form = bool(re.search(r"<form[^>]*>", low))
        has_apply_fields = bool(re.search(
            r"(first.?name|last.?name|resume|cover.?letter|email)", low))
        if has_form and has_apply_fields and "application" in low:
            return {"observed": "unknown", "status": None,
                    "note": "server-rendered application form detected over "
                            "plain HTTP — supervised re-verification required"}
    return {"observed": "browser_only", "status": None,
            "note": "no server-rendered application form over plain HTTP "
                    "on sampled URLs"}


def probe_aggregator(entry, fetch):
    """TealHQ-style: still an aggregator hop, not a direct form?"""
    for url in entry.get("sample_urls", [])[:MAX_SAMPLES_PER_ENTRY]:
        try:
            status, html = fetch.get(url)
        except FetchError as e:
            return _inconclusive(f"fetch failed ({e}) — cannot judge")
        if status != 200:
            return _inconclusive(f"HTTP {status}")
        low = (html or "").lower()
        has_form = bool(re.search(r"<form[^>]*>", low))
        outbound = bool(re.search(r"(apply on company|continue on|employer (web)?site|"
                                  r"apply now|external.?apply)", low))
        if has_form and re.search(r"(first.?name|resume|upload)", low):
            return {"observed": "unknown", "status": None,
                    "note": "page now appears to host a direct application "
                            "form — supervised re-verification required"}
        if not outbound:
            return _inconclusive("aggregator markers not found on page")
    return {"observed": "browser_only", "status": None,
            "note": "page still renders as an aggregator with an outbound "
                    "apply hop"}


def unknown_probe_findings(fetch, samples):
    """New/unknown ATS protocol step (a)-(c): endpoint, fields, walls."""
    findings = {"samples": [], "captcha_markers": [],
                "form_actions": [], "auth_walls": [], "api_hints": []}
    for url in samples[:MAX_SAMPLES_PER_ENTRY]:
        try:
            status, html = fetch.get(url)
        except FetchError as e:
            findings["samples"].append({"url": url, "error": str(e)})
            continue
        low = (html or "").lower()
        forms = re.findall(r"<form[^>]*action=[\"']([^\"']+)[\"']", low)
        caps = sorted({m for m in CAPTCHA_MARKERS if m in low})
        findings["samples"].append({"url": url, "http": status,
                                    "bytes": len(html or "")})
        findings["form_actions"].extend(forms)
        findings["captcha_markers"].extend(
            c for c in caps if c not in findings["captcha_markers"])
        if re.search(r"(sign.?in|log.?in|create.?account).{0,80}(required|to apply)",
                     low):
            findings["auth_walls"].append("login-gated application hint")
        api_hints = re.findall(r'["\'](/api/[^"\']+|https?://api\.[^"\']+)["\']',
                               html or "")
        findings["api_hints"].extend(h for h in api_hints[:5]
                                     if h not in findings["api_hints"])
    return findings


def probe_unknown(entry, fetch):
    """Rippling-style: verdict stays 'unknown'; findings only, never a flip
    to viable_direct. Suggestive findings -> NEEDS_REVIEW."""
    findings = unknown_probe_findings(fetch, entry.get("sample_urls", []))
    suggestive = (findings["form_actions"] and
                  not findings["captcha_markers"] and
                  not findings["auth_walls"])
    return {"observed": "unknown", "status": None,
            "needs_review": bool(suggestive),
            "note": "findings: " + json.dumps(findings)[:400],
            "findings": findings}


PROBES = {
    "greenhouse_detect": probe_greenhouse,
    "lever_wall_check": probe_lever,
    "http_render_check": probe_http_render,
    "aggregator_check": probe_aggregator,
    "unknown_probe": probe_unknown,
}


# ---------------------------------------------------------------------------
# Evaluation + registry mutation
# ---------------------------------------------------------------------------

def evaluate_entry(key, entry, probe_result, log_fn):
    """Compare observed verdict to recorded verdict. Returns result dict."""
    expected = (entry.get("probe") or {}).get("expected", entry.get("verdict"))
    if not entry.get("sample_urls"):
        return {"key": key, "ats": entry.get("ats"), "expected": expected,
                "observed": None, "status": "UNTESTABLE",
                "note": "no sample URLs in registry — cannot probe"}
    status = probe_result.get("status")
    observed = probe_result.get("observed")
    if status == "INCONCLUSIVE" or observed is None:
        return {"key": key, "ats": entry.get("ats"), "expected": expected,
                "observed": None, "status": "INCONCLUSIVE",
                "note": probe_result.get("note", "")}
    if observed == expected:
        entry["last_retested"] = today()
        result = {"key": key, "ats": entry.get("ats"), "expected": expected,
                  "observed": observed, "status": "CONFIRMED",
                  "note": probe_result.get("note", "")}
        if probe_result.get("needs_review"):
            result["status"] = "NEEDS_REVIEW"
            result["note"] += " | flagged: possible direct path, needs " \
                              "supervised verification"
        return result
    # FLIP — the payoff. Log it, update the registry, surface it.
    old = entry.get("verdict")
    new = observed
    note = probe_result.get("note", "")
    entry["verdict"] = new
    entry["last_retested"] = today()
    entry["evidence"] = (entry.get("evidence") or "") + \
        f" | FLIP {today()}: {old} -> {new}. {note}"
    (entry.setdefault("probe", {}))["expected"] = new
    log_fn("gate_encountered", role_id="", company="", ats=entry.get("ats", ""),
           source="ats-capability-radar",
           details={"gate": "edge_flip", "entry": key,
                    "old_verdict": old, "new_verdict": new,
                    "evidence": note[:300]})
    return {"key": key, "ats": entry.get("ats"), "expected": expected,
            "observed": new, "status": "FLIP", "note": note}


def run_probes(registry, fetch, log_fn):
    results = []
    for key, entry in registry.get("entries", {}).items():
        proto = (entry.get("probe") or {}).get("protocol")
        fn = PROBES.get(proto)
        if not fn:
            results.append({"key": key, "ats": entry.get("ats"),
                            "expected": entry.get("verdict"),
                            "observed": None, "status": "INCONCLUSIVE",
                            "note": f"no probe protocol '{proto}'"})
            continue
        res = evaluate_entry(key, entry, fn(entry, fetch), log_fn)
        results.append(res)
    return results


# ---------------------------------------------------------------------------
# New-ATS auto-detection
# ---------------------------------------------------------------------------

def _walk_strings(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _walk_strings(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _walk_strings(v)


def collect_pipeline_urls():
    urls = []
    for path in glob.glob(os.path.join(PIPE, "data", "queues", "*.json")):
        if "_backup" in path:
            continue
        try:
            data = json.load(open(path))
        except Exception:
            continue
        for s in _walk_strings(data):
            if isinstance(s, str) and s.startswith("http"):
                urls.append(s.split()[0])
    for path in (os.path.join(PIPE, "data", "application-ledger.json"),):
        if os.path.exists(path):
            try:
                data = json.load(open(path))
            except Exception:
                continue
            for s in _walk_strings(data):
                if isinstance(s, str) and s.startswith("http"):
                    urls.append(s.split()[0])
    # dedupe, keep order
    return list(dict.fromkeys(urls))


def _slug(name):
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def scan_new_ats(registry, fetch, log_fn, url_source=None):
    """Find ATS platforms absent from the registry; onboard as 'unknown'.

    Noise control: a repeated hostname is only onboarded when the probe
    finds plausible application-form markers (form actions or API hints).
    Hosts evaluated and dismissed land in registry["dismissed_hosts"] so
    later runs stay quiet. Unreachable hosts are retried on the next run.
    Known ATS vendors (ats.detect_ats hits) always onboard — they are ATS
    platforms by definition.
    """
    urls = url_source if url_source is not None else collect_pipeline_urls()
    dismissed = registry.setdefault("dismissed_hosts", {})
    registry_ats = {e.get("ats") for e in registry.get("entries", {}).values()}
    host_urls = {}
    new_keys = []
    for url in urls:
        detected = ats_mod.detect_ats(url)
        host = (urllib.parse.urlparse(url).hostname or "").lower()
        if not host:
            continue
        if detected != "unknown":
            name = detected
            if name in registry_ats:
                continue
            key = f"auto_{_slug(name)}"
            if key in registry.get("entries", {}):
                continue
            samples = [u for u in urls if ats_mod.detect_ats(u) == name][:2]
            new_keys.append(_onboard_new_ats(registry, key, name, samples,
                                             fetch, log_fn))
            registry_ats.add(name)
            continue
        host_urls.setdefault(host, []).append(url)
    for host, seen in host_urls.items():
        if len(seen) < 2 or host in SKIP_HOSTS:
            continue
        d = dismissed.get(host)
        if d and d.get("reason") != "unreachable":
            continue  # already evaluated and dismissed — stay quiet
        key = f"auto_host_{_slug(host)}"
        if key in registry.get("entries", {}):
            continue
        findings = unknown_probe_findings(fetch, seen[:2])
        forms = findings.get("form_actions") or []
        apis = findings.get("api_hints") or []
        errors = [s for s in findings.get("samples", []) if s.get("error")]
        if forms or apis:
            new_keys.append(_onboard_new_ats(registry, key, host, seen[:2],
                                             fetch, log_fn, findings))
        elif errors and len(errors) == len(findings.get("samples", [])):
            dismissed[host] = {"reason": "unreachable", "date": today(),
                               "detail": errors[0]["error"][:120]}
        else:
            dismissed[host] = {"reason": "no application-form markers",
                               "date": today()}
    return new_keys


def _onboard_new_ats(registry, key, name, samples, fetch, log_fn,
                     findings=None):
    findings = findings if findings is not None else \
        unknown_probe_findings(fetch, samples)
    log_fn("gate_encountered", role_id="", company="", ats=name,
           source="ats-capability-radar",
           details={"gate": "new_ats_detected", "ats": name,
                    "samples": len(samples),
                    "sample_url": (samples[0] if samples else "")[:200],
                    "findings": json.dumps(findings)[:300]})
    registry.setdefault("entries", {})[key] = {
        "ats": name,
        "edge": f"Auto-detected {today()}: platform seen in pipeline URLs, "
                "transport verdict unknown",
        "first_observed": today(),
        "evidence": "auto-onboard findings: " + json.dumps(findings)[:500],
        "verdict": "unknown",
        "probe": {"protocol": "unknown_probe", "expected": "unknown",
                  "note": "Re-run the unknown-ATS probe sequence. Verdict "
                          "stays 'unknown' until the onboarding protocol "
                          "(see registry onboarding_protocol) produces "
                          "evidence."},
        "last_retested": today(),
        "sample_urls": samples[:2],
    }
    return key


# ---------------------------------------------------------------------------
# Runner + reports
# ---------------------------------------------------------------------------

def _backup_registry():
    stamp = time.strftime("%Y%m%d-%H%M%S")
    bak = os.path.join(BASE, f"_registry-backup-{stamp}.json")
    with open(REGISTRY_PATH) as f, open(bak, "w") as g:
        g.write(f.read())
    return bak


def run(url_source=None, fetch=None, log_fn=None, delay=REQUEST_DELAY,
        write_reports=True, do_scan=True):
    with open(REGISTRY_PATH) as f:
        registry = json.load(f)
    fetch = fetch or RealFetcher(delay=delay)
    log_fn = log_fn or telemetry.log

    results = run_probes(registry, fetch, log_fn)
    new_keys = scan_new_ats(registry, fetch, log_fn,
                            url_source=url_source) if do_scan else []

    mutated = any(r["status"] == "FLIP" for r in results) or bool(new_keys)
    backup = _backup_registry() if mutated else None
    if mutated:
        with open(REGISTRY_PATH, "w") as f:
            json.dump(registry, f, indent=1)

    summary = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
               "results": results, "new_ats": new_keys,
               "registry_backup": backup}
    if write_reports:
        os.makedirs(REPORT_DIR, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        with open(os.path.join(REPORT_DIR, f"edge-probe-{stamp}.json"),
                  "w") as f:
            json.dump(summary, f, indent=1)
        with open(os.path.join(REPORT_DIR, f"edge-probe-{stamp}.md"),
                  "w") as f:
            f.write(_markdown(summary))
    return summary


def _markdown(summary):
    counts = {}
    for r in summary["results"]:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    lines = [f"# ATS capability radar — {summary['ts']}", "",
             "## Summary",
             "".join(f"- {k}: {v}\n" for k, v in sorted(counts.items())),
             f"- new ATS onboarded: {len(summary['new_ats'])}"]
    if summary["new_ats"]:
        lines.append("  - " + ", ".join(summary["new_ats"]))
    lines.append("")
    for r in summary["results"]:
        flag = " **<-- SURFACE**" if r["status"] in ("FLIP", "NEEDS_REVIEW") else ""
        lines.append(f"## {r['key']} — {r['status']}{flag}")
        lines.append(f"- expected: {r['expected']} | observed: {r['observed']}")
        lines.append(f"- note: {r['note']}")
        lines.append("")
    return "\n".join(lines)


def main(argv):
    do_scan = "--no-scan" not in argv
    scan_only = "--scan-only" in argv
    summary = run(do_scan=do_scan and not scan_only,
                  write_reports=True) if not scan_only else None
    if scan_only:
        with open(REGISTRY_PATH) as f:
            registry = json.load(f)
        new_keys = scan_new_ats(registry, RealFetcher(),
                                telemetry.log, url_source=None)
        if new_keys:
            _backup_registry()
            with open(REGISTRY_PATH, "w") as f:
                json.dump(registry, f, indent=1)
        summary = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                   "results": [], "new_ats": new_keys,
                   "registry_backup": None}
    counts = {}
    for r in summary["results"]:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    line = ("EDGE-PROBE {ts}: " + " ".join(f"{k}={v}" for k, v in
            sorted(counts.items())) +
            f" new_ats={len(summary['new_ats'])}").format(ts=summary["ts"])
    print(line)
    flips = [r for r in summary["results"] if r["status"] == "FLIP"]
    for r in flips:
        print(f"  FLIP {r['key']}: {r['expected']} -> {r['observed']}: {r['note']}")
    for k in summary["new_ats"]:
        print(f"  NEW ATS: {k}")
    if "--monthly" in argv:
        notable = flips or summary["new_ats"] or \
            [r for r in summary["results"]
             if r["status"] in ("NEEDS_REVIEW",)]
        print("NOTABLE" if notable else "ROUTINE — all confirmed, nothing to surface")


if __name__ == "__main__":
    main(sys.argv[1:])
