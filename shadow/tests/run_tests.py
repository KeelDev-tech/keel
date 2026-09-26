#!/usr/bin/env python3
"""Regression test runner for Workstream F (review + shadow).

Usage:
    python3 run_tests.py [-v]

Runs the full review/shadow regression suite via unittest discovery.
Exit code 0 = all green; nonzero = failures/errors.
"""

import sys
import unittest
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
KEEL_DIR = TESTS_DIR.parent.parent

sys.path.insert(0, str(KEEL_DIR))
sys.path.insert(0, str(TESTS_DIR))

if __name__ == "__main__":
    verbosity = 2 if "-v" in sys.argv else 1
    loader = unittest.TestLoader()
    suite = loader.discover(str(TESTS_DIR), pattern="test_*.py")
    runner = unittest.TextTestRunner(verbosity=verbosity)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
