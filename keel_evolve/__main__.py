"""Run the offline Keel evolution demonstration."""
import argparse
import json
import sys

from .demo import run


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("demo", choices=["demo", "workflow", "faultlab"])
    parser.add_argument("--home")
    args = parser.parse_args(argv)
    try:
        if args.demo == 'faultlab':
            from .faultlab import run as action
            result = action()
        else:
            if args.home is None:
                parser.error('--home is required for demo and workflow')
            from .workflow import run as workflow
            result = (run if args.demo == 'demo' else workflow)(args.home)
        print(json.dumps(result, indent=2, sort_keys=True, allow_nan=False))
        return 0 if result["status"] in ("EVOLUTION_DEMO_PASSED", "WORKFLOW_PASSED", "FAULTLAB_PASSED") else 3
    except (OSError, ValueError, TypeError, KeyError, RuntimeError):
        print("keel-evolve: blocked input or host; --home must be a new private local directory",
              file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
