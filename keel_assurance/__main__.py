"""Offline assurance, research metrics and bounded reviewer planning CLI."""
import argparse
import json
from pathlib import Path
import sys
from keel_flow.common import keys, strict_json, timestamp
from .core import evaluate
from .metrics import evaluate_predictions, evaluate_challenges, ecs_score, fit_conformal, conformal_sets


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["audit", "metrics", "conformal", "plan"])
    parser.add_argument("input")
    parser.add_argument("--now", help="required for audit; timezone-aware ISO time")
    parser.add_argument("--out", help="new output file; never overwrite an existing file")
    args = parser.parse_args(argv)
    try:
        with Path(args.input).open("rb") as stream:
            data = strict_json(stream.read(8 * 1024 * 1024 + 1))
        if args.command == "audit":
            if args.now is None: raise ValueError("audit requires --now")
            result = evaluate(data, now=timestamp(args.now))
        elif args.command == "metrics":
            keys(data, {"predictions", "challenges", "source_grounding"})
            predictions = evaluate_predictions(data["predictions"])
            challenges = evaluate_challenges(data["challenges"])
            result = {"predictions": predictions, "challenges": challenges,
                      "ecs": ecs_score(source_grounding=data["source_grounding"],
                                       calibration=None if predictions["ece"] is None else 1 - predictions["ece"],
                                       adversarial_correction=challenges["acr"]),
                      "execution_authorized": False,
                      "ecs_calibration_component": "1 - equal-width 10-bin ECE; descriptive only"}
        elif args.command == "conformal":
            keys(data, {"calibration_records", "training_ids", "test_records", "alpha"})
            # Parsing the test schema is performed by conformal_sets; do not infer labels.
            model = fit_conformal(data["calibration_records"], alpha=data["alpha"], training_ids=data["training_ids"])
            result = {"model": model, "evaluation": conformal_sets(model, data["test_records"]), "execution_authorized": False}
        else:
            from .planner import select_reviewers
            keys(data, {"reviewers", "required_capabilities", "budget_units", "latency_limit_ms"})
            result = select_reviewers(data["reviewers"], required_capabilities=data["required_capabilities"],
                                      budget_units=data["budget_units"], latency_limit_ms=data["latency_limit_ms"])
        output = json.dumps(result, sort_keys=True, indent=2, allow_nan=False) + "\n"
        if args.out:
            with Path(args.out).open("x", encoding="utf-8") as stream: stream.write(output)
        else:
            sys.stdout.write(output)
        return 3 if args.command == "audit" and result["state"] != "CHECKS_PASSED" else 0
    except (ValueError, KeyError, TypeError, OSError, OverflowError) as exc:
        print("keel-assurance: " + str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__": raise SystemExit(main())
