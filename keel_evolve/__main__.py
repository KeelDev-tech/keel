"""Run the offline Keel evolution demonstration."""
import argparse
import json
import sys

from .demo import run


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("demo", choices=["demo"])
    parser.add_argument("--home", required=True)
    args = parser.parse_args(argv)
    try:
        result = run(args.home)
        print(json.dumps(result, indent=2, sort_keys=True, allow_nan=False))
        return 0 if result["status"] == "EVOLUTION_DEMO_PASSED" else 3
    except (OSError, ValueError, TypeError, KeyError, RuntimeError):
        print("keel-evolve: blocked input or host; --home must be a new private local directory",
              file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
