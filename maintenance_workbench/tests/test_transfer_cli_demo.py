from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from tools.text_bundle import encode,decode,extract,BundleError,MAGIC,canonical as bundle_json,sha as bundle_sha
from keel_maint.__main__ import main
from keel_maint.contracts import strict_json,canonical
from keel_maint.demo import run_demo
from .common import sample

class TransferTests(unittest.TestCase):
    def test_byte_exact_roundtrip(self):
        files={"x.py":b"print('synthetic')\n","docs/a.txt":"évidence\n".encode()};raw=encode(files)
        self.assertEqual(decode(raw)[0],files)
        with tempfile.TemporaryDirectory() as t:
            p=Path(t)/"new";extract(raw,p)
            self.assertEqual({str(f.relative_to(p)):f.read_bytes() for f in p.rglob("*") if f.is_file()},files)
    def test_reproducible(self):self.assertEqual(encode({"b":b"b","a":b"a"}),encode({"a":b"a","b":b"b"}))
    def test_tamper_before_any_write(self):
        raw=encode({"x":b"secret-content"}).replace(b"secret-content",b"broken-content")
        with tempfile.TemporaryDirectory() as t:
            p=Path(t)/"out"
            with self.assertRaises(BundleError):extract(raw,p)
            self.assertFalse(p.exists())
    def test_destination_not_overwritten(self):
        with tempfile.TemporaryDirectory() as t:
            with self.assertRaises(FileExistsError):extract(encode({"x":b"x"}),t)
    def test_trailing_rejected(self):
        with self.assertRaises(BundleError):decode(encode({"x":b"x"})+b"extra")
    def test_traversal_rejected(self):
        with self.assertRaises(BundleError):encode({"../x":b"x"})
    def test_case_collision_rejected(self):
        with self.assertRaises(BundleError):encode({"A":b"x","a":b"x"})
    def test_parent_file_collision(self):
        with self.assertRaises(BundleError):encode({"a":b"x","a/b":b"x"})
    def test_content_cannot_escape_framing(self):
        data=b"\nEND\nFILE {}\nimport os\n";self.assertEqual(decode(encode({"x":data}))[0]["x"],data)
    def test_extract_never_executes(self):
        with tempfile.TemporaryDirectory() as t:
            mark=Path(t)/"mark";raw=encode({"malicious.py":f"open({str(mark)!r},'w').write('x')\n".encode()})
            extract(raw,Path(t)/"out");self.assertFalse(mark.exists())

class CliDemoTests(unittest.TestCase):
    def call(self,args):
        buf=io.StringIO()
        with redirect_stdout(buf):code=main(args)
        return code,strict_json(buf.getvalue())
    def test_doctor(self):
        code,result=self.call(["doctor"]);self.assertEqual(code,0);self.assertEqual(result["external_actions"],"NOT_SUPPORTED")
    def test_demo(self):
        with tempfile.TemporaryDirectory() as t:
            report=run_demo(Path(t)/"demo");self.assertEqual(report["status"],"DEMO_EXPECTATIONS_MET");self.assertFalse(report["target_source_executed"])
    def test_invalid_snapshot_cli_fails(self):
        with tempfile.TemporaryDirectory() as t:
            code,result=self.call(["verify","--snapshot",t]);self.assertEqual(code,2);self.assertEqual(result["status"],"ERROR")
    def test_capture_output_inside_source_rejected(self):
        with tempfile.TemporaryDirectory() as t:
            code,result=self.call(["capture","--source",t,"--config","missing","--out",str(Path(t)/"out")])
            self.assertEqual(code,2);self.assertIn("outside",result["message"])
    def test_recipe_output_new_only(self):
        s,r=sample()
        with tempfile.TemporaryDirectory() as t:
            root=Path(t);s.save(root/"snapshot");(root/"recipe.json").write_bytes(canonical(r));(root/"out.json").write_bytes(b"do not overwrite")
            code,result=self.call(["recipe","--snapshot",str(root/"snapshot"),"--recipe",str(root/"recipe.json"),"--out",str(root/"out.json")])
            self.assertEqual(code,2);self.assertEqual((root/"out.json").read_bytes(),b"do not overwrite")
