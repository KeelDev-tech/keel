"""Read-only CLI. Outputs proposals; no paid services or application submits."""
import argparse
import json
from pathlib import Path
from .contracts import strict_json, timestamp, utcnow
from .readiness import inventory, refill_plan
from .operations import buffer_plan, scheduler_drift
from .discovery import parse_greenhouse, fetch_public_board, HOST, allowed_discovery_url


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("readiness", "refill", "buffer", "scheduler-drift"):
        p = sub.add_parser(command); p.add_argument("input", type=Path)
        if command == "readiness":
            p.add_argument("--now", required=True, help="offset-aware snapshot evaluation time")
    p = sub.add_parser("discover")
    p.add_argument("--board", required=True)
    p.add_argument("--fixture", type=Path)
    p.add_argument("--fetch-public", action="store_true")
    p.add_argument("--observed-at", help="timestamp of fixture capture; required for fixtures")
    args = parser.parse_args()
    if args.command == "discover":
        if bool(args.fixture) == bool(args.fetch_public):
            parser.error("choose exactly one of --fixture or --fetch-public")
        if args.fixture and not args.observed_at:
            parser.error("fixtures require --observed-at; replay is not a fresh observation")
        url = f"https://{HOST}/v1/boards/{args.board}/jobs?content=true"
        allowed_discovery_url(url)
        data = strict_json(args.fixture.read_bytes()) if args.fixture else fetch_public_board(url)
        observed = timestamp(args.observed_at).isoformat() if args.fixture else utcnow().isoformat()
        result = parse_greenhouse(args.board, data, observed_at=observed)
    else:
        data = strict_json(args.input.read_bytes())
        if args.command == "readiness":
            result = inventory(data, now=timestamp(args.now))
        elif args.command == "refill":
            result = refill_plan(**data)
        elif args.command == "buffer":
            result = buffer_plan(data["queues"], data["packets"])
        else:
            result = scheduler_drift(data["expected"], data["observed"])
    print(json.dumps(result, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
