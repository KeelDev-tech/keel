from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from keel_maint.contracts import ContractError
from keel_maint.analysis import inspect_python,inspect_snapshot,dependency_graph,impact
from keel_maint.retrieval import search,AnalysisCache
from keel_maint.snapshot import make_snapshot
from .common import sample,copy_json

class AnalysisTests(unittest.TestCase):
    def test_parse_valid(self):self.assertEqual(inspect_python(b"def f(): return 1\n")["status"],"PARSED")
    def test_parse_invalid(self):self.assertEqual(inspect_python(b"def :\n")["status"],"INVALID")
    def test_target_source_never_executes(self):
        with tempfile.TemporaryDirectory() as t:
            p=Path(t)/"sentinel"
            raw=f"open({str(p)!r}, 'w').write('bad')\nimport urllib.request\n".encode()
            self.assertEqual(inspect_python(raw)["status"],"PARSED");self.assertFalse(p.exists())
    def test_dynamic_code_label(self):self.assertTrue(inspect_python(b"__import__('os')\n")["dynamic_code"])
    def test_parse_budget_not_pass(self):
        s,_=sample();r=inspect_snapshot(s,maximum_seconds=0)
        self.assertEqual(r["status"],"FAIL");self.assertEqual(r["parsed"],0)
    def test_missing_parser_not_pass(self):
        with patch("keel_maint.analysis.subprocess.run",side_effect=OSError("unavailable")):
            self.assertEqual(inspect_python(b"x=1")["status"],"UNAVAILABLE")
    def test_declared_and_static_edges(self):
        s,_=sample();g=dependency_graph(s);i=impact(g,["contracts/request.json"])
        self.assertIn("engine.py",i["affected"])
        self.assertEqual(i["witness_paths"]["engine.py"],["contracts/request.json","adapter-map.json","engine.py"])
        self.assertIn({"dependency":"helpers.py","consumer":"engine.py","origin":"static_import"},g["edges"])
    def test_cycle_not_infinite_or_dag_claim(self):
        g={"nodes":["a","b","c"],"edges":[{"dependency":"a","consumer":"b"},{"dependency":"b","consumer":"a"},{"dependency":"b","consumer":"c"}],"coverage":"STATIC_AND_DECLARED_ONLY"}
        r=impact(g,["a"]);self.assertEqual(r["unorderable_nodes"],["a","b","c"])
    def test_unknown_changed_path_rejected(self):
        with self.assertRaises(ContractError):impact({"nodes":["a"],"edges":[],"coverage":"partial"},["b"])
    def test_relative_import(self):
        c={"schema_version":1,"workspace":"relative","files":[{"path":"p/a.py","kind":"python"},{"path":"p/b.py","kind":"python"}],"protected_paths":[],"dependencies":[]}
        s=make_snapshot(c,{"p/a.py":b"from .b import thing\n","p/b.py":b"thing=1\n"})
        g=dependency_graph(s);self.assertIn("p/a.py",impact(g,["p/b.py"])["affected"])

class RetrievalTests(unittest.TestCase):
    def call(self,**kwargs):
        s,_=sample();defaults={"workspace":s.workspace,"paths":["docs/adapter.md"],"query":"contract"};defaults.update(kwargs)
        return search(s,**defaults)
    def test_fts_real_backend(self):
        r=self.call(mode="fts5");self.assertEqual(r["backend"],"sqlite_fts5_bm25");self.assertTrue(r["results"])
    def test_explicit_fallback(self):
        r=self.call(mode="tokens");self.assertEqual(r["backend"],"tokens");self.assertTrue(r["results"])
    def test_namespace_mismatch(self):
        with self.assertRaises(ContractError):self.call(workspace="other")
    def test_empty_scope(self):
        with self.assertRaises(ContractError):self.call(paths=[])
    def test_scope_outside_snapshot(self):
        with self.assertRaises(ContractError):self.call(paths=["secret.txt"])
    def test_rank_does_not_expand_scope(self):
        r=self.call(query="request sample_id contract")
        self.assertEqual({v["path"] for v in r["results"]},{"docs/adapter.md"})
    def test_fts_operators_are_not_executed(self):
        r=self.call(query='" OR * NOT NEAR(contract) --')
        self.assertTrue(all(x["path"]=="docs/adapter.md" for x in r["results"]))
    def test_results_never_authority(self):
        r=self.call();self.assertEqual(r["authorization"],"NONE")
        self.assertTrue(all(v["authority"]=="NONE" and v["source_status"]=="UNTRUSTED_SOURCE_COPY" for v in r["results"]))
    def test_context_budget(self):
        base,_=sample();data=dict(base.contents);data["docs/adapter.md"]=("contract evidence "*50).encode()
        snap=make_snapshot(base.config,data)
        r=search(snap,workspace=snap.workspace,paths=["docs/adapter.md"],query="contract",max_chars=100)
        self.assertLessEqual(r["characters"],100);self.assertTrue(r["results"][0]["excerpt_truncated"])
    def test_cache_returns_copy(self):
        cache=AnalysisCache();cache.put("k",{"x":[1]});v=cache.get("k");v["x"].append(2)
        self.assertEqual(cache.get("k"),{"x":[1]})
    def test_cache_capacity(self):
        cache=AnalysisCache(1);cache.put("a",{});cache.put("b",{});self.assertIsNone(cache.get("a"))
