import tempfile
from pathlib import Path
import unittest
from keel_next.qualification import qualify


class CrashQualificationTests(unittest.TestCase):
    def test_real_crashes(self):
        with tempfile.TemporaryDirectory() as parent:
            report = qualify(Path(parent) / 'qualification')
            self.assertEqual(report['status'], 'QUALIFICATION_PASSED', report)
            self.assertEqual(len(report['scenarios']), 4)
            self.assertTrue(all(all(row['checks'].values()) for row in report['scenarios'].values()))

    def test_existing_home_unchanged(self):
        with tempfile.TemporaryDirectory() as parent:
            marker = Path(parent) / 'keep'
            marker.write_text('original')
            with self.assertRaises(FileExistsError):
                qualify(parent)
            self.assertEqual(marker.read_text(), 'original')
