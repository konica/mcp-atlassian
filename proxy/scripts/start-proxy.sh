#!/usr/bin/env bash
# Start mcp-proxy locally (Step 2 from RUNBOOK.md Part 1.3)
# Upstream mcp-atlassian must already be running on port 8080.
#
# Usage:
#   ./start-proxy.sh           # normal mode (hot-reload)
#   ./start-proxy.sh --debug   # debugpy mode (attach VS Code on port 5678)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROXY_DIR="$(dirname "$SCRIPT_DIR")"

DEBUG_MODE=false
if [[ "${1:-}" == "--debug" ]]; then
  DEBUG_MODE=true
fi

# Load proxy/.env if present (overrides defaults; shell env vars take precedence)
ENV_FILE="$PROXY_DIR/.env"
if [[ -f "$ENV_FILE" ]]; then
  echo "Loading $ENV_FILE"
  set -a
  # shellcheck source=/dev/null
  source "$ENV_FILE"
  set +a
fi

export PROXY_UPSTREAM_URL="${PROXY_UPSTREAM_URL:-http://127.0.0.1:8080}"
export PROXY_READ_ONLY="${PROXY_READ_ONLY:-false}"
export PROXY_JIRA_PROJECTS_WHITELIST="${PROXY_JIRA_PROJECTS_WHITELIST:-}"
export PROXY_CONFLUENCE_SPACES_WHITELIST="${PROXY_CONFLUENCE_SPACES_WHITELIST:-}"
export PROXY_AUDIT_LOG_ENABLED="${PROXY_AUDIT_LOG_ENABLED:-true}"

# Kill any process already using port 8000
if lsof -ti :8000 &>/dev/null; then
  echo "Killing existing process on port 8000..."
  lsof -ti :8000 | xargs kill -9
fi

echo "Starting mcp-proxy..."
echo "  Upstream : $PROXY_UPSTREAM_URL"
echo "  Port     : 8000"
echo "  Read-only: $PROXY_READ_ONLY"
echo "  Jira WL  : ${PROXY_JIRA_PROJECTS_WHITELIST:-<all>}"
echo "  Conf WL  : ${PROXY_CONFLUENCE_SPACES_WHITELIST:-<all>}"
echo "  Debug    : $DEBUG_MODE"
echo ""

cd "$PROXY_DIR"

if [[ "$DEBUG_MODE" == "true" ]]; then
  echo "Waiting for debugger on port 5678 — attach via VS Code 'Attach to mcp-proxy'..."
  exec uv run --active python -m debugpy --listen 5678 --wait-for-client \
    -m uvicorn mcp_proxy.main:app \
    --host 0.0.0.0 \
    --port 8000
else
  exec uv run uvicorn mcp_proxy.main:app \
    --host 0.0.0.0 \
    --port 8000 \
    --reload
fi
