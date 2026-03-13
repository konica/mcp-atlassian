# MCP Proxy/Gateway — Architecture

> Covers: main request flow, read-only enforcement, filter configuration, and filter validation logic.
> Implementation lives in `proxy/src/mcp_proxy/`.

---

## 1. Component Overview

```
┌──────────────────────────────────────────────────────────────────┐
│  Docker network: mcp-internal                                    │
│                                                                  │
│  ┌────────────────────────────────────────────────────────────┐  │
│  │  mcp-proxy  (port 8000, PUBLIC)                           │  │
│  │                                                            │  │
│  │  FastAPI app  ──►  AccessControl  ──►  httpx proxy        │  │
│  │  main.py           enforcement.py      forward.py         │  │
│  │                    audit.py                               │  │
│  │                    config.py                              │  │
│  └────────────────────────┬───────────────────────────────────┘  │
│                           │ internal HTTP                        │
│  ┌────────────────────────▼───────────────────────────────────┐  │
│  │  mcp-atlassian  (port 8080, INTERNAL ONLY)                │  │
│  │                                                            │  │
│  │  FastMCP + UserTokenMiddleware                            │  │
│  │  Jira tools (50+) / Confluence tools (25+)               │  │
│  └────────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────────┘
         ▲
         │  HTTP POST /mcp  (streamable-http transport)
         │  Authorization: Token <PAT> | Basic <b64> | Bearer <jwt>
MCP Client (Claude Desktop / LibreChat)
```

**Key constraint**: `mcp-atlassian` has **no published ports** in `docker-compose.proxy.yml`. All external traffic must pass through `mcp-proxy`. Direct access to port 8080 is blocked at the Docker network level.

---

## 2. Main Request Flow

Every request from an MCP client hits `proxy_all()` in `main.py`. The flow branches at two decision points:

```
MCP Client
   │
   │  HTTP request (any method, any path)
   ▼
proxy_all()  [main.py]
   │
   ├─ method != POST?
   │       └──► forward_to_upstream()  (pass-through, no enforcement)
   │
   ├─ POST but body is not valid JSON?
   │       └──► forward_to_upstream()  (pass-through)
   │
   ├─ JSON but method != "tools/call"?
   │       └──► forward_to_upstream()  (initialize, notifications, tools/list, etc.)
   │
   └─ POST + JSON + method == "tools/call"
           │
           ├─ extract: tool_name, arguments, request_id
           ├─ extract: user_identity from Authorization header
           │
           ▼
       check_access()  [enforcement.py]
           │
           ├─ DENY  ──►  emit audit log (decision=deny)
           │              return HTTP 403 JSON-RPC error {code: -32001}
           │
           └─ ALLOW ──►  emit audit log (decision=allow)
                          re-inject raw body into patched Request
                          forward_to_upstream()  [forward.py]
                               │
                               └─ httpx.AsyncClient.send(stream=True)
                                  StreamingResponse  (SSE / chunked pass-through)
                                  try/finally: upstream_response.aclose()
```

### MCP protocol messages and their handling

| JSON-RPC `method` | Enforced? | Notes |
|---|---|---|
| `initialize` | No | Session setup — pass-through |
| `notifications/initialized` | No | Client acknowledgement — pass-through |
| `tools/list` | No | Tool discovery — pass-through (server's `READ_ONLY_MODE` hides write tools from listing) |
| `tools/call` | **Yes** | Full enforcement: read-only + whitelist checks |
| `ping` / others | No | Pass-through |

---

## 3. Read-Only Enforcement

### How it works

When `PROXY_READ_ONLY=true`, the proxy blocks any `tools/call` whose tool name is classified as a **write operation** — before the request reaches `mcp-atlassian`.

Classification happens in `is_write_tool()` (`enforcement.py`):

```python
_WRITE_TOOL_PATTERNS = frozenset([
    "create", "update", "delete", "add",
    "edit", "move", "upload", "transition",
    "remove", "link", "reply",
])

_READ_ONLY_OVERRIDES = frozenset([
    "jira_get_link_types",   # contains "link" but is a read operation
])

def is_write_tool(tool_name: str) -> bool:
    if tool_name in _READ_ONLY_OVERRIDES:
        return False                          # explicit override wins
    parts = set(tool_name.lower().split("_"))
    return bool(parts & _WRITE_TOOL_PATTERNS) # any segment matches a pattern
```

**Examples:**

| Tool name | Write? | Reason |
|---|---|---|
| `jira_get_issue` | No | No write segment |
| `jira_create_issue` | Yes | `"create"` segment |
| `jira_add_comment` | Yes | `"add"` segment |
| `jira_remove_watcher` | Yes | `"remove"` segment |
| `jira_transition_issue` | Yes | `"transition"` segment |
| `jira_get_link_types` | No | In `_READ_ONLY_OVERRIDES` |
| `jira_link_to_epic` | Yes | `"link"` segment, not in overrides |
| `confluence_delete_page` | Yes | `"delete"` segment |
| `confluence_search` | No | No write segment |

### Deny response

When a write tool is blocked, the proxy returns a JSON-RPC error that MCP clients understand:

```json
HTTP 403 Forbidden
{
  "jsonrpc": "2.0",
  "id": 42,
  "error": {
    "code": -32001,
    "message": "Tool 'jira_create_issue' is a write operation; server is in read-only mode."
  }
}
```

### Defense-in-depth

The proxy's `PROXY_READ_ONLY` is the **primary** enforcement layer. The upstream server's `READ_ONLY_MODE=true` acts as a **secondary** layer. Both should be enabled in production:

```
Proxy layer:   PROXY_READ_ONLY=true      → blocks at network, before mcp-atlassian
Server layer:  READ_ONLY_MODE=true       → hides write tools from tools/list + blocks at handler
```

If only the proxy enforces read-only, write tools still appear in `tools/list` responses — confusing to users but still safely blocked.

---

## 4. Filter Configuration

Filters are configured entirely via environment variables, parsed by `ProxyConfig` (`config.py`) using Pydantic `BaseSettings` with prefix `PROXY_`.

### Environment variables

| Variable | Type | Default | Description |
|---|---|---|---|
| `PROXY_UPSTREAM_URL` | string | `http://mcp-atlassian:8080` | Base URL of the upstream server |
| `PROXY_LISTEN_HOST` | string | `0.0.0.0` | Interface to bind |
| `PROXY_LISTEN_PORT` | int | `8000` | Port to listen on |
| `PROXY_READ_ONLY` | bool | `false` | Block all write tools |
| `PROXY_JIRA_PROJECTS_WHITELIST` | string | `""` | Comma-separated Jira project keys |
| `PROXY_CONFLUENCE_SPACES_WHITELIST` | string | `""` | Comma-separated Confluence space keys |
| `PROXY_AUDIT_LOG_ENABLED` | bool | `true` | Emit structured JSON audit logs |
| `PROXY_UPSTREAM_CONNECT_TIMEOUT` | float | `10.0` | httpx connect timeout (seconds) |
| `PROXY_UPSTREAM_READ_TIMEOUT` | float | `120.0` | httpx read timeout (seconds) |

### Filter semantics

| `PROXY_JIRA_PROJECTS_WHITELIST` value | Effect |
|---|---|
| `""` (empty) | Allow all Jira projects (no filter applied) |
| `"PROJ"` | Allow only project `PROJ`; deny all others |
| `"PROJ,DEMO,SUPPORT"` | Allow `PROJ`, `DEMO`, `SUPPORT`; deny everything else |

Same logic applies to `PROXY_CONFLUENCE_SPACES_WHITELIST` for Confluence space keys.

### Parsing

The raw comma-separated string is parsed on every config access via a `@property`:

```python
@property
def jira_projects_set(self) -> frozenset[str]:
    if not self.jira_projects_whitelist.strip():
        return frozenset()              # empty → allow all
    return frozenset(
        k.strip().upper()               # normalise to uppercase
        for k in self.jira_projects_whitelist.split(",")
        if k.strip()                    # skip empty tokens
    )
```

Keys are normalised to **uppercase** — `PROXY_JIRA_PROJECTS_WHITELIST=proj,Demo` is equivalent to `PROJ,DEMO`.

### Config singleton and cache

`get_config()` is decorated with `@lru_cache(maxsize=1)` — the `ProxyConfig` object is built once at startup and reused for all requests. To pick up a changed environment variable without restarting, call `get_config.cache_clear()` (used in tests; not recommended in production — restart the container instead).

---

## 5. Filter Validation Logic

Filter validation runs inside `check_access()` (`enforcement.py`) as the second and third checks, after the read-only check. It only activates when a whitelist is non-empty.

### Argument scanning — Jira

The proxy scans three categories of tool arguments to extract project keys:

```
┌─────────────────────────────────────────────────────────────────┐
│  Tool arguments scanned for Jira project keys                   │
│                                                                 │
│  1. Direct project key args                                     │
│     project_key, project, board_id                             │
│     → value used as-is (uppercased)                            │
│                                                                 │
│  2. Issue key args                                              │
│     issue_key, issue_id, source_issue_key,                     │
│     target_issue_key, parent_issue_key                         │
│     → regex extracts prefix: "PROJ-123" → "PROJ"              │
│                                                                 │
│  3. JQL query args                                              │
│     jql, query                                                  │
│     → regex finds: project = PROJ                              │
│                    project in (PROJ, DEMO)                      │
└─────────────────────────────────────────────────────────────────┘
```

**JQL extraction (best-effort regex):**

```python
_JQL_PROJECT_RE = re.compile(
    r"\bproject\s*(?:=|in\s*\()\s*['\"]?([A-Z][A-Z0-9_,\s'\"]+)['\"]?",
    re.IGNORECASE,
)
```

Matches:
- `project = PROJ`
- `project = "PROJ"`
- `project in (PROJ, DEMO)`
- `project in ('PROJ', 'DEMO')`

Does NOT catch:
- Aliased project references (`issuetype in subtaskIssueTypes()`)
- Saved filters (`filter = 12345`)
- Negation patterns (`project != PROJ`)

### Argument scanning — Confluence

```
space_key, space
→ value used as-is (uppercased)
```

CQL (`cql`, `query` args) is not currently parsed for space references — a known gap documented in the plan.

### Decision table

```
check_access(tool_name, arguments, read_only, jira_whitelist, confluence_whitelist)

Step 1 — Read-only check
  read_only=True AND is_write_tool(tool_name)?
    → DENY: "Tool '...' is a write operation; server is in read-only mode."

Step 2 — Jira whitelist check
  tool starts with "jira_" AND jira_whitelist is non-empty?
    extracted = extract_jira_projects(tool_name, arguments)
    disallowed = extracted - jira_whitelist
    disallowed is non-empty?
      → DENY: "Tool '...' references Jira project(s) ['X'] not in whitelist ['Y']."

Step 3 — Confluence whitelist check
  tool starts with "confluence_" AND confluence_whitelist is non-empty?
    extracted = extract_confluence_spaces(tool_name, arguments)
    disallowed = extracted - confluence_whitelist
    disallowed is non-empty?
      → DENY: "Tool '...' references Confluence space(s) ['X'] not in whitelist ['Y']."

→ ALLOW
```

### Known limitations

| Limitation | Impact | Mitigation |
|---|---|---|
| JQL injection via aliases or saved filters | User can bypass project whitelist with `filter=12345` | Enable per-user PAT so Jira's own ACL is the final gate |
| CQL not parsed for Confluence space | `confluence_search` with cross-space CQL passes through | Combine with `READ_ONLY_MODE` on the server |
| Tools with no project args (`jira_get_user_profile`, `jira_get_agile_boards`) | Cannot be restricted by project whitelist | These tools are instance-scoped by design |
| Response content not inspected | Proxy cannot sanitize cross-project data in tool results | Defence-in-depth with per-user PAT |

---

## 6. Credential & Environment Variable Flow

This section explains how the four values a user currently passes via `-e` flags in stdio mode
are passed through the proxy in streamable-http mode.

### Current model — stdio (`.mcp.json` today)

```
MCP Client (Claude Desktop)
  docker run --rm -i
    -e JIRA_URL=https://jira.mgm-tp.com/jira
    -e JIRA_PERSONAL_TOKEN=NjIzMzQ4...
    -e CONFLUENCE_URL=https://wiki.mgm-tp.com/confluence
    -e CONFLUENCE_PERSONAL_TOKEN=NTk0MzU0...
    ka-mcp-atlassian
```

Each run spawns an ephemeral container. All four values are injected as environment variables
directly into that container by the client. The user owns all four and the server lives only
for that session.

### Proxy model — streamable-http

The proxy does **not** kill the need for users to supply all four values. Instead, the values
move from `-e` flags on `docker run` into **four custom HTTP request headers** — a mechanism
that `mcp-atlassian`'s `UserTokenMiddleware` already supports natively.

```
MCP Client (.mcp.json)
  {
    "type": "streamable-http",
    "url": "http://localhost:8000/mcp",
    "headers": {
      "X-Atlassian-Jira-Url":                    "https://jira.mgm-tp.com/jira",
      "X-Atlassian-Jira-Personal-Token":          "NjIzMzQ4...",
      "X-Atlassian-Confluence-Url":               "https://wiki.mgm-tp.com/confluence",
      "X-Atlassian-Confluence-Personal-Token":    "NTk0MzU0..."
    }
  }
```

These four headers flow through the proxy to mcp-atlassian without any modification:

```
MCP Client
  │  POST /mcp
  │  X-Atlassian-Jira-Url:                 https://jira.mgm-tp.com/jira
  │  X-Atlassian-Jira-Personal-Token:      NjIzMzQ4...
  │  X-Atlassian-Confluence-Url:           https://wiki.mgm-tp.com/confluence
  │  X-Atlassian-Confluence-Personal-Token: NTk0MzU0...
  ▼
mcp-proxy  [forward.py — _forward_headers()]
  │  strips only hop-by-hop headers (connection, transfer-encoding, etc.)
  │  all four X-Atlassian-* headers pass through UNCHANGED
  │
  │  [main.py — _extract_user_identity()]
  │  reads X-Atlassian-Jira-Personal-Token last 8 chars for audit log
  │  → audit: { "user": "jira-pat:...g+doZW", ... }
  ▼
mcp-atlassian  [UserTokenMiddleware — _process_authentication_headers()]
  │  reads all four headers from the ASGI scope:
  │    b"x-atlassian-jira-url"                    → jira_url_str
  │    b"x-atlassian-jira-personal-token"         → jira_token_str
  │    b"x-atlassian-confluence-url"              → confluence_url_str
  │    b"x-atlassian-confluence-personal-token"   → confluence_token_str
  │
  │  validates Jira URL and Confluence URL against SSRF rules
  │  if validation fails → 401 Forbidden: Invalid * URL
  │
  │  populates request.state.atlassian_service_headers = {
  │    "X-Atlassian-Jira-Url": ...,
  │    "X-Atlassian-Jira-Personal-Token": ...,
  │    "X-Atlassian-Confluence-Url": ...,
  │    "X-Atlassian-Confluence-Personal-Token": ...
  │  }
  │  sets request.state.user_atlassian_auth_type = "pat"
  ▼
JiraFetcher / ConfluenceFetcher  [dependencies.py]
  │  builds per-request config from request.state.atlassian_service_headers
  │  uses X-Atlassian-Jira-Url + X-Atlassian-Jira-Personal-Token
  │  to authenticate against that user's specific Jira instance
  ▼
Jira / Confluence API  (each user's own instance)
```

### What each party owns

| Value | Who sets it | How it travels |
|---|---|---|
| `JIRA_URL` | **Each user** | `X-Atlassian-Jira-Url` request header |
| `JIRA_PERSONAL_TOKEN` | **Each user** | `X-Atlassian-Jira-Personal-Token` request header |
| `CONFLUENCE_URL` | **Each user** | `X-Atlassian-Confluence-Url` request header |
| `CONFLUENCE_PERSONAL_TOKEN` | **Each user** | `X-Atlassian-Confluence-Personal-Token` request header |
| `PROXY_READ_ONLY`, `PROXY_*_WHITELIST` | Ops / compose file | Env vars on `mcp-proxy` container — never sent to client |
| `READ_ONLY_MODE` | Ops / compose file | Env var on `mcp-atlassian` container (second defence) |

**Consequence**: because each user provides their own Jira/Confluence URLs, the
`mcp-atlassian` container does **not** need `JIRA_URL` or `CONFLUENCE_URL` env vars at all
for the header-based flow. Those env vars in `docker-compose.proxy.yml` can be removed or
left empty.

### Security properties of this model

| Property | Detail |
|---|---|
| **Credential isolation** | Each request carries its own token — the proxy never holds a shared service account credential |
| **SSRF protection** | `UserTokenMiddleware` calls `validate_url_for_ssrf()` on both URLs before use; invalid URLs are rejected with 401 before any tool runs |
| **Audit identity** | The proxy masks the token to last 8 chars (`jira-pat:...g+doZW`) in audit logs — the full PAT is never stored |
| **No credential injection by proxy** | The proxy only forwards headers; it never adds, replaces, or inspects credential values |
| **Multi-tenant ready** | Two users with different Jira instances can share the same proxy deployment — each request is independently routed by its own `X-Atlassian-Jira-Url` |

### Updated `.mcp.json` for proxy mode

Replace the current stdio config with:

```json
{
  "mcpServers": {
    "mcp-atlassian": {
      "type": "streamable-http",
      "url": "http://localhost:8000/mcp",
      "headers": {
        "X-Atlassian-Jira-Url": "https://jira.mgm-tp.com/jira",
        "X-Atlassian-Jira-Personal-Token": "YOUR_JIRA_PAT",
        "X-Atlassian-Confluence-Url": "https://wiki.mgm-tp.com/confluence",
        "X-Atlassian-Confluence-Personal-Token": "YOUR_CONFLUENCE_PAT"
      }
    }
  }
}
```

The four values the user previously passed as `-e` flags are now headers. Nothing else changes
from the user's perspective — they still own their own credentials and their own instance URLs.

---

## 7. Audit Logging

Every `tools/call` request (allowed or denied) emits a structured JSON line to the `mcp-proxy.audit` logger:

```json
{
  "ts": 1741694400.123,
  "decision": "deny",
  "tool": "jira_create_issue",
  "reason": "Tool 'jira_create_issue' is a write operation; server is in read-only mode.",
  "user": "basic:user@example.com",
  "request_id": "42",
  "args": {
    "project_key": "PROJ",
    "summary": "Fix the bug"
  }
}
```

**Credential safety**: The `Authorization` header is never logged verbatim. `_extract_user_identity()` produces:
- Basic auth → `basic:user@example.com` (email only, no password)
- Bearer token → `bearer:...abcd1234` (last 8 chars only)
- PAT → `pat:...abcd1234` (last 8 chars only)

Sensitive argument keys (`token`, `password`, `secret`, `api_key`, `api_token`, `credential`) are replaced with `[REDACTED]` in `args`.

---

## 8. File Reference

| File | Responsibility |
|---|---|
| `proxy/src/mcp_proxy/main.py` | FastAPI app, lifespan, request routing, audit emit, 403 responses |
| `proxy/src/mcp_proxy/enforcement.py` | `check_access()`, `is_write_tool()`, project/space extraction |
| `proxy/src/mcp_proxy/config.py` | `ProxyConfig` (Pydantic Settings), `get_config()` singleton |
| `proxy/src/mcp_proxy/forward.py` | `proxy_request()` — streaming httpx reverse proxy |
| `proxy/src/mcp_proxy/audit.py` | `emit()`, `_safe_arguments_summary()` |
| `docker-compose.proxy.yml` | Two-service stack; mcp-atlassian internal-only |
| `proxy/Dockerfile` | `python:3.12-slim` + uv build |
| `proxy/pyproject.toml` | Standalone uv project, dev extras for testing |
