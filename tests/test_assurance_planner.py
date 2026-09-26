"""Synthetic tests for bounded reviewer-selection planning."""
import copy
import itertools
import json
import random
import unittest

from keel_assurance.planner import MAX_SAFE_INTEGER, PlannerError, select_reviewers


def reviewer(identity, *, group=None, family=None, method=None, caps=None, available=True, cost=1, latency=10):
    return {"reviewer_id": identity, "independence_group": group or f"group-{identity}",
            "family": family or f"family-{identity}", "method": method or f"method-{identity}",
            "capabilities": ["evidence"] if caps is None else caps,
            "available": available, "cost_units": cost, "latency_ms": latency}


def select(rows, required=("evidence",), budget=100, latency=100):
    return select_reviewers(rows, required_capabilities=required, budget_units=budget, latency_limit_ms=latency)


class ReviewerPlannerTests(unittest.TestCase):
    def test_two_distinct_reviewers_meet_requirements(self):
        result = select([reviewer("a", cost=2, latency=4), reviewer("b", cost=3, latency=6)])
        self.assertEqual(result["status"], "FEASIBLE")
        self.assertEqual(result["selected_reviewer_ids"], ["a", "b"])
        self.assertEqual(result["cost_units"], 5)
        self.assertEqual(result["parallel_latency_ms"], 6)
        self.assertEqual(result["combinations_examined"], 4)
        self.assertEqual(result["feasible_subsets"], 1)
        self.assertFalse(result["execution_authorized"])

    def test_empty_singleton_and_unavailable_are_infeasible(self):
        for rows in ([], [reviewer("a")], [reviewer("a", available=False), reviewer("b", available=False)],
                     [reviewer("a", available=False), reviewer("b")]):
            with self.subTest(rows=rows):
                result = select(rows, required=())
                self.assertEqual(result["status"], "INFEASIBLE")
                self.assertEqual(result["selected_reviewer_ids"], [])
                self.assertIsNone(result["cost_units"])
                self.assertIsNone(result["parallel_latency_ms"])

    def test_same_group_family_or_method_fails(self):
        for dimension in ("independence_group", "family", "method"):
            rows = [reviewer("a"), reviewer("b")]
            rows[1][dimension] = rows[0][dimension]
            with self.subTest(dimension=dimension):
                self.assertEqual(select(rows)["status"], "INFEASIBLE")

    def test_crossed_marginal_diversity_is_not_an_independent_pair(self):
        rows = [reviewer("a", group="g1", family="f1", method="m1"),
                reviewer("b", group="g2", family="f2", method="m1"),
                reviewer("c", group="g2", family="f1", method="m2")]
        self.assertEqual(select(rows)["status"], "INFEASIBLE")

    def test_cosmetic_metadata_variants_do_not_fake_independence(self):
        rows = [reviewer("a", group="Group One"), reviewer("b", group="ＧＲＯＵＰ  ONE")]
        self.assertEqual(select(rows)["status"], "INFEASIBLE")

    def test_required_capabilities_can_span_more_than_two(self):
        rows = [reviewer("a", caps=["evidence"]), reviewer("b", caps=["policy"]),
                reviewer("c", caps=["security"], group="group-a", family="family-a", method="method-a")]
        result = select(rows, required=["evidence", "policy", "security"])
        self.assertEqual(result["selected_reviewer_ids"], ["a", "b", "c"])

    def test_missing_capabilities_cannot_pass(self):
        self.assertEqual(select([reviewer("a"), reviewer("b")], required=["missing"])["status"], "INFEASIBLE")

    def test_capability_matching_is_exact(self):
        self.assertEqual(select([reviewer("a"), reviewer("b")], required=["Evidence"])["status"], "INFEASIBLE")

    def test_budget_and_parallel_latency_boundaries(self):
        rows = [reviewer("a", cost=3, latency=10), reviewer("b", cost=4, latency=20)]
        self.assertEqual(select(rows, budget=7, latency=20)["status"], "FEASIBLE")
        self.assertEqual(select(rows, budget=6, latency=20)["status"], "INFEASIBLE")
        self.assertEqual(select(rows, budget=7, latency=19)["status"], "INFEASIBLE")

    def test_zero_budget_selects_only_genuinely_zero_cost_candidates(self):
        rows = [reviewer("a", cost=0, latency=0), reviewer("b", cost=0, latency=0), reviewer("c", cost=1)]
        result = select(rows, budget=0, latency=0)
        self.assertEqual(result["selected_reviewer_ids"], ["a", "b"])
        self.assertEqual(result["cost_units"], 0)
        self.assertEqual(result["parallel_latency_ms"], 0)

    def test_cost_before_latency_before_stable_ids(self):
        rows = [reviewer("a", cost=1, latency=50), reviewer("b", cost=1, latency=30),
                reviewer("c", cost=2, latency=1), reviewer("d", cost=1, latency=30)]
        self.assertEqual(select(rows)["selected_reviewer_ids"], ["b", "d"])
        rows.append(reviewer("e", cost=1, latency=30))
        self.assertEqual(select(rows)["selected_reviewer_ids"], ["b", "d"])

    def test_unavailable_cheapest_candidate_never_selected(self):
        rows = [reviewer("a", available=False, cost=0), reviewer("b", cost=2), reviewer("c", cost=3)]
        result = select(rows)
        self.assertEqual(result["selected_reviewer_ids"], ["b", "c"])
        self.assertEqual(result["combinations_examined"], 4)
        self.assertEqual(result["registered_count"], 3)
        self.assertEqual(result["available_count"], 2)

    def test_registry_order_and_capability_order_do_not_change_result(self):
        rows = [reviewer("c", caps=["policy", "evidence"]), reviewer("a"), reviewer("b", caps=["policy"])]
        baseline = select(rows, required=["policy", "evidence"])
        for order in itertools.permutations(rows):
            self.assertEqual(select(order, required=["evidence", "policy"]), baseline)

    def test_no_input_mutation_and_json_safe_result(self):
        rows = [reviewer("a"), reviewer("b")]
        initial = copy.deepcopy(rows)
        required = ["evidence"]
        json.dumps(select(rows, required=required), allow_nan=False)
        self.assertEqual(rows, initial)
        self.assertEqual(required, ["evidence"])

    def test_independent_reference_enumeration(self):
        rng = random.Random(72026)
        for trial in range(25):
            rows = [reviewer(str(i), group=f"g{rng.randrange(3)}", family=f"f{rng.randrange(3)}",
                             method=f"m{rng.randrange(3)}", caps=rng.sample(["a", "b", "c"], rng.randrange(4)),
                             available=rng.choice([True, True, False]), cost=rng.randrange(5), latency=rng.randrange(8))
                    for i in range(6)]
            budget, latency = rng.randrange(12), rng.randrange(8)
            possible = []
            for count in range(2, 7):
                for subset in itertools.combinations(rows, count):
                    if not all(r["available"] for r in subset):
                        continue
                    if not any(all(a[key] != b[key] for key in ("family", "method", "independence_group"))
                               for a, b in itertools.combinations(subset, 2)):
                        continue
                    cost = sum(r["cost_units"] for r in subset)
                    time = max(r["latency_ms"] for r in subset)
                    if cost > budget or time > latency or not {"a", "b"} <= set().union(*(set(r["capabilities"]) for r in subset)):
                        continue
                    possible.append((cost, time, tuple(sorted(r["reviewer_id"] for r in subset))))
            result = select(rows, required=["a", "b"], budget=budget, latency=latency)
            with self.subTest(trial=trial):
                self.assertEqual(result["feasible_subsets"], len(possible))
                if possible:
                    cost, time, ids = min(possible)
                    self.assertEqual((result["cost_units"], result["parallel_latency_ms"], tuple(result["selected_reviewer_ids"])),
                                     (cost, time, ids))
                else:
                    self.assertEqual(result["status"], "INFEASIBLE")

    def test_sixteen_candidate_bound_is_enforced(self):
        rows = [reviewer(str(i), group="same") for i in range(16)]
        result = select(rows)
        self.assertEqual(result["combinations_examined"], 65536)
        self.assertEqual(result["status"], "INFEASIBLE")
        with self.assertRaises(PlannerError):
            select(rows + [reviewer("extra")])

    def test_duplicate_ids_and_capabilities_rejected(self):
        with self.assertRaises(PlannerError):
            select([reviewer("a"), reviewer("a")])
        with self.assertRaises(PlannerError):
            select([reviewer("a", caps=["a", "a"])])
        with self.assertRaises(PlannerError):
            select([], required=["a", "a"])

    def test_exact_fields_and_all_entries_validated(self):
        rows = [reviewer("a", available=False)]
        for key in tuple(rows[0]):
            broken = copy.deepcopy(rows)
            del broken[0][key]
            with self.subTest(key=key), self.assertRaises(PlannerError):
                select(broken)
        rows[0]["quality"] = 1.0
        with self.assertRaises(PlannerError):
            select(rows)

    def test_strict_availability_integer_types_and_bounds(self):
        for key, value in (("available", 1), ("available", "true"), ("cost_units", True),
                           ("cost_units", -1), ("cost_units", 1.0), ("latency_ms", None),
                           ("latency_ms", float("inf")), ("cost_units", MAX_SAFE_INTEGER + 1)):
            row = reviewer("a")
            row[key] = value
            with self.subTest(key=key, value=value), self.assertRaises(PlannerError):
                select([row])
        for value in (True, -1, 1.0, None, MAX_SAFE_INTEGER + 1):
            with self.assertRaises(PlannerError):
                select([], budget=value)
            with self.assertRaises(PlannerError):
                select([], latency=value)

    def test_strings_and_collections_are_bounded(self):
        for value in ("", " padded ", "x\ny", "x\u200by", "x" * 129, None):
            row = reviewer("a")
            row["method"] = value
            with self.subTest(value=value), self.assertRaises(PlannerError):
                select([row])
        for rows in (iter([]), {}, "rows", None):
            with self.assertRaises(PlannerError):
                select(rows)
        for required in (iter([]), "a", [str(i) for i in range(33)]):
            with self.assertRaises(PlannerError):
                select([], required=required)


if __name__ == "__main__":
    unittest.main()
