#!/usr/bin/env python3
"""Tests for keel.math.keel_math — adopted-from-lab invariants plus the
alternative-derivation extension the lab explicitly lacked."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "math"))

from keel_math import (
    best_question_bundle,
    conformal_radius,
    eligibility_confirmed,
    empirical_cvar,
    finite,
    invalidate,
    require_aligned,
)


class FiniteCase(unittest.TestCase):
    def test_rejects_bool_nan_inf(self):
        for bad in (True, float("nan"), float("inf"), "3", None):
            with self.assertRaises(ValueError):
                finite(bad)
        self.assertEqual(finite(3), 3.0)


class EligibilityCase(unittest.TestCase):
    def test_identity_not_truthiness(self):
        self.assertTrue(eligibility_confirmed(True))
        for impostor in ("true", "True", 1, 1.0, [True], {"x": 1}):
            self.assertFalse(eligibility_confirmed(impostor),
                             "must not read %r as authorization" % (impostor,))
        self.assertFalse(eligibility_confirmed(None))
        self.assertFalse(eligibility_confirmed(False))


class CvarCase(unittest.TestCase):
    def test_fractional_tail_mass(self):
        # beta=.625 -> tail holds 1.5 observations: (10 + .5*0)/1.5
        self.assertAlmostEqual(empirical_cvar([0, 0, 0, 10], .625), 20 / 3)

    def test_mean_and_worst_tail(self):
        self.assertEqual(empirical_cvar([0, 0, 0, 10], 0), 2.5)
        self.assertEqual(empirical_cvar([0, 0, 0, 10], .9), 10)

    def test_rejects_bad_input(self):
        with self.assertRaises(ValueError):
            empirical_cvar([1], 1)
        with self.assertRaises(ValueError):
            empirical_cvar([], 0.5)
        with self.assertRaises(ValueError):
            empirical_cvar([float("nan")], 0.5)


class AlignedCase(unittest.TestCase):
    def test_aligned_ok(self):
        self.assertEqual(require_aligned([[1, 2], [3, 4]]), 2)

    def test_misaligned_rejected(self):
        with self.assertRaises(ValueError):
            require_aligned([[1, 2], [3]])
        with self.assertRaises(ValueError):
            require_aligned([[]])


class InvalidateCase(unittest.TestCase):
    def test_simple_chain(self):
        deps = {"answer_v1": [], "packet_a": ["answer_v1"],
                "ready_a": ["packet_a"], "unrelated": []}
        self.assertEqual(invalidate(deps, ["answer_v1"]),
                         ["answer_v1", "packet_a", "ready_a"])

    def test_unrelated_preserved(self):
        deps = {"a": [], "b": ["a"], "c": ["b"], "d": []}
        self.assertEqual(invalidate(deps, []), [])

    def test_alternative_derivation_survives(self):
        # packet justified by v1 OR v2: revoking v1 alone must not kill it.
        deps = {"v1": [], "v2": [],
                "packet": [["v1"], ["v2"]],
                "ready": [["packet"]]}
        self.assertEqual(invalidate(deps, ["v1"]), ["v1"])
        self.assertEqual(invalidate(deps, ["v1", "v2"]),
                         ["packet", "ready", "v1", "v2"])

    def test_conjunctive_derivation(self):
        # needs BOTH a and b via one derivation
        deps = {"a": [], "b": [], "p": [["a", "b"]]}
        self.assertEqual(invalidate(deps, ["a"]), ["a", "p"])
        self.assertEqual(invalidate(deps, []), [])

    def test_ungrounded_cycle_cannot_self_support(self):
        # a<->b with no grounded derivation: invalid even unrevoked
        deps = {"a": [["b"]], "b": [["a"]]}
        self.assertEqual(invalidate(deps, []), ["a", "b"])

    def test_cycle_terminates_and_dangling_rejected(self):
        self.assertEqual(invalidate({"a": ["b"], "b": ["a"]}, ["a"]),
                         ["a", "b"])
        with self.assertRaises(ValueError):
            invalidate({"a": ["missing"]}, ["a"])
        with self.assertRaises(ValueError):
            invalidate({"a": []}, ["ghost"])

    def test_mixed_format_rejected(self):
        with self.assertRaises(ValueError):
            invalidate({"a": ["b", ["c"]]}, [])


class ConformalCase(unittest.TestCase):
    def test_small_sample_does_not_fake_precision(self):
        self.assertIsNone(conformal_radius([1, 2], .05))
        self.assertEqual(conformal_radius(list(range(1, 20)), .1), 18.0)

    def test_rejects_bad_input(self):
        with self.assertRaises(ValueError):
            conformal_radius([-1], 0.1)
        with self.assertRaises(ValueError):
            conformal_radius([], 0.1)


class BundleCase(unittest.TestCase):
    def test_complementary_requirements_exact(self):
        # packet needs BOTH essay and residence: neither question alone
        # unlocks it. A greedy one-at-a-time heuristic would pick neither
        # (zero marginal gain each); exact enumeration picks the pair.
        questions = [
            {"id": "essay", "cost": 1, "resolves": {"essay"}},
            {"id": "residence", "cost": 1, "resolves": {"residence"}},
            {"id": "trivia", "cost": 1, "resolves": {"trivia"}},
        ]
        packets = [{"id": "p1", "value": 10,
                    "needs": {"essay", "residence"}}]
        res = best_question_bundle(questions, packets)
        self.assertEqual(sorted(res["selected"]), ["essay", "residence"])
        self.assertEqual(res["unlocked_value"], 10.0)

    def test_cost_discipline(self):
        questions = [
            {"id": "q1", "cost": 100, "resolves": {"r1"}},
            {"id": "q2", "cost": 1, "resolves": {"r1"}},
        ]
        packets = [{"id": "p", "value": 10, "needs": {"r1"}}]
        res = best_question_bundle(questions, packets)
        self.assertEqual(res["selected"], ["q2"])

    def test_never_asks_at_a_loss(self):
        questions = [{"id": "q", "cost": 50, "resolves": {"r"}}]
        packets = [{"id": "p", "value": 10, "needs": {"r"}}]
        res = best_question_bundle(questions, packets)
        self.assertEqual(res["selected"], [])
        self.assertEqual(res["objective"], 0.0)

    def test_rejects_bad_input(self):
        with self.assertRaises(ValueError):
            best_question_bundle([], [{"id": "p", "value": 1,
                                       "needs": {"r"}}])
        with self.assertRaises(ValueError):
            best_question_bundle(
                [{"id": "q", "cost": 1, "resolves": {"r"}},
                 {"id": "q", "cost": 1, "resolves": {"r"}}],
                [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
