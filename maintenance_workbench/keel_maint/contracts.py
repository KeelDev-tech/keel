"""Strict bounded data contracts; not a general JSON Schema implementation."""
from __future__ import annotations
import hashlib
import json
import math
import re
from typing import Any

class ContractError(ValueError):
    """An input cannot safely be interpreted under this protocol."""

MAX_JSON = 8 * 1024 * 1024
MAX_DEPTH = 40

def require(condition: bool, message: str) -> None:
    if not condition:
        raise ContractError(message)

def canonical(value: Any) -> bytes:
    def check(v: Any, depth: int = 0) -> None:
        require(depth <= MAX_DEPTH, "nesting limit exceeded")
        if type(v) is dict:
            require(all(type(k) is str for k in v), "JSON object keys must be strings")
            for child in v.values(): check(child, depth + 1)
        elif type(v) is list:
            for child in v: check(child, depth + 1)
        elif type(v) in (int, float):
            require(type(v) is int or math.isfinite(v), "non-finite number")
        elif v is not None and type(v) not in (str, bool):
            raise ContractError("unsupported JSON value")
    try:
        check(value)
        raw = json.dumps(value, sort_keys=True, ensure_ascii=False,
                         separators=(",", ":"), allow_nan=False).encode("utf-8")
        require(len(raw) <= MAX_JSON, "JSON byte limit exceeded")
        return raw
    except (RecursionError, UnicodeError, ValueError, TypeError) as exc:
        if isinstance(exc, ContractError): raise
        raise ContractError("invalid canonical JSON") from exc

def strict_json(raw: bytes | str) -> Any:
    def pairs(values):
        result = {}
        for key, value in values:
            require(key not in result, "duplicate JSON key")
            result[key] = value
        return result
    def reject(_): raise ContractError("non-finite JSON number")
    try:
        require(type(raw) in (str, bytes), "JSON text required")
        require(len(raw.encode("utf-8") if type(raw) is str else raw) <= MAX_JSON,
                "JSON byte limit exceeded")
        value = json.loads(raw, object_pairs_hook=pairs, parse_constant=reject)
        canonical(value)
        return value
    except (UnicodeError, RecursionError, ValueError, TypeError) as exc:
        if isinstance(exc, ContractError): raise
        raise ContractError("invalid JSON") from exc

def sha(raw: bytes) -> str: return hashlib.sha256(raw).hexdigest()
def digest(value: Any) -> str: return sha(canonical(value))

def text(value: Any, name: str = "text", maximum: int = 4096) -> str:
    require(type(value) is str and 0 < len(value) <= maximum and bool(value.strip()),
            f"{name}: nonblank bounded string required")
    require(not any(ord(c) < 32 for c in value), f"{name}: control character")
    return value

def integer(value: Any, name: str = "integer", maximum: int = 10**9) -> int:
    require(type(value) is int and 0 <= value <= maximum, f"{name}: nonnegative integer required")
    return value

def keys(value: Any, required: set[str], optional: set[str] | None = None) -> dict:
    require(type(value) is dict, "object required")
    require(required <= value.keys(), "missing required fields: " + ",".join(sorted(required-value.keys())))
    require(value.keys() <= required | (optional or set()), "unknown object field")
    return value

def version(value: dict) -> None:
    require(type(value.get("schema_version")) is int and value["schema_version"] == 1,
            "unsupported schema_version")

def identifier(value: Any) -> str:
    require(type(value) is str and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", value) is not None,
            "invalid identifier")
    return value

def hexdigest(value: Any) -> str:
    require(type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value) is not None, "invalid SHA-256")
    return value
