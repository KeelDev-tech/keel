"""Bounded owner CLI input, including blocking special files."""
import os
from pathlib import Path
import tempfile
import unittest

from keel_machine.__main__ import _read_plan


class PlanFileTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'plan.json'

    def test_reads_valid_json(self):
        self.path.write_text('{"arguments":{"text":"local"}}')
        self.assertEqual(_read_plan(self.path),{'arguments':{'text':'local'}})

    def test_rejects_duplicate_nested_keys(self):
        self.path.write_text('{"arguments":{"text":"first","text":"second"}}')
        with self.assertRaises(ValueError):_read_plan(self.path)

    def test_rejects_nonfinite_numbers(self):
        for value in ('NaN','Infinity','-Infinity'):
            with self.subTest(value=value):
                self.path.write_text('{"value":'+value+'}')
                with self.assertRaises(ValueError):_read_plan(self.path)

    def test_rejects_oversized_file(self):
        self.path.write_text(' '*8193)
        with self.assertRaises(ValueError):_read_plan(self.path)

    def test_rejects_symlink(self):
        other=self.path.with_name('other.json');other.write_text('{}')
        self.path.symlink_to(other)
        with self.assertRaises(OSError):_read_plan(self.path)

    def test_rejects_fifo_without_waiting_for_writer(self):
        os.mkfifo(self.path,0o600)
        with self.assertRaises(ValueError):_read_plan(self.path)


if __name__=='__main__':unittest.main()
