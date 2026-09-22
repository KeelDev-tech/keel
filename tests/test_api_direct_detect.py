"""Offline tests for api_direct_detect (no network — the Ashby/Lever/empty
paths return before any fetch)."""
import os
import sys
import unittest

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(BASE, "..", "engines"))

import api_direct_detect as add  # noqa: E402


class TestDetect(unittest.TestCase):
    def test_ashby_fail_closed(self):
        r = add.detect("https://jobs.ashbyhq.com/example-corp/12345678")
        self.assertFalse(r["candidate"])
        self.assertEqual(r["ats"], "ashby")
        self.assertIn("Ashby", r["reason"])

    def test_ashby_explicit_ats_param(self):
        r = add.detect("https://example.com/apply/123", ats="ashby")
        self.assertFalse(r["candidate"])
        self.assertEqual(r["ats"], "ashby")

    def test_empty_url_fail_closed(self):
        r = add.detect("")
        self.assertFalse(r["candidate"])

    def test_return_schema(self):
        r = add.detect("https://jobs.ashbyhq.com/example-corp/12345678")
        self.assertEqual(set(r), {"candidate", "ats", "reason"})

    def test_lever_still_recognized(self):
        r = add.detect("https://jobs.lever.co/example-corp/12345678")
        self.assertFalse(r["candidate"])
        self.assertEqual(r["ats"], "lever")


if __name__ == "__main__":
    unittest.main()
