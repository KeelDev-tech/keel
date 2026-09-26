#!/usr/bin/env python3
"""formation_writer.py -- writer-side null contract for formation.json.

Regression for pulse 444 (2026-09-18 20:30Z): the octopus coordinator
wrote a suppressed 'emergency-refill' arm record with an EXPLICIT null
"started" field ({"name": "emergency-refill", "started": None, ...}).
a.get("started", "") returned None, html.escape(None) raised
AttributeError, and the whole dashboard render died. The render side was
hardened first (pulse_dashboard._esc, metric
pulse_dashboard_none_escape_20260918); this module pins the WRITER side:
suppressed/incomplete arm records must OMIT the field, never emit
explicit null.

Public API:
    build_arm(name, task, status, started=None, finished=None, delta=None)
        -> dict with any None-valued key omitted.
    scrub_nulls(doc) -> (clean_doc, removed_count)
        Recursively strips dict keys and list items whose value is None.
        Never mutates the input.
    write_formation(path, doc) -> removed_count
        Scrubs, then writes JSON with zero explicit nulls in the raw text.
        Creates parent directories as needed.
    CLI: python3 monitors/formation_writer.py --scrub <formation.json>
        Reads the file, scrubs it, rewrites it, prints
        {"nulls_removed": N, "ok": true}, exits 0.

Null-scrub policy (documented choice): None list items are REMOVED,
not kept as null placeholders -- a formation.json contract test
requires that no dict value AND no list item anywhere be None, and a
list item carried through as JSON null would violate the same no-null
guarantee that killed the dashboard render in pulse 444.
"""
from __future__ import annotations

import argparse
import json
import os
import sys


def build_arm(name, task, status, started=None, finished=None, delta=None):
    """Build one arm record, omitting any field whose value is None.

    Suppressed/incomplete arms (e.g. never started) must not carry an
    explicit "started": null -- the key is omitted entirely.
    """
    arm = {
        "name": name,
        "task": task,
        "status": status,
        "started": started,
        "finished": finished,
        "delta": delta,
    }
    return {k: v for k, v in arm.items() if v is not None}


def scrub_nulls(doc):
    """Recursively strip None dict values and None list items.

    Returns (clean_doc, removed_count). The input is never mutated:
    every container on the path to a kept value is rebuilt.

    A dict key whose value is None counts as one removal; a None list
    item counts as one removal and is dropped from the list (policy:
    lists keep order but cannot carry explicit nulls).
    """
    removed = 0

    if isinstance(doc, dict):
        clean = {}
        for key, value in doc.items():
            if value is None:
                removed += 1
                continue
            kept, sub_removed = scrub_nulls(value)
            clean[key] = kept
            removed += sub_removed
        return clean, removed

    if isinstance(doc, list):
        clean = []
        for value in doc:
            if value is None:
                removed += 1
                continue
            kept, sub_removed = scrub_nulls(value)
            clean.append(kept)
            removed += sub_removed
        return clean, removed

    return doc, 0


def write_formation(path, doc):
    """Scrub doc, write it as JSON to path, return the removed count.

    Because the doc is scrubbed before serialization, the encoder can
    never emit an explicit null in the raw text. Parent directories of
    path are created when missing.
    """
    clean, removed = scrub_nulls(doc)
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(clean, fh, indent=2, sort_keys=True)
        fh.write("\n")
    return removed


def _scrub_cli(path):
    """Implementation of `formation_writer.py --scrub <file>`."""
    with open(path, encoding="utf-8") as fh:
        doc = json.load(fh)
    removed = write_formation(path, doc)
    return {"nulls_removed": removed, "ok": True}


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Scrub explicit nulls from a formation.json file.")
    parser.add_argument("--scrub", metavar="FILE",
                        help="scrub nulls from FILE, rewriting it in place")
    args = parser.parse_args(argv)
    if args.scrub:
        report = _scrub_cli(args.scrub)
        print(json.dumps(report))
        return 0
    parser.print_usage(sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
