#!/usr/bin/env python3
"""Summarize the GPT-vs-Gemini triage-polish comparison log.

Reads hidden_files/tray_polish_compare.jsonl (written by
triage_llm.polish_best) and prints actionable insight: per-provider
reliability, latency, and output length, the share of rows where both
providers succeeded, who got chosen, and recent side-by-side samples.

Usage:
  compare_polish.py            # summary + 5 most recent both-ok samples
  compare_polish.py --samples 10
  compare_polish.py --json     # machine-readable summary
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from keel_paths import HOME  # noqa: E402 — repo path convention
LOG = os.path.join(HOME, "hidden_files", "tray_polish_compare.jsonl")


def load_rows(path: str = LOG) -> list[dict]:
    rows = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        rows.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
    except FileNotFoundError:
        pass
    return rows


def summarize(rows: list[dict]) -> dict:
    n = len(rows)
    out = {"samples": n}
    for prov in ("gpt", "gemini"):
        cells = [r.get(prov, {}) for r in rows]
        oks = [c for c in cells if c.get("ok")]
        lat = [c.get("latency_ms", 0) for c in oks]
        lens = [len(c.get("output", "")) for c in oks]
        out[prov] = {
            "ok": len(oks),
            "ok_rate": round(len(oks) / n, 3) if n else 0.0,
            "avg_latency_ms": round(sum(lat) / len(lat)) if lat else 0,
            "avg_output_chars": round(sum(lens) / len(lens)) if lens else 0,
        }
    both_ok = [r for r in rows
               if r.get("gpt", {}).get("ok") and r.get("gemini", {}).get("ok")]
    out["both_ok"] = len(both_ok)
    out["chosen"] = {
        w: sum(1 for r in rows if r.get("chosen") == w)
        for w in ("gpt", "gemini", "deterministic")
    }
    return out


def _short(s: str, n: int = 110) -> str:
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1] + "…"


def report(rows: list[dict], n_samples: int = 5) -> str:
    s = summarize(rows)
    lines = [
        "tray polish compare: %d samples" % s["samples"],
    ]
    for prov in ("gpt", "gemini"):
        p = s[prov]
        lines.append(
            "  %-6s ok %d/%d (%.1f%%)  avg_latency %dms  avg_len %dch"
            % (prov, p["ok"], s["samples"], p["ok_rate"] * 100,
               p["avg_latency_ms"], p["avg_output_chars"]))
    c = s["chosen"]
    lines.append(
        "  both_ok %d  chosen: gpt=%d gemini=%d deterministic=%d"
        % (s["both_ok"], c["gpt"], c["gemini"], c["deterministic"]))
    both = [r for r in rows
            if r.get("gpt", {}).get("ok") and r.get("gemini", {}).get("ok")]
    if both and n_samples > 0:
        lines.append("")
        lines.append("--- side-by-side (both ok, most recent %d) ---"
                     % min(n_samples, len(both)))
        for r in both[-n_samples:]:
            lines.append("[%s] chosen=%s" % (r.get("ts", "?"), r.get("chosen")))
            lines.append("  IN:     " + _short(r.get("input", "")))
            lines.append("  GPT:    " + _short(r.get("gpt", {}).get("output", "")))
            lines.append("  GEMINI: " + _short(r.get("gemini", {}).get("output", "")))
    if not rows:
        lines.append("  (no samples yet -- rows appear once polish_best runs "
                     "with compare.enabled)")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Summarize the polish compare log")
    ap.add_argument("--samples", type=int, default=5)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    rows = load_rows()
    if args.json:
        print(json.dumps(summarize(rows), indent=2))
    else:
        print(report(rows, n_samples=args.samples))
    return 0


if __name__ == "__main__":
    sys.exit(main())
