"""Regression tests for the 2026-09-19 silent-defect sweep (keel tree).

Synthetic fixtures only; no live state, no queue writes.

FIX 1: READY-FOR-BROWSER eligibility disagreement
  buffer_queue_consistency_sweep.is_launchable must honor the canonical
  READY_STATES of apply_loop ("READY", "READY-FOR-BROWSER").

FIX 2: dict-shaped queue loader disagreement
  feeder_watchdog.load_queue and
  buffer_queue_consistency_sweep.load_queue_statuses must see identical
  entries for list payloads and for dict payloads keyed by
  entries/items/leads (via engines/queue_entries.load_queue_entries).

FIX 3: exporter's cross-queue last-wins
  export_flow_snapshot's queue_index must prefer the standard-queue entry
  on duplicate role_id (one-lead-one-queue).
"""
import json
import os
import re
import sys
import unittest

KEEL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENGINES = os.path.join(KEEL_DIR, "engines")
sys.path.insert(0, ENGINES)

import queue_entries  # noqa: E402
import buffer_queue_consistency_sweep as sweep  # noqa: E402
import feeder_watchdog as watchdog  # noqa: E402


def _write_json(path, obj):
    with open(path, "w") as f:
        json.dump(obj, f)


# ---------------------------------------------------------------------------
# FIX 1
# ---------------------------------------------------------------------------
class Fix1ReadyForBrowserTests(unittest.TestCase):
    def test_ready_for_browser_is_launchable(self):
        self.assertTrue(sweep.is_launchable("READY-FOR-BROWSER"))

    def test_ready_still_launchable(self):
        self.assertTrue(sweep.is_launchable("READY"))

    def test_parked_still_not_launchable(self):
        self.assertFalse(sweep.is_launchable("PARKED"))

    def test_matches_canonical_ready_states(self):
        # Canonical definition lives in apply_loop.READY_STATES
        # ("READY", "READY-FOR-BROWSER"); the sweep must not drift from it.
        # (apply_loop is deliberately not imported here: it is heavy.)
        canonical = ("READY", "READY-FOR-BROWSER")
        for state in canonical:
            self.assertTrue(sweep.is_launchable(state),
                            f"sweep disagrees with canonical READY_STATES on {state!r}")

    def test_case_and_whitespace_robust(self):
        self.assertTrue(sweep.is_launchable("  ready-for-browser  "))


# ---------------------------------------------------------------------------
# FIX 2
# ---------------------------------------------------------------------------
class Fix2QueueLoaderAgreementTests(unittest.TestCase):
    def _tmp_queue(self, tmpdir, payload):
        path = os.path.join(tmpdir, "q.json")
        _write_json(path, payload)
        return path

    def _both_loaders(self, tmpdir, payload):
        """Run both real loaders against the same synthetic payload."""
        path = self._tmp_queue(tmpdir, payload)
        old_queue = watchdog.QUEUE
        old_queues = sweep.QUEUES
        watchdog.QUEUE = path
        sweep.QUEUES = {"standard": path}
        try:
            watchdog_entries = watchdog.load_queue()
            statuses = sweep.load_queue_statuses()
        finally:
            watchdog.QUEUE = old_queue
            sweep.QUEUES = old_queues
        sweep_entries = [rid for rid, (qn, st) in statuses.items()]
        return watchdog_entries, statuses

    def test_list_shape_agrees(self):
        import tempfile
        entries = [{"role_id": "r1", "status": "READY"},
                   {"role_id": "r2", "status": "PARKED"}]
        with tempfile.TemporaryDirectory() as d:
            w, statuses = self._both_loaders(d, entries)
        self.assertEqual([e["role_id"] for e in w], ["r1", "r2"])
        self.assertEqual(statuses, {"r1": ("standard", "READY"),
                                   "r2": ("standard", "PARKED")})

    def test_dict_shapes_agree_entries_items_leads(self):
        import tempfile
        for key in ("entries", "items", "leads"):
            payload = {key: [{"role_id": "r9", "status": "READY"}]}
            with tempfile.TemporaryDirectory() as d:
                w, statuses = self._both_loaders(d, payload)
            with self.subTest(key=key):
                self.assertEqual(len(w), 1, "watchdog saw nothing")
                self.assertEqual(w[0]["role_id"], "r9")
                self.assertEqual(statuses, {"r9": ("standard", "READY")})

    def test_load_queue_entries_unit(self):
        e = [{"role_id": "x"}]
        self.assertEqual(queue_entries.load_queue_entries(e), e)  # list passthrough
        for key in ("entries", "items", "leads"):
            self.assertEqual(queue_entries.load_queue_entries({key: e}), e,
                             key)
        # first present key wins, consistent across consumers
        self.assertEqual(
            queue_entries.load_queue_entries({"leads": [{"role_id": "a"}],
                                              "entries": e}), e)
        self.assertEqual(queue_entries.load_queue_entries({}), [])
        self.assertEqual(queue_entries.load_queue_entries({"entries": None}), [])
        self.assertEqual(queue_entries.load_queue_entries("junk"), [])


# ---------------------------------------------------------------------------
# FIX 3
# ---------------------------------------------------------------------------
def _queue_index_from_source(standard, needs_input):
    """Execute the REAL queue_index comprehension line from
    export_flow_snapshot.py source against synthetic queues, so the test
    tracks the production expression exactly (mutation-checkable)."""
    src = open(os.path.join(KEEL_DIR, "export_flow_snapshot.py")).read()
    m = re.search(r"^\s*queue_index = (\{.*\})$", src, re.M)
    assert m, "queue_index line not found in export_flow_snapshot.py"
    ns = {"standard": standard, "needs_input": needs_input}
    exec("queue_index = " + m.group(1), ns)  # noqa: S102 - test-only, local file
    return ns["queue_index"]


class Fix3QueueIndexPrecedenceTests(unittest.TestCase):
    def test_standard_entry_wins_on_duplicate_role_id(self):
        standard = [{"role_id": "rid-1", "status": "READY",
                     "action_band": "APPLY", "fit": "high"}]
        needs_input = [{"role_id": "rid-1", "status": "PARKED-NEEDS-INPUT",
                        "action_band": "HOLD", "fit": "low"}]
        idx = _queue_index_from_source(standard, needs_input)
        self.assertIs(idx["rid-1"], standard[0])

    def test_unique_roles_all_indexed(self):
        standard = [{"role_id": "s1", "status": "READY"}]
        needs_input = [{"role_id": "n1", "status": "PARKED"}]
        idx = _queue_index_from_source(standard, needs_input)
        self.assertEqual(set(idx), {"s1", "n1"})

    def test_entries_without_role_id_skipped(self):
        standard = [{"status": "READY"}, {"role_id": "", "status": "READY"}]
        idx = _queue_index_from_source(standard, [])
        self.assertEqual(idx, {})


if __name__ == "__main__":
    unittest.main()
