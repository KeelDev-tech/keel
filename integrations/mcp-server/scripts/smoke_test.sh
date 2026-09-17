#!/bin/bash
# Smoke test: boot the Keel MCP server, handshake, list tools, call keel_ping.
# Reads full responses (no head-truncation: truncating the pipe SIGPIPEs curl
# under `set -o pipefail` and kills the run).
set -euo pipefail
cd "$(dirname "$0")/.."
PY=.venv/bin/python
PORT=18765
$PY mcp-server/server.py --port $PORT > /tmp/keel_smoke_srv.log 2>&1 & SRV=$!
trap "kill $SRV 2>/dev/null" EXIT
sleep 6
HDR=(-H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream')
INIT=$(curl -s -D - -o /tmp/keel_init_body.json -X POST http://127.0.0.1:$PORT/mcp \
  "${HDR[@]}" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-11-25","capabilities":{},"clientInfo":{"name":"smoke","version":"0"}}}')
SID=$(echo "$INIT" | grep -i 'mcp-session-id' | tr -d '\r' | awk '{print $2}')
[ -n "$SID" ] || { echo "FAIL: no session id"; cat /tmp/keel_init_body.json; exit 1; }
curl -s -X POST http://127.0.0.1:$PORT/mcp "${HDR[@]}" -H "mcp-session-id: $SID" \
  -d '{"jsonrpc":"2.0","method":"notifications/initialized"}' > /dev/null
post() { curl -s -X POST http://127.0.0.1:$PORT/mcp "${HDR[@]}" -H "mcp-session-id: $SID" -d "$1"; }
TOOLS_JSON=$(post '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}')
echo "$TOOLS_JSON" | grep -o '"name":"keel_[a-z_]*"' | sort -u
for t in keel_search_roles keel_score_role keel_score_band keel_verify_posting keel_ats_intel keel_probe_form keel_truthfulness_check keel_prescreen_packet keel_pipeline_status keel_answer_lookup keel_run_pipeline keel_ping; do
  echo "$TOOLS_JSON" | grep -q "\"name\":\"$t\"" || { echo "FAIL: tool $t missing"; exit 1; }
done
PING=$(post '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"keel_ping","arguments":{}}}')
echo "$PING" | grep -o 'keel-mcp ok[^"]*' | head -1
RUN=$(post '{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"keel_run_pipeline","arguments":{"query":"operations","limit":2}}}')
echo "$RUN" | grep -q '"isError":false' || { echo "FAIL: keel_run_pipeline errored"; exit 1; }
echo "$RUN" | grep -o 'discovered[^0-9]*[0-9]*' | head -1
echo "SMOKE OK"
