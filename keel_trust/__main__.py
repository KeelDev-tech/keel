"""Local evidence, operating brief, snapshot twin and incident replay CLI."""
import argparse
import json
from pathlib import Path
import sys
from .common import strict_json, timestamp
from . import evidence, report, twin, replay


def read(path):
    with Path(path).open("rb") as stream: return strict_json(stream.read(8 * 1024 * 1024 + 1))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["report", "evidence", "capture", "simulate", "drift", "replay"])
    parser.add_argument("input")
    parser.add_argument("--scenario", help="scenario file for simulate")
    parser.add_argument("--current", help="new canonical flow export for drift")
    parser.add_argument("--now", help="required current evaluation time; replay and simulate use recorded time")
    parser.add_argument("--format", choices=["json", "markdown"], default="json")
    parser.add_argument("--out", help="new file; refuses overwrite")
    args = parser.parse_args(argv)
    if args.format == "markdown" and args.command != "report": parser.error("markdown supports report only")
    if (args.command == "simulate") != bool(args.scenario): parser.error("simulate requires --scenario exclusively")
    if (args.command == "drift") != bool(args.current): parser.error("drift requires --current exclusively")
    if args.command in {"report", "evidence", "capture", "drift"} and not args.now: parser.error("--now is required")
    if args.command in {"simulate", "replay"} and args.now: parser.error("recorded time is used; omit --now")
    try:
        data = read(args.input)
        if args.command == "simulate": result = twin.simulate(data, read(args.scenario))
        elif args.command == "replay": result = replay.run(data)
        elif args.command == "drift": result = twin.drift(data, read(args.current), now=timestamp(args.now))
        else:
            fn = {"report": report.build, "evidence": evidence.evaluate, "capture": twin.capture}[args.command]
            result = fn(data, now=timestamp(args.now))
        rendered = report.markdown(result) if args.format == "markdown" else json.dumps(result, sort_keys=True, indent=2, allow_nan=False) + "\n"
        if args.out:
            with Path(args.out).open("x", encoding="utf-8") as stream: stream.write(rendered)
        else: sys.stdout.write(rendered)
        return (0 if result["status"] == "PASS" else 1) if args.command == "replay" else 0
    except (ValueError, TypeError, KeyError, OSError) as exc:
        print("keel-trust: " + str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__": raise SystemExit(main())
