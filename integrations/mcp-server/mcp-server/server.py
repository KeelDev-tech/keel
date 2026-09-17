"""Keel MCP server — exposes Keel's job-pipeline automation as MCP tools.

Transport: Streamable HTTP (MCP spec 2025-11-25 or later).
Safety: read-only / analysis-only tools over sample fixtures. See safety.py.

Run:
    python server.py [--host 127.0.0.1] [--port 8765] [--data-dir PATH]

Then point any MCP client at http://127.0.0.1:8765/mcp
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mcp.server.mcpserver import MCPServer  # noqa: E402

import keel_bridge  # noqa: E402
from safety import resolve_data_dir  # noqa: E402
from tools import discovery as _discovery  # noqa: E402
from tools import scoring as _scoring  # noqa: E402
from tools import verification as _verification  # noqa: E402
from tools import prescreen_tool as _prescreen  # noqa: E402
from tools import pipeline as _pipeline  # noqa: E402
from tools import honesty as _honesty  # noqa: E402

MCP_SPEC_MIN = "2025-11-25"

mcp = MCPServer(
    name="keel",
    instructions=(
        "Keel is an open-source job-application autopilot that refuses to lie. "
        "These tools expose its pipeline stages (discover, verify, score, prescreen) "
        "for analysis over sample data. All tools are read-only: nothing here submits "
        "applications, creates accounts, or contacts anyone. Anything needing a "
        "human's judgment is PARKed for the human instead of faked."
    ),
)

DATA_DIR: Path

# ---- tools ---------------------------------------------------------------
mcp.tool()(_discovery.keel_search_roles)
mcp.tool()(_scoring.keel_score_role)
mcp.tool()(_scoring.keel_score_band)
mcp.tool()(_verification.keel_verify_posting)
mcp.tool()(_verification.keel_ats_intel)
mcp.tool()(_prescreen.keel_prescreen_packet)
mcp.tool()(_pipeline.keel_pipeline_status)
mcp.tool()(_pipeline.keel_answer_lookup)
mcp.tool()(_pipeline.keel_run_pipeline)
mcp.tool()(_honesty.keel_probe_form)
mcp.tool()(_honesty.keel_truthfulness_check)


@mcp.tool()
def keel_ping() -> str:
    """Health check. Returns the server identity and MCP spec floor."""
    return f"keel-mcp ok | spec>={MCP_SPEC_MIN} | data_dir={DATA_DIR}"


# ---- resources -----------------------------------------------------------
@mcp.resource("keel://doctrine/summary")
def doctrine_summary() -> str:
    """One-paragraph summary of Keel's operating doctrine (read-only)."""
    return (
        "Keel automates the tedious parts of applying for jobs — discovery, "
        "verification, fit scoring, form prescreening — while refusing to invent "
        "qualifications. Anything needing a human's judgment (essays, attestations, "
        "commitments) is parked for the human instead of faked. Evidence is logged "
        "for every step so the system can audit and improve itself."
    )


# ---- prompts -------------------------------------------------------------
@mcp.prompt()
def triage_role(title: str, company: str, application_url: str = "") -> str:
    """Guided triage of one role through Keel's pipeline stages.

    Tells the agent exactly which tools to call in which order:
    verify the posting, pull ATS intel, score fit, prescreen the packet.
    """
    return (
        f"Triage the role '{title}' at {company} ({application_url or 'no URL given'}) "
        "using the Keel MCP tools, in this order:\n"
        "1. keel_verify_posting on the application URL (skip if no URL).\n"
        "2. keel_ats_intel on the URL to identify the ATS and fetch the job record.\n"
        "3. keel_probe_form on the application URL to enumerate the form questions.\n"
        "4. keel_score_role with the role's title/company/hard_requirements.\n"
        "5. keel_truthfulness_check with the applicant's verified_capabilities.\n"
        "6. If the band is APPLY or better, keel_prescreen_packet with the form intel.\n"
        "Report: verdict per stage, and whether a human needs to step in. "
        "Never invent qualifications, metrics, or answers."
    )


def main() -> None:
    global DATA_DIR
    parser = argparse.ArgumentParser(description="Keel MCP server (Streamable HTTP)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data-dir", default=None,
                        help="Directory of sample data (defaults to bundled samples). "
                             "Real pipeline/credential paths are refused.")
    args = parser.parse_args()
    DATA_DIR = resolve_data_dir(args.data_dir)
    keel_bridge.set_data_dir(DATA_DIR)
    print(f"[keel-mcp] data_dir={DATA_DIR}", flush=True)
    mcp.run(transport="streamable-http", host=args.host, port=args.port)


if __name__ == "__main__":
    main()
