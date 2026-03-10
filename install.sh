#!/usr/bin/env bash
set -euo pipefail

# IncidentFlow MCP Installer for VS Code workspace
# Usage:
#   curl -fsSL https://incidentflow.io/install.sh | bash
#   OR
#   ./install.sh

BOLD='\033[1m'
ACCENT='\033[38;2;59;130;246m'
SUCCESS='\033[38;2;16;185;129m'
WARN='\033[38;2;245;158;11m'
ERROR='\033[38;2;239;68;68m'
INFO='\033[38;2;148;163;184m'
NC='\033[0m'

SERVER_NAME="${INCIDENTFLOW_SERVER_NAME:-incidentflow}"
SERVER_URL="${INCIDENTFLOW_SERVER_URL:-http://localhost:8000/mcp}"
CONFIG_FILE="${INCIDENTFLOW_CONFIG_FILE:-.vscode/mcp.json}"

log() {
  printf "%b%s%b\n" "$INFO" "$1" "$NC"
}

ok() {
  printf "%b%s%b\n" "$SUCCESS" "$1" "$NC"
}

warn() {
  printf "%b%s%b\n" "$WARN" "$1" "$NC"
}

err() {
  printf "%b%s%b\n" "$ERROR" "$1" "$NC" >&2
}

headline() {
  printf "\n%b%s%b\n" "$BOLD$ACCENT" "$1" "$NC"
}

require_jq() {
  if ! command -v jq >/dev/null 2>&1; then
    err "jq is required to safely update JSON."
    echo
    log "Install jq and run again:"
    echo "  macOS: brew install jq"
    echo "  Ubuntu/Debian: sudo apt-get install -y jq"
    echo "  Fedora: sudo dnf install -y jq"
    exit 1
  fi
}

ensure_config_file() {
  mkdir -p "$(dirname "$CONFIG_FILE")"

  if [[ ! -f "$CONFIG_FILE" ]]; then
    log "No existing $CONFIG_FILE found. Creating a new one..."
    printf '{\n  "servers": {}\n}\n' > "$CONFIG_FILE"
    return
  fi

  if ! jq empty "$CONFIG_FILE" >/dev/null 2>&1; then
    err "$CONFIG_FILE exists but is not valid JSON."
    err "Please fix it manually, then run the installer again."
    exit 1
  fi
}


merge_server() {
  local tmp_file
  tmp_file="$(mktemp)"

  jq \
    --arg server_name "$SERVER_NAME" \
    --arg server_url "$SERVER_URL" \
    '
    .servers = (.servers // {}) |
    .servers[$server_name] = {
      "url": $server_url,
      "type": "http"
    }
    ' "$CONFIG_FILE" > "$tmp_file"

  mv "$tmp_file" "$CONFIG_FILE"
}

print_result() {
  echo
  ok "IncidentFlow MCP server configured successfully."
  log "Config file: $CONFIG_FILE"
  log "Server name: $SERVER_NAME"
  log "Server URL:  $SERVER_URL"
  echo

  log "Next steps:"
  echo "  1. Reload VS Code"
  echo "     Cmd/Ctrl + Shift + P -> Developer: Reload Window"
  echo "  2. Open .vscode/mcp.json and confirm the server is present"
  echo "  3. Start using IncidentFlow from your MCP-enabled workflow"
  echo

  log "Example entry added/updated:"
  cat <<EOF
{
  "servers": {
    "$SERVER_NAME": {
      "url": "$SERVER_URL",
      "type": "http"
    }
  }
}
EOF
}

main() {
  headline "Installing IncidentFlow MCP for VS Code workspace"
  require_jq
  ensure_config_file

  if jq -e --arg server_name "$SERVER_NAME" '.servers[$server_name]' "$CONFIG_FILE" >/dev/null 2>&1; then
    warn "Server '$SERVER_NAME' already exists in $CONFIG_FILE"
    log "Updating it to the latest URL..."
  else
    log "Adding server '$SERVER_NAME' to $CONFIG_FILE..."
  fi

  merge_server
  print_result
}

main "$@"