"""Synthetic-only security ledger regressions; no default ledger paths."""
import json
import os
from pathlib import Path
import select
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from security.ledger.events import SecurityLedger


def record(ledger, name="synthetic"):
    return ledger.record(agent_id="fixture-agent", action_name=name,
                         decision="ALLOW", reasons=["synthetic test"])


class SecurityLedgerRegressions(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="keel-ledger-regression-")
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "events.jsonl"
        self.env = patch.dict(os.environ, {"KEEL_SECURITY_LEDGER": str(self.path)})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_two_stale_instances_extend_one_chain(self):
        first = SecurityLedger(str(self.path))
        second = SecurityLedger(str(self.path))
        a = record(first, "a")
        b = record(second, "b")
        self.assertEqual(b["seq"], 1)
        self.assertEqual(b["prev_hash"], a["hash"])
        self.assertTrue(SecurityLedger(str(self.path)).verify()[0])

    def test_parallel_processes_preserve_every_event(self):
        script = """import sys
from security.ledger.events import SecurityLedger
l = SecurityLedger(sys.argv[1])
print('ready', flush=True)
sys.stdin.readline()
for i in range(10):
    l.record(agent_id=sys.argv[2], action_name=str(i), decision='ALLOW')
"""
        env = dict(os.environ, PYTHONPATH=str(ROOT), PYTHONDONTWRITEBYTECODE="1")
        children = []
        try:
            for number in range(4):
                child = subprocess.Popen(
                    [sys.executable, "-B", "-c", script, str(self.path), str(number)],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, text=True, env=env)
                children.append(child)
            for child in children:
                self.assertTrue(select.select([child.stdout], [], [], 10)[0],
                                "worker did not reach start barrier")
                self.assertEqual(child.stdout.readline().strip(), "ready")
            for child in children:
                child.stdin.write("go\n")
                child.stdin.flush()
            for child in children:
                stdout, stderr = child.communicate(timeout=15)
                self.assertEqual(child.returncode, 0, stderr)
            ledger = SecurityLedger(str(self.path))
            self.assertEqual(len(ledger.records()), 40)
            self.assertTrue(ledger.verify()[0])
            self.assertEqual({(r["body"]["agent_id"], r["body"]["action_name"])
                              for r in ledger.records()},
                             {(str(a), str(i)) for a in range(4) for i in range(10)})
        finally:
            for child in children:
                if child.poll() is None:
                    child.kill()
                child.communicate(timeout=5)

    def test_corrupt_chain_refuses_append_without_changing_file(self):
        ledger = SecurityLedger(str(self.path))
        record(ledger)
        row = json.loads(self.path.read_text())
        row["body"]["agent_id"] = "tampered"
        self.path.write_text(json.dumps(row) + "\n")
        before = self.path.read_bytes()
        fresh = SecurityLedger(str(self.path))
        self.assertFalse(fresh.verify()[0])
        with self.assertRaises(OSError):
            record(fresh)
        self.assertEqual(self.path.read_bytes(), before)

    def test_corruption_after_construction_is_rechecked(self):
        ledger = SecurityLedger(str(self.path))
        record(ledger)
        with self.path.open("a") as handle:
            handle.write('{"forged":true}\n')
        before = self.path.read_bytes()
        with self.assertRaises(OSError):
            record(ledger)
        self.assertEqual(self.path.read_bytes(), before)

    def test_malformed_rows_fail_closed(self):
        malformed = [b'[{"x":1}]\n', b'{"seq":', b'\xff\n',
                     b'{"seq":0,"seq":1}\n', b'{"value":NaN}\n', b'\n']
        for data in malformed:
            with self.subTest(data=data):
                self.path.write_bytes(data)
                ledger = SecurityLedger(str(self.path))
                self.assertFalse(ledger.verify()[0])
                with self.assertRaises(OSError):
                    record(ledger)
                self.assertEqual(self.path.read_bytes(), data)

    def test_unterminated_complete_row_is_not_silently_repaired(self):
        record(SecurityLedger(str(self.path)))
        self.path.write_bytes(self.path.read_bytes().rstrip(b"\n"))
        before = self.path.read_bytes()
        ledger = SecurityLedger(str(self.path))
        self.assertFalse(ledger.verify()[0])
        with self.assertRaises(OSError):
            record(ledger)
        self.assertEqual(self.path.read_bytes(), before)

    def test_verify_reloads_and_detects_same_count_tampering(self):
        ledger = SecurityLedger(str(self.path))
        record(ledger)
        row = json.loads(self.path.read_text())
        row["body"]["decision"] = "DENY"
        self.path.write_text(json.dumps(row) + "\n")
        self.assertFalse(ledger.verify()[0])

    def test_fsyncs_file_and_directory_before_return(self):
        ledger = SecurityLedger(str(self.path))
        kinds = []
        real_fsync = os.fsync
        def capture(fd):
            kinds.append(stat.S_IFMT(os.fstat(fd).st_mode))
            return real_fsync(fd)
        with patch("security.ledger.events.os.fsync", side_effect=capture):
            result = record(ledger)
        self.assertEqual(result["body"]["decision"], "ALLOW")
        self.assertIn(stat.S_IFREG, kinds)
        self.assertIn(stat.S_IFDIR, kinds)

    def test_fsync_error_never_returns_allow(self):
        ledger = SecurityLedger(str(self.path))
        with patch("security.ledger.events.os.fsync", side_effect=OSError("fixture failure")):
            with self.assertRaises(OSError):
                record(ledger)

    def test_short_writes_are_completed(self):
        ledger = SecurityLedger(str(self.path))
        real_write = os.write
        with patch("security.ledger.events.os.write",
                   side_effect=lambda fd, data: real_write(fd, data[:11])):
            record(ledger)
        self.assertTrue(SecurityLedger(str(self.path)).verify()[0])

    def test_partial_write_failure_is_preserved_and_blocks_future_append(self):
        ledger = SecurityLedger(str(self.path))
        real_write = os.write
        writes = 0
        def partial(fd, data):
            nonlocal writes
            writes += 1
            if writes == 1:
                return real_write(fd, data[:17])
            raise OSError("synthetic interrupted write")
        with patch("security.ledger.events.os.write", side_effect=partial):
            with self.assertRaises(OSError):
                record(ledger)
        before = self.path.read_bytes()
        self.assertEqual(len(before), 17)
        with self.assertRaises(OSError):
            record(SecurityLedger(str(self.path)))
        self.assertEqual(self.path.read_bytes(), before)

    def test_process_death_preserves_partial_tail_and_releases_lock(self):
        script = """import os, sys
from security.ledger.events import SecurityLedger
l = SecurityLedger(sys.argv[1])
real_write = os.write
def partial(fd, data):
    real_write(fd, data[:17])
    print('partial', flush=True)
    sys.stdin.readline()
    raise RuntimeError('test parent should kill before continuing')
os.write = partial
l.record(agent_id='fixture-agent', action_name='crash-test', decision='ALLOW')
"""
        env = dict(os.environ, PYTHONPATH=str(ROOT), PYTHONDONTWRITEBYTECODE="1")
        child = subprocess.Popen(
            [sys.executable, "-B", "-c", script, str(self.path)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, env=env)
        try:
            self.assertTrue(select.select([child.stdout], [], [], 10)[0],
                            "worker did not reach partial write")
            self.assertEqual(child.stdout.readline().strip(), "partial")
            child.kill()
            child.communicate(timeout=5)
            before = self.path.read_bytes()
            self.assertEqual(len(before), 17)
            # Constructor acquires the same lock after the killed owner.
            ledger = SecurityLedger(str(self.path))
            self.assertFalse(ledger.verify()[0])
            with self.assertRaises(OSError):
                record(ledger)
            self.assertEqual(self.path.read_bytes(), before)
        finally:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=5)

    def test_ledger_symlink_refused(self):
        target = Path(self.temp.name) / "other.jsonl"
        target.write_bytes(b"")
        self.path.symlink_to(target)
        with self.assertRaises(OSError):
            record(SecurityLedger(str(self.path)))
        self.assertEqual(target.read_bytes(), b"")

    def test_lock_symlink_refused(self):
        target = Path(self.temp.name) / "unrelated.txt"
        target.write_bytes(b"unchanged")
        Path(str(self.path) + ".lock").symlink_to(target)
        with self.assertRaises(OSError):
            record(SecurityLedger(str(self.path)))
        self.assertEqual(target.read_bytes(), b"unchanged")

    def test_hardlinked_ledger_refused(self):
        self.path.write_bytes(b"")
        target = Path(self.temp.name) / "alias.jsonl"
        os.link(self.path, target)
        with self.assertRaises(OSError):
            record(SecurityLedger(str(self.path)))
        self.assertEqual(target.read_bytes(), b"")

    def test_existing_valid_bytes_and_envelope_preserved(self):
        ledger = SecurityLedger(str(self.path))
        first = record(ledger)
        original = self.path.read_bytes()
        second = record(SecurityLedger(str(self.path)))
        self.assertTrue(self.path.read_bytes().startswith(original))
        self.assertEqual(set(second), set(first))
        self.assertEqual(second["prev_hash"], first["hash"])


if __name__ == "__main__":
    unittest.main()
