#!/usr/bin/env python3
"""Pulse dashboard: renders the pulse state as HTML.

The verify-contention tile reads the latest verify-retry scan_summary
and shows the honest split: true_concurrent_mutation (real
concurrent-writer signal — the tile keys amber ONLY off this) vs
scan_apply_derivation_delta (benign internal derivation delta, never
trips the tile).
"""
import html
import json
import os
import sys
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# Telemetry root (pathlib.Path so JP / "telemetry" / "events.jsonl"
# works; tests redirect it with a compatible fake).
JP = Path(_ROOT)


def _esc(value):
    """HTML-escape a dashboard value; None renders as empty."""
    if value is None:
        return ""
    return html.escape(str(value))


def verify_contention_stats():
    """(true_concurrent_mutation, scan_apply_derivation_delta, ts).

    Reads the latest verify-retry scan_summary event. Malformed JSON,
    malformed records, and other sources are ignored. Returns
    (None, None, None) when no usable summary exists; a pre-split
    summary (valid event, no split keys) returns (None, None, ts).
    """
    path = JP / "telemetry" / "events.jsonl"
    latest = None
    try:
        with open(path, "r", encoding="utf-8") as stream:
            for line in stream:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except Exception:
                    continue
                if (isinstance(event, dict)
                        and event.get("event_type") == "scan_summary"
                        and event.get("source") == "verify-retry"):
                    latest = event
    except (FileNotFoundError, OSError):
        return None, None, None
    if latest is None:
        return None, None, None
    details = latest.get("details")
    if not isinstance(details, dict):
        return None, None, None
    return (details.get("true_concurrent_mutation"),
            details.get("scan_apply_derivation_delta"),
            latest.get("ts"))


def render(data):
    """Render the dashboard HTML (minimal; values escaped via _esc)."""
    tcm, sad, ts = verify_contention_stats()
    rows = [
        "<html><body>",
        f"<p>as of {_esc(ts)}</p>",
        f"<p>true_concurrent_mutation={_esc(tcm)} "
        f"scan_apply_derivation_delta={_esc(sad)}</p>",
        "</body></html>",
    ]
    return "\n".join(rows)


def main():
    print(render({}))


if __name__ == "__main__":
    main()
