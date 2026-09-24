"""Run the full Keel security Phase 1 regression suite.

Usage: python3 run_all.py  (from this directory, or via unittest discover)
"""
import os
import sys
import unittest

if __name__ == "__main__":
    here = os.path.dirname(os.path.abspath(__file__))
    # tests/ -> security/ -> keel/ on the path.
    sys.path.insert(0, os.path.dirname(os.path.dirname(here)))
    # Isolate every test from the production ledger / safe-mode flag.
    import tempfile
    tmp = tempfile.mkdtemp(prefix="keel-sec-test-")
    os.environ["KEEL_SECURITY_LEDGER"] = os.path.join(tmp, "ledger.jsonl")
    os.environ["KEEL_SAFE_MODE_FILE"] = os.path.join(tmp, "safe_mode")
    loader = unittest.TestLoader()
    suite = loader.discover(here, pattern="test_*.py")
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
