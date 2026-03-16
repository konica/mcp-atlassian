# Local Debug Guide — mcp-atlassian + mcp-proxy

> How to start the stack locally using the Docker image for the downstream server,
> run the proxy natively, and verify the full request flow end-to-end.

---

## Prerequisites

| Tool | Check |
|---|---|
| Docker Desktop running | `docker info` |
| `ka-mcp-atlassian` image built | `docker images ka-mcp-atlassian` |
| proxy venv ready | `ls proxy/.venv` |
| ports 8080 and 8000 free | `lsof -i :8080 -i :8000` |

---

## Step 1 — Start the downstream mcp-atlassian (Docker)

```bash
docker run --rm -d \
  --name mcp-atlassian \
  -p 8080:8080 \
  ka-mcp-atlassian \
  --transport streamable-http \
  --host 0.0.0.0 \
  --port 8080 \
  --path /mcp
```

**Notes:**
- `--rm` removes the container automatically when stopped
- No Atlassian credential env vars are needed — each user passes their own via `X-Atlassian-*` request headers
- Port 8080 is exposed to `localhost` only for the proxy to reach it

Verify it is healthy:
```bash
curl http://localhost:8080/healthz
# → {"status":"ok"}
```

Check logs if it fails to start:
```bash
docker logs mcp-atlassian
```

---

## Step 2 — Start the proxy (native uv)

Open a second terminal in the repo root:

```bash
cd /path/to/ka-mcp-atlassian/proxy

# Install deps if not done yet
uv sync --all-extras

# Start with hot-reload and debug logging
PROXY_UPSTREAM_URL=http://localhost:8080 \
PROXY_READ_ONLY=false \
PROXY_AUDIT_LOG_ENABLED=true \
uv run uvicorn mcp_proxy.main:app \
  --host 127.0.0.1 \
  --port 8000 \
  --reload \
  --log-level debug
```

Verify it is healthy:
```bash
curl http://127.0.0.1:8000/healthz
# → {"status":"ok"}
```

**Optional env vars to test enforcement:**
```bash
PROXY_READ_ONLY=true                        # block all write tools
PROXY_JIRA_PROJECTS_WHITELIST=PROJ,DEMO     # restrict to these projects
PROXY_CONFLUENCE_SPACES_WHITELIST=ENG,HR    # restrict to these spaces
```

---

## Step 3 — Establish an MCP session

MCP `streamable-http` requires an `initialize` handshake before any other call.
The server returns a `mcp-session-id` header that must be included in all subsequent requests.

```bash
# Save the session ID into a shell variable
SESSION_ID=$(curl -s -X POST http://127.0.0.1:8000/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "X-Atlassian-Jira-Url: https://jira.mgm-tp.com/jira" \
  -H "X-Atlassian-Jira-Personal-Token: YOUR_JIRA_PAT" \
  -H "X-Atlassian-Confluence-Url: https://wiki.mgm-tp.com/confluence" \
  -H "X-Atlassian-Confluence-Personal-Token: YOUR_CONFLUENCE_PAT" \
  -d '{
    "jsonrpc": "2.0",
    "id": 0,
    "method": "initialize",
    "params": {
      "protocolVersion": "2024-11-05",
      "capabilities": {},
      "clientInfo": { "name": "curl-debug", "version": "1.0" }
    }
  }' \
  -D - 2>/dev/null \
  | grep -i "mcp-session-id" | awk '{print $2}' | tr -d '\r')

echo "Session: $SESSION_ID"
```

> **Two common errors before this step:**
> - `Not Acceptable` — missing `Accept: application/json, text/event-stream` header
> - `Missing session ID` — skipped `initialize`, went straight to `tools/list` or `tools/call`

---

## Step 4 — Test requests through the proxy

All requests after `initialize` must include:
1. `Accept: application/json, text/event-stream`
2. `mcp-session-id: $SESSION_ID`
3. The four `X-Atlassian-*` credential headers

Responses are **SSE streams** (`text/event-stream`). Extract the JSON payload with `grep '^data:' | sed 's/^data: //'`.

---

### Test A — List all available tools

```bash
curl -s -X POST http://127.0.0.1:8000/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "mcp-session-id: $SESSION_ID" \
  -H "X-Atlassian-Jira-Url: https://jira.mgm-tp.com/jira" \
  -H "X-Atlassian-Jira-Personal-Token: YOUR_JIRA_PAT" \
  -H "X-Atlassian-Confluence-Url: https://wiki.mgm-tp.com/confluence" \
  -H "X-Atlassian-Confluence-Personal-Token: YOUR_CONFLUENCE_PAT" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}' \
  | grep '^data:' | sed 's/^data: //' | jq '.result.tools[].name'
```

Expected: printed list of tool names (`jira_get_issue`, `confluence_search`, …).
This request is **not enforced** — it always passes through.

---

### Test B — Allowed read tool call

```bash
curl -s -X POST http://127.0.0.1:8000/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "mcp-session-id: $SESSION_ID" \
  -H "X-Atlassian-Jira-Url: https://jira.mgm-tp.com/jira" \
  -H "X-Atlassian-Jira-Personal-Token: YOUR_JIRA_PAT" \
  -d '{
    "jsonrpc": "2.0", "id": 2, "method": "tools/call",
    "params": { "name": "jira_get_issue", "arguments": { "issue_key": "PROJ-1" } }
  }' \
  | grep '^data:' | sed 's/^data: //' | jq .
```

Expected: HTTP 200, SSE response with issue JSON inside `result`.

---

### Test C — Denied: write tool blocked (read-only mode)

Start the proxy with `PROXY_READ_ONLY=true` then:

```bash
curl -s -X POST http://127.0.0.1:8000/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "mcp-session-id: $SESSION_ID" \
  -H "X-Atlassian-Jira-Url: https://jira.mgm-tp.com/jira" \
  -H "X-Atlassian-Jira-Personal-Token: YOUR_JIRA_PAT" \
  -d '{
    "jsonrpc": "2.0", "id": 3, "method": "tools/call",
    "params": { "name": "jira_create_issue", "arguments": { "project_key": "PROJ", "summary": "Test" } }
  }' | jq .
```

Expected HTTP 403:
```json
{
  "jsonrpc": "2.0",
  "id": 3,
  "error": {
    "code": -32001,
    "message": "Tool 'jira_create_issue' is a write operation; server is in read-only mode."
  }
}
```

---

### Test D — Denied: project not in whitelist

Start the proxy with `PROXY_JIRA_PROJECTS_WHITELIST=PROJ` then:

```bash
curl -s -X POST http://127.0.0.1:8000/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "mcp-session-id: $SESSION_ID" \
  -H "X-Atlassian-Jira-Url: https://jira.mgm-tp.com/jira" \
  -H "X-Atlassian-Jira-Personal-Token: YOUR_JIRA_PAT" \
  -d '{
    "jsonrpc": "2.0", "id": 4, "method": "tools/call",
    "params": { "name": "jira_get_issue", "arguments": { "issue_key": "OTHER-42" } }
  }' | jq .
```

Expected HTTP 403:
```json
{
  "jsonrpc": "2.0",
  "id": 4,
  "error": {
    "code": -32001,
    "message": "Tool 'jira_get_issue' references Jira project(s) ['OTHER'] which are not in the whitelist ['PROJ']."
  }
}
```

---

### Test E — Denied: JQL query with out-of-scope project

```bash
curl -s -X POST http://127.0.0.1:8000/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "mcp-session-id: $SESSION_ID" \
  -H "X-Atlassian-Jira-Url: https://jira.mgm-tp.com/jira" \
  -H "X-Atlassian-Jira-Personal-Token: YOUR_JIRA_PAT" \
  -d '{
    "jsonrpc": "2.0", "id": 5, "method": "tools/call",
    "params": { "name": "jira_search", "arguments": { "jql": "project in (PROJ, SENSITIVE) AND status = Open" } }
  }' | jq .
```

Expected HTTP 403 — `SENSITIVE` is extracted from the JQL and blocked.

---

## Step 5 — Read the audit log

Audit log entries are emitted as JSON lines on stdout. Tail them from the proxy terminal
or (if running in background) from the output file:

```bash
# If started in background by Claude Code
tail -f /private/tmp/claude-*/tasks/*.output | grep '"decision"' | jq .
```

Example allow entry:
```json
{ "ts": 1741694400.1, "decision": "allow", "tool": "jira_get_issue",
  "reason": "allowed", "user": "jira-pat:...g+doZW", "request_id": "2",
  "args": { "issue_key": "PROJ-1" } }
```

Example deny entry:
```json
{ "ts": 1741694401.3, "decision": "deny", "tool": "jira_create_issue",
  "reason": "Tool 'jira_create_issue' is a write operation; server is in read-only mode.",
  "user": "jira-pat:...g+doZW", "request_id": "3",
  "args": { "project_key": "PROJ", "summary": "Test" } }
```

---

## Step 6 — Attach a debugger (VS Code)

To set breakpoints in `enforcement.py`, `main.py`, etc:

**1. Kill the running proxy and relaunch with debugpy:**
```bash
lsof -ti :8000 | xargs kill -9

cd /path/to/ka-mcp-atlassian/proxy

PROXY_UPSTREAM_URL=http://localhost:8080 \
PROXY_READ_ONLY=true \
PROXY_AUDIT_LOG_ENABLED=true \
uv run python -m debugpy --listen 5678 --wait-for-client \
  -m uvicorn mcp_proxy.main:app \
  --host 127.0.0.1 --port 8000 --reload
```

The process blocks with `Waiting for client to attach...`.

**2. Add `.vscode/launch.json` at repo root:**
```json
{
  "version": "0.2.0",
  "configurations": [
    {
      "name": "Attach to mcp-proxy",
      "type": "debugpy",
      "request": "attach",
      "connect": { "host": "localhost", "port": 5678 },
      "pathMappings": [
        {
          "localRoot": "${workspaceFolder}/proxy/src",
          "remoteRoot": "${workspaceFolder}/proxy/src"
        }
      ]
    }
  ]
}
```

**3. Press F5 in VS Code** → breakpoints in `check_access()`, `_extract_user_identity()`, `proxy_request()` will now hit on every request.

---

## Teardown

```bash
# Stop the proxy (Ctrl+C in its terminal, or:)
lsof -ti :8000 | xargs kill -9

# Stop the mcp-atlassian Docker container
docker stop mcp-atlassian
```
