#!/usr/bin/env python3
"""ats_discovery sweep runner — fetch registered boards, normalize, stage.

Read-only discovery. Dry-run by default; --live writes a staging file to
hidden_files/discovery-staging/ for the sanctioned staging-ingest path.

Adapters never invent fit: staged entries are band PARKED / status PARKED
("unscored — fit-scoring pass required"), URL-bearing, zero-error under
queue_intake.validate_entry(). Dead postings are staged as CLOSED-EXPIRED
(explicit dead signal on record), never silently dropped.

429 = hard stop for that platform's scope (other platforms continue).
"""

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

BASE = os.path.dirname(os.path.abspath(__file__))          # .../ats_discovery
PKG_PARENT = os.path.dirname(BASE)                        # .../application-executor
sys.path.insert(0, PKG_PARENT)

import ats_discovery  # noqa: E402
from ats_discovery import normalize as N  # noqa: E402
from ats_discovery.fetcher import http_get, paced_get, RateLimited  # noqa: E402
import dedupe_gate  # noqa: E402
import queue_intake  # noqa: E402
# J-20260916-0022-feed-427: canonical emission dedupe (dedupe_gate-based,
# never hand-rolled greps) before any staging-file write.
# J-20260916-0425-sour-562: emission precheck — schema-valid, canonical
# role_id, intra-batch dedupe — runs BEFORE the staging write, so malformed
# stubs and batch-internal dupes never reach ingest.
from staging_ingest import emission_dedupe, emission_precheck  # noqa: E402

from keel_paths import HOME as PIPE, DATA  # noqa: E402
REGISTRY = os.path.join(PKG_PARENT, "ats_board_registry.json")
STAGED_DIR = os.path.join(PIPE, "hidden_files", "discovery-staging")
BLOCKLIST = os.path.join(DATA, "employer-blocklist.md")

PDT = ZoneInfo("America/Los_Angeles")
PACE_BOARD = 2.0   # seconds between boards
PACE_DETAIL = 1.5  # seconds between per-posting detail fetches

# Target role-family triage keywords (honest keyword evidence, not fit scoring).
# Tunable; --no-triage disables.
TRIAGE_PAT = re.compile(
    r"(operations|program manager|project manager|business operations|"
    r"chief of staff|go-?to-?market|gtm|revenue operations|revops|"
    r"growth|strategy|strategic|ai tooling|automation|product operations|"
    r"sales operations|marketing operations|business systems|"
    r"transformation|enablement|program operations|operations manager|"
    r"general manager|gm\b|operations lead|head of operations)",
    re.I)


def load_registry():
    return json.load(open(REGISTRY))


def blocked_names():
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


def load_json(path, default):
    try:
        return json.load(open(path))
    except Exception:
        return default


def build_entry(cand, triage_on):
    """Candidate dict -> staging entry dict (or None to skip)."""
    title = N.norm_title(cand["title"])
    if triage_on and not TRIAGE_PAT.search(title):
        return None, "triage"
    company = N.norm_company(cand["company"] or cand["employer"])
    url = cand["posting_url"]
    date_tag = datetime.now(PDT).strftime("%Y%m%d")
    entry = {
        "role_id": N.make_role_id(cand["platform"], company, title,
                                  cand["source_job_id"], date_tag),
        "company": company,
        "title": title,
        "location": (cand["location"] or {}).get("raw", ""),
        "action_band": "PARKED",
        "status": "CLOSED-EXPIRED" if cand["dead"] else "PARKED",
        "application_url": url,
        "posting_url": url,
        "ats": cand["ats_label"],
        "discovery_platform": cand["platform"],
        "discovery_slug": cand["slug"],
        "source_job_id": cand["source_job_id"],
        "description": (cand["description"] or "")[:6000],
        "description_hash": N.description_hash(cand["description"]),
        "posted_date": cand["posted_date"],
        "compensation": cand["compensation"],
        "employment_type": cand["employment_type"],
        "remote": cand["remote"],
        "liveness": cand["liveness"],
        "source": f"ats8-{cand['platform']}",
        "date_discovered": datetime.now(PDT).strftime("%Y-%m-%d"),
        "status_updated": datetime.now(PDT).strftime("%Y-%m-%d %H:%M PDT"),
        "queue_notes": (
            f"ats8 discovery {cand['platform']}/{cand['slug']}: unscored — "
            f"fit-scoring pass required. source_job_id={cand['source_job_id']}."
        ),
    }
    if cand["dead"]:
        entry["queue_notes"] += " explicit dead signal; kept on record."
    errors, _ = queue_intake.validate_entry(entry)
    if errors:
        return None, f"validate: {errors}"
    return entry, None


def sweep_platform(pkey, adapter, boards, ctx, args):
    """Fetch all active boards for one platform. Returns summary dict.

    completed_observations semantics: only fully completed boards count in
    raw/staged. A board that aborts (exception, 429 hard stop) or is skipped
    contributes ZERO to completed_observations — unexecuted fetches are never
    counted as completed observations. Structured abort records land in
    summary["aborts"]; summary["skipped"] keeps the non-exception skips.
    """
    summary = {"platform": pkey, "boards": 0, "completed_boards": 0, "raw": 0,
               "staged": 0, "completed_observations": 0, "dupes": 0,
               "blocked": 0, "dead": 0, "skipped": [], "aborts": [],
               "rate_limited": False}
    getter = lambda u: paced_get(u, PACE_DETAIL)  # noqa: E731
    completed_raw = 0
    for slug, meta in boards.items():
        if meta.get("status") not in ("active", "probe"):
            summary["skipped"].append(f"{slug}: status={meta.get('status')}")
            continue
        employer = meta.get("employer", slug)
        adapter.SKIP_LOG.clear() if hasattr(adapter, "SKIP_LOG") else None
        if hasattr(adapter, "ABORT_LOG"):
            adapter.ABORT_LOG.clear()
        summary["boards"] += 1
        try:
            raw = adapter.fetch_board(slug, getter)
        except RateLimited:
            summary["rate_limited"] = True
            summary["skipped"].append(f"{slug}: 429 hard stop")
            break  # stop this platform, others continue
        except Exception as e:
            summary["skipped"].append(f"{slug}: fetch error {e}")
            summary["aborts"].extend(getattr(adapter, "ABORT_LOG", []))
            continue
        for s in getattr(adapter, "SKIP_LOG", []):
            summary["skipped"].append(f"{s.get('slug')}: {s.get('reason')}")
        summary["raw"] += len(raw)
        completed_raw += len(raw)  # board completed fully: it counts
        summary["completed_boards"] += 1
        for r in raw:
            try:
                cand = adapter.normalize(r, slug, employer)
            except ValueError as e:
                summary["skipped"].append(f"{slug}: normalize {e}")
                continue
            entry, why = build_entry(cand, not args.no_triage)
            if entry is None:
                if why != "triage":
                    summary["skipped"].append(f"{slug}: {why}")
                continue
            if is_blocked(entry["company"], ctx["blocked"]):
                summary["blocked"] += 1
                continue
            verdict, evidence = dedupe_gate.check_candidate(
                entry["company"], entry["title"], entry["posting_url"],
                ledger_rows=ctx["ledger"], queue_entries=ctx["queue"])
            if verdict == "duplicate":
                summary["dupes"] += 1
                continue
            if verdict == "suspect":
                entry["queue_notes"] += (
                    f" dedupe suspect: {evidence.get('note', '')}.")
            # description-hash repost fallback
            dh = entry["description_hash"]
            if dh in ctx["desc_hashes"] and ctx["desc_hashes"][dh] != entry["posting_url"]:
                entry["queue_notes"] += (
                    f" possible repost (desc-hash match): "
                    f"{ctx['desc_hashes'][dh]}.")
            if cand["dead"]:
                summary["dead"] += 1
            ctx["staged"].append(entry)
            summary["staged"] += 1
        time.sleep(PACE_BOARD)
    # completed_observations: only fully completed boards count. Unexecuted
    # fetches (aborted boards, 429 hard stops, skipped statuses) never count
    # as completed observations.
    summary["completed_observations"] = completed_raw
    return summary


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--platform", default="all",
                    help="platform key or 'all' (wiring order)")
    ap.add_argument("--slug", default=None, help="single board slug")
    ap.add_argument("--live", action="store_true",
                    help="write staging file (default: dry-run)")
    ap.add_argument("--no-triage", action="store_true",
                    help="disable title-family triage")
    args = ap.parse_args(argv)

    reg = load_registry()
    pkeys = (ats_discovery.WIRING_ORDER if args.platform == "all"
             else [args.platform])
    for p in pkeys:
        if p not in ats_discovery.ADAPTERS:
            print(f"unknown platform: {p}", file=sys.stderr)
            return 2

    ctx = {
        "blocked": blocked_names(),
        "ledger": load_json(os.path.join(DATA, "application-ledger.json"), []),
        "queue": load_json(os.path.join(DATA, "queues",
                                        "standard-queue.json"), []),
        "staged": [],
        "desc_hashes": {},
    }
    for e in ctx["queue"]:
        d = (e.get("description") or "")
        if d:
            ctx["desc_hashes"][N.description_hash(d)] = (
                e.get("posting_url") or e.get("application_url") or "")

    summaries = []
    for pkey in pkeys:
        boards = reg.get(pkey, {}).get("boards", {})
        if args.slug:
            boards = {k: v for k, v in boards.items() if k == args.slug}
            if not boards:
                print(f"no such slug {args.slug} on {pkey}", file=sys.stderr)
                return 2
        summaries.append(sweep_platform(
            pkey, ats_discovery.ADAPTERS[pkey], boards, ctx, args))

    out = {"dry_run": not args.live, "summaries": summaries,
           "staged_total": len(ctx["staged"])}
    if args.live and ctx["staged"]:
        # J-20260916-0425-sour-562: emission precheck FIRST — schema-valid,
        # canonical role_id, intra-batch (role_id + employer+title) dedupe
        # BEFORE the staging write. Measured 2412 staged_rejected/24h,
        # ~95% avoidable (1698 schema-invalid + 587 in-batch dupes) — those
        # now die at emission with one summary line instead of one ingest
        # rejection event each. Fail-closed: invalid stubs are withheld,
        # never rewritten (see staging_ingest.emission_precheck).
        ctx["staged"], _pc_withheld, pc_report = emission_precheck(
            ctx["staged"])
        out["emission_precheck"] = pc_report
        print(f"emission_precheck: {pc_report['fresh']} fresh, "
              f"{pc_report['withheld']} withheld from staging: "
              f"{pc_report['withheld_reason_counts']}")
        # J-20260916-0022-feed-427: canonical emission dedupe. The per-
        # candidate checks in sweep_platform use the standard queue + a
        # point-in-time ledger snapshot; this final canonical pass also
        # covers the all-queues index and intra-batch collisions, so
        # duplicates never occupy staging at all.
        fresh, dupes, dedupe_report = emission_dedupe(ctx["staged"])
        out["emission_dedupe"] = dedupe_report
        print(f"emission_dedupe: {dedupe_report['fresh']} fresh, "
              f"{dedupe_report['dupes']} dupes withheld from staging")
        if not fresh:
            print("all staged leads withheld by emission dedupe — "
                  "no staging file written")
            print(json.dumps(out, indent=1))
            return 0
        os.makedirs(STAGED_DIR, exist_ok=True)
        ts = datetime.now(PDT).strftime("%Y%m%d-%H%M%S")
        name = f"ats8-{args.platform}-{ts}.json"
        path = os.path.join(STAGED_DIR, name)
        json.dump(fresh, open(path, "w"), indent=1)
        out["staging_file"] = path
    print(json.dumps(out, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
