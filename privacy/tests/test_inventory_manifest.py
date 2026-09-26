"""Regression: inventory hashing and release-manifest immutability."""

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.expanduser("~/workspace"))

from keel.privacy.inventory import (
    hash_bytes, hash_file, inventory_artifacts, ArtifactRecord,
    save_inventory, load_inventory,
)
from keel.privacy.release_manifest import (
    build_release, verify_manifest, save_manifest, load_manifest,
)


class TestInventory(unittest.TestCase):
    def test_hash_bytes_deterministic(self):
        self.assertEqual(hash_bytes(b"abc"), hash_bytes(b"abc"))
        self.assertNotEqual(hash_bytes(b"abc"), hash_bytes(b"abd"))
        self.assertEqual(len(hash_bytes(b"")), 64)

    def test_hash_file_matches_bytes(self):
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b"keel-privacy-test")
            path = f.name
        try:
            self.assertEqual(hash_file(path), hash_bytes(b"keel-privacy-test"))
        finally:
            os.unlink(path)

    def test_inventory_assigns_ids_and_digests(self):
        with tempfile.TemporaryDirectory() as d:
            p1 = Path(d) / "a.md"
            p2 = Path(d) / "b.py"
            p1.write_text("hello")
            p2.write_text("print(1)")
            records = inventory_artifacts([p1, p2])
            self.assertEqual([r.artifact_id for r in records],
                             ["artifact-001", "artifact-002"])
            self.assertEqual(records[0].sha256, hash_bytes(b"hello"))
            for r in records:
                self.assertEqual(r.external_egress, "DENY")
                self.assertEqual(r.classification, "UNCLASSIFIED")

    def test_inventory_missing_file_raises(self):
        with self.assertRaises(FileNotFoundError):
            ArtifactRecord.from_path("artifact-001", "/does/not/exist.bin")

    def test_inventory_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "x.txt"
            p.write_text("data")
            records = inventory_artifacts([p])
            inv_path = str(Path(d) / "inv.json")
            save_inventory(records, inv_path)
            loaded = load_inventory(inv_path)
            self.assertEqual(loaded[0]["sha256"], records[0].sha256)


class TestReleaseManifest(unittest.TestCase):
    def _records(self):
        return [
            ArtifactRecord(artifact_id="artifact-001", name="a.md",
                           sha256="a" * 64, size_bytes=3,
                           media_type="text/markdown"),
            ArtifactRecord(artifact_id="artifact-002", name="b.py",
                           sha256="b" * 64, size_bytes=8, media_type="text/x-python"),
        ]

    def test_build_release_binds_digests(self):
        m = build_release(self._records(), metadata={"purpose": "test"})
        self.assertTrue(m.release_id.startswith("PR-"))
        self.assertEqual(len(m.manifest_digest), 64)
        ok, reason = verify_manifest(m)
        self.assertTrue(ok, reason)

    def test_mutation_breaks_manifest(self):
        m = build_release(self._records())
        tampered = m.to_dict()
        tampered["artifacts"][0]["sha256"] = "c" * 64
        ok, reason = verify_manifest(tampered)
        self.assertFalse(ok)
        self.assertIn("mismatch", reason)

    def test_empty_release_refused(self):
        with self.assertRaises(ValueError):
            build_release([])

    def test_duplicate_digest_refused(self):
        recs = self._records()
        recs[1].sha256 = recs[0].sha256
        with self.assertRaises(ValueError):
            build_release(recs)

    def test_manifest_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            m = build_release(self._records())
            path = str(Path(d) / "manifest.json")
            save_manifest(m, path)
            loaded = load_manifest(path)
            ok, _ = verify_manifest(loaded)
            self.assertTrue(ok)
            self.assertEqual(loaded.release_id, m.release_id)


if __name__ == "__main__":
    unittest.main()
