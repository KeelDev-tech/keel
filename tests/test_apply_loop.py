"""Workspace loading and missing-file guidance for the public apply loop."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ENGINES = Path(__file__).resolve().parent.parent / "engines"
sys.path.insert(0, str(ENGINES))
import apply_loop


class TestLoadQueue(unittest.TestCase):
    def test_missing_queue_exits_with_setup_guidance(self):
        with tempfile.TemporaryDirectory() as workspace:
            queue = str(Path(workspace) / "data" / "queues" / "standard-queue.json")
            with patch.object(apply_loop, "QUEUE", queue):
                with self.assertRaises(SystemExit) as raised:
                    apply_loop.load_queue()
            message = str(raised.exception)
            self.assertIn(queue, message)
            self.assertIn("not found", message)
            self.assertIn("./setup.sh", message)
            self.assertIn("KEEL_HOME", message)

    def test_existing_queue_formats_are_unchanged(self):
        entries = [{"role_id": "TEST-ROLE-1", "status": "READY"}]
        for payload in (entries, {"entries": entries}, {"items": entries}):
            with self.subTest(payload=payload), tempfile.TemporaryDirectory() as workspace:
                queue = Path(workspace) / "queue.json"
                queue.write_text(json.dumps(payload), encoding="utf-8")
                with patch.object(apply_loop, "QUEUE", str(queue)):
                    self.assertEqual(apply_loop.load_queue(), entries)

    def test_invalid_json_is_not_reported_as_missing(self):
        with tempfile.TemporaryDirectory() as workspace:
            queue = Path(workspace) / "queue.json"
            queue.write_text("not json", encoding="utf-8")
            with patch.object(apply_loop, "QUEUE", str(queue)):
                with self.assertRaises(json.JSONDecodeError):
                    apply_loop.load_queue()

    def test_permission_error_is_not_reported_as_missing(self):
        with patch("builtins.open", side_effect=PermissionError("not readable")):
            with self.assertRaises(PermissionError):
                apply_loop.load_queue()


class TestLoadAnswerBank(unittest.TestCase):
    def test_missing_banks_keep_empty_fallback(self):
        with tempfile.TemporaryDirectory() as workspace:
            with patch.object(apply_loop, "HOME", workspace), patch.object(
                apply_loop, "BASE", workspace
            ):
                self.assertEqual(
                    apply_loop.load_answer_bank(),
                    {"answers": {}, "banded_questions": {}, "gates": {}},
                )


class TestApplyLoopCLI(unittest.TestCase):
    def run_loop(self, workspace):
        return subprocess.run(
            [sys.executable, "-X", "utf8", str(ENGINES / "apply_loop.py")],
            env={**os.environ, "KEEL_HOME": workspace},
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=10,
        )

    def test_empty_workspace_has_one_line_error_without_traceback(self):
        with tempfile.TemporaryDirectory() as workspace:
            result = self.run_loop(workspace)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertEqual(len(result.stderr.splitlines()), 1)
        self.assertIn("standard-queue.json", result.stderr)
        self.assertIn("./setup.sh", result.stderr)
        self.assertIn("KEEL_HOME", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_initialized_empty_queue_still_succeeds(self):
        with tempfile.TemporaryDirectory() as workspace:
            data = Path(workspace) / "data"
            (data / "queues").mkdir(parents=True)
            queue = data / "queues" / "standard-queue.json"
            queue.write_text('{"entries": []}', encoding="utf-8")
            (data / "answer_bank.json").write_text("{}", encoding="utf-8")
            result = self.run_loop(workspace)
            self.assertEqual(json.loads(queue.read_text(encoding="utf-8")), {"entries": []})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(result.stdout, "No READY leads. Nothing to do.\n")


if __name__ == "__main__":
    unittest.main()
