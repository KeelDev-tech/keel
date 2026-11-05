"""Corrupt cache rows cannot authorize work or starve still-gated neighbors."""
import contextlib
import copy
import io
import json
import socket
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engines"))
import apply_loop as loop
import queue_io
import ready_gate


class BufferStateValidationTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="keel-buffer-state-")
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.queues = self.home / "data/queues"
        self.queues.mkdir(parents=True)
        self.buffer = self.home / "packets/buffer"
        self.buffer.mkdir(parents=True)
        self.resume = self.home / "resume.txt"
        self.resume.write_text("Synthetic material")
        self.bank = {"answers": {}, "gates": {}, "banded_questions": {}}
        self.row = {"role_id": "tail", "company": "Synthetic", "title": "Role",
                    "status": "READY", "fit_score": 80, "action_band": "APPLY",
                    "ats_url": "https://jobs.lever.co/synthetic/tail",
                    "materials": {"resume": str(self.resume)}}
        for name in ("standard", "strategic", "needs_input", "rejected"):
            (self.queues / (name + "-queue.json")).write_text(
                json.dumps([self.row] if name == "standard" else []))
        (self.home / "data/application-ledger.json").write_text("[]")
        (self.home / "data/employer-blocklist.md").write_text("# Blocked employers\n")
        self.packet = {**{k: self.row[k] for k in ("role_id", "company", "title", "ats_url")},
                       "brief": "Synthetic preparation", "scope": "preparation_only",
                       "execution_authorized": False, "upload_files": [str(self.resume)]}
        ready_gate.seal_packet(self.packet, self.row, self.bank, workspace=self.home)
        self.path = self.buffer / "tail.json"
        self.path.write_text(json.dumps(self.packet))
        self.now = datetime.now(timezone.utc)
        self.valid = {"role_id": "tail", "packet_path": str(self.path),
                      "built_at": self.now.isoformat(), "origin": "standard",
                      "launch_task_id": "synthetic-owner"}
        self.cache = self.home / "buffer-state.json"
        self.cache.write_text("[]")
        self.visits, self.saved, self.closed = [], [], []
        def deny(entry, origin):
            self.visits.append((entry["role_id"], origin))
            return False, "synthetic eligibility hold"
        settings = {"HOME": str(self.home), "DATA": str(self.home / "data"),
                    "QUEUE": str(self.queues / "standard-queue.json"),
                    "SQUEUE": str(self.queues / "strategic-queue.json"),
                    "LEDGER": str(self.home / "data/application-ledger.json"),
                    "BLOCKLIST": str(self.home / "data/employer-blocklist.md"),
                    "PACKETS": str(self.home / "packets"), "BUFFER_DIR": str(self.buffer),
                    "BUFFER_STATE": str(self.cache),
                    "BUFFER_WATERMARK": str(self.home / "watermark.json"),
                    "load_answer_bank": lambda: self.bank, "eligible": deny,
                    "save_buffer_state": lambda rows: self.saved.append(copy.deepcopy(rows)),
                    "_buffer_lock": lambda: SimpleNamespace(close=lambda: self.closed.append(True))}
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        for key, value in settings.items():
            stack.enter_context(patch.object(loop, key, value))
        stack.enter_context(patch.object(ready_gate, "HOME", str(self.home)))
        stack.enter_context(patch.object(queue_io, "_LOCK_PATH", str(self.home / "queue.lock")))
        self.release = stack.enter_context(patch.object(loop, "_release_launch_lock"))
        self.build = stack.enter_context(patch.object(loop, "build_packet", side_effect=AssertionError("denied row")))
        self.acquire = stack.enter_context(patch.object(loop, "_launch_guard", side_effect=AssertionError("denied row")))
        stack.enter_context(patch.object(loop.log_event, "log", return_value=None))
        stack.enter_context(patch.object(socket.socket, "connect", side_effect=AssertionError("network forbidden")))
        self.before = {p: p.read_bytes() for p in self.queues.iterdir()}
        self.packet_bytes = self.path.read_bytes()

    def bad_rows(self):
        yield from (None, 7, False, [], "bad")
        for field in ("role_id", "packet_path"):
            for value in (None, [], {}, True, 7, "", "   "):
                yield {**self.valid, field: value}
        for field in ("launch_task_id", "origin"):
            for value in ([], {}, True, 7):
                yield {**self.valid, field: value}

    def refresh(self):
        with contextlib.redirect_stdout(io.StringIO()):
            return loop.refresh_buffer(now=self.now)

    def assert_safe(self):
        self.assertEqual(self.before, {p: p.read_bytes() for p in self.queues.iterdir()})
        self.assertEqual(self.path.read_bytes(), self.packet_bytes)
        self.assertFalse(ready_gate.packet_admission(
            self.packet, self.row, self.bank, workspace=self.home)["allowed"])
        self.build.assert_not_called()
        self.acquire.assert_not_called()
        self.release.assert_not_called()

    def test_disk_cache_and_direct_count_preserve_valid_neighbors(self):
        for bad in self.bad_rows():
            for rows in ([bad, self.valid], [self.valid, bad]):
                with self.subTest(bad=bad, first=rows[0] is bad):
                    self.cache.write_text(json.dumps(rows))
                    before = copy.deepcopy(rows)
                    self.assertEqual(loop._buffer_fresh_count(rows, self.now), 1)
                    self.assertEqual(rows, before)
                    self.assertEqual(loop.load_buffer_state(), [self.valid])
                    self.assertEqual(loop._buffered_fresh_packet("tail", self.now), str(self.path))
                    self.assertEqual(loop._buffered_launch_task_id("tail"), "synthetic-owner")
                    self.assertEqual(self.refresh(), 1)
                    self.assertEqual(self.saved[-1], [self.valid])
                    self.assert_safe()

    def test_corrupt_rows_do_not_starve_denied_tail_or_infer_ownership(self):
        for bad in self.bad_rows():
            with self.subTest(bad=bad):
                self.visits.clear()
                self.cache.write_text(json.dumps([bad]))
                self.assertEqual(self.refresh(), 0)
                self.assertEqual(self.visits, [("tail", "standard")])
                self.assertEqual(self.saved[-1], [])
                self.assertTrue(self.closed[-1])
                self.assert_safe()

    def test_unreadable_encoding_and_existing_corruption_recover(self):
        for raw in (b"\xff", b"{", b"null", b"{}"):
            with self.subTest(raw=raw):
                self.visits.clear()
                self.cache.write_bytes(raw)
                self.assertEqual(loop.load_buffer_state(), [])
                self.assertEqual(self.refresh(), 0)
                self.assertEqual(self.visits, [("tail", "standard")])
                self.assert_safe()

    def test_pending_scan_stays_bounded_with_corrupt_cache(self):
        rows = [self.row, {**self.row, "role_id": "second", "fit_score": 79}]
        (self.queues / "standard-queue.json").write_text(json.dumps(rows))
        self.before = {p: p.read_bytes() for p in self.queues.iterdir()}
        self.cache.write_text("[null]")
        with patch.dict("os.environ", {"KEEL_BUFFER_SCAN_LIMIT": "1"}):
            self.assertEqual(self.refresh(), 0)
            self.assertEqual(self.visits, [("tail", "standard")])
            self.assertEqual(len(loop._load_buffer_watermark()["scan_pending"]), 1)
            self.assertEqual(self.refresh(), 0)
        self.assertEqual(self.visits, [("tail", "standard"), ("second", "standard")])
        self.assert_safe()

    def test_malformed_top_levels_and_descriptor_paths_never_reach_stat(self):
        for state in (None, False, 7, "bad", self.valid):
            with self.subTest(state=state):
                self.assertEqual(loop._buffer_fresh_count(state, self.now, [], self.bank), 0)
        for value in (False, True, 0, 1, 2, 7):
            bad = {**self.valid, "packet_path": value}
            self.cache.write_text(json.dumps([bad]))
            with self.subTest(path=value), patch.object(loop.os.path, "isfile") as stat:
                self.assertEqual(loop._buffer_fresh_count([bad], self.now, [], self.bank), 0)
                self.assertIsNone(loop._buffered_fresh_packet("tail", self.now))
                stat.assert_not_called()
            self.assertEqual(self.refresh(), 0)
            self.assert_safe()

    def test_legacy_timestamp_and_optional_owner_metadata_are_preserved(self):
        legacy = {key: value for key, value in self.valid.items()
                  if key not in ("launch_task_id", "origin")}
        legacy["built_at"] = self.now.replace(tzinfo=None).isoformat()
        self.cache.write_text(json.dumps([legacy]))
        self.assertEqual(loop.load_buffer_state(), [legacy])
        self.assertEqual(loop._buffer_fresh_count([legacy], self.now), 1)
        self.assertEqual(loop._buffered_launch_task_id("tail"), "")
        self.assert_safe()

    def test_holds_duplicate_ids_and_expiry_still_refuse_freshness(self):
        for row in ({**self.row, "human_hold": True}, self.row):
            tagged = [(row, "standard")]
            if not row.get("human_hold"):
                tagged.append((row, "strategic"))
            self.assertEqual(loop._buffer_fresh_count([None, self.valid], self.now, tagged, self.bank), 0)
        stale = {**self.valid, "built_at": (self.now - timedelta(days=2)).isoformat()}
        self.assertEqual(loop._buffer_fresh_count([None, stale], self.now), 0)
        self.assert_safe()

    def test_valid_stale_packet_retains_existing_owner_cleanup(self):
        stale = {**self.valid, "built_at": (self.now - timedelta(days=2)).isoformat()}
        self.cache.write_text(json.dumps([None, stale]))
        self.assertEqual(self.refresh(), 0)
        self.release.assert_called_once_with("tail", "synthetic-owner")
        self.assertFalse(self.path.exists())
        self.assertEqual((self.home / "packets/archive/tail.json").read_bytes(), self.packet_bytes)
        self.assertEqual(self.before, {p: p.read_bytes() for p in self.queues.iterdir()})


if __name__ == "__main__":
    unittest.main()
