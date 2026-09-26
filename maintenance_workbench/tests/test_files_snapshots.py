import os
from pathlib import Path
import tempfile
import unittest
from keel_maint.contracts import ContractError,strict_json,canonical
from keel_maint.safeio import relative,read_under,write_tree,read_file,MAX_FILE
from keel_maint.snapshot import capture,load,make_snapshot,changes
from .common import sample,copy_json

class FilesTests(unittest.TestCase):
    def test_traversal(self):
        for p in ("../x","a/../b","/tmp/x","a\\b","a//b","a/./b","a:"):
            with self.subTest(path=p),self.assertRaises(ContractError):relative(p)
    def test_new_tree_only(self):
        with tempfile.TemporaryDirectory() as t:
            p=Path(t)/"out";write_tree(p,{"a.txt":b"a"})
            with self.assertRaises(ContractError):write_tree(p,{"a.txt":b"b"})
            self.assertEqual((p/"a.txt").read_bytes(),b"a")
    def test_file_parent_collision(self):
        with tempfile.TemporaryDirectory() as t:
            with self.assertRaises(ContractError):write_tree(Path(t)/"out",{"a":b"x","a/b":b"y"})
            self.assertFalse((Path(t)/"out").exists())
    def test_symlink_file(self):
        with tempfile.TemporaryDirectory() as t:
            p=Path(t);(p/"real").write_bytes(b"secret");(p/"link").symlink_to(p/"real")
            with self.assertRaises(ContractError):read_under(p,"link")
    def test_symlink_parent(self):
        with tempfile.TemporaryDirectory() as t:
            p=Path(t);(p/"real").mkdir();(p/"real/x").write_bytes(b"x");(p/"alias").symlink_to(p/"real")
            with self.assertRaises(ContractError):read_under(p,"alias/x")
    def test_symlink_root(self):
        with tempfile.TemporaryDirectory() as t:
            p=Path(t);(p/"real").mkdir();(p/"real/x").write_bytes(b"x");(p/"alias").symlink_to(p/"real")
            with self.assertRaises(ContractError):read_under(p/"alias","x")
    def test_hardlink(self):
        with tempfile.TemporaryDirectory() as t:
            p=Path(t);(p/"a").write_bytes(b"a");os.link(p/"a",p/"b")
            with self.assertRaises(ContractError):read_under(p,"b")
    def test_fifo_does_not_block(self):
        with tempfile.TemporaryDirectory() as t:
            os.mkfifo(Path(t)/"pipe")
            with self.assertRaises(ContractError):read_under(t,"pipe")
    def test_byte_limit(self):
        with tempfile.TemporaryDirectory() as t:
            (Path(t)/"x").write_bytes(b"1234")
            with self.assertRaises(ContractError):read_under(t,"x",3)
    def test_private_permissions(self):
        with tempfile.TemporaryDirectory() as t:
            p=Path(t)/"out";write_tree(p,{"x":b"x"})
            self.assertEqual(p.stat().st_mode&0o777,0o700)
            self.assertEqual((p/"x").stat().st_mode&0o777,0o600)

class SnapshotTests(unittest.TestCase):
    def test_roundtrip(self):
        s,_=sample()
        with tempfile.TemporaryDirectory() as t:
            s.save(Path(t)/"s");r=load(Path(t)/"s")
            self.assertEqual(r.id,s.id);self.assertEqual(r.contents,s.contents)
    def test_tampered_object(self):
        s,_=sample()
        with tempfile.TemporaryDirectory() as t:
            p=Path(t)/"s";s.save(p);(p/"objects"/s.manifest["files"][0]["sha256"]).write_bytes(b"corrupt")
            with self.assertRaises(ContractError):load(p)
    def test_tampered_manifest(self):
        s,_=sample();s.manifest["config"]["workspace"]="changed"
        with self.assertRaises(ContractError):s.validate()
    def test_duplicate_allowlist(self):
        s,_=sample();c=copy_json(s.config);c["files"].append(c["files"][0])
        with self.assertRaises(ContractError):make_snapshot(c,s.contents)
    def test_bad_kind(self):
        s,_=sample();c=copy_json(s.config);c["files"][0]["kind"]={}
        with self.assertRaises(ContractError):make_snapshot(c,s.contents)
    def test_allowlist_ignores_other_files(self):
        s,_=sample()
        with tempfile.TemporaryDirectory() as t:
            root=Path(t)/"source";write_tree(root,s.contents);(root/"secret.txt").write_text("DO NOT INGEST")
            before={p:p.read_bytes() for p in root.rglob("*") if p.is_file()}
            observed=capture(root,s.config)
            self.assertNotIn("secret.txt",observed.contents)
            self.assertEqual(before,{p:p.read_bytes() for p in root.rglob("*") if p.is_file()})
    def test_missing_allowlisted_file_not_empty(self):
        s,_=sample()
        with tempfile.TemporaryDirectory() as t:
            with self.assertRaises(ContractError):capture(t,s.config)
    def test_workspace_diff_rejected(self):
        s,_=sample();c=copy_json(s.config);c["workspace"]="other";b=make_snapshot(c,s.contents)
        with self.assertRaises(ContractError):changes(s,b)
    def test_config_drift_requires_review(self):
        s,_=sample();c=copy_json(s.config);c["protected_paths"]=[];b=make_snapshot(c,s.contents)
        with self.assertRaises(ContractError):changes(s,b)
    def test_changed_path_exact(self):
        s,_=sample();d=dict(s.contents);d["helpers.py"]+=b"# change\n";b=make_snapshot(s.config,d)
        self.assertEqual(changes(s,b)["changed_paths"],["helpers.py"])
    def test_empty_snapshot_rejected(self):
        s,_=sample();c=copy_json(s.config);c["files"]=[]
        with self.assertRaises(ContractError):make_snapshot(c,{})
