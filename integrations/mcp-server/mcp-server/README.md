# Keel MCP Server

A self-hosted [Model Context Protocol](https://modelcontextprotocol.io) server exposing
Keel's job-pipeline automation stages as agent-callable tools over Streamable HTTP.

Built for the **Amazon Developer Hackathon** (Alexa+ track + Open Source mini-challenge).
MCP spec: 2025-11-25 minimum (SDK implements a later version; handshake negotiates).

## What it does

| Tool | Stage it exposes |
|---|---|
| `keel_search_roles` | Job discovery (queries → postings) |
| `keel_verify_posting` | Posting liveness verification |
| `keel_ats_intel` | ATS detection + public job records |
| `keel_probe_form` | Application-form question extraction |
| `keel_score_role` / `keel_score_band` | Fit scoring against the candidate profile |
| `keel_truthfulness_check` | Anti-fabrication gap check ("refuses to lie") |
| `keel_prescreen_packet` | Application-packet gate screening (dry run) |
| `keel_pipeline_status` | Telemetry / pipeline status summary |
| `keel_run_pipeline` | Agentic composite: discover → verify → score → prescreen |
| `keel_answer_lookup` | Example answer-bank lookup (synthetic fixtures only) |

Plus `keel_ping` (health check), the `keel://doctrine/*` resources, and a `triage-role` prompt.

## Safety

- **Loopback only.** `--host` accepts literal loopback IP addresses (default `127.0.0.1`);
  wildcard, LAN, public addresses, and hostnames are refused before transport startup.
  This adapter has no authentication layer. Loopback does not authenticate local users;
  do not publish it through a proxy or tunnel without a separately verified authenticated boundary.
- **Read-only / analysis-only.** No tool submits applications, creates accounts, or contacts anyone.
- **Sample data only.** The server reads from a bundled `sample_data/` directory.
  It refuses to resolve paths under the operator's live pipeline tree, `credentials/`, or any real
  answer bank / queue. See `safety.py`; the refusals are covered by `tests/test_safety.py`.

## Setup & run

```bash
python3 -m venv .venv
.venv/bin/pip install -r integrations/mcp-server/mcp-server/requirements.txt
.venv/bin/python integrations/mcp-server/mcp-server/server.py --port 8765
# point any MCP client at http://127.0.0.1:8765/mcp
```

Root CI runs the adapter safety, honesty, and tool suites with offline transport fixtures.
These tests validate the adapter boundary and engine contracts; they do not qualify
the installed MCP SDK handshake or the private Muse executor.

Run the tests:

```bash
.venv/bin/python -m pytest integrations/mcp-server/tests/ -q
```

Smoke-test the Streamable HTTP handshake:

```bash
./integrations/mcp-server/scripts/smoke_test.sh
```

## License

Apache-2.0 (same as Keel).
