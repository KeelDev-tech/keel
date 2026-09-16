#!/usr/bin/env python3
"""learning_join.py — proposal -> implementation -> metric joins.

Closes the learning-loop leak where shipped proposals have no
machine-readable link to their measured metric.

Join keys, strongest first:
  1. STRONG: optimization-log entry's `blackboard` field == proposal id.
  2. MEDIUM: proposal id string appears in the entry's evidence blob.
  3. WEAK: normalized name/title token overlap (flagged as weak).

Each join row:
  proposal {id, domain, title, ts, by_loop, status}
  implementation {claimed_by, claimed_ts, shipped_by, shipped_ts}
  metric {link_strength, name, metric, baseline_metric, post_metric, verdict}

Orphan reports:
  - shipped proposals with NO linked metric (the leak to drive to zero)
  - metric entries with no linked proposal
  - proposals claimed but never shipped (stale leases)

Outputs: data/hidden_files/outcome-tracking/learning-join-<stamp>.json/.md
and learning-join-latest.* copies. Read-only: never writes the blackboard
or the optimization log.
"""

import json
import os
import re
import sys
from datetime import datetime, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
_ENGINES = os.path.dirname(_HERE)
if _ENGINES not in sys.path:
    sys.path.insert(0, _ENGINES)
from keel_paths import HOME as KEEL_HOME, DATA  # noqa: E402

BB_PATH = os.path.join(KEEL_HOME, "hidden_files", "pulse",
                       "judgment-blackboard.json")
OPT_PATH = os.path.join(DATA, "hidden_files", "optimization-log.jsonl")
OUT_DIR = os.path.join(DATA, "hidden_files", "outcome-tracking")

ID_RE = re.compile(r"J-2026\d{4}-\d{4}-[a-z]+-\d+|F-2026\d{4}-\d{4}-[a-z0-9-]+")


def _norm(s):
    return set(re.findall(r"[a-z0-9]+", (s or "").lower())) - {
        "the", "a", "an", "and", "of", "to", "for", "with", "on", "in"}


def load_proposals(bb_path=None):
    d = json.load(open(bb_path or BB_PATH))
    return d.get("entries", [])


def load_metrics(opt_path=None):
    out = []
    with open(opt_path or OPT_PATH) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except Exception:
                    continue
    return out


def implementation_of(entry):
    """Claim/ship history -> implementation record."""
    claimed_by = claimed_ts = shipped_by = shipped_ts = None
    for h in entry.get("history") or []:
        ev = h.get("event")
        if ev == "claimed" and claimed_by is None:
            claimed_by, claimed_ts = h.get("by"), h.get("ts")
        if ev == "shipped":
            shipped_by, shipped_ts = h.get("by"), h.get("ts")
    return {"claimed_by": claimed_by, "claimed_ts": claimed_ts,
            "shipped_by": shipped_by, "shipped_ts": shipped_ts}


def link_strength(proposal, metric):
    """STRONG / MEDIUM / WEAK / none."""
    pid = proposal.get("id", "")
    if metric.get("blackboard") == pid:
        return "STRONG"
    blob = json.dumps(metric.get("evidence", [])) + json.dumps(
        {k: v for k, v in metric.items() if k != "evidence"})
    if pid and pid in blob:
        return "MEDIUM"
    pt, mt = _norm(proposal.get("title", "")), _norm(
        (metric.get("title") or "") + " " + (metric.get("name") or ""))
    if pt and mt and len(pt & mt) >= 3:
        return "WEAK"
    return "none"


def join(proposals=None, metrics=None):
    proposals = proposals if proposals is not None else load_proposals()
    metrics = metrics if metrics is not None else load_metrics()
    rows = []
    used_metrics = set()
    for p in proposals:
        impl = implementation_of(p)
        best, best_strength = None, "none"
        order = {"none": 0, "WEAK": 1, "MEDIUM": 2, "STRONG": 3}
        for i, m in enumerate(metrics):
            s = link_strength(p, m)
            if order[s] > order[best_strength]:
                best, best_strength = m, s
        if best is not None:
            used_metrics.add(id(best))
        rows.append({
            "proposal_id": p.get("id"),
            "domain": p.get("domain"),
            "title": p.get("title"),
            "proposal_ts": p.get("ts"),
            "proposed_by": p.get("by_loop"),
            "status": p.get("status"),
            "implementation": impl,
            "metric_link": best_strength,
            "metric": ({
                "name": best.get("name"), "title": best.get("title"),
                "metric": best.get("metric"),
                "baseline_metric": best.get("baseline_metric"),
                "post_metric": best.get("post_metric"),
                "verdict": best.get("verdict"),
                "ts": best.get("ts"),
            } if best is not None else None),
        })
    shipped_no_metric = [
        r for r in rows
        if r["status"] == "shipped" and r["metric_link"] == "none"]
    claimed_never_shipped = [
        r for r in rows
        if r["implementation"]["claimed_by"]
        and not r["implementation"]["shipped_by"]]
    orphan_metrics = [
        {"name": m.get("name"), "title": m.get("title"),
         "ts": m.get("ts"), "verdict": m.get("verdict")}
        for m in metrics if id(m) not in used_metrics]
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "counts": {
            "proposals": len(rows),
            "shipped": sum(1 for r in rows if r["status"] == "shipped"),
            "shipped_with_metric": sum(
                1 for r in rows
                if r["status"] == "shipped" and r["metric_link"] != "none"),
            "shipped_without_metric": len(shipped_no_metric),
            "claimed_never_shipped": len(claimed_never_shipped),
            "metrics": len(metrics),
            "orphan_metrics": len(orphan_metrics),
        },
        "rows": rows,
        "shipped_without_metric": [
            {"proposal_id": r["proposal_id"], "domain": r["domain"],
             "title": r["title"], "shipped_by": r["implementation"]["shipped_by"],
             "shipped_ts": r["implementation"]["shipped_ts"]}
            for r in shipped_no_metric],
        "claimed_never_shipped": [
            {"proposal_id": r["proposal_id"], "domain": r["domain"],
             "title": r["title"],
             "claimed_by": r["implementation"]["claimed_by"],
             "claimed_ts": r["implementation"]["claimed_ts"]}
            for r in claimed_never_shipped],
        "orphan_metrics": orphan_metrics,
    }


def render_markdown(rep):
    L = []
    A = L.append
    c = rep["counts"]
    A("# Learning Join — proposal → implementation → metric")
    A("")
    A(f"_Generated {rep['generated_at']}._")
    A("")
    A(f"- Proposals: {c['proposals']} ({c['shipped']} shipped)")
    A(f"- Shipped WITH a linked metric: {c['shipped_with_metric']}")
    A(f"- Shipped WITHOUT a metric (the leak): "
      f"**{c['shipped_without_metric']}**")
    A(f"- Claimed but never shipped: {c['claimed_never_shipped']}")
    A(f"- Metric entries: {c['metrics']} "
      f"({c['orphan_metrics']} orphan)")
    A("")
    if rep["shipped_without_metric"]:
        A("## Shipped proposals with no linked metric")
        A("")
        for r in rep["shipped_without_metric"][:30]:
            A(f"- `{r['proposal_id']}` [{r['domain']}] {r['title']} "
              f"(shipped by {r['shipped_by']})")
        if len(rep["shipped_without_metric"]) > 30:
            A(f"- …and {len(rep['shipped_without_metric']) - 30} more "
              f"(see JSON).")
        A("")
    A("## Link strength")
    A("")
    A("STRONG = optimization-log `blackboard` field names the proposal id; "
      "MEDIUM = id appears in the entry's evidence blob; WEAK = title/name "
      "token overlap (flagged, needs a human to confirm). "
      "New upgrades must log the `blackboard` field for a STRONG link.")
    A("")
    return "\n".join(L) + "\n"


def main(argv):
    out_dir = OUT_DIR
    if "--out" in argv:
        out_dir = argv[argv.index("--out") + 1]
    os.makedirs(out_dir, exist_ok=True)
    rep = join()
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    jp = os.path.join(out_dir, f"learning-join-{stamp}.json")
    mp = os.path.join(out_dir, f"learning-join-{stamp}.md")
    json.dump(rep, open(jp, "w"), indent=1)
    open(mp, "w").write(render_markdown(rep))
    json.dump(rep, open(os.path.join(out_dir, "learning-join-latest.json"),
                        "w"), indent=1)
    open(os.path.join(out_dir, "learning-join-latest.md"),
         "w").write(render_markdown(rep))
    print(f"report: {mp}")
    print(f"shipped_without_metric: {rep['counts']['shipped_without_metric']}")
    return rep


if __name__ == "__main__":
    main(sys.argv[1:])
