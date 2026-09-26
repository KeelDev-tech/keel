from copy import deepcopy
import math

import pytest

from keel_machine.common import MachineError
from keel_machine.contracts import check_contract, validate_contract


def contract():
    return {"type":"object","properties":{
        "id":{"type":"string","minLength":1,"maxLength":20},
        "rows":{"type":"array","maxItems":3,"items":{"type":"object","properties":{
            "value":{"type":"number","minimum":0,"maximum":1},
            "count":{"type":"integer","minimum":0,"maximum":10},
            "valid":{"type":"boolean"}},"required":["value","count","valid"],"additionalProperties":False}},
        "status":{"type":"string","enum":["ok","hold"]}},
        "required":["id","rows"],"additionalProperties":False}


def value():return {"id":"safe","rows":[{"value":.5,"count":1,"valid":True}]}


def test_normalizes_limits_without_mutating_schema():
    spec={"type":"array","items":{"type":"string"}}
    frozen=validate_contract(spec)
    assert frozen["maxItems"]==256 and frozen["items"]["maxLength"]==4096
    assert "maxItems" not in spec
    assert validate_contract(frozen)==frozen
    assert check_contract(contract(),value())=={"status":"PASS","violations":[]}


@pytest.mark.parametrize("mutate,code",[
    (lambda v:v.update(secret="password"),"UNDECLARED_PROPERTY"),
    (lambda v:v.pop("id"),"REQUIRED_PROPERTY_MISSING"),
    (lambda v:v.update(id=""),"STRING_LENGTH_OUT_OF_BOUNDS"),
    (lambda v:v.update(rows=[v["rows"][0]]*4),"ITEM_COUNT_OUT_OF_BOUNDS"),
    (lambda v:v["rows"][0].update(count=True),"TYPE_MISMATCH"),
    (lambda v:v["rows"][0].update(valid=1),"TYPE_MISMATCH"),
    (lambda v:v["rows"][0].update(value=-.01),"NUMBER_OUT_OF_BOUNDS"),
    (lambda v:v.update(status="user-secret-token"),"ENUM_MISMATCH"),
])
def test_violations_are_bounded_and_content_free(mutate,code):
    item=value();mutate(item)
    report=check_contract(contract(),item)
    assert report["status"]=="HOLD" and code in report["violations"]
    assert "secret" not in str(report) and "password" not in str(report)


@pytest.mark.parametrize("spec",[
    {"type":"object","properties":{},"required":[]},
    {"type":"object","properties":{},"required":[],"additionalProperties":True},
    {"type":"object","properties":{},"required":["unknown"],"additionalProperties":False},
    {"type":"string","pattern":".*"},
    {"type":"string","$ref":"https://evil.invalid"},
    {"type":["string","null"]},
    {"type":"array"},
    {"type":"array","items":{"type":"string"},"maxItems":10001},
    {"type":"string","maxLength":True},
    {"type":"number","minimum":float("nan")},
    {"type":"integer","minimum":.1,"maximum":.9},
    {"type":"integer","enum":[True]},
    {"type":"string","enum":["a","a"]},
    {"type":"string","maxLength":1,"enum":["long"]},
])
def test_malformed_or_executable_contracts_hold(spec):
    with pytest.raises(MachineError):validate_contract(spec)
    assert check_contract(spec,{})=={"status":"HOLD","violations":["INVALID_CONTRACT"]}


@pytest.mark.parametrize("item",[float("inf"),float("nan"),{1:"non-string"},set(),2**10000,"\ud800"])
def test_non_json_and_unbounded_values_hold(item):
    assert check_contract({"type":"number"},item)["status"]=="HOLD"


def test_cycles_and_structure_bombs_are_bounded():
    cycle=[];cycle.append(cycle)
    assert check_contract({"type":"array","items":{"type":"null"}},cycle)["status"]=="HOLD"
    schema={"type":"null"}
    for _ in range(30):schema={"type":"array","items":schema}
    assert check_contract(schema,[])=={"status":"HOLD","violations":["INVALID_CONTRACT"]}
    assert check_contract({"type":"string","maxLength":65536},"x"*262145)["status"]=="HOLD"


def test_optional_null_and_empty_closed_objects():
    empty={"type":"object","properties":{},"required":[],"additionalProperties":False}
    assert check_contract(empty,{})["status"]=="PASS"
    assert check_contract({"type":"null"},None)["status"]=="PASS"
    assert check_contract({"type":"null"},False)["status"]=="HOLD"


def test_number_enum_does_not_admit_bool():
    assert check_contract({"type":"number","enum":[1]},True)["status"]=="HOLD"


def test_wide_container_rejected_before_expanding_traversal():
    schema={"type":"array","maxItems":10000,"items":{"type":"null"}}
    assert check_contract(schema,[None]*20001)=={"status":"HOLD","violations":["VALUE_NOT_BOUNDED_JSON"]}
