#!/usr/bin/env python3
"""Writer-side null contract for formation.json (2026-09-18, gap item B).

Regression for pulse 444 (2026-09-18 20:30Z): the octopus coordinator wrote
a suppressed 'emergency-refill' arm record with an EXPLICIT null
"started" field ({"name": "emergency-refill", "started": None, ...}).
a.get("started", "") returned None, html.escape(None) raised
AttributeError, and the whole dashboard render died. The render side was
hardened first (pulse_dashboard._esc, metric
pulse_dashboard_none_escape_20260918); this file pins the WRITER side:
suppressed/incomplete arm records must OMIT the field, never emit
explicit null -- and formation_writer.py --scrub strips any null that
slips through before the dashboard regenerates.

A legacy fixture reproduces the exact old-writer output; the writer must
emit a dict with no "started" key and no explicit null anywhere in the
serialized JSON.

Run: python3 test_formation_writer.py
"""
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest

BASE = os.path.dirname(os.path.abspath(__file__))
FW_PATH = os.path.abspath(os.path.join(BASE, "..", "monitors",
                                        "formation_writer.py"))

# The exact record shape the OLD writer path produced for a suppressed arm:
# explicit null where the arm never started.
LEGACY_NULL_STARTED_ARM = {
    "name": "emergency-refill",
    "task": "Emergency-refill delta on ready_floor_breach",
    "status": "suppressed",
    "started": None,
    "finished": None,
    "delta": None,
}

LEGACY_FORMATION = {
    "ts": "2026-09-18T20:30:00Z",
    "digest": "digest-20260918T203000Z.md",
    "snapshot_file": "state-snapshot.json",
    "arms": [dict(LEGACY_NULL_STARTED_ARM)],
    "efficiency_delta": "quiet pulse",
}


def load_formation_writer():
    spec = importlib.util.spec_from_file_location("formation_writer", FW_PATH)
    fw = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fw)
    return fw


def no_explicit_nulls(node):
    """True when no dict value and no list item anywhere is None."""
    if isinstance(node, dict):
        return all(v is not None and no_explicit_nulls(v)
                   for v in node.values())
    if isinstance(node, list):
        return all(v is not None and no_explicit_nulls(v) for v in node)
    return True


class BuildArmTest(unittest.TestCase):
    """build_arm() omits None fields instead of emitting explicit nulls."""

    def test_started_none_omits_key(self):
        fw = load_formation_writer()
        arm = fw.build_arm(
            name="emergency-refill",
            task="Emergency-refill delta on ready_floor_breach",
            status="suppressed",
            started=None,          # the suppressed arm never started
            finished=None,
            delta=None,
        )
        self.assertNotIn("started", arm,
                         "suppressed arm must not carry a 'started' key")
        self.assertNotIn("finished", arm)
        self.assertNotIn("delta", arm)
        self.assertEqual(arm["name"], "emergency-refill")
        self.assertEqual(arm["status"], "suppressed")

    def test_started_value_kept(self):
        fw = load_formation_writer()
        arm = fw.build_arm(name="ARM 1", task="t", status="done",
                           started="2026-09-18T21:03:10Z")
        self.assertEqual(arm["started"], "2026-09-18T21:03:10Z")

    def test_no_none_values_in_output(self):
        fw = load_formation_writer()
        arm = fw.build_arm(name="ARM 2", task=None, status="running",
                           started=None)
        self.assertTrue(no_explicit_nulls(arm))


class ScrubNullsTest(unittest.TestCase):
    """scrub_nulls() strips the exact legacy null-started record."""

    def test_legacy_null_started_arm_scrubbed(self):
        fw = load_formation_writer()
        clean, removed = fw.scrub_nulls(dict(LEGACY_NULL_STARTED_ARM))
        self.assertNotIn("started", clean,
                         "explicit null 'started' must be stripped, "
                         "not kept as None")
        self.assertEqual(removed, 3)  # started + finished + delta
        self.assertEqual(clean["name"], "emergency-refill")
        self.assertEqual(clean["status"], "suppressed")

    def test_nested_nulls_scrubbed(self):
        fw = load_formation_writer()
        doc = {"arms": [dict(LEGACY_NULL_STARTED_ARM)],
               "meta": {"x": None}, "top": None}
        clean, removed = fw.scrub_nulls(doc)
        self.assertTrue(no_explicit_nulls(clean))
        self.assertNotIn("top", clean)
        self.assertNotIn("x", clean["meta"])
        self.assertGreaterEqual(removed, 4)

    def test_clean_document_untouched(self):
        fw = load_formation_writer()
        doc = {"arms": [{"name": "ARM 1", "started": "2026-09-18T21:03:10Z"}]}
        clean, removed = fw.scrub_nulls(doc)
        self.assertEqual(clean, doc)
        self.assertEqual(removed, 0)


class WriteFormationTest(unittest.TestCase):
    """End-to-end: the file on disk carries zero explicit nulls."""

    def test_write_formation_has_no_nulls_in_raw_text(self):
        fw = load_formation_writer()
        tmp = os.path.join(tempfile.mkdtemp(prefix="fwtest-"),
                           "formation.json")
        removed = fw.write_formation(tmp, LEGACY_FORMATION)
        self.assertEqual(removed, 3)
        with open(tmp, encoding="utf-8") as fh:
            raw = fh.read()
        self.assertNotIn('"started": null', raw,
                         "raw JSON must not contain an explicit null")
        self.assertNotIn(": null", raw,
                         "raw JSON must not contain ANY explicit null")
        doc = json.loads(raw)
        arm = doc["arms"][0]
        self.assertNotIn("started", arm)
        self.assertTrue(no_explicit_nulls(doc))

    def test_scrub_cli_on_legacy_file(self):
        tmp = os.path.join(tempfile.mkdtemp(prefix="fwtest-"),
                           "formation.json")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(LEGACY_FORMATION, fh)
        proc = subprocess.run(
            [sys.executable, FW_PATH, "--scrub", tmp],
            capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = json.loads(proc.stdout)
        self.assertEqual(report["nulls_removed"], 3)
        self.assertTrue(report["ok"])
        with open(tmp, encoding="utf-8") as fh:
            doc = json.load(fh)
        self.assertNotIn("started", doc["arms"][0])
        self.assertTrue(no_explicit_nulls(doc))


if __name__ == "__main__":
    unittest.main(verbosity=1)
