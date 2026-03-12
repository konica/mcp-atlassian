# Troubleshooting mcp-atlassian: Streamable HTTP Transport

Lessons learned connecting Claude Code and curl to mcp-atlassian over
`streamable-http` transport.

---

## Quick verification

```bash
# 1. Server alive?
curl -s http://localhost:8080/healthz
# → {"status":"ok"}

# 2. Full tools/list via script
./scripts/inspect_mcp_http.sh

# 3. Call a specific tool
./scripts/inspect_mcp_http.sh tools/call jira_search '{"query":"project = DS"}'
```

---

## Starting the server

```bash
# Minimal — no Atlassian credentials baked in, credentials come via headers
uv run mcp-atlassian --transport streamable-http --port 8080

# With Docker
docker run --rm -p 8080:8080 \
  -e TRANSPORT=streamable-http \
  -e PORT=8080 \
  -e MCP_ALLOWED_URL_DOMAINS=jira.example.com,wiki.example.com \
  ka-mcp-atlassian
```

**What the startup logs should say** when no credentials are configured:
```
INFO - Confluence is not configured or required environment variables are missing.
INFO - Jira is not configured or required environment variables are missing.
```
This is expected — tools are enabled per-request based on the headers received.

---

## Problem 1 — `HTTP 401` → OAuth discovery loop → `HTTP 404: Invalid OAuth error response`

### Symptom
Claude Code or the MCP SDK reports:
```
HTTP 404: Invalid OAuth error response:
  SyntaxError: JSON Parse error: Unexpected identifier "Not". Raw body: Not Found
```

Server logs show this cascade:
```
POST /mcp                                    → 401 Unauthorized
GET  /.well-known/oauth-authorization-server → 404 Not Found
GET  /.well-known/openid-configuration       → 404 Not Found
GET  /.well-known/oauth-protected-resource   → 404 Not Found
POST /register                               → 404 Not Found
```

### Root cause
The server returned **401** on the first `POST /mcp`. The MCP SDK interprets any
non-2xx response as an authentication challenge and tries OAuth discovery. All the
`/.well-known/*` endpoints return 404 (OAuth proxy not enabled), so the SDK throws
the misleading "Invalid OAuth error response" error.

The 401 itself is caused by the **SSRF protection** in `servers/main.py:524-531`:
when `X-Atlassian-Jira-Url` or `X-Atlassian-Confluence-Url` resolves to a private
IP address, the middleware rejects the request before processing auth.

```
POST /mcp (with X-Atlassian-Jira-Url: https://jira.internal.com)
  │
  ▼
UserTokenMiddleware._process_authentication_headers()
  │  validate_url_for_ssrf("https://jira.internal.com")
  │  → "DNS for jira.internal.com resolves to non-global IP: 172.24.5.x"
  │
  ▼
scope["state"]["auth_validation_error"] = "Forbidden: Invalid Jira URL - ..."
  │
  ▼
HTTP 401 Unauthorized  ← triggers OAuth discovery in the MCP SDK
```

### Fix
Add the internal domain(s) to `MCP_ALLOWED_URL_DOMAINS` in `.env`:

```bash
MCP_ALLOWED_URL_DOMAINS=jira.example.com,wiki.example.com
```

Allowlisted domains bypass DNS resolution checks. Restart the server after
changing `.env`.

**Verify the fix:**
```bash
curl -s -X POST http://localhost:8080/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "X-Atlassian-Jira-Url: https://jira.example.com/jira" \
  -H "X-Atlassian-Jira-Personal-Token: YOUR_PAT" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"test","version":"1.0"}}}'
# → event: message\ndata: {"jsonrpc":"2.0","id":1,"result":{...}}
```

---

## Problem 2 — MCP Inspector: `Connection failed: TypeError: Load failed`

### Symptom
Opening the MCP Inspector UI (`http://localhost:6274`), entering the server URL
manually, and clicking Connect fails immediately.

### Root cause
When a URL is entered manually in the Inspector UI, the **browser** makes a
direct `fetch()` to `http://localhost:8080/mcp`. The mcp-atlassian server does not
return `Access-Control-Allow-Origin` headers, so the browser blocks the response
(CORS policy violation). `fetch()` throws `TypeError: Load failed` (Safari) or
`TypeError: Failed to fetch` (Chrome).

### Fix
Start the Inspector with `--transport` and `--server-url` flags. This routes all
MCP traffic through the Inspector's **Node.js proxy** (port 6277) instead of the
browser, bypassing CORS entirely:

```bash
npx @modelcontextprotocol/inspector \
  --transport http \
  --server-url http://localhost:8080/mcp \
  --header "X-Atlassian-Jira-Url: https://jira.example.com/jira" \
  --header "X-Atlassian-Jira-Personal-Token: YOUR_PAT" \
  --header "X-Atlassian-Confluence-Url: https://wiki.example.com/confluence" \
  --header "X-Atlassian-Confluence-Personal-Token: YOUR_PAT"
```

Then open the printed URL and click **Connect** — no manual configuration needed
in the UI.

---

## Problem 3 — Server loads `.env` even when you don't want it to

### Symptom
Server picks up credentials from the project's `.env` even when run from a
different directory or with `env -i`.

### Root cause
`load_dotenv()` (called in `__init__.py:313`) walks up the directory tree from
`cwd` looking for `.env`. Running `uv run --project /path/to/project` does not
change `cwd`, but `uv` itself may change the working directory to the project
root before invoking the command, depending on the version.

### Fix
Pass an explicit empty env file to prevent `.env` loading:

```bash
uv run mcp-atlassian --transport streamable-http --port 8080 --env-file /dev/null
```

**Confirm** no credentials were loaded by checking startup logs:
```
INFO - Confluence is not configured or required environment variables are missing.
INFO - Jira is not configured or required environment variables are missing.
```

---

## Problem 4 — `tools/list` returns 0 tools

### Symptom
`tools/list` succeeds (200 OK) but `result.tools` is empty.

### Causes and checks

| Cause | How to check | Fix |
|---|---|---|
| Missing `X-Atlassian-*` headers | Verify headers are sent on the same request as `tools/list` | Add headers to every request |
| SSRF block (silent) | Check server log for `Forbidden: Invalid Jira URL` | Add domain to `MCP_ALLOWED_URL_DOMAINS` |
| `mcp-session-id` missing | Omitting session ID creates a new unauthenticated session | Always send `mcp-session-id` from `initialize` response |
| `TOOLSETS` filter too narrow | Check `TOOLSETS` env var | Set `TOOLSETS=all` or unset it |

---

## Problem 5 — curl `jq` parse error on tools/list response

### Symptom
```
jq: parse error: Invalid numeric literal at line 1, column 6
```

### Root cause
The streamable-http response is SSE format:
```
event: message
data: {"jsonrpc":"2.0",...}

```
Piping the raw response to `jq` fails because `event: message` is not valid JSON.

### Fix
Filter to `data:` lines before passing to `jq`:
```bash
curl ... | grep "^data:" | sed 's/^data: //' | jq '.'
```

---

## Streamable HTTP: mandatory two-step flow

Unlike stdio, streamable-http is **stateful**. Every session requires:

```bash
# Step 1 — initialize, capture session ID from response headers
SESSION=$(curl -s -X POST http://localhost:8080/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "X-Atlassian-Jira-Url: $JIRA_URL" \
  -H "X-Atlassian-Jira-Personal-Token: $JIRA_TOKEN" \
  -H "X-Atlassian-Confluence-Url: $CONF_URL" \
  -H "X-Atlassian-Confluence-Personal-Token: $CONF_TOKEN" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"test","version":"1.0"}}}' \
  -D /tmp/mcp_headers.txt && \
  grep -i mcp-session-id /tmp/mcp_headers.txt | awk '{print $2}' | tr -d '\r')

# Step 2 — use session ID on all subsequent requests
curl -s -X POST http://localhost:8080/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "mcp-session-id: $SESSION" \
  -H "X-Atlassian-Jira-Url: $JIRA_URL" \
  -H "X-Atlassian-Jira-Personal-Token: $JIRA_TOKEN" \
  -H "X-Atlassian-Confluence-Url: $CONF_URL" \
  -H "X-Atlassian-Confluence-Personal-Token: $CONF_TOKEN" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}' \
  | grep "^data:" | sed 's/^data: //' | jq '.result.tools[].name'
```

The `X-Atlassian-*` headers must be repeated on **every request** — the
middleware processes them independently each time.
