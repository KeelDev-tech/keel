"""KeelContract/1: a strict documented subset, NOT full JSON Schema."""
from __future__ import annotations
import math
from .contracts import require, canonical, digest

SUPPORTED={"type","properties","required","additionalProperties","items","enum","minLength","minimum","maximum"}
TYPES={"object","array","string","integer","number","boolean","null"}

def check_schema(schema: dict, depth: int=0) -> None:
    require(depth<=20,"schema depth limit")
    require(type(schema) is dict and "type" in schema and set(schema)<=SUPPORTED,"unsupported contract keyword")
    require(type(schema["type"]) is str and schema["type"] in TYPES,"unsupported contract type")
    kind=schema["type"]
    for field in ("properties","required","additionalProperties"):
        require(field not in schema or kind=="object","object keyword on another type")
    if kind=="object":
        props=schema.get("properties",{});req=schema.get("required",[])
        require(type(props) is dict and len(props)<=200 and all(type(k) is str for k in props),"invalid properties")
        require(type(req) is list and all(type(x) is str for x in req) and len(req)==len(set(req)) and set(req)<=set(props),"invalid required")
        require(type(schema.get("additionalProperties",False)) is bool,"additionalProperties must be boolean")
        for sub in props.values():check_schema(sub,depth+1)
    require("items" not in schema or kind=="array","items on non-array")
    if "items" in schema:check_schema(schema["items"],depth+1)
    if "minLength" in schema:
        require(kind=="string" and type(schema["minLength"]) is int and 0<=schema["minLength"]<=100000,"invalid minLength")
    for bound in ("minimum","maximum"):
        if bound in schema:
            require(kind in ("integer","number") and type(schema[bound]) in (int,float) and (type(schema[bound]) is int or math.isfinite(schema[bound])),"invalid numeric bound")
    require(not ("minimum" in schema and "maximum" in schema) or schema["minimum"]<=schema["maximum"],"reversed bounds")
    if "enum" in schema:
        e=schema["enum"]
        require(type(e) is list and 0<len(e)<=200 and len({canonical(x) for x in e})==len(e),"invalid enum")
    canonical(schema)


def validate(value, schema: dict) -> list[str]:
    check_schema(schema);canonical(value)
    errors=[]
    def walk(v,s,path,depth):
        if depth>40:errors.append(path+":depth");return
        kind=s["type"]
        matches={"object":type(v)is dict,"array":type(v)is list,"string":type(v)is str,
                 "integer":type(v)is int,"number":type(v)in(int,float),"boolean":type(v)is bool,"null":v is None}
        if not matches[kind]:errors.append(path+":type");return
        if "enum" in s and canonical(v) not in [canonical(x) for x in s["enum"]]:errors.append(path+":enum")
        if kind=="object":
            props=s.get("properties",{})
            for key in s.get("required",[]):
                if key not in v:errors.append(path+"."+key+":required")
            for key,child in v.items():
                if key in props:walk(child,props[key],path+"."+key,depth+1)
                elif not s.get("additionalProperties",False):errors.append(path+"."+key+":unexpected")
        elif kind=="array" and "items" in s:
            for i,child in enumerate(v):walk(child,s["items"],path+f"[{i}]",depth+1)
        elif kind=="string" and len(v)<s.get("minLength",0):errors.append(path+":minLength")
        elif kind in ("integer","number"):
            if "minimum" in s and v<s["minimum"]:errors.append(path+":minimum")
            if "maximum" in s and v>s["maximum"]:errors.append(path+":maximum")
    walk(value,schema,"$",0)
    return errors


def compare(before: dict, after: dict) -> dict:
    check_schema(before);check_schema(after)
    findings=[]
    def walk(a,b,path):
        if a["type"]!=b["type"]:findings.append({"path":path,"change":"type_changed"});return
        if "enum" in b and ("enum" not in a or not {canonical(x) for x in a["enum"]}<={canonical(x) for x in b["enum"]}):
            findings.append({"path":path,"change":"accepted_enum_narrowed"})
        for key,default in (("minLength",0),("minimum",float("-inf"))):
            if b.get(key,default)>a.get(key,default):findings.append({"path":path,"change":key+"_tightened"})
        if b.get("maximum",float("inf"))<a.get("maximum",float("inf")):
            findings.append({"path":path,"change":"maximum_tightened"})
        if a["type"]=="object":
            for key in sorted(set(b.get("required",[]))-set(a.get("required",[]))):
                findings.append({"path":path+"."+key,"change":"new_required_field"})
            ap=a.get("properties",{});bp=b.get("properties",{})
            if a.get("additionalProperties",False) and not b.get("additionalProperties",False):
                findings.append({"path":path,"change":"additional_properties_restricted"})
            for key in sorted(set(ap)-set(bp)):
                if not b.get("additionalProperties",False):findings.append({"path":path+"."+key,"change":"property_no_longer_accepted"})
            for key in sorted(set(ap)&set(bp)):walk(ap[key],bp[key],path+"."+key)
        if a["type"]=="array" and "items" in b:
            if "items" not in a:findings.append({"path":path,"change":"array_items_constrained"})
            else:walk(a["items"],b["items"],path+"[]")
    walk(before,after,"$")
    return {"status":"UNCHANGED" if canonical(before)==canonical(after) else "REVIEW_REQUIRED","findings":findings,
            "direction":"new_request_validator_accepting_old_inputs","complete_compatibility_proof":False,
            "before_sha256":digest(before),"after_sha256":digest(after)}
