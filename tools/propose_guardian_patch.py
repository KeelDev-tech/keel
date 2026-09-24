#!/usr/bin/env python3
"""Print a narrow diff for the reported guardian branch; NEVER modify its file.

Refuses unknown function/branch shapes. Telemetry must be wired separately,
before all decision returns, using docs/SUPPLY_RECOVERY.md.
"""
import argparse
import ast
import difflib
from pathlib import Path


def propose(source):
    tree = ast.parse(source)
    functions = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "should_fire"]
    if len(functions) != 1:
        raise ValueError("expected exactly one top-level should_fire function")
    function = functions[0]
    names = {a.arg for a in function.args.posonlyargs + function.args.args + function.args.kwonlyargs}
    if not {"ready", "actionable"} <= names:
        raise ValueError("unknown should_fire signature; inspect current source")
    body = list(function.body)
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
        body.pop(0)
    expected = ast.parse('if ready >= READY_FLOOR:\n    return False, "pool healthy"\n').body[0]
    if not body or ast.dump(body[0]) != ast.dump(expected):
        raise ValueError("reported early-return shape not found; source may differ or already be fixed")
    ret = body[0].body[0]
    lines = source.splitlines(keepends=True)
    prefix = lines[ret.lineno - 1][:ret.col_offset]
    if prefix.strip() or ret.lineno != ret.end_lineno:
        raise ValueError("unsupported inline or multiline return; inspect current source")
    newline = "\r\n" if lines[ret.lineno - 1].endswith("\r\n") else "\n"
    insertion = (prefix + "if actionable == 0:" + newline + prefix + "    return False, \"buffer full, supply starved\"" + newline)
    lines.insert(ret.lineno - 1, insertion)
    candidate = "".join(lines)
    ast.parse(candidate)
    return candidate


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    args = parser.parse_args()
    source = args.source.read_bytes().decode("utf-8")
    candidate = propose(source)
    print("".join(difflib.unified_diff(source.splitlines(True), candidate.splitlines(True),
                                      fromfile="a/pool_guardian.py", tofile="b/pool_guardian.py")), end="")


if __name__ == "__main__":
    main()
