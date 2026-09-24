"""Deterministic research-metric tests; all labels are synthetic fixtures."""
import copy
import json
import math
import unittest

from keel_assurance.metrics import (
    MAX_RECORDS, MetricsError, conformal_sets, ecs_score,
    evaluate_challenges, evaluate_predictions, fit_conformal, wilson_interval,
)


def prediction(identity="p1", p=0.8, outcome=True, abstained=False):
    return {"id": identity, "probability": p, "outcome": outcome, "abstained": abstained}


def challenge(identity="c1", before=False, after=True, revised=True, challenged=True, ref="fixture:truth:v1"):
    return {"id": identity, "challenged": challenged, "revised": revised,
            "before_correct": before, "after_correct": after, "ground_truth_ref": ref}


class PredictionMetricsTests(unittest.TestCase):
    def test_perfect_predictions_have_zero_losses(self):
        report = evaluate_predictions([prediction("a", 0, False), prediction("b", 1, True)])
        self.assertEqual(report["brier_loss"], 0)
        self.assertEqual(report["ece"], 0)
        self.assertEqual(report["selective_accuracy"], 1)
        self.assertEqual(report["coverage"], 1)
        self.assertFalse(report["execution_authority"])

    def test_brier_is_not_pure_calibration(self):
        report = evaluate_predictions([prediction("a", 0.5, True), prediction("b", 0.5, False)])
        self.assertEqual(report["brier_loss"], 0.25)
        self.assertEqual(report["ece"], 0)
        self.assertEqual(report["accuracy_all_labeled"], 0.5)

    def test_known_fixture_losses(self):
        report = evaluate_predictions([prediction("a", 0.8, True), prediction("b", 0.4, False)], bins=2)
        self.assertAlmostEqual(report["brier_loss"], 0.1)
        self.assertAlmostEqual(report["ece"], 0.3)
        self.assertEqual([b["count"] for b in report["bins"]], [1, 1])

    def test_unknowns_and_abstentions_cannot_inflate_sample_size(self):
        report = evaluate_predictions([
            prediction("a", 0.9, True), prediction("b", 0.9, False, True),
            prediction("c", 0.2, None), prediction("d", 0.1, None, True),
        ])
        self.assertEqual(report["counts"]["labeled"], 2)
        self.assertEqual(report["counts"]["unknown_outcomes"], 2)
        self.assertEqual(report["counts"]["answered_unknown"], 1)
        self.assertEqual(report["coverage"], 0.5)
        self.assertEqual(report["labeled_coverage"], 0.5)
        self.assertEqual(report["selective_accuracy"], 1)
        self.assertEqual(report["accuracy_all_labeled"], 0.5)
        self.assertAlmostEqual(report["brier_loss"], 0.41)

    def test_empty_and_unlabeled_are_unavailable_not_passes(self):
        for records in ([], [prediction(outcome=None)]):
            report = evaluate_predictions(records)
            for key in ("brier_loss", "ece", "selective_accuracy", "selective_accuracy_wilson", "accuracy_all_labeled"):
                self.assertIsNone(report[key])
            self.assertTrue(all(b["mean_probability"] is None for b in report["bins"]))

    def test_all_abstaining_reports_probabilistic_loss_but_no_selective_accuracy(self):
        report = evaluate_predictions([prediction(abstained=True)])
        self.assertEqual(report["coverage"], 0)
        self.assertAlmostEqual(report["brier_loss"], 0.04)
        self.assertIsNone(report["selective_accuracy"])

    def test_half_open_bin_boundaries_and_one(self):
        records = [prediction(str(index), p, True) for index, p in enumerate([0, 0.25, 0.5, 0.75, 1])]
        report = evaluate_predictions(records, bins=4)
        self.assertEqual([b["count"] for b in report["bins"]], [1, 1, 1, 2])
        self.assertEqual([b["upper_inclusive"] for b in report["bins"]], [False, False, False, True])

    def test_threshold_tie_predicts_true(self):
        self.assertEqual(evaluate_predictions([prediction(p=0.5)])["selective_accuracy"], 1)
        self.assertEqual(evaluate_predictions([prediction(p=0.5)], threshold=0.6)["selective_accuracy"], 0)

    def test_invalid_probabilities_rejected(self):
        for value in (True, False, None, "0.5", math.nan, math.inf, -math.inf, -0.1, 1.1, 10 ** 1000):
            with self.subTest(value=repr(value)[:40]), self.assertRaises(MetricsError):
                evaluate_predictions([prediction(p=value)])

    def test_strict_boolean_labels_and_abstention(self):
        for value in (0, 1, "true", [], {}):
            with self.subTest(value=value), self.assertRaises(MetricsError):
                evaluate_predictions([prediction(outcome=value)])
        for value in (None, 0, 1, "false"):
            with self.subTest(value=value), self.assertRaises(MetricsError):
                evaluate_predictions([prediction(abstained=value)])

    def test_invalid_schema_and_ids(self):
        bad = [{}, {"id": "x", "probability": 0.5}, {**prediction(), "unused": 1}, []]
        bad.extend(prediction(identity=value) for value in ("", " a", "x\ny", 1, "x" * 257))
        for value in bad:
            with self.subTest(value=value), self.assertRaises(MetricsError):
                evaluate_predictions([value])

    def test_duplicate_and_conflicting_ids_rejected(self):
        for second in (prediction(), prediction(outcome=False)):
            with self.assertRaises(MetricsError):
                evaluate_predictions([prediction(), second])

    def test_bounded_finite_sequences_only(self):
        for rows in ([prediction()] * (MAX_RECORDS + 1), iter([prediction()]), "rows", None):
            with self.assertRaises(MetricsError):
                evaluate_predictions(rows)
        for bins in (True, 0, 101, 2.5):
            with self.assertRaises(MetricsError):
                evaluate_predictions([], bins=bins)

    def test_inputs_unchanged_and_result_strict_json(self):
        rows = [prediction(), prediction("missing", outcome=None)]
        original = copy.deepcopy(rows)
        json.dumps(evaluate_predictions(rows), allow_nan=False)
        self.assertEqual(rows, original)


class WilsonTests(unittest.TestCase):
    def test_reference_values(self):
        low, high = wilson_interval(5, 10)
        self.assertAlmostEqual(low, 0.236593090512564, places=12)
        self.assertAlmostEqual(high, 0.763406909487436, places=12)
        self.assertIsNone(wilson_interval(0, 0))

    def test_extreme_counts_remain_bounded(self):
        for positives in (0, 1, 9999, 10000):
            low, high = wilson_interval(positives, 10000)
            self.assertLessEqual(0, low)
            self.assertLessEqual(low, high)
            self.assertLessEqual(high, 1)

    def test_invalid_counts_and_z(self):
        for successes, total in ((True, 2), (1, False), (-1, 5), (4, 3), (1.0, 2), (0, MAX_RECORDS + 1)):
            with self.assertRaises(MetricsError):
                wilson_interval(successes, total)
        for z in (0, math.inf, True, 11):
            with self.assertRaises(MetricsError):
                wilson_interval(1, 2, z=z)


class ChallengeMetricsTests(unittest.TestCase):
    def test_rates_use_explicit_noninterchangeable_denominators(self):
        report = evaluate_challenges([
            challenge("fixed"), challenge("still_wrong", False, False),
            challenge("regressed", True, False), challenge("still_right", True, True, False),
            challenge("unnecessary", True, True, True),
        ])
        self.assertEqual(report["acr"], 0.5)
        self.assertEqual(report["harmful_revision_rate"], 1 / 3)
        self.assertEqual(report["correction_precision"], 0.25)
        self.assertEqual(report["counts"]["ineffective_or_unnecessary_revisions"], 2)
        self.assertFalse(report["execution_authority"])

    def test_missing_grounding_and_labels_visible_without_double_subtraction(self):
        report = evaluate_challenges([
            challenge("valid"), challenge("no_ref", ref=None),
            challenge("no_labels", before=None, after=None),
            challenge("both", before=None, ref=None), challenge("not_challenged", challenged=False),
        ])
        self.assertEqual(report["counts"]["challenged"], 4)
        self.assertEqual(report["counts"]["eligible"], 1)
        self.assertEqual(report["counts"]["missing_labels"], 2)
        self.assertEqual(report["counts"]["missing_ground_truth_ref"], 2)
        self.assertEqual(report["counts"]["excluded_union"], 3)
        self.assertEqual(report["acr"], 1)

    def test_zero_denominators_remain_null(self):
        for rows in ([], [challenge(challenged=False)], [challenge(ref=None)]):
            report = evaluate_challenges(rows)
            self.assertIsNone(report["acr"])
            self.assertIsNone(report["harmful_revision_rate"])
            self.assertIsNone(report["correction_precision"])

    def test_no_revisions_means_no_precision(self):
        report = evaluate_challenges([challenge(before=False, after=False, revised=False)])
        self.assertEqual(report["acr"], 0)
        self.assertIsNone(report["correction_precision"])

    def test_contradictory_revision_and_duplicate_rejected(self):
        with self.assertRaises(MetricsError):
            evaluate_challenges([challenge(revised=False)])
        with self.assertRaises(MetricsError):
            evaluate_challenges([challenge(), challenge(after=False)])

    def test_challenge_types_are_strict(self):
        for key, value in (("revised", 1), ("challenged", "yes"), ("before_correct", 0),
                           ("after_correct", "true"), ("ground_truth_ref", ""), ("id", None)):
            row = challenge()
            row[key] = value
            with self.subTest(key=key), self.assertRaises(MetricsError):
                evaluate_challenges([row])


class EcsTests(unittest.TestCase):
    def test_descriptive_versioned_equal_weight_default(self):
        report = ecs_score(source_grounding=1, calibration=0.8, adversarial_correction=0.6)
        self.assertAlmostEqual(report["score"], 0.8)
        self.assertEqual(report["version"], "keel.ecs.descriptive.v1")
        self.assertFalse(report["empirically_validated"])
        self.assertFalse(report["execution_authority"])

    def test_missing_component_is_null_not_renormalized(self):
        report = ecs_score(source_grounding=1, calibration=None, adversarial_correction=1)
        self.assertIsNone(report["score"])
        self.assertEqual(report["missing_components"], ["calibration"])
        self.assertEqual(report["status"], "unavailable")

    def test_custom_weights_recorded(self):
        weights = {"source_grounding": 2, "calibration": 1, "adversarial_correction": 1}
        original = copy.deepcopy(weights)
        report = ecs_score(source_grounding=1, calibration=0, adversarial_correction=0, weights=weights)
        self.assertEqual(report["score"], 0.5)
        self.assertEqual(weights, original)

    def test_bad_weights_and_components_rejected(self):
        for weights in ({}, {"source_grounding": 1, "calibration": 0, "adversarial_correction": 1},
                        {"source_grounding": 1, "calibration": True, "adversarial_correction": 1}):
            with self.assertRaises(MetricsError):
                ecs_score(source_grounding=1, calibration=1, adversarial_correction=1, weights=weights)
        for value in (math.nan, True, 1.01, "1"):
            with self.assertRaises(MetricsError):
                ecs_score(source_grounding=value, calibration=1, adversarial_correction=1)


class ConformalTests(unittest.TestCase):
    def test_finite_sample_vacuous_small_calibration(self):
        model = fit_conformal([prediction("cal")], alpha=0.1, training_ids=["train"], test_ids=["test"])
        self.assertEqual(model["quantile_rank"], 2)
        self.assertEqual(model["threshold"], 1)
        report = conformal_sets(model, [prediction("test", 1, None)])
        self.assertEqual(report["predictions"][0]["labels"], [False, True])
        self.assertTrue(report["predictions"][0]["abstain"])
        self.assertFalse(report["execution_authority"])

    def test_exact_finite_sample_boundary_uses_nth_score(self):
        model = fit_conformal([prediction(f"cal{i}", 0.9, True) for i in range(9)], alpha=0.1)
        self.assertEqual(model["quantile_rank"], 9)
        self.assertFalse(model["finite_sample_vacuous"])
        self.assertAlmostEqual(model["threshold"], 0.1)

    def test_singletons_empty_and_explicit_abstention(self):
        model = fit_conformal([prediction(f"cal{i}", 0.9, True) for i in range(9)])
        report = conformal_sets(model, [prediction("yes", 0.99, None), prediction("no", 0.01, None),
                                        prediction("unclear", 0.5, None), prediction("abstain", 0.99, None, True)])
        self.assertEqual([r["labels"] for r in report["predictions"]], [[True], [False], [], [True]])
        self.assertEqual([r["abstain"] for r in report["predictions"]], [False, False, True, True])

    def test_labels_do_not_influence_test_set(self):
        model = fit_conformal([prediction(f"cal{i}", 0.9, True) for i in range(9)])
        results = [conformal_sets(model, [prediction("test", 0.99, outcome)])["predictions"]
                   for outcome in (True, False, None)]
        self.assertEqual(results[0], results[1])
        self.assertEqual(results[1], results[2])

    def test_training_calibration_test_leakage_rejected(self):
        for training_ids, test_ids in ((["cal"], []), ([], ["cal"]), (["same"], ["same"])):
            with self.assertRaises(MetricsError):
                fit_conformal([prediction("cal")], training_ids=training_ids, test_ids=test_ids)
        model = fit_conformal([prediction("cal")], training_ids=["train"], test_ids=["reserved"])
        for identity in ("cal", "train", "not_reserved"):
            with self.assertRaises(MetricsError):
                conformal_sets(model, [prediction(identity)])

    def test_missing_calibration_labels_abstention_and_invalid_alpha(self):
        for rows in ([], [prediction(outcome=None)], [prediction(abstained=True)]):
            with self.assertRaises(MetricsError):
                fit_conformal(rows)
        for alpha in (0, 1, True, math.nan, math.inf, "0.1"):
            with self.assertRaises(MetricsError):
                fit_conformal([prediction()], alpha=alpha)

    def test_model_inconsistency_detected(self):
        model = fit_conformal([prediction(f"cal{i}", 0.9) for i in range(9)])
        for key, value in (("threshold", 0.7), ("quantile_rank", 8), ("calibration_size", 10),
                           ("schema_version", True), ("finite_sample_vacuous", True),
                           ("method", "unknown"), ("nonconformity_scores", [0.1])):
            corrupt = copy.deepcopy(model)
            corrupt[key] = value
            with self.subTest(key=key), self.assertRaises(MetricsError):
                conformal_sets(corrupt, [prediction("test")])

    def test_duplicate_partition_ids_and_test_ids_rejected(self):
        with self.assertRaises(MetricsError):
            fit_conformal([prediction("cal")], training_ids=["train", "train"])
        model = fit_conformal([prediction("cal")])
        with self.assertRaises(MetricsError):
            conformal_sets(model, [prediction("test"), prediction("test")])

    def test_roundtrip_and_nonmutation(self):
        records = [prediction(f"cal{i}", i / 10, i > 5) for i in range(10)]
        original = copy.deepcopy(records)
        model = fit_conformal(records)
        frozen = json.dumps(model, sort_keys=True, allow_nan=False)
        result = conformal_sets(json.loads(frozen), [prediction("test", 0.9, None)])
        json.dumps(result, allow_nan=False)
        self.assertEqual(records, original)
        self.assertEqual(json.dumps(model, sort_keys=True, allow_nan=False), frozen)


if __name__ == "__main__":
    unittest.main()
