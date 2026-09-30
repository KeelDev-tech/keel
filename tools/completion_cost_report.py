#!/usr/bin/env python3
"""Read an explicit offline JSON cohort and print cost accounting to stdout."""
import argparse
import json
import sys
from pathlib import Path

# Support direct execution from a clean checkout, including python -S.
if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from keel_assurance.completion_cost import summarize_completion_cost
from keel_assurance.metrics import MetricsError


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise MetricsError('duplicate JSON key')
        result[key] = value
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', type=Path)
    args = parser.parse_args(argv)
    try:
        with args.input.open(encoding='utf-8') as stream:
            payload = json.load(stream, object_pairs_hook=_object)
        report = summarize_completion_cost(payload)
    except (OSError, ValueError, TypeError) as exc:
        print(f'Invalid cost evidence: {exc}', file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2, allow_nan=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
