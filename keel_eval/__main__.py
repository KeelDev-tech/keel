"""Labelled replay or explicitly requested local-server evaluation; no execution."""
import argparse
import json
import sys

from keel_agent.io import read_json, write_private
from .evaluation import EvaluationError, _config, evaluate_replay, run_local


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    replay = commands.add_parser("replay", help="score a supplied transcript without model calls")
    replay.add_argument("dataset")
    replay.add_argument("results")
    replay.add_argument("--out")
    local = commands.add_parser("local", help="explicitly call an already-running literal-loopback model server")
    local.add_argument("dataset")
    local.add_argument("--config", required=True)
    local.add_argument("--weights-sha256", default=None, help="optional operator declaration; not weight attestation")
    local.add_argument("--out")
    args = parser.parse_args(argv)
    try:
        dataset = read_json(args.dataset)
        if args.command == "replay":
            result = evaluate_replay(dataset, read_json(args.results))
        else:
            result = run_local(dataset, _config(read_json(args.config)), declared_weights_sha256=args.weights_sha256)
        if args.out:
            write_private(args.out, result)
        else:
            print(json.dumps(result, indent=2, sort_keys=True, allow_nan=False))
        # Zero means scoring completed, never model acceptance or authorization.
        return 3 if result["metrics"]["errors"]["numerator"] else 0
    except (ValueError, TypeError, OSError, KeyError, RecursionError):
        print("keel-eval: invalid input, configuration, or output destination", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
