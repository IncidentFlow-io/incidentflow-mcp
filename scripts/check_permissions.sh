#!/usr/bin/env bash
# Check what an API key / OAuth token may do against MCP and platform-api.
#
#   IF_TOKEN=if_wst_... EXPECT=run bash scripts/check_permissions.sh
#
# EXPECT is the broadest MCP scope on the key: none | read | run | write.
# Every row must say OK.
# Env: MCP_URL (default http://127.0.0.1:8001/mcp),
#      PLATFORM_URL (default http://127.0.0.1:8000).
set -u
: "${IF_TOKEN:?export IF_TOKEN=<key or token>}"
EXPECT="${EXPECT:-run}"
MCP_URL="${MCP_URL:-http://127.0.0.1:8001/mcp}"
PLATFORM_URL="${PLATFORM_URL:-http://127.0.0.1:8000}"
rank() { case "$1" in none) echo 0 ;; read) echo 1 ;; run) echo 2 ;; write) echo 3 ;; esac; }
HAVE=$(rank "$EXPECT")
fails=0

mcp() { # method params -> prints HTTP status
  curl -s -o /dev/null -w '%{http_code}' -m 40 "$MCP_URL" \
    -H "Authorization: Bearer $IF_TOKEN" \
    -H 'Accept: application/json, text/event-stream' \
    -H 'Content-Type: application/json' \
    -d "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"$1\",\"params\":$2}"
}

check() { # label needed_rank actual_status
  local want
  if [ "$HAVE" -ge "$2" ]; then want=200; else want=403; fi
  if [ "$3" = "$want" ]; then
    printf 'OK    %-46s HTTP %s\n' "$1" "$3"
  else
    printf 'FAIL  %-46s HTTP %s (expected %s)\n' "$1" "$3" "$want"
    fails=$((fails + 1))
  fi
}

init='{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"perm-check","version":"1"}}'
check "mcp:read        initialize" 1 "$(mcp initialize "$init")"
check "mcp:read        tools/list" 1 "$(mcp tools/list '{}')"
check "mcp:tools:run   tools/call mcp_version" 2 "$(mcp tools/call '{"name":"mcp_version","arguments":{}}')"
check "mcp:tools:run   tools/call k8s_list_namespaces" 2 \
  "$(mcp tools/call '{"name":"k8s_list_namespaces","arguments":{"environment":"dev"}}')"
check "mcp:tools:write tools/call knowledge_upsert (dry run)" 3 \
  "$(mcp tools/call '{"name":"knowledge_upsert","arguments":{"title":"perm-check","text":"perm-check","document_type":"knowledge","dry_run":true}}')"

echo
echo "Platform-api scopes (informational: 200 = granted, 401/403 = not):"
printf '  incidents:read  GET  /api/v1/incidents            HTTP %s\n' "$(curl -s -o /dev/null -w '%{http_code}' \
  "$PLATFORM_URL/api/v1/incidents?limit=1" -H "Authorization: Bearer $IF_TOKEN")"
# An empty alert list is rejected after the permission check (422), so a granted
# key is proven without creating a signal; 403 means the key lacks signals:ingest.
printf '  signals:ingest  POST /api/v1/webhooks/alertmanager HTTP %s  (422 = granted, nothing stored; 403 = not)\n' "$(curl -s -o /dev/null -w '%{http_code}' \
  -X POST "$PLATFORM_URL/api/v1/webhooks/alertmanager" -H "Authorization: Bearer $IF_TOKEN" \
  -H 'Content-Type: application/json' -d '{"version":"4","status":"firing","alerts":[]}')"

echo
[ "$fails" -eq 0 ] && echo "All MCP permission checks passed for EXPECT=$EXPECT." || echo "$fails MCP check(s) failed."
exit "$fails"
