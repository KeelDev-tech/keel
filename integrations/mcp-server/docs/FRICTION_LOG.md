# Friction log (hackathon judging bonus: up to +10%)

Specific task attempted, steps taken, expected vs. actual, severity, workaround,
actionable suggestion. Append as encountered.

## 2026-09-16 — MCP Python SDK v1→v2 migration
- **Task:** scaffold FastMCP server with `pip install mcp`.
- **Expected:** `from mcp.server.fastmcp import FastMCP` (v1 API in all tutorials).
- **Actual:** pip installed mcp 2.2.0; `mcp.server.fastmcp` raises ModuleNotFoundError
  with a migration pointer. FastMCP renamed to `mcp.server.mcpserver.MCPServer`.
- **Severity:** low (clear error message with migration link).
- **Workaround:** migrated scaffold to `MCPServer`; pinned `mcp>=2.2.0` in requirements.
- **Suggestion for Amazon/SDK owners:** n/a (Anthropic SDK, not Amazon) — noted for the
  writeup only.
