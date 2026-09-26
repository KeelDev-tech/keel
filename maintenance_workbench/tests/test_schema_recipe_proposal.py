import unittest
from unittest.mock import patch
from keel_maint.contracts import ContractError,canonical,sha
from keel_maint.schema import validate,compare,check_schema
from keel_maint.proposals import stage
from keel_maint.recipes import execute,validate_recipe
from keel_maint.snapshot import make_snapshot
from keel_maint.retrieval import AnalysisCache
from .common import sample,copy_json

class SchemaTests(unittest.TestCase):
    def schema(self):return {"type":"object","properties":{"n":{"type":"integer","minimum":0}},"required":["n"],"additionalProperties":False}
    def test_valid(self):self.assertEqual(validate({"n":1},self.schema()),[])
    def test_bool_integer(self):self.assertTrue(validate({"n":True},self.schema()))
    def test_missing(self):self.assertIn("$.n:required",validate({},self.schema()))
    def test_unknown_property(self):self.assertIn("$.extra:unexpected",validate({"n":1,"extra":1},self.schema()))
    def test_nonfinite(self):
        with self.assertRaises(ContractError):validate(float("nan"),{"type":"number"})
    def test_unsupported_ref(self):
        with self.assertRaises(ContractError):check_schema({"type":"object","$ref":"remote"})
    def test_enum_type_distinction(self):self.assertTrue(validate(1,{"type":"integer","enum":[True]}))
    def test_required_enum_change_detected(self):
        a=self.schema();b=copy_json(a);b["properties"]["x"]={"type":"string"};b["required"].append("x")
        self.assertIn("new_required_field",[x["change"] for x in compare(a,b)["findings"]])
    def test_bounds_change(self):
        a={"type":"number","minimum":0};b={"type":"number","minimum":2}
        self.assertEqual(compare(a,b)["findings"][0]["change"],"minimum_tightened")
    def test_unchanged_not_complete_proof(self):self.assertFalse(compare(self.schema(),self.schema())["complete_compatibility_proof"])
    def test_array_nested(self):self.assertEqual(validate([1,2],{"type":"array","items":{"type":"integer"}}),[])
    def test_unknown_nested_keyword(self):
        with self.assertRaises(ContractError):check_schema({"type":"array","items":{"type":"string","pattern":"x"}})

class ProposalTests(unittest.TestCase):
    def proposal(self,s,path="adapter-map.json"):
        return {"schema_version":1,"workspace":s.workspace,"base_snapshot":s.id,"reason":"synthetic change",
                "changes":[{"path":path,"expected_sha256":sha(s.contents[path]),"replacement":"{}\n"}]}
    def test_candidate_not_source(self):
        s,_=sample();before=dict(s.contents);candidate,report,diff=stage(s,self.proposal(s))
        self.assertEqual(s.contents,before);self.assertNotEqual(s.id,candidate.id)
        self.assertEqual(report["authorization"],"NONE");self.assertIn("adapter-map.json",diff)
    def test_stale_base(self):
        s,_=sample();p=self.proposal(s);p["base_snapshot"]="0"*64
        with self.assertRaises(ContractError):stage(s,p)
    def test_wrong_file_hash(self):
        s,_=sample();p=self.proposal(s);p["changes"][0]["expected_sha256"]="0"*64
        with self.assertRaises(ContractError):stage(s,p)
    def test_protected_fixture(self):
        s,_=sample()
        with self.assertRaises(ContractError):stage(s,self.proposal(s,"fixtures/input.json"))
    def test_protected_policy(self):
        s,_=sample()
        with self.assertRaises(ContractError):stage(s,self.proposal(s,"policies/fixture-policy.json"))
    def test_approve_flag_rejected(self):
        s,_=sample();p=self.proposal(s);p["approved"]=True
        with self.assertRaises(ContractError):stage(s,p)
    def test_repeated_change(self):
        s,_=sample();p=self.proposal(s);p["changes"]*=2
        with self.assertRaises(ContractError):stage(s,p)
    def test_noop_not_claimed_as_change(self):
        s,_=sample();p=self.proposal(s);p["changes"][0]["replacement"]=s.contents["adapter-map.json"].decode()
        with self.assertRaises(ContractError):stage(s,p)

class RecipeTests(unittest.TestCase):
    def test_real_recipe(self):
        s,r=sample();v=execute(s,r);self.assertEqual(v["status"],"LOCAL_CHECKS_PASSED");self.assertFalse(v["deployment_authorized"])
    def test_cache_repeat_equal(self):
        s,r=sample();cache=AnalysisCache();self.assertEqual(execute(s,r,cache),execute(s,r,cache))
    def test_no_shell_operation(self):
        s,r=sample();r["steps"][0]["operation"]="shell"
        with self.assertRaises(ContractError):execute(s,r)
    def test_unknown_args_fail(self):
        s,r=sample();r["steps"][0]["args"]={"command":"touch /tmp/bad"}
        self.assertEqual(execute(s,r)["status"],"LOCAL_CHECKS_NOT_PASSED")
    def test_dependency_cycle(self):
        s,r=sample();r["steps"][0]["needs"]=["contract"]
        with self.assertRaises(ContractError):execute(s,r)
    def test_no_checks_no_pass(self):
        s,r=sample();r["required_checks"]=[]
        with self.assertRaises(ContractError):execute(s,r)
    def test_informational_step_not_check(self):
        s,r=sample();r["required_checks"]=["context"]
        with self.assertRaises(ContractError):execute(s,r)
    def test_invalid_python_blocks_dependent(self):
        s,r=sample();d=dict(s.contents);d["helpers.py"]=b"def :\n";s=make_snapshot(s.config,d)
        v=execute(s,r);self.assertEqual(v["steps"]["contract"]["status"],"BLOCKED");self.assertEqual(v["status"],"LOCAL_CHECKS_NOT_PASSED")
    def test_missing_document_no_pass(self):
        s,r=sample();r["steps"][1]["args"]["input_path"]="missing.json"
        self.assertEqual(execute(s,r)["status"],"LOCAL_CHECKS_NOT_PASSED")
    def test_required_contract_failure_not_hidden(self):
        s,r=sample();d=dict(s.contents);d["adapter-map.json"]=b'{}';s=make_snapshot(s.config,d)
        self.assertEqual(execute(s,r)["status"],"LOCAL_CHECKS_NOT_PASSED")
