#!/usr/bin/env bash
# inspect_mcp_http.sh — Inspect mcp-atlassian over Streamable HTTP transport
#
# Usage:
#   ./scripts/inspect_mcp_http.sh                        # tools/list (default)
#   ./scripts/inspect_mcp_http.sh tools/list
#   ./scripts/inspect_mcp_http.sh tools/call jira_search '{"query":"project = DS"}'
#
# Environment variable overrides:
#   MCP_URL    default: http://localhost:8080/mcp
#   JIRA_URL   Jira instance URL
#   JIRA_TOKEN Jira personal access token
#   CONF_URL   Confluence instance URL
#   CONF_TOKEN Confluence personal access token

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
ENV_FILE="$PROJECT_DIR/.env"

# Load .env for defaults (if present)
if [[ -f "$ENV_FILE" ]]; then
  # shellcheck disable=SC1090
  set -o allexport
  source "$ENV_FILE"
  set +o allexport
fi

MCP_URL="${MCP_URL:-http://localhost:8080/mcp}"
MCP_JSON="$PROJECT_DIR/.mcp.json"

# Read credentials: env vars take priority, then .mcp.json headers, then .env URLs
_mcp_json_val() {
  jq -r ".mcpServers[].headers[\"$1\"] // empty" "$MCP_JSON" 2>/dev/null | head -1
}

JIRA_URL="${JIRA_URL:-$(_mcp_json_val X-Atlassian-Jira-Url)}"
JIRA_TOKEN="${JIRA_TOKEN:-$(_mcp_json_val X-Atlassian-Jira-Personal-Token)}"
CONF_URL="${CONF_URL:-$(_mcp_json_val X-Atlassian-Confluence-Url)}"
CONF_TOKEN="${CONF_TOKEN:-$(_mcp_json_val X-Atlassian-Confluence-Personal-Token)}"

METHOD="${1:-tools/list}"
TOOL_NAME="${2:-}"
TOOL_ARGS="${3:-{}}"

# Validate required values
if [[ -z "$JIRA_URL" || -z "$JIRA_TOKEN" || -z "$CONF_URL" || -z "$CONF_TOKEN" ]]; then
  echo "ERROR: Missing credentials. Export before running:"
  echo "  JIRA_URL, JIRA_TOKEN, CONF_URL, CONF_TOKEN"
  exit 1
fi

COMMON_HEADERS=(
  -H "Content-Type: application/json"
  -H "Accept: application/json, text/event-stream"
  -H "X-Atlassian-Jira-Url: $JIRA_URL"
  -H "X-Atlassian-Jira-Personal-Token: $JIRA_TOKEN"
  -H "X-Atlassian-Confluence-Url: $CONF_URL"
  -H "X-Atlassian-Confluence-Personal-Token: $CONF_TOKEN"
)

echo "=== Connecting to $MCP_URL ===" >&2

# Step 1: initialize — capture session ID
INIT_RESPONSE=$(curl -s -X POST "$MCP_URL" \
  "${COMMON_HEADERS[@]}" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"inspect_mcp_http.sh","version":"1.0"}}}' \
  -D /tmp/mcp_headers.txt)

SESSION_ID=$(grep -i "mcp-session-id" /tmp/mcp_headers.txt | awk '{print $2}' | tr -d '\r')

if [[ -z "$SESSION_ID" ]]; then
  echo "ERROR: No session ID returned. Is the server running at $MCP_URL?" >&2
  echo "Response: $INIT_RESPONSE" >&2
  exit 1
fi

echo "Session: $SESSION_ID" >&2
echo "" >&2

# Step 2: run the requested method
case "$METHOD" in
  tools/list)
    PAYLOAD='{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}'
    echo "=== tools/list ===" >&2
    curl -s -X POST "$MCP_URL" \
      "${COMMON_HEADERS[@]}" \
      -H "mcp-session-id: $SESSION_ID" \
      -d "$PAYLOAD" \
    | grep "^data:" | sed 's/^data: //' \
    | jq '.result.tools[] | {name, description: .description[:80]}'
    ;;

  tools/call)
    if [[ -z "$TOOL_NAME" ]]; then
      echo "ERROR: tool name required for tools/call" >&2
      echo "Usage: $0 tools/call <tool_name> '<json_args>'" >&2
      exit 1
    fi
    PAYLOAD=$(jq -n \
      --arg name "$TOOL_NAME" \
      --argjson args "$TOOL_ARGS" \
      '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":$name,"arguments":$args}}')
    echo "=== tools/call $TOOL_NAME ===" >&2
    echo "Args: $TOOL_ARGS" >&2
    echo "" >&2
    curl -s -X POST "$MCP_URL" \
      "${COMMON_HEADERS[@]}" \
      -H "mcp-session-id: $SESSION_ID" \
      -d "$PAYLOAD" \
    | grep "^data:" | sed 's/^data: //' \
    | jq '.result'
    ;;

  *)
    # Generic — send any method/params as-is
    PAYLOAD=$(jq -n --arg method "$METHOD" '{"jsonrpc":"2.0","id":2,"method":$method,"params":{}}')
    echo "=== $METHOD ===" >&2
    curl -s -X POST "$MCP_URL" \
      "${COMMON_HEADERS[@]}" \
      -H "mcp-session-id: $SESSION_ID" \
      -d "$PAYLOAD" \
    | grep "^data:" | sed 's/^data: //' \
    | jq '.'
    ;;
esac
