"""Tests for employer_patterns.py.

Uses the sanitized example fixture (employer_form_patterns.example.json)
via EMPLOYER_PATTERNS_FILE so no real employer data is ever touched.
Exercises: lookup hit/miss, ATS matching, case-insensitivity, merge
non-clobber behavior.

Run: python3 -m unittest discover -s tests  (or python3 test_employer_patterns.py)
"""
import json
import os
import shutil
import sys
import tempfile

BASE = os.path.dirname(os.path.abspath(__file__))
ENGINES = os.path.join(BASE, "..", "engines")
TMP = tempfile.mkdtemp(prefix="emp_patterns_test_")
os.environ["EMPLOYER_PATTERNS_FILE"] = os.path.join(TMP, "employer_form_patterns.json")
shutil.copy(
    os.path.join(ENGINES, "employer_form_patterns.example.json"),
    os.environ["EMPLOYER_PATTERNS_FILE"],
)

sys.path.insert(0, ENGINES)
import employer_patterns as ep

PASS = []
FAIL = []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name + (f" — {detail}" if detail and not cond else ""))


# 1. lookup hit (example-corp fixture)
p = ep.get_patterns("ExampleCorp", "greenhouse")
check("lookup hit returns entry", p is not None and p["employer"] == "ExampleCorp")
check("lookup hit has 1 blocker", p is not None and len(p["blockers"]) == 1, str(p))
check("lookup hit confidence medium", p is not None and p["confidence"] == "medium")
check("lookup hit evidence encounters=1", p is not None and p["evidence"]["encounters"] == 1)

# 2. lookup miss
check("unknown employer -> None", ep.get_patterns("Nonexistent Corp", "greenhouse") is None)
check("ATS mismatch -> None", ep.get_patterns("ExampleCorp", "lever") is None)
check("empty employer -> None", ep.get_patterns("", "greenhouse") is None)

# 3. case-insensitivity
for variant in ["examplecorp", "EXAMPLECORP", "  ExampleCorp  ", "eXaMpLeCoRp"]:
    q = ep.get_patterns(variant, "GREENHOUSE")
    check(f"case-insensitive employer+ats: {variant!r}", q is not None and q["employer"] == "ExampleCorp")

# 4. merge does not clobber an existing entry
before = ep.get_patterns("ExampleCorp", "greenhouse")
merged = ep.record_pattern(
    "ExampleCorp", "greenhouse",
    ["newly observed: required portfolio URL"],
    ["What is your portfolio URL?"],
    "test encounter 2026-09-15",
)
check("merge keeps old blockers", all(b in merged["blockers"] for b in before["blockers"]))
check("merge adds new blocker", "newly observed: required portfolio URL" in merged["blockers"])
check("merge adds new question", "What is your portfolio URL?" in merged["questions"])
check("merge bumps encounters", merged["evidence"]["encounters"] == before["evidence"]["encounters"] + 1)

# 5. recording a brand-new employer
fresh = ep.record_pattern("Fresh Startup", "ashby", ["blocker x"], ["Q?"], "test source")
check("new employer recorded", ep.get_patterns("Fresh Startup", "ashby") is not None)
check("new employer blockers kept", fresh["blockers"] == ["blocker x"])

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
# Guarded so `python3 -m unittest discover -s tests` (CI) can import this
# module without SystemExit killing collection. Direct runs keep exit-code behavior.
if __name__ == "__main__":
    sys.exit(1 if FAIL else 0)
