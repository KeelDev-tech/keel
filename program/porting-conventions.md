# Porting conventions — live engine edits (adopted by dev-support-deep-sweep run 241, 2026-09-20)

## Atomic-write rule (hard)
All edits to engine files imported by hot loops (pool_guardian, verify_retry,
application-executor) MUST go through atomic writes:

1. Write new content to `file.py.tmp.<pid>` in the same directory.
2. `py_compile.compile()` the tmp file and abort on failure — never publish a
   file that fails to compile.
3. `os.replace(tmp, file)` — single atomic rename. On POSIX, readers see
   old-or-new, never partial.
4. On rename failure: remove tmp, log, keep the old file in place.

## Why
pool_guardian runs on a ~30s respawning loop and imports engine files each
iteration. A direct in-place edit (write / edit tool) lands a partially
written file that the next loop iteration imports — run 241 observed an
IndentationError at genuine_pat.py:262 crashing two loop imports before the
file self-recovered. Atomic rename closes that window entirely.

## Edit-then-verify
Rule 2 (compile-check the tmp file before rename) applies even to small port
edits and one-line fixes. No exception for "trivial" changes.

## Optional hardening (proposed, not wired)
A Tier-1 guard that `py_compile`s hot-loop files before each guardian launch
is cheap defense-in-depth if import crashes recur. Left to the pulse owner.

## Adoption
Mechanics convention sanctioned by the deep-sweep octopus coordinator, run 241.
Not a threshold or policy change — no charter impact.
