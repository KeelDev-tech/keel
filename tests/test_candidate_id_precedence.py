"""Regression tests for PR #21 review thread 1: candidate-ID precedence.

Contract under test (export_flow_snapshot.py):
  1. KEEL_CANDIDATE_ID env var wins over everything.
  2. hidden_files/candidate-id.json (private host, gitignored, mode 0600)
     wins over the synthetic default.
  3. The synthetic default "demo-candidate" is used when neither is set, so
     no real identifier lives in the public tree.
  4. A malformed or unreadable restricted file fails closed to the default.
  5. History stability: the candidate key feeds application_identity()
     digests, so the private host keeps the HISTORICAL key in the restricted
     file; with that key configured, freshly computed digests reproduce the
     historical ones (no orphaned attempt telemetry, no duplicate
     applications).

Run: python3 -m unittest discover -s tests
"""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

import export_flow_snapshot as efs
from keel_local.contracts import application_identity

HISTORICAL = "historical-key-fixture"


def _write_id_file(directory, payload):
    path = os.path.join(directory, "candidate-id.json")
    with open(path, "w") as f:
        if isinstance(payload, str):
            f.write(payload)
        else:
            json.dump(payload, f)
    return path


class TestCandidateIdPrecedence(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.patch_file = mock.patch.object(
            efs, "_CANDIDATE_ID_FILE",
            os.path.join(self.tmp.name, "candidate-id.json"),
        )
        self.patch_file.start()
        self.addCleanup(self.patch_file.stop)
        # Start each test with the env var absent; individual tests opt in.
        self.env = mock.patch.dict(os.environ)
        self.env.start()
        self.addCleanup(self.env.stop)
        os.environ.pop(efs._CANDIDATE_ID_ENV, None)

    def test_env_wins_over_file_and_default(self):
        _write_id_file(self.tmp.name, {"candidate_id": "file-key"})
        os.environ[efs._CANDIDATE_ID_ENV] = "env-key"
        self.assertEqual(efs._load_candidate_id(), "env-key")

    def test_env_value_is_stripped(self):
        os.environ[efs._CANDIDATE_ID_ENV] = "  env-key  "
        self.assertEqual(efs._load_candidate_id(), "env-key")

    def test_blank_env_falls_through_to_file(self):
        _write_id_file(self.tmp.name, {"candidate_id": "file-key"})
        os.environ[efs._CANDIDATE_ID_ENV] = "   "
        self.assertEqual(efs._load_candidate_id(), "file-key")

    def test_file_wins_over_default(self):
        _write_id_file(self.tmp.name, {"candidate_id": HISTORICAL})
        self.assertEqual(efs._load_candidate_id(), HISTORICAL)

    def test_default_when_nothing_configured(self):
        self.assertEqual(
            efs._load_candidate_id(), efs._EXAMPLE_CANDIDATE_ID)
        self.assertEqual(efs._EXAMPLE_CANDIDATE_ID, "demo-candidate")

    def test_malformed_file_fails_closed_to_default(self):
        _write_id_file(self.tmp.name, "{not valid json")
        self.assertEqual(
            efs._load_candidate_id(), efs._EXAMPLE_CANDIDATE_ID)

    def test_file_missing_key_fails_closed_to_default(self):
        _write_id_file(self.tmp.name, {"other": "value"})
        self.assertEqual(
            efs._load_candidate_id(), efs._EXAMPLE_CANDIDATE_ID)

    def test_file_blank_key_fails_closed_to_default(self):
        _write_id_file(self.tmp.name, {"candidate_id": "  "})
        self.assertEqual(
            efs._load_candidate_id(), efs._EXAMPLE_CANDIDATE_ID)

    def test_historical_key_reproduces_historical_digest(self):
        # The review thread's core risk: the key is digest input, so a
        # changed key orphans historical attempt telemetry. With the
        # historical key configured (as the private host keeps it in the
        # restricted file), digests recompute identically.
        _write_id_file(self.tmp.name, {"candidate_id": HISTORICAL})
        configured = efs._load_candidate_id()
        args = ("greenhouse", "employer-1", "posting-1")
        self.assertEqual(
            application_identity(configured, *args),
            application_identity(HISTORICAL, *args))

    def test_changed_key_changes_digest(self):
        # Guard for the invariant above: if the key ever stopped being
        # digest input, the stability test would pass vacuously.
        args = ("greenhouse", "employer-1", "posting-1")
        self.assertNotEqual(
            application_identity(HISTORICAL, *args),
            application_identity(efs._EXAMPLE_CANDIDATE_ID, *args))


if __name__ == "__main__":
    unittest.main()
