"""Offline tests for the dashboard gate-blocked panel (no network)."""
import json
import os
import sys
import tempfile
import unittest

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(BASE, "..", "engines"))

import build_dashboard as bd  # noqa: E402


def make_home(files):
    tmp = tempfile.mkdtemp()
    for rel, content in files.items():
        p = os.path.join(tmp, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(content)
    return tmp


TELEMETRY = "\n".join([
    json.dumps({"ts": "2026-09-15T00:00:00Z", "event_type": "gate_blocked",
                "role_id": "R1", "details": {"gate": "needs_input"}}),
    json.dumps({"ts": "2026-09-15T01:00:00Z", "type": "gate_blocked",
                "role_id": "R2", "details": {"gate": "low_fit"}}),
    json.dumps({"ts": "2026-09-15T02:00:00Z", "event_type": "gate_blocked",
                "role_id": "R3", "details": {"gate": "needs_input"}}),
    json.dumps({"ts": "2026-09-15T03:00:00Z", "event_type": "lead_discovered",
                "role_id": "R4", "details": {}}),
    "not-json {{{",
])


class TestGatePanel(unittest.TestCase):
    def test_counts_by_gate(self):
        # The counts test uses complete valid input; corruption is tested below.
        home = make_home({"data/telemetry/events.jsonl": TELEMETRY.rsplit('\n', 1)[0]})
        data = bd.collect(home=home)
        self.assertEqual(data["gate_blocks"], {"needs_input": 2, "low_fit": 1})
        html = bd.render(data)
        self.assertIn("Gate blocks", html)
        self.assertIn("needs_input", html)
        self.assertIn("low_fit", html)

    def test_missing_telemetry_is_unknown_not_zero(self):
        home = make_home({})
        data = bd.collect(home=home)
        self.assertIsNone(data["gate_blocks"])
        html = bd.render(data)
        self.assertIn("Unknown", html)

    def test_malformed_lines_make_total_unknown(self):
        home = make_home({"data/telemetry/events.jsonl": "not-json {{{"})
        data = bd.collect(home=home)
        self.assertIsNone(data["gate_blocks"])
        self.assertTrue(any('malformed' in warning for warning in data['warnings']))


if __name__ == "__main__":
    unittest.main()
