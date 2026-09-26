"""Offline CLI: python3 -m keel_flow board EXPORT --now ISO_TIMESTAMP."""
import argparse
import json
from pathlib import Path
import sys
from .common import strict_json, timestamp
from .board import build, markdown


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["board"])
    parser.add_argument("export")
    parser.add_argument("--now", required=True, help="timezone-aware evaluation timestamp")
    parser.add_argument("--format", choices=["json", "markdown"], default="json")
    parser.add_argument("--assurance", help="optional snapshot-bound cognitive assurance envelope JSON")
    parser.add_argument("--out", help="new output file; existing files are never overwritten")
    args = parser.parse_args(argv)
    try:
        with Path(args.export).open("rb") as stream:
            snapshot = strict_json(stream.read(8 * 1024 * 1024 + 1))
        assurance = None
        if args.assurance:
            with Path(args.assurance).open("rb") as stream:
                assurance = strict_json(stream.read(8 * 1024 * 1024 + 1))
            if type(assurance) is not dict:
                raise ValueError("assurance envelope object required")
        result = build(snapshot, now=timestamp(args.now), assurance=assurance)
        rendered = markdown(result) if args.format == "markdown" else json.dumps(result, sort_keys=True, indent=2, allow_nan=False) + "\n"
        if args.out:
            with Path(args.out).open("x", encoding="utf-8") as stream: stream.write(rendered)
        else:
            sys.stdout.write(rendered)
        return 0
    except (ValueError, TypeError, KeyError, OSError) as exc:
        print("keel-flow: " + str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__": raise SystemExit(main())
