"""Regression tests for PR #21 review thread 2: recount gate-name aliases.

Contract under test (geo-pipeline/recount.py):
  1. PUBLIC_GATE_NAMES aliases the two private operator gate names to their
     sanitized public forms.
  2. count_gates() applies the alias at generation time, so re-running the
     recount reproduces the sanitized docs/geo/stats.json keys instead of
     restoring the private ones. Telemetry itself keeps the private names.
  3. Non-aliased gates pass through unchanged.

Run: python3 -m unittest discover -s tests
"""
import json
import os
import sys
import tempfile
import types
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "geo-pipeline"))

# recount.py imports canon_ledger_status from the private pipeline's
# outcome-tracking dir at module level (sys.path derived from
# ~/workspace/job-pipeline). That path does not exist on a fresh clone /
# CI runner, so importing recount there would explode before any test runs.
# count_gates() — the contract under test here — never touches
# canon_ledger_status, so stub the unrelated import deterministically.
_stub = types.ModuleType("ledger_append")
_stub.canon_ledger_status = lambda s: s
sys.modules["ledger_append"] = _stub

import recount


def _telemetry_line(role_id, gate, backfilled=False):
    return json.dumps({
        "event_type": "gate_blocked",
        "role_id": role_id,
        "details": {"gate": gate, "backfilled": backfilled},
    })


class TestRecountGateAliases(unittest.TestCase):
    def test_alias_map_covers_both_private_names(self):
        self.assertEqual(
            recount.PUBLIC_GATE_NAMES.get("needs_trent_input"),
            "needs_operator_input")
        self.assertEqual(
            recount.PUBLIC_GATE_NAMES.get("trent_input_needs_user"),
            "operator_input_needs_user")

    def test_count_gates_emits_sanitized_keys(self):
        lines = [
            _telemetry_line("R-1", "needs_trent_input"),
            _telemetry_line("R-2", "needs_trent_input"),
            _telemetry_line("R-3", "trent_input_needs_user"),
            _telemetry_line("R-4", "some_other_gate"),
            _telemetry_line("R-5", "needs_trent_input", backfilled=True),
        ]
        with tempfile.NamedTemporaryFile(
                "w", suffix=".jsonl", delete=False) as f:
            f.write("\n".join(lines) + "\n")
            path = f.name
        try:
            result = recount.count_gates(path)
        finally:
            os.unlink(path)
        per_gate = result["per_gate"]
        # Sanitized keys present with the right distinct-pair counts.
        self.assertEqual(per_gate.get("needs_operator_input"), 3)
        self.assertEqual(per_gate.get("operator_input_needs_user"), 1)
        # Private names never leak into the generated output.
        self.assertNotIn("needs_trent_input", per_gate)
        self.assertNotIn("trent_input_needs_user", per_gate)
        # Unrelated gates pass through untouched.
        self.assertEqual(per_gate.get("some_other_gate"), 1)

    def test_backfilled_private_gate_pair_dedupes_under_alias(self):
        # The same (role, private-gate) pair appearing live and backfilled
        # must dedupe to one pair under the sanitized name (methodology v2).
        lines = [
            _telemetry_line("R-9", "needs_trent_input"),
            _telemetry_line("R-9", "needs_trent_input", backfilled=True),
        ]
        with tempfile.NamedTemporaryFile(
                "w", suffix=".jsonl", delete=False) as f:
            f.write("\n".join(lines) + "\n")
            path = f.name
        try:
            result = recount.count_gates(path)
        finally:
            os.unlink(path)
        self.assertEqual(result["per_gate"].get("needs_operator_input"), 1)
        self.assertEqual(result["distinct_pairs"], 1)
        self.assertEqual(result["overlap"], 1)


if __name__ == "__main__":
    unittest.main()
