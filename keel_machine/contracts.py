"""Bounded, closed JSON contracts; data never supplies executable validation."""
import math

from .common import MachineError, require, canonical, clone

MAX_SCHEMA_DEPTH = 12
MAX_SCHEMA_NODES = 512
MAX_VALUE_DEPTH = 24
MAX_VALUE_NODES = 20000
MAX_VIOLATIONS = 32
_TYPES = {"object", "array", "string", "integer", "number", "boolean", "null"}
_KEYS = {
    "object": {"type", "properties", "required", "additionalProperties", "minProperties", "maxProperties"},
    "array": {"type", "items", "minItems", "maxItems"},
    "string": {"type", "minLength", "maxLength", "enum"},
    "integer": {"type", "minimum", "maximum", "enum"},
    "number": {"type", "minimum", "maximum", "enum"},
    "boolean": {"type", "enum"}, "null": {"type"},
}


def _kind(value, kind):
    if kind == "number":
        return type(value) in (int, float) and math.isfinite(value)
    return type(value) is {"object":dict, "array":list, "string":str,
        "integer":int, "boolean":bool, "null":type(None)}[kind]


def _bounded_json(value, *, max_depth=MAX_VALUE_DEPTH, max_nodes=MAX_VALUE_NODES):
    """Bound traversal before serialization; cycles cannot evade the depth cap."""
    stack, count = [(value, 0)], 0
    while stack:
        item, depth = stack.pop()
        count += 1
        require(count <= max_nodes and depth <= max_depth, "contract_structure_limit")
        require(type(item) in (dict, list, str, int, float, bool, type(None)), "contract_json_type")
        if type(item) is float:
            require(math.isfinite(item), "contract_nonfinite")
        if type(item) is int:
            require(item.bit_length() <= 1024, "contract_integer_limit")
        if type(item) is str:
            require(len(item) <= 262144, "contract_string_limit")
        if type(item) is dict:
            require(len(item) <= max_nodes, "contract_structure_limit")
            require(all(type(key) is str and len(key) <= 256 for key in item), "contract_key_invalid")
            stack.extend((child, depth + 1) for child in item.values())
        elif type(item) is list:
            require(len(item) <= max_nodes, "contract_structure_limit")
            stack.extend((child, depth + 1) for child in item)
    canonical(value, limit=262144)


def validate_contract(contract):
    """Validate and normalize a deliberately small JSON Schema-like subset.

    Not a general JSON Schema engine. Unknown keywords, refs, patterns, unions,
    implicit object properties, and permissive extra properties are rejected.
    """
    _bounded_json(contract)
    result = clone(contract)
    stack, count = [(result, 0)], 0
    while stack:
        spec, depth = stack.pop()
        count += 1
        require(count <= MAX_SCHEMA_NODES and depth <= MAX_SCHEMA_DEPTH, "contract_schema_limit")
        require(type(spec) is dict and type(spec.get("type")) is str and spec["type"] in _TYPES, "contract_type_required")
        kind = spec["type"]
        require(set(spec) <= _KEYS[kind], "contract_keyword_unsupported")
        if kind == "object":
            require(type(spec.get("properties")) is dict and len(spec["properties"]) <= 128, "contract_properties_required")
            require(type(spec.get("required")) is list and all(type(key) is str for key in spec["required"]), "contract_required_invalid")
            require(len(spec["required"]) == len(set(spec["required"])) and set(spec["required"]) <= set(spec["properties"]), "contract_required_invalid")
            require(spec.get("additionalProperties") is False, "contract_closed_object_required")
            low, high = spec.setdefault("minProperties", 0), spec.setdefault("maxProperties", len(spec["properties"]))
            require(type(low) is int and type(high) is int and 0 <= low <= high <= len(spec["properties"]), "contract_property_bounds")
            require(len(spec["required"]) <= high and low <= len(spec["properties"]), "contract_unsatisfiable")
            stack.extend((child, depth+1) for child in spec["properties"].values())
        elif kind == "array":
            require("items" in spec, "contract_items_required")
            low, high = spec.setdefault("minItems", 0), spec.setdefault("maxItems", 256)
            require(type(low) is int and type(high) is int and 0 <= low <= high <= 10000, "contract_item_bounds")
            stack.append((spec["items"], depth+1))
        elif kind == "string":
            low, high = spec.setdefault("minLength", 0), spec.setdefault("maxLength", 4096)
            require(type(low) is int and type(high) is int and 0 <= low <= high <= 65536, "contract_length_bounds")
        elif kind in ("number", "integer"):
            low, high = spec.setdefault("minimum", -1e15), spec.setdefault("maximum", 1e15)
            require(type(low) in (int,float) and type(high) in (int,float) and math.isfinite(low) and math.isfinite(high) and -1e15 <= low <= high <= 1e15, "contract_number_bounds")
            if kind == "integer":
                require(math.ceil(low) <= math.floor(high), "contract_unsatisfiable")
        if "enum" in spec:
            values = spec["enum"]
            require(type(values) is list and 1 <= len(values) <= 64, "contract_enum_invalid")
            require(all(_kind(v, kind) for v in values), "contract_enum_type")
            encoded = [canonical(v) for v in values]
            require(len(set(encoded)) == len(encoded), "contract_enum_duplicate")
            if kind == "string":
                require(all(spec["minLength"] <= len(v) <= spec["maxLength"] for v in values), "contract_enum_bounds")
            if kind in ("integer", "number"):
                require(all(spec["minimum"] <= v <= spec["maximum"] for v in values), "contract_enum_bounds")
    return result


def check_contract(contract, value):
    """Return content-free violations. Invalid contracts and values both HOLD."""
    try:
        spec = validate_contract(contract)
    except (MachineError, TypeError, ValueError, OverflowError, RecursionError):
        return {"status":"HOLD", "violations":["INVALID_CONTRACT"]}
    try:
        _bounded_json(value)
    except (MachineError, TypeError, ValueError, OverflowError, RecursionError):
        return {"status":"HOLD", "violations":["VALUE_NOT_BOUNDED_JSON"]}
    violations, stack = set(), [(spec, value)]
    while stack and len(violations) < MAX_VIOLATIONS:
        rule, item = stack.pop()
        kind = rule["type"]
        if not _kind(item, kind):
            violations.add("TYPE_MISMATCH")
            continue
        if "enum" in rule and canonical(item) not in {canonical(v) for v in rule["enum"]}:
            violations.add("ENUM_MISMATCH")
        if kind == "object":
            if set(item) - set(rule["properties"]):
                violations.add("UNDECLARED_PROPERTY")
            if set(rule["required"]) - set(item):
                violations.add("REQUIRED_PROPERTY_MISSING")
            if not rule["minProperties"] <= len(item) <= rule["maxProperties"]:
                violations.add("PROPERTY_COUNT_OUT_OF_BOUNDS")
            stack.extend((rule["properties"][key], child) for key, child in item.items() if key in rule["properties"])
        elif kind == "array":
            if not rule["minItems"] <= len(item) <= rule["maxItems"]:
                violations.add("ITEM_COUNT_OUT_OF_BOUNDS")
            stack.extend((rule["items"], child) for child in item)
        elif kind == "string" and not rule["minLength"] <= len(item) <= rule["maxLength"]:
            violations.add("STRING_LENGTH_OUT_OF_BOUNDS")
        elif kind in ("integer", "number") and not rule["minimum"] <= item <= rule["maximum"]:
            violations.add("NUMBER_OUT_OF_BOUNDS")
    return {"status":"HOLD" if violations else "PASS", "violations":sorted(violations)}
