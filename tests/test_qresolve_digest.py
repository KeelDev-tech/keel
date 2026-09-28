"""Digest integration stays read-only by default and preserves resolver evidence."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

from engines import input_tray_digest as digest
from engines import queue_io


REPO = Path(__file__).resolve().parents[1]
STAMP = "2026-09-27T12:00:00+00:00"


def lead(role_id="fixture-1", *, fit=60, question="What is your preferred work location?"):
    return {"role_id": role_id, "fit_score": fit, "status": "PARKED-NEEDS-INPUT",
            "employer": "ExampleCo", "title": "Operations", "unresolved": [question]}


class DigestIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.qdir = self.home / "data" / "queues"
        self.qdir.mkdir(parents=True)
        self.hdir = self.home / "hidden_files"
        values = {"BASE": str(self.home), "QDIR": str(self.qdir), "HDIR": str(self.hdir),
                  "BANK": str(self.home / "data" / "answer_bank.json"),
                  "WM": str(self.hdir / "input-tray-logged.json"),
                  "TRAY_JSON": str(self.hdir / "input-tray.json"),
                  "FAM_HIST": str(self.hdir / "input-tray-families.json")}
        self.patch = mock.patch.multiple(digest, **values)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        previous = queue_io.get_lock_path()
        queue_io.set_lock_path(str(self.hdir / "queue.lock"))
        self.addCleanup(queue_io.set_lock_path, previous)
        self.env = mock.patch.dict(os.environ, {"KEEL_HOME": str(self.home),
                                               "KEEL_TRAY_MIN_FIT": "60"})
        self.env.start()
        self.addCleanup(self.env.stop)
        Path(digest.BANK).write_text('{"answers": {}}')
        self.write_leads([lead()])

    def write_leads(self, rows):
        (self.qdir / "needs_input-queue.json").write_text(json.dumps(rows))
        (self.qdir / "standard-queue.json").write_text("[]")

    def snapshot(self):
        return {str(p.relative_to(self.home)): (p.stat().st_mtime_ns,
                  hashlib.sha256(p.read_bytes()).hexdigest())
                for p in self.home.rglob("*") if p.is_file()}

    def cli(self, *args):
        env = dict(os.environ, KEEL_HOME=str(self.home), PYTHONDONTWRITEBYTECODE="1")
        return subprocess.run([sys.executable, "-B", str(REPO / "engines" / "input_tray_digest.py"),
                               *args], env=env, cwd=REPO, text=True,
                              capture_output=True, timeout=15, check=False)

    def test_fresh_cli_uses_configured_paths_and_default_is_read_only(self):
        before = self.snapshot()
        result = self.cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("preferred work location", result.stdout)
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(self.hdir.exists())
        delivered = self.cli("--deliver")
        self.assertEqual(delivered.returncode, 0, delivered.stderr)
        tray = json.loads(Path(digest.TRAY_JSON).read_text())
        self.assertEqual(len(tray["cards"]), 1)
        self.assertEqual(tray["cards"][0]["leads"][0]["role_id"], "fixture-1")
        self.assertTrue(Path(digest.WM).is_file())
        self.assertTrue(Path(digest.FAM_HIST).is_file())
        delivered_before = self.snapshot()
        self.assertEqual(self.cli().returncode, 0)
        self.assertEqual(self.snapshot(), delivered_before)

    def test_minimum_fit_defaults_to_sixty_and_can_be_overridden(self):
        self.write_leads([lead("below", fit=59), lead("at", fit=60),
                          lead("above", fit=75), lead("invalid", fit="nan")])
        self.assertEqual(next(iter(digest.collect_cards().values()))["unblock_leads"], 2)
        self.assertEqual(next(iter(digest.collect_cards(min_fit=75).values()))["unblock_leads"], 1)
        with mock.patch.dict(os.environ, {"KEEL_TRAY_MIN_FIT": "75"}):
            self.assertEqual(next(iter(digest.collect_cards().values()))["unblock_leads"], 1)
        for value in (float("nan"), float("inf"), -1, 101, "bad", True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                digest.collect_cards(min_fit=value)
        before = self.snapshot()
        self.assertEqual(self.cli("--min-fit", "nan").returncode, 2)
        self.assertEqual(self.snapshot(), before)

    def test_full_question_survives_display_truncation(self):
        question = ("Which work location do you prefer? " + "Supporting context " * 20
                    + "Only consider the role if there are no certification requirements.")
        self.write_leads([lead(question=question)])
        cards = digest.collect_cards()
        card = next(iter(cards.values()))
        self.assertEqual(card["norm"], question)
        self.assertLess(len(card["question"]), len(question))
        payload = digest.write_tray_json(cards, set(), {}, {}, STAMP, persist=False)
        self.assertEqual(payload["cards"][0]["norm"], question)
        changed = question.replace("no certification", "certification")
        self.assertNotEqual(digest.card_key_for(None, digest.normalize_question(changed)), card["key"])

    def test_every_system_blocker_drops_even_safe_bank_draft(self):
        for question in ("Application route is apply-by-email", "queue write failed",
                         "ambiguous verify_retry transport unavailable"):
            with self.subTest(question=question):
                self.write_leads([lead(question=question)])
                cards = digest.collect_cards()
                card = next(iter(cards.values()))
                card["draft"] = {"bank_key": "work_location_general", "value": "Example City"}
                payload = digest.write_tray_json(cards, set(), {}, {}, STAMP, persist=False)
                result = payload["cards"][0]
                self.assertEqual(result["status"], "SYSTEM-BLOCKED")
                self.assertIsNone(result["draft"])

    def test_proposal_decoration_survives_repeated_deliveries(self):
        calls = []
        def decorate(cards):
            calls.append(len(cards))
            cards[0]["draft"] = {"owner": "qresolve", "bank_key": "location_general",
                                  "value": "Example City", "evidence": [{"pointer": "/answers/location_general"}]}
            return cards
        fake = types.SimpleNamespace(decorate_cards=decorate)
        with mock.patch.dict(sys.modules, {"engines.qresolve": fake}):
            for _ in range(2):
                with mock.patch("builtins.print"):
                    self.assertEqual(digest.main(["--deliver"]), 0)
                card = json.loads(Path(digest.TRAY_JSON).read_text())["cards"][0]
                self.assertEqual(card["draft"]["owner"], "qresolve")
                self.assertEqual(card["draft"]["value"], "Example City")
                self.assertEqual(card["status"], "NEEDS-YOU")
        self.assertEqual(calls, [1, 1])

    def test_failed_decorator_removes_only_owned_drafts(self):
        def broken(cards):
            cards[1]["draft"] = None
            raise ValueError("changed corpus")
        cards = [{"key": "a", "status": "NEEDS-YOU", "draft": {"owner": "qresolve", "value": "x"}},
                 {"key": "b", "status": "NEEDS-YOU", "draft": {"bank_key": "human", "value": "y"}}]
        with mock.patch.dict(sys.modules, {"engines.qresolve": types.SimpleNamespace(decorate_cards=broken)}):
            result = digest._decorate_cards(cards)
        self.assertIsNone(result[0]["draft"])
        self.assertEqual(result[1]["draft"], cards[1]["draft"])

    def test_decorator_cannot_turn_system_state_into_answer(self):
        cards = [{"key": "a", "status": "SYSTEM-BLOCKED", "draft": None}]
        def broken(cards):
            cards[0].update(status="NEEDS-YOU", draft={"value": "Yes"})
            return cards
        with mock.patch.dict(sys.modules, {"engines.qresolve": types.SimpleNamespace(decorate_cards=broken)}):
            result = digest._decorate_cards(cards)
        self.assertEqual(result[0]["status"], "SYSTEM-BLOCKED")
        self.assertIsNone(result[0]["draft"])

    def test_backups_exist_before_atomic_replace_with_lock_held(self):
        self.hdir.mkdir()
        original = b'{"old":true}\n'
        Path(digest.TRAY_JSON).write_bytes(original)
        real_atomic = queue_io.atomic_write_json
        def checked_write(path, payload):
            self.assertGreater(getattr(queue_io._state, "depth", 0), 0)
            backups = list(self.hdir.glob("_backup-input-tray.json-*"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_bytes(), original)
            return real_atomic(path, payload)
        with mock.patch.object(queue_io, "atomic_write_json", side_effect=checked_write):
            digest.write_tray_json(digest.collect_cards(), set(), {}, {}, STAMP)
        self.assertEqual(json.loads(Path(digest.TRAY_JSON).read_text())["generated_at"], STAMP)

    def test_fifo_queue_is_rejected_without_waiting_or_writing(self):
        queue = self.qdir / "needs_input-queue.json"
        queue.unlink()
        os.mkfifo(queue)
        result = self.cli()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unsafe_or_oversized_tray_source", result.stderr)
        self.assertFalse(self.hdir.exists())

    def test_fifo_previous_tray_cannot_block_backup_under_lock(self):
        self.hdir.mkdir()
        os.mkfifo(digest.TRAY_JSON)
        result = self.cli("--deliver")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unsafe_or_oversized_tray_source", result.stderr)
        self.assertFalse(Path(digest.WM).exists())
        self.assertFalse(list(self.hdir.glob("_backup-input-tray.json-*")))

    def test_unsafe_or_malformed_sources_never_become_empty_state(self):
        path = self.qdir / "needs_input-queue.json"
        for raw in ('{bad', '{}', '[NaN]', '{"a":1,"a":2}'):
            with self.subTest(raw=raw):
                path.write_text(raw)
                with self.assertRaises(ValueError):
                    digest.collect_cards()
        path.unlink()
        target = self.home / "elsewhere.json"
        target.write_text("[]")
        path.symlink_to(target)
        with self.assertRaises(OSError):
            digest.collect_cards()
        path.unlink()
        path.write_text("[]")
        with self.assertRaises(ValueError):
            digest._read_bytes(path, limit=1)

    def test_nonpersistent_helpers_never_create_hidden_directory(self):
        cards = digest.collect_cards()
        before = self.snapshot()
        hist = digest.update_family_history(cards, set(), STAMP, False, persist=False)
        digest.write_tray_json(cards, set(), hist, {}, STAMP, persist=False)
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(self.hdir.exists())


if __name__ == "__main__":
    unittest.main()
