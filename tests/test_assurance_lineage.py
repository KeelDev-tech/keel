"""Adversarial and boundary checks for necessary-dependency lineage."""

import copy
from datetime import datetime, timezone
import random
import unittest

from keel_assurance.lineage import (
    MAX_DEPTH,
    MAX_EDGES,
    MAX_NODES,
    MAX_ROOTS,
    affected,
    evaluate_lineage,
)


NOW = "2026-09-18T12:00:00Z"


def node(identifier="source", *, parents=None, **changes):
    result = {
        "id": identifier,
        "revision": "sha256:a",
        "expected_revision": "sha256:a",
        "status": "CURRENT",
        "observed_at": "2026-09-18T11:00:00Z",
        "expires_at": "2026-09-18T13:00:00Z",
        "parents": [] if parents is None else parents,
    }
    result.update(changes)
    return result


class LineageTests(unittest.TestCase):
    def evaluate(self, nodes, roots=None, now=NOW):
        if roots is None:
            roots = [nodes[-1]["id"]] if nodes else []
        return evaluate_lineage(nodes, roots, now=now)

    def test_current_root_and_empty_graph(self):
        self.assertEqual(self.evaluate([node()]), {
            "roots": {"source": {"valid": True, "reasons": []}},
            "invalidated_ids": [],
        })
        self.assertEqual(self.evaluate([]), {"roots": {}, "invalidated_ids": []})

    def test_observation_is_inclusive_expiry_exclusive(self):
        source = node(observed_at=NOW)
        self.assertTrue(self.evaluate([source])["roots"]["source"]["valid"])
        source = node(expires_at=NOW)
        self.assertEqual(self.evaluate([source])["roots"]["source"], {
            "valid": False, "reasons": ["source:expired"],
        })

    def test_future_observation(self):
        source = node(observed_at="2026-09-18T12:00:00.000001Z")
        self.assertEqual(self.evaluate([source])["roots"]["source"]["reasons"],
                         ["source:observed_in_future"])

    def test_revision_is_exact_not_case_folded(self):
        source = node(revision="ABC", expected_revision="abc")
        self.assertEqual(self.evaluate([source])["roots"]["source"]["reasons"],
                         ["source:revision_mismatch"])

    def test_revoked_ancestor_propagates_and_unknown_is_not_current(self):
        graph = [node("a", status="REVOKED"), node("b", parents=["a"]),
                 node("c", parents=["b"]), node("unrelated", status="UNKNOWN")]
        result = self.evaluate(graph, ["c"])
        self.assertEqual(result["roots"]["c"], {"valid": False, "reasons": ["a:revoked"]})
        self.assertEqual(result["invalidated_ids"], ["a", "b", "c", "unrelated"])

    def test_every_parent_is_necessary_no_alternative_proofs(self):
        graph = [node("good"), node("bad", status="REVOKED"),
                 node("claim", parents=["good", "bad"])]
        self.assertFalse(self.evaluate(graph)["roots"]["claim"]["valid"])

    def test_shared_ancestor_reasons_deduplicated(self):
        graph = [node("a", status="REVOKED", revision="different", expires_at=NOW),
                 node("b", parents=["a"]), node("c", parents=["a"]),
                 node("d", parents=["b", "c"])]
        self.assertEqual(self.evaluate(graph)["roots"]["d"]["reasons"],
                         ["a:expired", "a:revision_mismatch", "a:revoked"])

    def test_equivalent_offsets_and_aware_datetime(self):
        source = node(observed_at="2026-09-18T05:00:00-07:00")
        for now in [NOW, "2026-09-18T14:00:00+02:00", datetime(2026, 9, 18, 12, tzinfo=timezone.utc)]:
            self.assertTrue(self.evaluate([source], now=now)["roots"]["source"]["valid"])

    def test_no_mutation_and_order_invariance(self):
        graph = [node("x", status="UNKNOWN"), node("z", parents=["x", "a"]), node("a")]
        before = copy.deepcopy(graph)
        roots = ["z", "a"]
        expected = self.evaluate(graph, roots)
        self.assertEqual(graph, before)
        self.assertEqual(roots, ["z", "a"])
        shuffled = copy.deepcopy(graph)
        random.Random(42).shuffle(shuffled)
        for item in shuffled:
            item["parents"].reverse()
        self.assertEqual(self.evaluate(shuffled, list(reversed(roots))), expected)
        self.assertEqual(list(expected["roots"]), ["a", "z"])

    def test_affected_transitive_children_not_ancestors(self):
        graph = [node("a"), node("b", parents=["a"]), node("c", parents=["b"]), node("d")]
        self.assertEqual(affected(graph, ["b"]), ["b", "c"])
        self.assertEqual(affected(graph, ["d"]), ["d"])
        self.assertEqual(affected(graph, ["a", "b"]), ["a", "b", "c"])
        self.assertEqual(affected(graph, []), [])

    def test_irrelevant_change_does_not_invalidate_selected_root(self):
        graph = [node("a"), node("b", parents=["a"]), node("other", revision="new")]
        self.assertEqual(affected(graph, ["other"]), ["other"])
        result = self.evaluate(graph, ["b"])
        self.assertTrue(result["roots"]["b"]["valid"])
        self.assertEqual(result["invalidated_ids"], ["other"])

    def test_cycle_even_if_unselected_and_self_support(self):
        for graph in [
            [node("a", parents=["a"])],
            [node("a", parents=["b"]), node("b", parents=["a"])],
            [node("good"), node("a", parents=["b"]), node("b", parents=["a"])],
        ]:
            with self.subTest(graph=graph), self.assertRaisesRegex(ValueError, "cycle"):
                self.evaluate(graph, [graph[0]["id"]])
            with self.assertRaisesRegex(ValueError, "cycle"):
                affected(graph, [])

    def test_dangling_edges_even_unselected(self):
        graph = [node("good"), node("bad", parents=["missing"])]
        with self.assertRaisesRegex(ValueError, "dangling"):
            self.evaluate(graph, ["good"])
        with self.assertRaisesRegex(ValueError, "dangling"):
            affected(graph, ["good"])

    def test_duplicates_rejected(self):
        for graph, roots in [([node(), node()], ["source"]),
                             ([node()], ["source", "source"]),
                             ([node("a"), node("b", parents=["a", "a"])], ["b"])]:
            with self.subTest(graph=graph, roots=roots), self.assertRaises(ValueError):
                self.evaluate(graph, roots)
        with self.assertRaises(ValueError):
            affected([node()], ["source", "source"])

    def test_missing_selected_ids_rejected(self):
        with self.assertRaises(ValueError):
            self.evaluate([node()], ["missing"])
        with self.assertRaises(ValueError):
            affected([node()], ["missing"])

    def test_strict_node_schema(self):
        for key in node():
            value = node()
            del value[key]
            with self.subTest(missing=key), self.assertRaises(ValueError):
                self.evaluate([value], [])
        value = node(extra=True)
        with self.assertRaises(ValueError):
            self.evaluate([value], [])

    def test_strict_field_types_and_strings(self):
        for key in ["id", "revision", "expected_revision", "status"]:
            for invalid in [None, False, True, 1, 0.0, [], {}, "", " ", "a\n", "\x00", "a" * 257]:
                with self.subTest(key=key, invalid=invalid), self.assertRaises(ValueError):
                    self.evaluate([node(**{key: invalid})], [])
        for status in ["current", "VALID", "expired", "FALSE"]:
            with self.subTest(status=status), self.assertRaises(ValueError):
                self.evaluate([node(status=status)])
        for invalid in [None, False, (), {}, "source", [True], [""], ["a\x00"]]:
            value = node()
            value["parents"] = invalid
            with self.subTest(parents=invalid), self.assertRaises(ValueError):
                self.evaluate([value], [])

    def test_container_types_rejected(self):
        for invalid in [None, False, {}, (), ""]:
            with self.subTest(nodes=invalid), self.assertRaises(ValueError):
                evaluate_lineage(invalid, [], now=NOW)
            with self.subTest(roots=invalid), self.assertRaises(ValueError):
                evaluate_lineage([node()], invalid, now=NOW)
            with self.subTest(changed=invalid), self.assertRaises(ValueError):
                affected([node()], invalid)
        with self.assertRaises(ValueError):
            self.evaluate([True], [])

    def test_invalid_timestamps_and_now_rejected(self):
        invalid_timestamps = [None, False, 1, "", "2026-09-18", "2026-09-18T12:00:00",
                              "2026-09-18 12:00:00Z", "2026-02-30T12:00:00Z",
                              "2026-09-18T12:00:60Z", "2026-09-18T12:00:00+00:60",
                              "2026-09-18T12:00:00+24:00", "2026-09-18T12:00:00.1234567Z",
                              "0001-01-01T00:00:00+23:00", "9999-12-31T23:59:59-23:00"]
        for value in invalid_timestamps:
            for field in ["observed_at", "expires_at"]:
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    self.evaluate([node(**{field: value})], [])
            with self.subTest(now=value), self.assertRaises(ValueError):
                self.evaluate([node()], now=value)
        with self.assertRaises(ValueError):
            self.evaluate([node()], now=datetime(2026, 9, 18, 12))

    def test_empty_or_backwards_freshness_interval_rejected(self):
        for observed, expires in [(NOW, NOW), ("2026-09-18T13:00:00Z", NOW)]:
            with self.subTest(observed=observed), self.assertRaises(ValueError):
                self.evaluate([node(observed_at=observed, expires_at=expires)])

    def test_long_chain_iterative_and_depth_limit(self):
        graph = [node(f"n{i:04d}", parents=[f"n{i - 1:04d}"] if i else []) for i in range(MAX_DEPTH)]
        self.assertTrue(self.evaluate(graph)["roots"][graph[-1]["id"]]["valid"])
        self.assertEqual(len(affected(graph, ["n0000"])), MAX_DEPTH)
        graph.append(node("too-deep", parents=[graph[-1]["id"]]))
        with self.assertRaisesRegex(ValueError, "depth"):
            self.evaluate(graph)

    def test_node_and_root_limits(self):
        graph = [node(f"n{i}") for i in range(MAX_NODES)]
        self.assertTrue(self.evaluate(graph)["roots"][graph[-1]["id"]]["valid"])
        with self.assertRaisesRegex(ValueError, "nodes"):
            self.evaluate(graph + [node("too-many")])
        with self.assertRaisesRegex(ValueError, "roots"):
            self.evaluate(graph, [item["id"] for item in graph[:MAX_ROOTS + 1]])

    def test_edge_limit(self):
        sources = [node(f"a{i}") for i in range(129)]
        dependents = [node(f"b{i}", parents=[item["id"] for item in sources]) for i in range(128)]
        self.assertGreater(129 * 128, MAX_EDGES)
        with self.assertRaisesRegex(ValueError, "edges"):
            self.evaluate(sources + dependents, [])

    def test_reference_model_on_random_dags(self):
        rng = random.Random(7331)
        for trial in range(20):
            graph = []
            expected_valid = {}
            for i in range(50):
                identifier = f"n{i:02d}"
                parents = [f"n{j:02d}" for j in range(i) if rng.random() < 0.10]
                revoked = rng.random() < 0.15
                graph.append(node(identifier, parents=parents, status="REVOKED" if revoked else "CURRENT"))
                expected_valid[identifier] = not revoked and all(expected_valid[parent] for parent in parents)
            result = self.evaluate(graph, list(expected_valid))
            with self.subTest(trial=trial):
                self.assertEqual({key: value["valid"] for key, value in result["roots"].items()}, expected_valid)
                self.assertEqual(result["invalidated_ids"], sorted(key for key, valid in expected_valid.items() if not valid))


if __name__ == "__main__":
    unittest.main()
