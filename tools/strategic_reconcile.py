#!/usr/bin/env python3
"""Strategic-lane reconciliation (J-20260918-1140-inte-2062, ARM-K1 2026-09-18).

Read-only diagnostic. Loads the strategic queue + manifests + application
ledger and classifies every queue/manifest/ledger divergence into a
disposition class. Writes the reconciliation report; makes NO queue, ledger,
or telemetry writes (the promotion path itself is a morning decision for the
owning loop — stamping READY is execution-adjacent).

Disposition classes:
  consistent               queue/manifest agree; nothing to do
  submitted_evidence_ok    queue SUBMITTED + exactly one ledger SUBMITTED row
                           with canonical confirmation evidence
  submitted_duplicate_rows queue SUBMITTED + >1 ledger rows same role_id
                           (staging NEEDS_INPUT rows beside the real SUBMITTED)
  submitted_needs_ledger   queue SUBMITTED, zero ledger rows (ALEWORX class)
  submitted_stale_outcome  queue SUBMITTED, ledger shows a terminal outcome
                           (REJECTED / INTERVIEW_INVITED) — queue status drift
  trapped_ready_reopen     manifest READY + queue in a non-terminal parked/
                           skipped/unverified status — candidate for re-verify
                           + promote, NOT auto-stamped here
  trapped_ready_gated      manifest READY + queue GATED — the two sources
                           disagree about the gate; reconcile the condition
                           (stale write vs live gate) before any promote
  trapped_ready_closed     manifest READY + queue CLOSED/CLOSED-EXPIRED —
                           likely dead move; confirm-dead before any reopen
  trapped_ready_needs_input manifest READY_TO_SUBMIT + queue NEEDS_INPUT —
                           Trent-only blocker; tray-visible routing needed
  trapped_ready_gated      manifest READY + queue GATED — sources disagree
                           about the gate; reconcile condition before promote
  gated_agent_verifiable   GATED on a condition the verify/agent path can
                           resolve (stale crawl, live page unopened, full JD
                           unknown) — miscategorized; belongs in the verify
                           lane, not a Trent gate
  gated_trent_only         GATED on a genuine Trent-only condition
                           (attestations, certificates not held, tenure
                           framing, availability, relocation calculus) —
                           needs classifier-visible fields + tray routing
  gated_unresolved_manifest GATED with no resolvable manifest or no
                           condition line — condition genuinely unknown
  manifest_unparsed        manifest exists but no status line found

Gated-condition classification is keyword-rule based and deliberately
conservative: anything not clearly agent-verifiable reads trent_only or
unknown — a false "agent-verifiable" label would route a Trent-only blocker
to an agent path that must never answer for him.
"""
import hashlib
import json
import os
import re

WS = os.path.expanduser("~/workspace")
JP = os.path.join(WS, "job-pipeline")
STRATEGIC_QUEUE = os.path.join(JP, "queue", "strategic-queue.json")
LEDGER = os.path.join(JP, "ledger", "application-ledger.json")
MANIFEST_DIRS = [os.path.join(JP, "manifests"),
                 os.path.join(JP, "queue", "manifests"),
                 os.path.join(JP, "engines", "manifests")]

TERMINAL_QUEUE = {"CLOSED", "CLOSED-EXPIRED"}
NONLAUNCHABLE_PARKED = {"PARKED", "PARKED-NEEDS-INPUT",
                        "PARKED-AWAITING-MATERIALS", "SKIPPED", "UNVERIFIED"}


def norm(s):
    return re.sub(r"[^a-z0-9 ]", "", (s or "").lower()).strip()


def manifest_index():
    idx = {}
    for d in MANIFEST_DIRS:
        if os.path.isdir(d):
            for f in os.listdir(d):
                idx.setdefault(f.lower(), os.path.join(d, f))
    return idx


def resolve_manifest(entry, idx):
    mp = (entry.get("manifest_path") or "").strip()
    if mp:
        for cand in (os.path.join(JP, mp), os.path.join(WS, mp)):
            if os.path.isfile(cand):
                return cand
        b = os.path.basename(mp).lower()
        if b in idx:
            return idx[b]
    rid = (entry.get("role_id") or "").lower() + ".md"
    return idx.get(rid)


_STATUS_LINE_RX = re.compile(r"(?im)^.*current status\s*:\s*(.+)$")


def manifest_status(entry, idx):
    """(status_text|None, provenance). status_text is the raw Current Status
    line, truncated; provenance in {ok, inferred, no_line, missing}."""
    p = resolve_manifest(entry, idx)
    if not p:
        return None, "missing"
    txt = open(p, encoding="utf-8", errors="replace").read()
    m = _STATUS_LINE_RX.search(txt)
    if m:
        return m.group(1).strip()[:90], "ok"
    if "READY_TO_SUBMIT" in txt:
        return "READY_TO_SUBMIT (inferred)", "inferred"
    if re.search(r"(?im)\bREADY\b", txt):
        return "READY (inferred)", "inferred"
    return None, "no_line"


def is_manifest_ready(manifest_status_text):
    return bool(manifest_status_text) and bool(
        re.search(r"\bREADY(_TO_SUBMIT)?\b", manifest_status_text, re.I))


# ------------------------------------------------------------------ GATED conditions
_AGENT_VERIFIABLE_RX = re.compile(
    r"live status|stale crawl|live page not opened|full (?:jd|requirements?) "
    r"(?:not|un)read|requirements? (?:not read|unknown|unconfirmed)|"
    r"live status.{0,20}(?:unconfirmed|re-confirm)|re-confirm before|"
    r"confirm (?:the )?full|employer-direct apply route|apply route unconfirmed|"
    r"title inconsistency|confirm exact title", re.I)

_TRENT_ONLY_RX = re.compile(
    r"attest|certificate|rbs|tenure gate|years.of.experience|framing|"
    r"availability|schedule|relocation|commute|physical|lift|travel.{0,20}"
    r"(?:to customer|%)|degree.{0,20}(?:fram|preferred)|on-sale|wms|"
    r"comfortab|confirm.{0,20}relocation|candidate (?:must|to) confirm|"
    r"not held", re.I)


def gated_condition_class(condition):
    """trent_only | agent_verifiable | unknown. Conservative by design."""
    c = condition or ""
    if _AGENT_VERIFIABLE_RX.search(c):
        return "agent_verifiable"
    if _TRENT_ONLY_RX.search(c):
        return "trent_only"
    return "unknown"


def gate_condition_text(entry, idx):
    p = resolve_manifest(entry, idx)
    if not p:
        return "MANIFEST_UNRESOLVED"
    txt = open(p, encoding="utf-8", errors="replace").read()
    m = re.search(
        r"(?im)^[#>*\-\s]*(?:gate|gated|blocked|condition|"
        r"pre-application requirement)\s*[-:\u2013\u2014]\s*(.+)$", txt)
    if m:
        return m.group(1).strip()[:160]
    m2 = re.search(
        r"(?im)\b(do not submit until|until trent|candidate must confirm|"
        r"candidate confirm|requires trent)(.{0,120})", txt)
    if m2:
        return (m2.group(1) + m2.group(2)).strip()[:160]
    return "NO_CONDITION_LINE"


# ------------------------------------------------------------------ ledger
def ledger_index():
    """Index ledger rows by role_id AND by (normalized company|employer,
    normalized title) — backfilled rows use `employer` not `company`
    (apply_loop already_submitted lesson, 2026-09-15), so role_id alone
    misses real matches."""
    rows = json.load(open(LEDGER))
    by_role = {}
    by_ct = {}
    for r in rows:
        rid = r.get("role_id")
        if rid:
            by_role.setdefault(rid, []).append(r)
        comp = norm(r.get("company") or r.get("employer"))
        tit = norm(r.get("title"))
        if comp and tit:
            by_ct.setdefault((comp, tit), []).append(r)
    return by_role, by_ct


def ledger_rows_for(entry, by_role, by_ct):
    rows = list(by_role.get(entry.get("role_id"), []))
    key = (norm(entry.get("company")), norm(entry.get("title")))
    for r in by_ct.get(key, []):
        if r not in rows:
            rows.append(r)
    return rows


def submitted_evidence(row):
    """Canonical submitted-evidence fields (2026-09-17 ledger-checker rule)."""
    return bool((row.get("confirmation_text") or "").strip()
                or (row.get("confirmation_url") or "").strip())


# ------------------------------------------------------------------ classifier
def classify_entry(entry, manifest_st, provenance, ledger_rows):
    """Return (disposition_class, detail). Pure function of its inputs."""
    qs = entry.get("status")
    rid = entry.get("role_id")
    ready = is_manifest_ready(manifest_st)

    if qs == "SUBMITTED":
        if not ledger_rows:
            return ("submitted_needs_ledger",
                    "queue SUBMITTED with zero ledger rows; queue's "
                    "submitted_at is the only evidence")
        if len(ledger_rows) > 1:
            return ("submitted_duplicate_rows",
                    f"{len(ledger_rows)} ledger rows for one role_id: "
                    + ", ".join(sorted({r.get("status") or "?" for r in ledger_rows})))
        row = ledger_rows[0]
        lst = row.get("status")
        if lst == "SUBMITTED" and submitted_evidence(row):
            return ("submitted_evidence_ok",
                    "exactly one ledger SUBMITTED row with canonical "
                    "confirmation evidence")
        if lst == "SUBMITTED":
            return ("submitted_evidence_ok",
                    "exactly one ledger SUBMITTED row; confirmation evidence "
                    "weak (no confirmation_text/url) — flag")
        return ("submitted_stale_outcome",
                f"queue SUBMITTED but ledger shows {lst} — queue status drift")

    if ready and qs not in ("READY", "READY-FOR-BROWSER", "FIRING", "SUBMITTED"):
        if qs in TERMINAL_QUEUE:
            return ("trapped_ready_closed",
                    f"manifest '{manifest_st[:60]}' but queue {qs} — "
                    "likely dead move; confirm-dead before any reopen")
        if qs in ("NEEDS_INPUT", "PARKED-NEEDS-INPUT"):
            return ("trapped_ready_needs_input",
                    f"manifest '{manifest_st[:60]}' — firing conditions met "
                    "except Trent-only blockers; needs tray-visible routing")
        if qs == "GATED":
            return ("trapped_ready_gated",
                    f"manifest '{manifest_st[:60]}' but queue GATED — "
                    "sources disagree about the gate; reconcile the "
                    "condition (stale write vs live gate) before any promote")
        return ("trapped_ready_reopen",
                f"manifest '{manifest_st[:60]}' trapped in {qs} — "
                "re-verify liveness, then promote via owning loop")

    if qs == "GATED":
        return ("gated_pending_condition",
                "condition classified separately by gated_condition_class")
    if provenance == "missing":
        return ("manifest_unparsed", "no manifest resolvable")
    return ("consistent", f"queue {qs} / manifest {manifest_st or '?'} agree")


# ------------------------------------------------- dry-run migration manifest
# Read-only proposal layer (2026-09-19, P8). The classifier above only
# *reports* dispositions; this manifest turns the actionable ones into a
# structured, reviewable move plan WITHOUT touching the canonical queue.
# A future consumer with sanctioned ownership would apply it; none exists
# yet, and this module must never grow one without separate authorization.
MIGRATION_TOOL_REVISION = "strategic_reconcile/migration-manifest:1"

# Disposition -> proposed destination for a future authorized consumer.
# None means the manifest proposes no move for that disposition.
MIGRATION_DESTINATIONS = {
    "trapped_ready_reopen": "READY (via owning loop after liveness re-verify)",
    "trapped_ready_needs_input": "NEEDS_INPUT (tray-visible routing; no auto-promote)",
    "trapped_ready_gated": "GATED (reconcile gate condition first; no promote)",
    "trapped_ready_closed": "REJECTED (confirm-dead; no reopen)",
    "submitted_stale_outcome": "queue status sync to ledger state (no new application)",
}


def queue_revision(queue_path):
    """Content digest of the source queue file; the manifest's freshness anchor."""
    with open(queue_path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def build_migration_manifest(entries, report_rows, queue_digest):
    """Pure function: (source entries, classifier rows, queue digest) -> manifest.

    Every input row gets exactly one item, including no-move rows (their
    disposition is preserved, destination None). The original entry is
    retained verbatim so any future application is reversible by restoring
    originals. Idempotent: same inputs -> identical items. The manifest
    carries expected_source_revision so a consumer must reject stale plans.
    """
    items = []
    for entry, rec in zip(entries, report_rows):
        disp = rec.get("disposition")
        dest = MIGRATION_DESTINATIONS.get(disp)
        original_blob = json.dumps(entry, sort_keys=True, ensure_ascii=True)
        migration_id = "mig-" + hashlib.sha256(
            f"{rec.get('role_id')}|{disp}|{queue_digest}".encode()).hexdigest()[:16]
        items.append({
            "migration_id": migration_id,
            "role_id": rec.get("role_id"),
            "disposition": disp,
            "proposed_destination": dest,
            "reason": rec.get("detail"),
            "evidence": {
                "queue_status": rec.get("queue_status"),
                "manifest_status": rec.get("manifest_status"),
                "manifest_provenance": rec.get("manifest_provenance"),
                "ledger_statuses": rec.get("ledger_statuses"),
                "ledger_rows": rec.get("ledger_rows"),
            },
            "original_row": entry,
            "original_row_digest": hashlib.sha256(original_blob.encode()).hexdigest(),
            "expected_source_revision": queue_digest,
            "authority": "dry-run only; no queue mutation authorized",
        })
    items.sort(key=lambda i: i["migration_id"])
    return {
        "tool_revision": MIGRATION_TOOL_REVISION,
        "expected_source_revision": queue_digest,
        "authority": "dry-run only; no queue mutation authorized",
        "moves_proposed": sum(1 for i in items if i["proposed_destination"]),
        "rows_accounted": len(items),
        "items": items,
    }


def manifest_is_fresh(manifest, queue_path):
    """A consumer must reject a manifest whose source queue has moved on."""
    try:
        return manifest.get("expected_source_revision") == queue_revision(queue_path)
    except OSError:
        return False


def reconcile(queue_path=STRATEGIC_QUEUE):
    """Full read-only reconciliation. Returns (report_rows, summary, manifest)."""
    raw = open(queue_path, "rb").read()
    digest = hashlib.sha256(raw).hexdigest()
    rows = json.loads(raw)
    idx = manifest_index()
    by_role, by_ct = ledger_index()
    out = []
    for e in rows:
        mst, prov = manifest_status(e, idx)
        lrows = ledger_rows_for(e, by_role, by_ct)
        disp, detail = classify_entry(e, mst, prov, lrows)
        rec = {"role_id": e.get("role_id"), "company": e.get("company"),
               "title": e.get("title"), "fit_score": e.get("fit_score"),
               "queue_status": e.get("status"), "manifest_status": mst,
               "manifest_provenance": prov,
               "ledger_rows": len(lrows),
               "ledger_statuses": sorted({r.get("status") or "?" for r in lrows}),
               "disposition": disp, "detail": detail}
        if disp == "gated_pending_condition":
            cond = gate_condition_text(e, idx)
            rec["gate_condition"] = cond
            rec["gate_condition_class"] = gated_condition_class(cond)
        out.append(rec)
    summary = {}
    for r in out:
        summary[r["disposition"]] = summary.get(r["disposition"], 0) + 1
    gsum = {}
    for r in out:
        if r["disposition"] == "gated_pending_condition":
            k = r["gate_condition_class"]
            gsum[k] = gsum.get(k, 0) + 1
    summary = {"dispositions": summary, "gated_classes": gsum, "n": len(out)}
    manifest = build_migration_manifest(rows, out, digest)
    return out, summary, manifest


def render_report(rows, summary):
    L = []
    L.append("# Strategic-lane reconciliation — 2026-09-18 (ARM-K1, read-only)")
    L.append("")
    L.append("Source: J-20260918-1140-inte-2062. No queue/ledger/telemetry writes made;")
    L.append("the promotion path itself (stamping READY) is a morning decision for the")
    L.append("owning loop. Engine classifier: keel/tools/strategic_reconcile.py;")
    L.append("regression tests: keel/tests/test_strategic_reconcile.py.")
    L.append("")
    L.append(f"Entries reconciled: {summary['n']}")
    L.append("")
    L.append("## Disposition summary")
    for d, c in sorted(summary["dispositions"].items(), key=lambda x: -x[1]):
        L.append(f"- {d}: {c}")
    L.append("")
    L.append(f"## GATED condition classes ({len(summary['gated_classes'])})")
    for k, c in sorted(summary["gated_classes"].items(), key=lambda x: -x[1]):
        L.append(f"- {k}: {c}")
    L.append("")
    for disp in ["submitted_needs_ledger", "submitted_duplicate_rows",
                 "submitted_stale_outcome", "trapped_ready_closed",
                 "trapped_ready_gated", "trapped_ready_needs_input",
                 "trapped_ready_reopen", "gated_pending_condition",
                 "manifest_unparsed"]:
        group = [r for r in rows if r["disposition"] == disp]
        if not group:
            continue
        L.append(f"## {disp} ({len(group)})")
        for r in group:
            line = (f"- {r['role_id']} | {r['company']} — {r['title']} "
                    f"(fit {r['fit_score']}) | queue={r['queue_status']} "
                    f"manifest={r['manifest_status'] or r['manifest_provenance']} "
                    f"ledger={r['ledger_statuses']}")
            if r["disposition"] == "gated_pending_condition":
                line += (f" | gate[{r['gate_condition_class']}]: "
                         f"{r['gate_condition'][:100]}")
            L.append(line)
            L.append(f"  detail: {r['detail']}")
        L.append("")
    return "\n".join(L)


def main():
    rows, summary, manifest = reconcile()
    report = render_report(rows, summary)
    out_path = os.path.join(WS, "keel", "hidden_files",
                            "strategic-reconcile-2026-09-18.md")
    with open(out_path, "w") as f:
        f.write(report)
    json_path = out_path.replace(".md", ".json")
    with open(json_path, "w") as f:
        json.dump({"summary": summary, "rows": rows}, f, indent=1)
    manifest_path = out_path.replace(".md", ".manifest.json")
    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=1, sort_keys=True)
    print(json.dumps(summary, indent=1))
    print("report:", out_path)
    print("manifest (dry-run, no queue mutation):", manifest_path)


if __name__ == "__main__":
    main()
