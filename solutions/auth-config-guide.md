# mcp-atlassian: Authentication Configuration Guide

How to pass credentials to the mcp-atlassian server — via environment variables,
CLI flags, or HTTP request headers — and how those values flow through the code.

---

## 1. Entry points

The server starts from `src/mcp_atlassian/__init__.py:main()` (registered as the
`mcp-atlassian` CLI entry point in `pyproject.toml`).

Configuration is read in this order of precedence (highest wins):

```
CLI flags  >  Environment variables  >  defaults
```

CLI flags are immediately written to `os.environ` (`__init__.py:378-415`) so that
the downstream config factories see them as env vars.

---

## 2. Method A — Environment variables (all transports)

### Mapping: env var → config field → code

| Env var | Config field | Read at |
|---|---|---|
| `JIRA_URL` | `JiraConfig.url` | `jira/config.py:168` |
| `JIRA_USERNAME` | `JiraConfig.username` | `jira/config.py:178` |
| `JIRA_API_TOKEN` | `JiraConfig.api_token` | `jira/config.py:179` |
| `JIRA_PERSONAL_TOKEN` | `JiraConfig.personal_token` | `jira/config.py:180` |
| `JIRA_SSL_VERIFY` | `JiraConfig.ssl_verify` | `jira/config.py:236` |
| `CONFLUENCE_URL` | `ConfluenceConfig.url` | `confluence/config.py:92` |
| `CONFLUENCE_USERNAME` | `ConfluenceConfig.username` | `confluence/config.py:102` |
| `CONFLUENCE_API_TOKEN` | `ConfluenceConfig.api_token` | `confluence/config.py:103` |
| `CONFLUENCE_PERSONAL_TOKEN` | `ConfluenceConfig.personal_token` | `confluence/config.py:104` |
| `CONFLUENCE_SSL_VERIFY` | `ConfluenceConfig.ssl_verify` | `confluence/config.py:163` |

### Auth-type auto-detection (Server/DC example)

`JiraConfig.from_env()` checks the URL first to decide Cloud vs Server/DC, then
picks an auth type by priority:

```
is_cloud = is_atlassian_cloud_url(JIRA_URL)   # jira/config.py:187

Server/DC priority:
  1. JIRA_PERSONAL_TOKEN set  →  auth_type = "pat"   (line 220)
  2. OAuth env vars set       →  auth_type = "oauth" (line 221)
  3. USERNAME + API_TOKEN set →  auth_type = "basic" (line 223)
  4. nothing                  →  ValueError           (line 226)

Cloud priority:
  1. OAuth env vars set       →  auth_type = "oauth" (line 191)
  2. USERNAME + API_TOKEN set →  auth_type = "basic" (line 193)
  3. nothing                  →  ValueError           (line 196)
```

### Flow

```
Docker -e JIRA_URL=...
         JIRA_PERSONAL_TOKEN=...
         JIRA_SSL_VERIFY=false
    │
    ▼
JiraConfig.from_env()                    [jira/config.py:159]
    │  os.getenv("JIRA_URL")             → config.url
    │  os.getenv("JIRA_PERSONAL_TOKEN")  → config.personal_token
    │  is_env_ssl_verify("JIRA_SSL_VERIFY") → config.ssl_verify = False
    │  auth_type = "pat"
    ▼
JiraFetcher(config=config)               [servers/jira.py]
    │  builds requests.Session with
    │  Authorization: Bearer <personal_token>
    ▼
Jira API calls
```

### Equivalent CLI flags (same result, no -e flags needed)

```bash
mcp-atlassian \
  --jira-url https://jira.example.com \
  --jira-personal-token YOUR_PAT \
  --no-jira-ssl-verify \
  --confluence-url https://wiki.example.com \
  --confluence-personal-token YOUR_PAT \
  --no-confluence-ssl-verify
```

In Docker `.mcp.json` (stdio transport):

```json
{
  "mcpServers": {
    "mcp-atlassian": {
      "command": "docker",
      "args": [
        "run", "--rm", "-i",
        "ka-mcp-atlassian",
        "--jira-url", "https://jira.example.com",
        "--jira-personal-token", "YOUR_PAT",
        "--no-jira-ssl-verify",
        "--confluence-url", "https://wiki.example.com",
        "--confluence-personal-token", "YOUR_PAT",
        "--no-confluence-ssl-verify"
      ]
    }
  }
}
```

---

## 3. Method B — Per-request HTTP headers (HTTP transport only)

Requires the server to run with `TRANSPORT=sse` or `TRANSPORT=streamable-http`.
Credentials are sent by the MCP client on every request. **No Atlassian env vars
are needed on the server at all** — not even `ATLASSIAN_OAUTH_ENABLE`.

### Supported headers

| HTTP header | Replaces env var | Notes |
|---|---|---|
| `X-Atlassian-Jira-Url` | `JIRA_URL` | SSRF-validated |
| `X-Atlassian-Jira-Personal-Token` | `JIRA_PERSONAL_TOKEN` | PAT only |
| `X-Atlassian-Confluence-Url` | `CONFLUENCE_URL` | SSRF-validated |
| `X-Atlassian-Confluence-Personal-Token` | `CONFLUENCE_PERSONAL_TOKEN` | PAT only |

> **Limitation:** `ssl_verify` is always `True` in this path — it cannot be
> overridden via headers (`dependencies.py:549`).

### Flow

```
Server startup — no JIRA_URL / CONFLUENCE_URL env vars set
    │
    ▼
main_lifespan()                          [servers/main.py:122]
    │  get_available_services() → {jira: False, confluence: False}
    │  loaded_jira_config = None
    │  loaded_confluence_config = None
    │  server starts and waits for requests
    │
    ▼ (per request)

MCP client HTTP POST /mcp
  Headers:
    X-Atlassian-Jira-Url: https://jira.example.com
    X-Atlassian-Jira-Personal-Token: YOUR_PAT
    X-Atlassian-Confluence-Url: https://wiki.example.com
    X-Atlassian-Confluence-Personal-Token: YOUR_PAT
    │
    ▼
UserTokenMiddleware.__call__()           [servers/main.py:386]
    │
    ├─ _process_authentication_headers() [servers/main.py:484]
    │     reads b"x-atlassian-jira-url"            → jira_url_str
    │     reads b"x-atlassian-jira-personal-token" → jira_token_str
    │     validates URLs for SSRF                   [utils/urls.py]
    │     scope["state"]["atlassian_service_headers"] = {
    │         "X-Atlassian-Jira-Url": jira_url_str,
    │         "X-Atlassian-Jira-Personal-Token": jira_token_str, ...
    │     }
    │     scope["state"]["user_atlassian_auth_type"] = "pat"  (line 591)
    │
    ▼ (tools/list request)
AtlassianMCP._list_tools_mcp()          [servers/main.py:212]
    │  service_headers = request.state.atlassian_service_headers
    │  header_based_services = get_available_services(service_headers)
    │  → {jira: True, confluence: True}           (utils/environment.py:162-168)
    │
    │  jira_available = (full_jira_config is not None)   # False (no env config)
    │                   OR header_based_services["jira"]  # True  (line 289)
    │  → jira_available = True → Jira tools included in response
    │
    ▼ (tool call request)
get_jira_fetcher(ctx)                    [servers/dependencies.py:679]
    │
    └─ _get_fetcher(ctx, _jira_spec())   [servers/dependencies.py:502]
          url_header_val   = service_headers["X-Atlassian-Jira-Url"]
          token_header_val = service_headers["X-Atlassian-Jira-Personal-Token"]
          user_auth_type == "pat"  →  Branch 1 (line 533)
          │
          header_config = JiraConfig(
              url=url_header_val,
              auth_type="pat",
              personal_token=token_header_val,
              ssl_verify=True,              # always True for header auth
          )
          │
          ▼
          JiraFetcher(config=header_config)
              │  validates token by calling get_current_user_account_id()
              ▼
          cached on request.state.jira_fetcher for this request
```

### Server-side setup (no credentials baked in)

```bash
docker run --rm -p 8080:8080 \
  -e TRANSPORT=streamable-http \
  -e PORT=8080 \
  ka-mcp-atlassian
```

No Atlassian credentials needed. The server starts with no Jira/Confluence config
and enables tools dynamically per-request based on the headers received.

### Client `.mcp.json` for streamable-http transport

```json
{
  "mcpServers": {
    "mcp-atlassian": {
      "type": "streamable-http",
      "url": "http://localhost:8080/mcp",
      "headers": {
        "X-Atlassian-Jira-Url": "https://jira.example.com",
        "X-Atlassian-Jira-Personal-Token": "YOUR_PAT",
        "X-Atlassian-Confluence-Url": "https://wiki.example.com",
        "X-Atlassian-Confluence-Personal-Token": "YOUR_PAT"
      }
    }
  }
}
```

---

## 4. Method C — Authorization header with global URL env vars

A hybrid: the server is started with URL and SSL settings as env vars, but the
token is injected per-request. Useful for multi-user deployments where everyone
shares the same Atlassian instance.

```
Server env vars:  JIRA_URL, JIRA_SSL_VERIFY, CONFLUENCE_URL, CONFLUENCE_SSL_VERIFY
Per-request:      Authorization: Token <PAT>
```

Flow:

```
Authorization: Token YOUR_PAT
    │
    ▼
UserTokenMiddleware._parse_auth_header()  [servers/main.py:601]
    │  "Token " prefix  →  auth_type = "pat"
    │  scope["state"]["user_atlassian_token"] = token
    │  scope["state"]["user_atlassian_auth_type"] = "pat"
    │
    ▼
_get_fetcher()  →  Branch 3 (line 596)
    │  global_config = JiraConfig loaded from env (url, ssl_verify, proxy, etc.)
    │  resolved_auth_type = "pat"   (via _resolve_bearer_auth_type)
    │  user_config = dataclasses.replace(global_config,
    │      auth_type="pat",
    │      personal_token=token,
    │  )
    ▼
JiraFetcher(config=user_config)
    │  inherits url, ssl_verify, proxies from global_config
    │  uses per-request token for auth
    ▼
Jira API calls
```

---

## 5. Decision guide

```
Do you need to disable SSL verification (self-signed certs)?
  YES → Use Method A (env vars or CLI flags). Headers cannot disable SSL.

Single user, simple setup?
  stdio transport  → Method A: env vars or CLI flags in .mcp.json args
  HTTP transport   → Method A or C

Multi-user or per-request different credentials?
  HTTP transport   → Method B: X-Atlassian-* headers per request

Multi-user, same Atlassian instance, different tokens?
  HTTP transport   → Method C: Authorization header + URL in server env vars
```

---

## 6. Key source files

| File | Role |
|---|---|
| `src/mcp_atlassian/__init__.py:235` | CLI entry, maps flags → env vars, starts server |
| `src/mcp_atlassian/jira/config.py:159` | `JiraConfig.from_env()` — reads all Jira env vars |
| `src/mcp_atlassian/confluence/config.py:83` | `ConfluenceConfig.from_env()` — reads all Confluence env vars |
| `src/mcp_atlassian/servers/main.py:484` | `_process_authentication_headers()` — parses X-Atlassian-* headers |
| `src/mcp_atlassian/servers/main.py:601` | `_parse_auth_header()` — parses Authorization header |
| `src/mcp_atlassian/servers/dependencies.py:502` | `_get_fetcher()` — selects auth branch, builds fetcher |
| `src/mcp_atlassian/servers/dependencies.py:350` | `_create_user_config_for_fetcher()` — clones global config with per-request creds |
| `src/mcp_atlassian/utils/environment.py:86` | `get_available_services()` — checks which services are configured at startup |
