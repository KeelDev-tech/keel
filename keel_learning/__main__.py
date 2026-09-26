"""Local statistical arithmetic demo. No real observations or model calls."""
import argparse
import json
from .controller import bounded_confidence_sequence


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["demo", "capabilities"])
    args = parser.parse_args(argv)
    result = {"schema": "keel.learning.capabilities.v1", "paid_services_required": False,
        "experiment_registry": "host_validator_required", "random_assignment": "local_exact_rational_draw",
        "estimator": "frozen_predictor_doubly_robust", "confidence_method": "hoeffding_time_union",
        "reliability": "frozen_family_exact_binomial_time_union", "execution_authorized": False}
    if args.command == "demo":
        result.update(synthetic=True, values=[0,1,1,0,1],
            confidence_sequence=bounded_confidence_sequence([0,1,1,0,1],lower=0,upper=1,delta=.05),
            status="SYNTHETIC_DEMONSTRATION_ONLY")
    print(json.dumps(result, indent=2, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
