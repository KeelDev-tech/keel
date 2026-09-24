#!/usr/bin/env python3
"""ats_matrix.py — reader + query API for the ATS capability matrix.

Build item 4 of 10 (2026-09-16): a machine-readable registry of tested ATS
boards, populated from evidence only. The registry records CAPABILITY FLAGS
(what is reachable / verifiable / walled), never submission mechanics,
request fingerprints, bypass techniques, or payload details.

Query API:
    capability(board, flag) -> bool   # True only when the board is
                                      # *verified* and the flag is
                                      # explicitly set. Unknown or
                                      # unverified boards -> False
                                      # (fail-soft: no capability claimed).
    status(board) -> "verified" | "unverified" | "unknown"
    known(board) -> bool              # board is registered at all
    blockers(board) -> list           # known_blockers, [] when unknown
    entry(board) -> dict              # full registered entry or {}
    source_capability(name, flag)      # same flags for the "sources" map
                                      # (linkedin, indeed)

Aliases ("jazzhr" -> "applytojob", "lever_embed" -> "lever", ...) are
resolved before lookup.

CLI:
    python3 ats_matrix.py --table            # ASCII capability table
    python3 ats_matrix.py --board lever      # one board's full entry
    python3 ats_matrix.py --capability lever api_direct_supported

RETIREMENT (Workstream A, 2026-09-18 — Keel Blocker Resolution Directive
§1): API-direct is RETIRED as a write transport (lifecycle
RETIRED_WRITE_PATH, see api_direct_policy.py). capability() consults the
policy first and can never report api_direct_supported=True, whatever the
stored evidence says. Discovery/inspection flags (guest_discovery,
form_probe_ok) are unaffected; the JSON evidence arrays are preserved as
historical provider intelligence.
"""

import json
import os
import sys

import api_direct_policy  # noqa: E402 — retirement gate (no cycles: policy is import-free)

_HERE = os.path.dirname(os.path.abspath(__file__))
_JSON_PATH = os.environ.get(
    "ATS_MATRIX_JSON", os.path.join(_HERE, "ats_matrix.json"))

_MATRIX = None

CAPABILITY_FLAGS = ("api_direct_supported", "guest_discovery",
                    "form_probe_ok")


def _board_key(board):
    if not board:
        return ""
    return str(board).strip().lower()


def load_matrix(path=None):
    """Load (and cache) the matrix JSON. Raises only on corrupt JSON."""
    global _MATRIX
    if _MATRIX is not None and path is None:
        return _MATRIX
    with open(path or _JSON_PATH) as f:
        data = json.load(f)
    if path is None:
        _MATRIX = data
    return data


def normalize_board(board, matrix=None):
    """Resolve aliases -> canonical board key. Unknown stays unknown."""
    key = _board_key(board)
    m = matrix or load_matrix()
    return m.get("aliases", {}).get(key, key)


def known(board, matrix=None):
    """True when the board is registered at all (verified or not)."""
    m = matrix or load_matrix()
    return normalize_board(board, m) in m.get("boards", {})


def status(board, matrix=None):
    """'verified' | 'unverified' (registered, no evidence) | 'unknown'
    (not registered at all)."""
    m = matrix or load_matrix()
    key = normalize_board(board, m)
    entry = m.get("boards", {}).get(key)
    if entry is None:
        return "unknown"
    return entry.get("status", "unverified")


def entry(board, matrix=None):
    """Full registered entry (alias-resolved), or {} when unknown."""
    m = matrix or load_matrix()
    key = normalize_board(board, m)
    return dict(m.get("boards", {}).get(key, {}))


def capability(board, flag, matrix=None):
    """Query a capability flag. Returns True ONLY when the board is
    registered with status 'verified' and the flag is explicitly true.
    Everything else (unknown board, unverified board, unknown flag)
    returns False — fail-soft by construction, no capability is ever
    claimed without evidence.

    RETIREMENT (Workstream A, 2026-09-18): the 'api_direct_supported'
    flag is the advertisement for the retired write transport. While
    api_direct_policy's lifecycle is RETIRED_WRITE_PATH this consult
    returns False unconditionally — the write claim can never be
    advertised True, whatever the stored evidence says. Discovery and
    inspection flags are unaffected."""
    if flag not in CAPABILITY_FLAGS:
        return False
    if (flag == "api_direct_supported"
            and api_direct_policy.retired()):
        # Deterministic retirement override: no transport selector can
        # read this matrix and choose the retired write path. The stored
        # JSON evidence remains as historical provider intelligence.
        return False
    m = matrix or load_matrix()
    key = normalize_board(board, m)
    e = m.get("boards", {}).get(key)
    if not e or e.get("status") != "verified":
        return False
    return bool(e.get(flag))


def blockers(board, matrix=None):
    """known_blockers for a board; [] when unknown or unverified."""
    m = matrix or load_matrix()
    if status(board, m) != "verified":
        return []
    key = normalize_board(board, m)
    return list(m.get("boards", {}).get(key, {}).get("known_blockers", []))


def source_capability(name, flag, matrix=None):
    """Same flag query for the 'sources' map (linkedin, indeed)."""
    if flag not in CAPABILITY_FLAGS:
        return False
    m = matrix or load_matrix()
    e = m.get("sources", {}).get(_board_key(name))
    if not e or e.get("status") != "verified":
        return False
    return bool(e.get(flag))


def table(matrix=None):
    """ASCII capability table: one row per board, flags as Y/-."""
    m = matrix or load_matrix()
    boards = m.get("boards", {})
    rows = []
    for key in sorted(boards):
        e = boards[key]
        st = (e.get("status") or "?")[:10]
        cells = [
            "Y" if (e.get("status") == "verified" and e.get(f)) else "-"
            for f in CAPABILITY_FLAGS
        ]
        bl = ",".join(e.get("known_blockers", [])) or "-"
        lv = e.get("last_verified") or "-"
        rows.append((key, st, cells, bl, lv))
    w_key = max([len(r[0]) for r in rows] + [5])
    w_st = max([len(r[1]) for r in rows] + [6])
    hdr = (f"{'board':<{w_key}}  {'status':<{w_st}}  "
           f"{'direct':<6}  {'guest':<5}  {'probe':<5}  "
           f"{'verified':<10}  known_blockers")
    out = [hdr, "-" * min(len(hdr), 120)]
    for key, st, cells, bl, lv in rows:
        out.append(
            f"{key:<{w_key}}  {st:<{w_st}}  "
            f"{cells[0]:<6}  {cells[1]:<5}  {cells[2]:<5}  "
            f"{lv:<10}  {bl}")
    return "\n".join(out)


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="ATS capability matrix query")
    ap.add_argument("--table", action="store_true",
                    help="dump the matrix as an ASCII table")
    ap.add_argument("--board", metavar="KEY",
                    help="print one board's full entry as JSON")
    ap.add_argument("--capability", nargs=2, metavar=("BOARD", "FLAG"),
                    help="query one capability flag (true/false)")
    ap.add_argument("--json", metavar="PATH",
                    help="override the matrix JSON path")
    args = ap.parse_args(argv)
    m = load_matrix(args.json) if args.json else load_matrix()
    if args.board:
        print(json.dumps(entry(args.board, m), indent=2))
        return 0
    if args.capability:
        board, flag = args.capability
        print("true" if capability(board, flag, m) else "false")
        return 0
    print(table(m))
    return 0


if __name__ == "__main__":
    sys.exit(main())
