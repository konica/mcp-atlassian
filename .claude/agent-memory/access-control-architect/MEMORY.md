# Access Control Architect — Persistent Memory

## Key architectural patterns (confirmed from codebase)

### READ_ONLY_MODE enforcement (two independent layers)
- Layer 1: `AtlassianMCP._list_tools_mcp` excludes tools with `"write"` tag from tool list
- Layer 2: `@check_write_access` decorator raises ToolError at call time if `app_lifespan_ctx.read_only`
- Both are in `src/mcp_atlassian/servers/main.py` and `src/mcp_atlassian/utils/decorators.py`
- `is_read_only_mode()` reads `READ_ONLY_MODE` env var via `is_env_extended_truthy`

### Per-user credential routing (already implemented)
- `UserTokenMiddleware` in `servers/main.py` extracts per-request credentials
- Supports: `Authorization: Bearer`, `Authorization: Token`, `Authorization: Basic`, plus
  `X-Atlassian-Jira-Personal-Token` / `X-Atlassian-Jira-Url` service-specific headers
- `get_jira_fetcher` / `get_confluence_fetcher` in `servers/dependencies.py` construct
  per-user JiraFetcher/ConfluenceFetcher from request.state values
- Global config fallback fires if no per-user headers present (lines 658–676 of dependencies.py)

### Projects/spaces filter — advisory only, NOT a security boundary
- `JIRA_PROJECTS_FILTER` checked in only 3/~49 Jira tools: `get_issue` (prefix),
  `search` (JQL append, bypassable), `get_all_projects` (post-filter list)
- `CONFLUENCE_SPACES_FILTER` checked in only 1/~24 Confluence tools: `search` (CQL append, bypassable)
- `_create_user_config_for_fetcher` propagates filter from base_config to per-user configs
- Filter is bypassed by explicit `project=` clauses in JQL/CQL queries (substring-match bypass)
- CRITICAL BUG: `_jira_spec()` header-PAT branch uses `filter_kwargs={"projects_filter": None}` —
  per-user header-PAT configs get NO project filter. Must fix before deploying enforcement.
- Full enforcement plan (Solution 2): `solutions/ds-2031-access-control-plan-full-fork-enforcement.md`

### Tool tagging convention
- Tags: `{"jira"/"confluence", "read"/"write", "toolset:<name>"}`
- All write tools must have `@check_write_access` + `"write"` tag
- Toolset names defined in `utils/toolsets.py`; 15 Jira + 6 Confluence toolsets

### Auth type priority (Server/DC): PAT > OAuth > Basic
- See `JiraConfig.from_env()` and `ConfluenceConfig.from_env()`
- `_resolve_bearer_auth_type` disambiguates Bearer tokens as OAuth vs PAT based on global config

### Token validation on every new per-user fetcher
- `_create_and_validate` calls validate_fn (Jira: `get_current_user_account_id`, Confluence: `get_current_user_info`)
- SSRF protection: `validate_url_for_ssrf` applied via response hook when `attach_ssrf_hook=True`

## Important file paths

| Concern | File |
|---------|------|
| Middleware / per-user auth extraction | `src/mcp_atlassian/servers/main.py` |
| Fetcher dependency resolution | `src/mcp_atlassian/servers/dependencies.py` |
| Server lifespan / MainAppContext | `src/mcp_atlassian/servers/context.py` |
| READ_ONLY_MODE check utility | `src/mcp_atlassian/utils/io.py` |
| Write guard + error decorators | `src/mcp_atlassian/utils/decorators.py` |
| JiraConfig (auth, filter, from_env) | `src/mcp_atlassian/jira/config.py` |
| ConfluenceConfig | `src/mcp_atlassian/confluence/config.py` |
| Toolset definitions | `src/mcp_atlassian/utils/toolsets.py` |
| Tool enable/disable | `src/mcp_atlassian/utils/tools.py` |
| Solutions context | `solutions/ds-2031-access-control.md` |

## Security-sensitive env vars

- `READ_ONLY_MODE` — must be `true` for read-only AI deployments
- `IGNORE_HEADER_AUTH` — must NOT be `true` (disables per-user auth)
- `JIRA_PERSONAL_TOKEN` / `CONFLUENCE_PERSONAL_TOKEN` — global fallback credentials; scope carefully
- `JIRA_PROJECTS_FILTER` / `CONFLUENCE_SPACES_FILTER` — advisory only, not a security boundary
- `TOOLSETS` — can limit exposed tools as a secondary hardening measure

## Access control decisions recorded

- DS-2031 Solution 1 (decorator RBAC) full plan: `solutions/ds-2031-access-control-plan-decorator-rbac.md`
- DS-2031 Solution 2 (full fork enforcement) full plan: `solutions/ds-2031-access-control-plan-full-fork-enforcement.md`
- DS-2031 Solution 3 analysis: see `solutions/ds-2031-access-control-plan-pat-only.md`
- Solution 3 is acceptable ONLY when: per-user PAT enforced + READ_ONLY_MODE=true + Atlassian permissions correctly scoped
- Global fallback in `dependencies.py` is the primary risk vector for Solution 3

## DS-2031 Solution 4 (Proxy/Gateway) Key Decisions

- Plan file: `solutions/ds-2031-access-control-plan-proxy-gateway.md`
- Only works with `streamable-http` or `sse` transport (NOT stdio)
- Proxy parses JSON-RPC body; only `method=tools/call` is subject to enforcement
- Tools with no project-scoped args pass through when whitelist is active (known limitation)
- JQL extraction: regex-based only; not a full AST parser; catches common patterns
- Body re-injection: read full body → enforce → create new `Request` with patched `receive`
- Streaming: `httpx.AsyncClient` with `stream=True` + Starlette `StreamingResponse`
- Network segmentation critical: mcp-atlassian port must be internal-only (no published ports)
- `.mcp.json` change required: `command/args` (stdio) → `type/url/headers` (HTTP)
- Two-layer read-only recommended: `PROXY_READ_ONLY=true` AND `READ_ONLY_MODE=true` upstream
- Audit log: structured JSON per decision on `mcp-proxy.audit` logger
- Docker Compose: `docker-compose.proxy.yml` overlay (separate from e2e test compose)
- Proxy in `proxy/` subdir with own `pyproject.toml`; deps: fastapi, httpx, uvicorn, pydantic-settings
- Error response format: JSON-RPC error with code `-32001` and HTTP 403

## DS-2031 Solution 1 Key Decisions

- JQL/CQL bypass: existing substring check (`"project = " not in jql.lower()`) bypassed by crafted OR predicates
- Fix: unconditionally append `AND project IN (...)` — always wrap original JQL without inspecting it
- New decorators: `enforce_project_access(issue_key_param=, project_key_param=, issue_keys_param=)`
  and `enforce_space_access(space_key_param=, page_space_resolver=)` in `utils/decorators.py`
- New helpers: `build_jql_allowlist_clause`, `build_cql_allowlist_clause`, `assert_space_allowed`
  in `utils/access_control.py` (new file)
- Allowlists stored as frozenset[str] in `MainAppContext` (upper-cased at startup)
- Coverage sentinel: `"access_controlled"` tag on every tool; test asserts 100% coverage
- Board/sprint tools (board_id, sprint_id) cannot pre-validate project — known gap
- Confluence page_id-only tools need post-fetch validation via `assert_space_allowed`
- Decorator stack order: `@enforce_project_access` → `@check_write_access` → tool body

## DS-2031 Solution 2 Key Decisions

- JQL/CQL bypass fix: use regex rewrite to strip ALL caller-supplied project/space clauses
  and replace with whitelist-derived clause (not just append)
- New module: `utils/access_control.py` — parse_filter_set, check_project_access,
  check_space_access, rewrite_jql_project_clause, rewrite_cql_space_clause
- New module: `utils/audit.py` — structured JSON audit log on `mcp-atlassian.audit` logger
- New exception: `MCPAtlassianAccessDeniedError` in `exceptions.py`
- Decorator stack order: `@check_write_access` (outermost) → `@enforce_project_filter` → tool body
- Header-PAT filter bug fix required in `_jira_spec()` filter_kwargs before deployment
- Upstream rebase strategy: minimize diff to utils/ files + two search.py files only
- Board/sprint tools: gap for sprint/board ID pre-validation; search results filtered via JQL rewrite
