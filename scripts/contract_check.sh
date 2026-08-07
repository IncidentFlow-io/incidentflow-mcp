#!/usr/bin/env bash
# Curl smoke test of the published MCP contract against a running server.
#
# Levels: (1) service publishes version, (2) service publishes JSON Schemas,
# (3) a real tool response validates against its published schema.
#
# Env:
#   MCP_URL     base URL of the running MCP server (default http://127.0.0.1:8001)
#   MCP_TOKEN   OAuth access token (required for tools/list and tools/call)
#
# Requires: curl, jq, and check-jsonschema (uv tool install check-jsonschema).
set -euo pipefail

MCP_URL="${MCP_URL:-http://127.0.0.1:8001}"
ACCEPT='application/json, text/event-stream'
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# SSE responses are prefixed with "data: "; strip it if present.
_json() { sed -n 's/^data: //p; t; p'; }

echo "== Level 1: version =="
curl -fsS "$MCP_URL/version" | jq '{service, service_version, api_version, contract_version, supported_api_versions}'

echo "== Level 2: schema catalog =="
curl -fsS "$MCP_URL/schemas" | jq '{api_version, schema_version, count: (.schemas | length)}'
curl -fsS "$MCP_URL/schemas/incidentflow.mcp-version.response" -o "$TMP/mcp-version.schema.json"
jq -e '."$schema" == "https://json-schema.org/draft/2020-12/schema"' "$TMP/mcp-version.schema.json" >/dev/null
echo "  fetched incidentflow.mcp-version.response schema"

if [[ -z "${MCP_TOKEN:-}" ]]; then
  echo "MCP_TOKEN not set — skipping authenticated tools/list + tools/call (levels require it)."
  exit 0
fi

AUTH=(-H "Authorization: Bearer ${MCP_TOKEN}")

echo "== tools/list: every tool has input+output schema =="
curl -fsS "$MCP_URL/mcp" "${AUTH[@]}" -H "Content-Type: application/json" -H "Accept: ${ACCEPT}" \
  --data '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}' \
  | _json \
  | jq -r '.result.tools[] | select((.inputSchema==null) or (.outputSchema==null)) | .name' \
  | { grep . && { echo "  FAIL: tools missing schemas"; exit 1; } || echo "  all tools have input+output schemas"; }

echo "== Level 3: call mcp_version and validate its response =="
curl -fsS "$MCP_URL/mcp" "${AUTH[@]}" -H "Content-Type: application/json" -H "Accept: ${ACCEPT}" \
  --data '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"mcp_version","arguments":{}}}' \
  | _json | jq '.result.structuredContent' > "$TMP/mcp-version.response.json"

check-jsonschema --schemafile "$TMP/mcp-version.schema.json" "$TMP/mcp-version.response.json"
echo "OK — response validates against the published schema."
