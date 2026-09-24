#!/usr/bin/env python3
"""Print host observations; never install or start a model/browser service."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from keel_agent.io import read_json
from keel_sources.host import inspect_host


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", help="operator-owned JSON with reviewers and optional browser settings")
    parser.add_argument("--detailed", action="store_true",
                        help="run fixed Node version/resolution probes; no browser launch")
    args = parser.parse_args(argv)
    try:
        report = inspect_host(read_json(args.config) if args.config else None,
                              detailed=args.detailed)
    except (OSError, ValueError):
        print(json.dumps({"status": "UNVERIFIED", "error": "invalid_or_unreadable_host_configuration",
                          "execution_authorized": False}))
        return 2
    print(json.dumps(report, indent=2, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
