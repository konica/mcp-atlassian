# Access Control Analysis: DS-2031 — Solution 4: Proxy/Gateway Layer

> **Scope**: Deep-dive into the proxy/gateway approach for enforcing read-only mode and
> project/space whitelists in front of `ka-mcp-atlassian`.

---

## 1. Existing Solution Summary

### Problem recap

`ka-mcp-atlassian` has two existing guard mechanisms:

| Guard | Location | What it covers |
|---|---|---|
| `READ_ONLY_MODE=true` | `check_write_access` decorator + `_list_tools_mcp` tag filter | Blocks all tools tagged `write` at both list-time and call-time |
| `JIRA_PROJECTS_FILTER` / `CONFLUENCE_SPACES_FILTER` | `JiraConfig.projects_filter` / `ConfluenceConfig.spaces_filter` | Applied by only 3/75 tools (`jira_get_issue`, `jira_search`, `confluence_search`) |

The `READ_ONLY_MODE` guard is solid — it is enforced in two places (tool listing via tags and
`check_write_access` at call-time via `lifespan_context.read_only`). The whitelist filters are
the weak link: they are not applied to the other 72 tools and can be bypassed by crafting JQL
queries with explicit `project=OTHER` clauses.

### Solution 4 definition (from `ds-2031-access-control.md`)

> Run a lightweight HTTP proxy that intercepts all MCP tool calls and enforces whitelists before
> forwarding to `mcp-atlassian`.

This is only viable when the server runs with `--transport streamable-http` (or `sse`). The
`stdio` transport communicates via stdin/stdout and cannot be intercepted by an HTTP proxy.
`ka-mcp-atlassian` fully supports `streamable-http` via `uvicorn` (already a declared
dependency) and the `AtlassianMCP.http_app()` factory.

### How the existing MCP request lifecycle works

```
MCP Client (Claude Desktop / LibreChat)
   │  HTTP POST /mcp  (JSON-RPC: method=tools/call, params={name, arguments})
   ▼
UserTokenMiddleware  ← ASGI middleware in main.py
   │  extracts Bearer/PAT/Basic from Authorization header
   │  populates request.state.{user_atlassian_*}
   ▼
AtlassianMCP._list_tools_mcp / tool handler
   │  uses get_jira_fetcher(ctx) / get_confluence_fetcher(ctx)
   ▼
JiraFetcher / ConfluenceFetcher  ← calls Atlassian REST API
```

An HTTP proxy sits before `UserTokenMiddleware`, intercepting the raw JSON-RPC body.

---

## 2. Approaches Considered

| # | Approach | Summary |
|---|---|---|
| A | **Python FastAPI proxy** (recommended) | Lightweight ASGI app; parses MCP JSON-RPC body; enforces whitelist and read-only; forwards to mcp-atlassian via `httpx`. Runs in same Docker Compose stack. |
| B | **Traefik with middleware plugin** | Traefik as reverse proxy + custom Lua/Go plugin for JSON-RPC parsing. Zero Python, but requires plugin authoring. |
| C | **Kong Gateway** | Enterprise-grade API gateway with plugin ecosystem. Overkill for this use case; significant ops overhead. |
| D | **nginx + njs** | nginx with JavaScript scripting module to parse JSON-RPC bodies. Operational but fragile for streaming responses. |
| E | **ASGI middleware inside mcp-atlassian** | Add a new ASGI middleware class alongside `UserTokenMiddleware` — no separate process. Minimal ops but modifies the codebase. |

**Recommended: Approach A** (Python FastAPI proxy) for the reasons described in Section 4.

---

## 3. Cost Evaluation

### 3.1 Approach A — Python FastAPI Proxy

| Dimension | Assessment |
|---|---|
| **Dev cost** | 2–3 days. Requires Python async HTTP client (`httpx`), JSON-RPC body parsing, streaming proxy support (SSE / chunked transfer). Test coverage for the proxy alone adds ~1 day. Skill required: async Python, ASGI. |
| **DevOps cost** | New Docker image (~60 MB Python slim + fastapi + httpx). One additional service in Docker Compose. No change to K8s Helm chart needed if exposed via same Ingress with path rewrite. Environment variable surface grows by ~8 vars (proxy config). |
| **Maintenance cost** | Low-to-medium. Proxy is independent of upstream mcp-atlassian releases. Must be updated if MCP protocol version changes `tools/call` message schema. Audit log output adds minor storage concern. |
| **Security cost/risk** | Proxy becomes a trust boundary. If bypassed (direct access to mcp-atlassian port), all controls are circumvented — network segmentation is mandatory. Credential passthrough means the proxy sees all Authorization headers; must not log them in plaintext. |

**Pros:**
1. Complete control over enforcement logic in Python — same language as the main project
2. Audit logging, rate limiting, and allowlist in one file, independently deployable
3. Can enforce whitelist on **all** tool calls regardless of which Jira/Confluence endpoints
   the tool internally uses — solves the 72-tool coverage gap without patching each tool
4. mcp-atlassian image stays unmodified — clean separation of concerns
5. Streaming responses (SSE / chunked HTTP) passthrough is trivially handled by `httpx`

**Cons:**
1. Requires `streamable-http` transport — cannot protect `stdio` deployments
2. Additional network hop adds latency (~1–5 ms on loopback)
3. The proxy is a single point of failure; must be kept running
4. Response bodies (tool results) from mcp-atlassian are opaque to the proxy — it can only
   inspect the *request* (tool name + arguments); it cannot sanitize response data that leaks
   cross-project content if the upstream Jira API returns it
5. Streaming `initialize` / `notifications` messages must be passed through untouched

**Key challenges:**
- MCP `streamable-http` uses a session lifecycle: `initialize` → `notifications/initialized` →
  `tools/call`. The proxy must passthrough non-`tools/call` messages transparently.
- SSE response streaming: `httpx.AsyncClient` with `stream()` is required; response must be
  forwarded chunk-by-chunk to avoid buffering the entire response.
- Project key extraction from tool arguments requires knowledge of argument schema per tool
  (e.g., `issue_key` contains the project prefix; `project_key` is explicit; `jql` may embed
  project references). JQL injection attacks require additional sanitization beyond simple
  argument matching.

---

### 3.2 Approach B — Traefik with middleware plugin

| Dimension | Assessment |
|---|---|
| **Dev cost** | 4–6 days. Must author a Traefik plugin (Go or Lua). Go expertise required for body inspection of POST requests. |
| **DevOps cost** | Traefik already common in K8s environments. Plugin distribution requires a separate registry or bundled binary. |
| **Maintenance cost** | High. Traefik plugin API changes between versions. JSON-RPC parsing in Go adds maintenance burden. |
| **Security cost/risk** | Traefik is battle-tested. Plugin sandbox limits blast radius. |

**Pros:** No Python runtime in the proxy path; Traefik handles TLS termination, rate limiting
natively; standard ops tooling.

**Cons:** Go/Lua expertise required; plugin authoring is complex; Traefik not currently in the
stack; streaming response handling is non-trivial in Traefik plugins.

---

### 3.3 Approach C — Kong Gateway

| Dimension | Assessment |
|---|---|
| **Dev cost** | 5–8 days. Kong plugin in Lua or Go. |
| **DevOps cost** | Very high. Kong requires PostgreSQL or Cassandra backend, separate admin API, separate portal. |
| **Maintenance cost** | Very high. Enterprise ops burden. |
| **Security cost/risk** | Kong is production-grade. Extensive audit logging built-in. |

**Pros:** Enterprise-grade, extensive plugin ecosystem, built-in rate limiting and JWT
verification.

**Cons:** Massive ops overhead for what is a single-service access control need; requires
dedicated DB; cost is disproportionate to the problem scope.

---

### 3.4 Approach D — nginx + njs

| Dimension | Assessment |
|---|---|
| **Dev cost** | 3–4 days. JavaScript in `njs` module; awkward async model. |
| **DevOps cost** | nginx is lightweight. njs adds ~5 MB. Helm/Docker changes minimal. |
| **Maintenance cost** | Medium. njs is a non-standard JS dialect with limited library support. |
| **Security cost/risk** | Well-understood ops model but SSE streaming is fragile with nginx `proxy_pass`. |

**Pros:** nginx is universally understood; lightweight image; no Python dependency.

**Cons:** njs lacks streaming-safe JSON body parsing; SSE buffering is a known nginx issue
(`proxy_buffering off` required but unreliable for long-lived connections); fragile for MCP
session state.

---

### 3.5 Approach E — ASGI middleware inside mcp-atlassian

| Dimension | Assessment |
|---|---|
| **Dev cost** | 1–2 days. Add a new `AccessControlMiddleware` class alongside `UserTokenMiddleware`. |
| **DevOps cost** | Zero additional infra. One new file in the codebase. |
| **Maintenance cost** | Medium. Coupled to upstream releases; must be rebased when `main.py` changes. |
| **Security cost/risk** | Cannot be bypassed via network (middleware is in-process). Simplest trust model. |

**Pros:** No new service; no network hop; cannot be circumvented via direct port access;
integrates cleanly with existing lifespan context.

**Cons:** Modifies the mcp-atlassian codebase (acceptable since this is already a fork); harder
to update independently of the server; all logic is in-process so a bug in the middleware can
crash the server.

> **Note**: Approach E is actually the most pragmatic choice for the fork scenario. It is
> documented in the Alternative Approaches section (Section 7) since the task focuses on
> Solution 4 (proxy). If ops complexity is a concern, Approach E should be seriously considered
> as a complement or replacement.

---

## 4. Recommended Approach

**Approach A: Python FastAPI/Starlette proxy** is recommended for the proxy/gateway solution.

**Justification:**
1. Same language and dependency tree as `ka-mcp-atlassian` — no new toolchain
2. `httpx` is already a declared dependency in `pyproject.toml`
3. `starlette` and `uvicorn` are already declared dependencies
4. Trivially containerisable with the existing Alpine-based Python image pattern
5. Independently deployable — mcp-atlassian image can be updated without touching the proxy
6. The enforcement logic (whitelist + read-only) lives in one auditable file

**Architecture:**

```
                    ┌─────────────────────────────────────┐
MCP Client          │  Docker Compose / K8s Pod           │
(Claude Desktop /   │                                     │
 LibreChat)  ──────►│  mcp-proxy:8080                     │
                    │    FastAPI/Starlette app             │
                    │    ├── AccessControlMiddleware       │
                    │    │   ├── parse JSON-RPC body       │
                    │    │   ├── check tool name           │
                    │    │   ├── check read_only mode      │
                    │    │   ├── extract project keys      │
                    │    │   ├── validate against whitelist│
                    │    │   └── write audit log entry     │
                    │    └── httpx reverse proxy ─────────►│  mcp-atlassian:9000
                    │                                     │    (internal only)
                    └─────────────────────────────────────┘
```

**What the proxy enforces:**
1. **Read-only mode** — if `PROXY_READ_ONLY=true`, reject any tool call whose name matches
   write tool patterns (same tag-based logic as mcp-atlassian, but enforced at network layer
   before the request reaches the server)
2. **Project/space whitelist** — extract project keys from tool arguments (`issue_key`,
   `project_key`, `space_key`, `jql`, `cql`) and reject if not in the allowed set
3. **Audit logging** — structured JSON log per tool call: timestamp, user identity
   (from Authorization header), tool name, arguments (sanitized), allow/deny decision

**What the proxy does NOT do:**
- Does not re-authenticate users — it passes the Authorization header through unchanged
- Does not inspect response bodies — only request arguments are validated
- Does not handle JQL/CQL injection fully (see Challenges, Section 6)

---

## 5. Comprehensive Implementation Plan

### Pre-requisites

1. `ka-mcp-atlassian` running with `--transport streamable-http` (not `stdio`)
2. Docker Compose available (or K8s with two containers in same pod)
3. Network policy ensuring `mcp-proxy` port is exposed but `mcp-atlassian` port is NOT
   directly accessible from outside the Docker network / K8s namespace
4. Python ≥ 3.10 and `uv` available for local development of the proxy

**New Python dependencies for the proxy** (separate `pyproject.toml`):
```
fastapi>=0.115.0
httpx>=0.28.0
uvicorn>=0.27.1
pydantic>=2.10.6
python-dotenv>=1.0.1
```

> All are already present in mcp-atlassian's own `pyproject.toml`; the proxy can share the
> same virtual environment or use a minimal standalone one.

---

### Step-by-Step Implementation

#### Step 1 — Create the proxy directory structure

```
proxy/
├── pyproject.toml          # standalone uv project for the proxy
├── Dockerfile              # Alpine Python, same pattern as main Dockerfile
├── src/
│   └── mcp_proxy/
│       ├── __init__.py
│       ├── main.py         # FastAPI app + entry point
│       ├── config.py       # Pydantic Settings for env-based config
│       ├── enforcement.py  # Whitelist + read-only enforcement logic
│       ├── audit.py        # Structured audit logging
│       └── forward.py      # httpx streaming reverse proxy
└── tests/
    ├── unit/
    │   ├── test_enforcement.py
    │   ├── test_audit.py
    │   └── test_config.py
    └── integration/
        └── test_proxy_e2e.py
```

#### Step 2 — Write `proxy/src/mcp_proxy/config.py`

Environment-based configuration using Pydantic `BaseSettings`.

```python
"""Proxy configuration loaded from environment variables."""

from __future__ import annotations

import os
from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ProxyConfig(BaseSettings):
    """Configuration for the MCP access-control proxy.

    All values are loaded from environment variables. Prefix: ``PROXY_``.

    Attributes:
        upstream_url: Full base URL of the upstream mcp-atlassian server.
        listen_host: Host to bind the proxy server to.
        listen_port: Port to bind the proxy server to.
        read_only: If True, reject all write tool calls before forwarding.
        jira_projects_whitelist: Allowed Jira project keys. Empty set = allow all.
        confluence_spaces_whitelist: Allowed Confluence space keys. Empty set = allow all.
        audit_log_enabled: Whether to emit structured audit log entries.
        upstream_connect_timeout: httpx connect timeout in seconds.
        upstream_read_timeout: httpx read timeout in seconds.
    """

    model_config = SettingsConfigDict(env_prefix="PROXY_", env_file=".env")

    upstream_url: str = "http://mcp-atlassian:9000"
    listen_host: str = "0.0.0.0"  # noqa: S104
    listen_port: int = 8080
    read_only: bool = False
    jira_projects_whitelist: str = ""   # comma-separated, e.g. "PROJ,DEMO"
    confluence_spaces_whitelist: str = ""  # comma-separated, e.g. "ENG,HR"
    audit_log_enabled: bool = True
    upstream_connect_timeout: float = 10.0
    upstream_read_timeout: float = 120.0  # tool calls can be slow

    @field_validator("upstream_url")
    @classmethod
    def validate_upstream_url(cls, v: str) -> str:
        """Ensure upstream URL does not have trailing slash."""
        return v.rstrip("/")

    @property
    def jira_projects_set(self) -> frozenset[str]:
        """Parsed set of allowed Jira project keys (upper-cased)."""
        if not self.jira_projects_whitelist.strip():
            return frozenset()
        return frozenset(
            k.strip().upper()
            for k in self.jira_projects_whitelist.split(",")
            if k.strip()
        )

    @property
    def confluence_spaces_set(self) -> frozenset[str]:
        """Parsed set of allowed Confluence space keys (upper-cased)."""
        if not self.confluence_spaces_whitelist.strip():
            return frozenset()
        return frozenset(
            k.strip().upper()
            for k in self.confluence_spaces_whitelist.split(",")
            if k.strip()
        )


@lru_cache(maxsize=1)
def get_config() -> ProxyConfig:
    """Return the singleton proxy configuration.

    Returns:
        Cached ProxyConfig instance.
    """
    return ProxyConfig()
```

#### Step 3 — Write `proxy/src/mcp_proxy/enforcement.py`

Core allow/deny logic. This is the most security-critical file.

```python
"""Access control enforcement for MCP tool calls.

Provides project/space whitelist enforcement and read-only mode blocking.
The proxy intercepts the JSON-RPC ``tools/call`` method body and applies
these rules before forwarding to the upstream mcp-atlassian server.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tool classification
# ---------------------------------------------------------------------------

# Tools whose names include these substrings are considered write operations.
# This mirrors the ``write`` tag logic in mcp-atlassian's _list_tools_mcp.
_WRITE_TOOL_PATTERNS: frozenset[str] = frozenset(
    [
        "create",
        "update",
        "delete",
        "add",
        "edit",
        "move",
        "upload",
        "transition",
        "batch_create",
        "link",
        "reply",
    ]
)

# Arguments that directly name a Jira project key.
_JIRA_PROJECT_KEY_ARGS: frozenset[str] = frozenset(
    ["project_key", "project", "board_id"]
)

# Arguments that contain a Jira issue key (e.g., "PROJ-123").
_JIRA_ISSUE_KEY_ARGS: frozenset[str] = frozenset(
    ["issue_key", "issue_id", "source_issue_key", "target_issue_key", "parent_issue_key"]
)

# Arguments that may contain JQL; the project prefix inside the issue_key is
# used for quick validation. Full JQL AST parsing is out of scope here but
# a best-effort regex is applied.
_JQL_ARGS: frozenset[str] = frozenset(["jql", "query"])

# Arguments that contain Confluence space keys.
_CONFLUENCE_SPACE_KEY_ARGS: frozenset[str] = frozenset(["space_key", "space"])

# Regex: extract Jira project key from an issue key like "PROJ-123".
_ISSUE_KEY_RE = re.compile(r"^([A-Z][A-Z0-9_]+)-\d+$")

# Regex: best-effort extraction of explicit project references in JQL.
# Matches: project = PROJ, project in (PROJ, DEMO), project = "PROJ"
_JQL_PROJECT_RE = re.compile(
    r"\bproject\s*(?:=|in\s*\()\s*['\"]?([A-Z][A-Z0-9_,\s'\"]+)['\"]?",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class EnforcementResult:
    """Result of an access control check.

    Attributes:
        allowed: True if the request should be forwarded.
        reason: Human-readable reason for deny decisions.
        tool_name: Name of the tool being called.
        extracted_projects: Project/space keys extracted from arguments.
    """

    allowed: bool
    reason: str
    tool_name: str
    extracted_projects: frozenset[str] = frozenset()


def is_write_tool(tool_name: str) -> bool:
    """Determine whether a tool name represents a write operation.

    Args:
        tool_name: The MCP tool name, e.g. ``jira_create_issue``.

    Returns:
        True if any write pattern appears as a word segment in the tool name.
    """
    parts = set(tool_name.lower().split("_"))
    return bool(parts & _WRITE_TOOL_PATTERNS)


def _extract_project_from_issue_key(issue_key: str) -> str | None:
    """Extract Jira project key from an issue key string.

    Args:
        issue_key: Issue key like ``PROJ-123``.

    Returns:
        Project key like ``PROJ``, or None if the format does not match.
    """
    m = _ISSUE_KEY_RE.match(issue_key.strip().upper())
    return m.group(1) if m else None


def _extract_projects_from_jql(jql: str) -> frozenset[str]:
    """Extract explicit project references from a JQL query (best-effort).

    This is NOT a full JQL AST parser. It uses a regex to find
    ``project = KEY`` or ``project in (KEY1, KEY2)`` patterns.
    Obfuscated JQL (e.g., via aliases) is not detected.

    Args:
        jql: A JQL query string.

    Returns:
        Set of project key strings found in the query.
    """
    found: set[str] = set()
    for match in _JQL_PROJECT_RE.finditer(jql):
        raw = match.group(1)
        # Split on commas/parens, strip quotes and whitespace
        for part in re.split(r"[,\s()\"']+", raw):
            clean = part.strip().upper()
            if clean and re.match(r"^[A-Z][A-Z0-9_]+$", clean):
                found.add(clean)
    return frozenset(found)


def extract_jira_projects(tool_name: str, arguments: dict[str, Any]) -> frozenset[str]:
    """Extract Jira project keys from a tool call's arguments.

    Args:
        tool_name: The tool name (used for context logging only).
        arguments: The ``arguments`` dict from the MCP ``tools/call`` body.

    Returns:
        Frozenset of project key strings (upper-cased).
    """
    keys: set[str] = set()

    for arg_name in _JIRA_PROJECT_KEY_ARGS:
        val = arguments.get(arg_name)
        if val and isinstance(val, str):
            keys.add(val.strip().upper())

    for arg_name in _JIRA_ISSUE_KEY_ARGS:
        val = arguments.get(arg_name)
        if val and isinstance(val, str):
            project = _extract_project_from_issue_key(val)
            if project:
                keys.add(project)

    for arg_name in _JQL_ARGS:
        val = arguments.get(arg_name)
        if val and isinstance(val, str):
            keys.update(_extract_projects_from_jql(val))

    return frozenset(keys)


def extract_confluence_spaces(
    tool_name: str, arguments: dict[str, Any]
) -> frozenset[str]:
    """Extract Confluence space keys from a tool call's arguments.

    Args:
        tool_name: The tool name (used for context logging only).
        arguments: The ``arguments`` dict from the MCP ``tools/call`` body.

    Returns:
        Frozenset of space key strings (upper-cased).
    """
    keys: set[str] = set()
    for arg_name in _CONFLUENCE_SPACE_KEY_ARGS:
        val = arguments.get(arg_name)
        if val and isinstance(val, str):
            keys.add(val.strip().upper())
    return frozenset(keys)


def check_access(
    tool_name: str,
    arguments: dict[str, Any],
    *,
    read_only: bool,
    jira_whitelist: frozenset[str],
    confluence_whitelist: frozenset[str],
) -> EnforcementResult:
    """Evaluate whether a tool call should be allowed.

    Applies three sequential checks:
    1. Read-only mode: rejects any write tool.
    2. Jira project whitelist: rejects calls referencing out-of-scope projects.
    3. Confluence space whitelist: rejects calls referencing out-of-scope spaces.

    A whitelist that is empty (``frozenset()``) means "allow all" for that
    dimension.

    Args:
        tool_name: The MCP tool name.
        arguments: Parsed tool arguments from the JSON-RPC body.
        read_only: Whether write tools should be blocked.
        jira_whitelist: Allowed Jira project keys; empty = allow all.
        confluence_whitelist: Allowed Confluence space keys; empty = allow all.

    Returns:
        EnforcementResult indicating allow or deny with reason.
    """
    # --- Check 1: read-only mode ---
    if read_only and is_write_tool(tool_name):
        return EnforcementResult(
            allowed=False,
            reason=f"Tool '{tool_name}' is a write operation; server is in read-only mode.",
            tool_name=tool_name,
        )

    # --- Check 2: Jira project whitelist ---
    is_jira_tool = tool_name.startswith("jira_")
    if is_jira_tool and jira_whitelist:
        jira_projects = extract_jira_projects(tool_name, arguments)
        if jira_projects:
            disallowed = jira_projects - jira_whitelist
            if disallowed:
                return EnforcementResult(
                    allowed=False,
                    reason=(
                        f"Tool '{tool_name}' references Jira project(s) "
                        f"{sorted(disallowed)} which are not in the whitelist "
                        f"{sorted(jira_whitelist)}."
                    ),
                    tool_name=tool_name,
                    extracted_projects=jira_projects,
                )

    # --- Check 3: Confluence space whitelist ---
    is_confluence_tool = tool_name.startswith("confluence_")
    if is_confluence_tool and confluence_whitelist:
        spaces = extract_confluence_spaces(tool_name, arguments)
        if spaces:
            disallowed = spaces - confluence_whitelist
            if disallowed:
                return EnforcementResult(
                    allowed=False,
                    reason=(
                        f"Tool '{tool_name}' references Confluence space(s) "
                        f"{sorted(disallowed)} which are not in the whitelist "
                        f"{sorted(confluence_whitelist)}."
                    ),
                    tool_name=tool_name,
                    extracted_projects=spaces,
                )

    return EnforcementResult(
        allowed=True,
        reason="allowed",
        tool_name=tool_name,
    )
```

#### Step 4 — Write `proxy/src/mcp_proxy/audit.py`

```python
"""Structured audit logging for MCP proxy access control decisions."""

from __future__ import annotations

import json
import logging
import time
from typing import Any

logger = logging.getLogger("mcp-proxy.audit")


def emit(
    *,
    decision: str,
    tool_name: str,
    reason: str,
    user_identity: str | None,
    arguments_summary: dict[str, Any] | None = None,
    request_id: str | None = None,
) -> None:
    """Emit a structured audit log entry.

    Output is a single JSON line written to the ``mcp-proxy.audit`` logger.
    Configure handlers externally (e.g., write to file, ship to SIEM).

    Args:
        decision: ``"allow"`` or ``"deny"``.
        tool_name: The MCP tool name that was called.
        reason: Human-readable decision reason.
        user_identity: User email or token fingerprint extracted from request.
        arguments_summary: Sanitized subset of arguments (no secrets).
        request_id: Optional MCP session or request ID for correlation.
    """
    entry: dict[str, Any] = {
        "ts": time.time(),
        "decision": decision,
        "tool": tool_name,
        "reason": reason,
        "user": user_identity or "unknown",
    }
    if request_id:
        entry["request_id"] = request_id
    if arguments_summary:
        entry["args"] = arguments_summary

    logger.info(json.dumps(entry))


def _safe_arguments_summary(arguments: dict[str, Any]) -> dict[str, Any]:
    """Extract a sanitized summary of tool arguments for audit logging.

    Redacts any key whose name suggests it may contain a secret.

    Args:
        arguments: Raw arguments from the MCP tool call.

    Returns:
        Dict with sensitive values replaced by ``"[REDACTED]"``.
    """
    _SENSITIVE_KEYS = frozenset(
        ["token", "password", "secret", "api_key", "api_token", "credential"]
    )
    return {
        k: "[REDACTED]" if any(s in k.lower() for s in _SENSITIVE_KEYS) else v
        for k, v in arguments.items()
    }
```

#### Step 5 — Write `proxy/src/mcp_proxy/forward.py`

Streaming reverse proxy using `httpx`. Must handle both regular HTTP responses and
SSE / chunked streaming responses without buffering.

```python
"""Streaming HTTP reverse proxy for forwarding MCP requests to upstream server."""

from __future__ import annotations

import logging
from typing import AsyncIterator

import httpx
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse

logger = logging.getLogger(__name__)

# Headers that must not be forwarded to upstream (hop-by-hop).
_HOP_BY_HOP = frozenset(
    [
        "connection",
        "keep-alive",
        "transfer-encoding",
        "te",
        "trailers",
        "upgrade",
        "proxy-authorization",
        "proxy-authenticate",
    ]
)


def _forward_headers(request: Request) -> dict[str, str]:
    """Build headers to forward to upstream, excluding hop-by-hop headers.

    Args:
        request: Incoming Starlette request.

    Returns:
        Dict of headers to include in the upstream request.
    """
    return {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in _HOP_BY_HOP
    }


async def proxy_request(
    request: Request,
    upstream_url: str,
    client: httpx.AsyncClient,
) -> Response:
    """Forward a request to the upstream server and stream the response back.

    Handles both regular JSON responses and SSE / chunked streaming responses.
    The request body is read in full before forwarding (required for JSON-RPC
    body inspection by the enforcement layer).

    Args:
        request: The incoming Starlette request (body already consumed by
            the calling middleware and re-injected).
        upstream_url: The full upstream URL (base URL + path).
        client: Shared httpx.AsyncClient instance.

    Returns:
        Starlette Response (streaming if upstream uses SSE/chunked).
    """
    body = await request.body()
    headers = _forward_headers(request)

    try:
        upstream_response = await client.send(
            client.build_request(
                method=request.method,
                url=upstream_url,
                headers=headers,
                content=body,
                params=dict(request.query_params),
            ),
            stream=True,
        )
    except httpx.ConnectError as e:
        logger.error("Upstream connect error: %s", e)
        return Response(
            content='{"error": "Upstream server unavailable"}',
            status_code=502,
            media_type="application/json",
        )
    except httpx.TimeoutException as e:
        logger.error("Upstream timeout: %s", e)
        return Response(
            content='{"error": "Upstream request timed out"}',
            status_code=504,
            media_type="application/json",
        )

    response_headers = {
        k: v
        for k, v in upstream_response.headers.items()
        if k.lower() not in _HOP_BY_HOP
    }

    async def _stream_body() -> AsyncIterator[bytes]:
        async with upstream_response.aiter_bytes(chunk_size=4096) as chunks:
            async for chunk in chunks:
                yield chunk

    return StreamingResponse(
        content=_stream_body(),
        status_code=upstream_response.status_code,
        headers=response_headers,
        media_type=upstream_response.headers.get("content-type"),
    )
```

#### Step 6 — Write `proxy/src/mcp_proxy/main.py`

The FastAPI application with the enforcement middleware.

```python
"""MCP access-control proxy — main application entry point.

This proxy intercepts MCP JSON-RPC ``tools/call`` requests, applies
read-only and project/space whitelist enforcement, emits audit log entries,
and forwards allowed requests to the upstream mcp-atlassian server.

All other MCP protocol messages (initialize, notifications, tools/list, etc.)
are forwarded transparently without modification.

Usage:
    uvicorn mcp_proxy.main:app --host 0.0.0.0 --port 8080
"""

from __future__ import annotations

import base64
import json
import logging
import os
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from mcp_proxy.audit import _safe_arguments_summary, emit
from mcp_proxy.config import get_config
from mcp_proxy.enforcement import check_access
from mcp_proxy.forward import proxy_request

logger = logging.getLogger("mcp-proxy")

_MCP_TOOL_CALL_METHOD = "tools/call"


# ---------------------------------------------------------------------------
# Lifespan: create/destroy the shared httpx client
# ---------------------------------------------------------------------------


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Manage the shared httpx.AsyncClient lifecycle."""
    config = get_config()
    timeout = httpx.Timeout(
        connect=config.upstream_connect_timeout,
        read=config.upstream_read_timeout,
        write=30.0,
        pool=10.0,
    )
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        app.state.http_client = client
        logger.info(
            "MCP proxy started. Upstream: %s | read_only=%s | "
            "jira_whitelist=%s | confluence_whitelist=%s",
            config.upstream_url,
            config.read_only,
            sorted(config.jira_projects_set) or "all",
            sorted(config.confluence_spaces_set) or "all",
        )
        yield
    logger.info("MCP proxy shut down.")


app = FastAPI(title="MCP Access Control Proxy", lifespan=_lifespan)


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------


@app.get("/healthz", include_in_schema=False)
async def health() -> JSONResponse:
    """Liveness probe endpoint."""
    return JSONResponse({"status": "ok"})


# ---------------------------------------------------------------------------
# Helper: extract user identity from Authorization header for audit logs
# ---------------------------------------------------------------------------


def _extract_user_identity(request: Request) -> str | None:
    """Extract a redacted user identifier from the Authorization header.

    Returns the email for Basic auth, or a masked token suffix for Bearer/PAT,
    to enable correlation in audit logs without storing full credentials.

    Args:
        request: The incoming Starlette request.

    Returns:
        A redacted identity string, or None if no auth header is present.
    """
    auth = request.headers.get("authorization", "")
    if not auth:
        return None
    if auth.startswith("Basic "):
        try:
            decoded = base64.b64decode(auth[6:]).decode("utf-8")
            email = decoded.split(":", 1)[0]
            return f"basic:{email}"
        except Exception:
            return "basic:decode-error"
    if auth.startswith("Bearer "):
        token = auth[7:].strip()
        return f"bearer:...{token[-8:]}" if len(token) > 8 else "bearer:<short>"
    if auth.startswith("Token "):
        token = auth[6:].strip()
        return f"pat:...{token[-8:]}" if len(token) > 8 else "pat:<short>"
    return "unknown-auth-type"


# ---------------------------------------------------------------------------
# Main catch-all proxy route
# ---------------------------------------------------------------------------


@app.api_route(
    "/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "OPTIONS", "HEAD", "PATCH"],
)
async def proxy_all(request: Request, path: str) -> Response:
    """Intercept all requests; apply enforcement on tools/call; proxy the rest.

    Only POST requests with a ``tools/call`` JSON-RPC method body are subject
    to enforcement. All other requests are forwarded transparently.

    Args:
        request: The incoming Starlette request.
        path: The URL path (used to build the upstream URL).

    Returns:
        Upstream response, or a 403 JSON-RPC error if enforcement denies.
    """
    config = get_config()
    client: httpx.AsyncClient = request.app.state.http_client
    upstream_url = f"{config.upstream_url}/{path}"

    # Only enforce on POST (MCP uses POST for tool calls in streamable-http)
    if request.method != "POST":
        return await proxy_request(request, upstream_url, client)

    # Read and parse the JSON-RPC body
    raw_body = await request.body()
    rpc_body: dict[str, Any] | None = None

    try:
        rpc_body = json.loads(raw_body)
    except (json.JSONDecodeError, ValueError):
        # Not JSON — pass through (could be a ping / health check POST)
        return await proxy_request(request, upstream_url, client)

    method = rpc_body.get("method") if isinstance(rpc_body, dict) else None

    # Only enforce on tools/call
    if method != _MCP_TOOL_CALL_METHOD:
        return await proxy_request(request, upstream_url, client)

    # Extract tool name and arguments
    params = rpc_body.get("params", {})
    tool_name: str = params.get("name", "") if isinstance(params, dict) else ""
    arguments: dict[str, Any] = (
        params.get("arguments", {}) if isinstance(params, dict) else {}
    )
    request_id = rpc_body.get("id")
    user_identity = _extract_user_identity(request)

    if not tool_name:
        # Malformed tool call — forward and let the server handle it
        return await proxy_request(request, upstream_url, client)

    # Enforcement check
    result = check_access(
        tool_name,
        arguments,
        read_only=config.read_only,
        jira_whitelist=config.jira_projects_set,
        confluence_whitelist=config.confluence_spaces_set,
    )

    if config.audit_log_enabled:
        emit(
            decision="allow" if result.allowed else "deny",
            tool_name=tool_name,
            reason=result.reason,
            user_identity=user_identity,
            arguments_summary=_safe_arguments_summary(arguments),
            request_id=str(request_id) if request_id is not None else None,
        )

    if not result.allowed:
        logger.warning(
            "DENIED tool='%s' user='%s' reason='%s'",
            tool_name,
            user_identity or "unknown",
            result.reason,
        )
        # Return a JSON-RPC error response matching the MCP protocol spec.
        # Error code -32600 = Invalid Request; use -32001 for policy violations.
        error_response = {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {
                "code": -32001,
                "message": result.reason,
            },
        }
        return JSONResponse(
            content=error_response,
            status_code=403,
        )

    # Allowed — forward to upstream with original body
    # Re-attach body so proxy_request can read it
    async def _replay_body() -> bytes:
        return raw_body

    # Starlette does not allow re-reading a consumed body directly.
    # We create a new Request with the same scope but patched receive.
    from starlette.datastructures import Headers
    from starlette.types import Receive, Scope

    async def _receive() -> dict[str, Any]:
        return {"type": "http.request", "body": raw_body, "more_body": False}

    patched_request = Request(request.scope, receive=_receive)
    return await proxy_request(patched_request, upstream_url, client)
```

#### Step 7 — Write `proxy/Dockerfile`

```dockerfile
FROM python:3.12-slim

WORKDIR /app

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

COPY pyproject.toml uv.lock* ./
RUN uv sync --frozen --no-dev

COPY src/ ./src/

ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONUNBUFFERED=1

EXPOSE 8080

CMD ["uvicorn", "mcp_proxy.main:app", "--host", "0.0.0.0", "--port", "8080"]
```

#### Step 8 — Write `docker-compose.proxy.yml`

This is a Docker Compose overlay that adds the proxy in front of mcp-atlassian. It is kept
separate from the main compose file to avoid touching e2e test infrastructure.

```yaml
# docker-compose.proxy.yml
# Usage: docker compose -f docker-compose.proxy.yml up
#
# Exposes the proxy on host port 8080.
# mcp-atlassian is internal-only (no published ports).
#
# Environment variables expected in a .env file:
#   JIRA_URL, JIRA_PERSONAL_TOKEN (or JIRA_API_TOKEN + JIRA_USERNAME)
#   CONFLUENCE_URL, CONFLUENCE_PERSONAL_TOKEN
#   PROXY_JIRA_PROJECTS_WHITELIST=PROJ,DEMO
#   PROXY_CONFLUENCE_SPACES_WHITELIST=ENG,HR
#   PROXY_READ_ONLY=false

services:
  mcp-atlassian:
    image: ka-mcp-atlassian:latest
    build:
      context: .
      dockerfile: Dockerfile
    command:
      - "--transport"
      - "streamable-http"
      - "--port"
      - "9000"
      - "--host"
      - "0.0.0.0"
      - "--path"
      - "/mcp"
    environment:
      JIRA_URL: ${JIRA_URL}
      JIRA_PERSONAL_TOKEN: ${JIRA_PERSONAL_TOKEN:-}
      JIRA_USERNAME: ${JIRA_USERNAME:-}
      JIRA_API_TOKEN: ${JIRA_API_TOKEN:-}
      JIRA_SSL_VERIFY: ${JIRA_SSL_VERIFY:-true}
      CONFLUENCE_URL: ${CONFLUENCE_URL:-}
      CONFLUENCE_PERSONAL_TOKEN: ${CONFLUENCE_PERSONAL_TOKEN:-}
      CONFLUENCE_USERNAME: ${CONFLUENCE_USERNAME:-}
      CONFLUENCE_API_TOKEN: ${CONFLUENCE_API_TOKEN:-}
      CONFLUENCE_SSL_VERIFY: ${CONFLUENCE_SSL_VERIFY:-true}
      # READ_ONLY_MODE on the upstream acts as a second line of defense.
      # The proxy's PROXY_READ_ONLY is the primary enforcement layer.
      READ_ONLY_MODE: ${READ_ONLY_MODE:-false}
    # IMPORTANT: no 'ports' key — this service is internal only.
    networks:
      - mcp-internal
    healthcheck:
      test: ["CMD-SHELL", "wget -qO- http://localhost:9000/healthz || exit 1"]
      interval: 15s
      timeout: 5s
      retries: 5

  mcp-proxy:
    image: ka-mcp-proxy:latest
    build:
      context: proxy/
      dockerfile: Dockerfile
    ports:
      - "8080:8080"
    environment:
      PROXY_UPSTREAM_URL: http://mcp-atlassian:9000
      PROXY_LISTEN_PORT: "8080"
      PROXY_READ_ONLY: ${PROXY_READ_ONLY:-false}
      PROXY_JIRA_PROJECTS_WHITELIST: ${PROXY_JIRA_PROJECTS_WHITELIST:-}
      PROXY_CONFLUENCE_SPACES_WHITELIST: ${PROXY_CONFLUENCE_SPACES_WHITELIST:-}
      PROXY_AUDIT_LOG_ENABLED: "true"
    depends_on:
      mcp-atlassian:
        condition: service_healthy
    networks:
      - mcp-internal
    healthcheck:
      test: ["CMD-SHELL", "wget -qO- http://localhost:8080/healthz || exit 1"]
      interval: 10s
      timeout: 3s
      retries: 3

networks:
  mcp-internal:
    driver: bridge
    internal: false  # set to true in production if proxy is behind an LB
```

#### Step 9 — Update `.mcp.json` for HTTP transport

The current `.mcp.json` uses `docker run ... -i` (stdio transport). For the proxy setup, the
client must connect via HTTP instead. Claude Desktop and LibreChat both support HTTP-based MCP
via the `url` key:

```json
{
  "mcpServers": {
    "mcp-atlassian": {
      "type": "streamable-http",
      "url": "http://localhost:8080/mcp",
      "headers": {
        "Authorization": "Token YOUR_PERSONAL_ACCESS_TOKEN"
      }
    }
  }
}
```

For LibreChat, the equivalent in `librechat.yaml`:
```yaml
mcpServers:
  mcp-atlassian:
    type: streamable-http
    url: http://mcp-proxy:8080/mcp
```

> **Important**: The MCP client must send credentials via the `Authorization` header. The proxy
> passes this header through unchanged to mcp-atlassian. The proxy uses the header only for
> audit log identity extraction; it does NOT validate the credentials itself.

#### Step 10 — Add `proxy/pyproject.toml`

```toml
[project]
name = "mcp-proxy"
version = "0.1.0"
requires-python = ">=3.10"
dependencies = [
    "fastapi>=0.115.0",
    "httpx>=0.28.0",
    "uvicorn[standard]>=0.27.1",
    "pydantic>=2.10.6,<3.0",
    "pydantic-settings>=2.0.0",
    "python-dotenv>=1.0.1",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0.0",
    "pytest-anyio>=0.0.0",
    "anyio>=4.0.0",
    "httpx>=0.28.0",
    "pytest-cov>=4.0.0",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"
```

---

### Testing Strategy

#### Unit tests — `proxy/tests/unit/test_enforcement.py`

```python
"""Unit tests for MCP proxy enforcement logic."""

import pytest
from mcp_proxy.enforcement import (
    EnforcementResult,
    check_access,
    extract_confluence_spaces,
    extract_jira_projects,
    is_write_tool,
)


class TestIsWriteTool:
    """Tests for write-tool classification."""

    def test_create_issue_is_write(self) -> None:
        assert is_write_tool("jira_create_issue") is True

    def test_get_issue_is_not_write(self) -> None:
        assert is_write_tool("jira_get_issue") is False

    def test_delete_page_is_write(self) -> None:
        assert is_write_tool("confluence_delete_page") is True

    def test_search_is_not_write(self) -> None:
        assert is_write_tool("jira_search") is False

    def test_add_comment_is_write(self) -> None:
        assert is_write_tool("jira_add_comment") is True

    def test_batch_create_is_write(self) -> None:
        assert is_write_tool("jira_batch_create_issues") is True


class TestExtractJiraProjects:
    """Tests for Jira project key extraction."""

    def test_extracts_from_issue_key(self) -> None:
        result = extract_jira_projects("jira_get_issue", {"issue_key": "PROJ-123"})
        assert result == frozenset(["PROJ"])

    def test_extracts_from_project_key_arg(self) -> None:
        result = extract_jira_projects(
            "jira_create_issue", {"project_key": "DEMO"}
        )
        assert result == frozenset(["DEMO"])

    def test_extracts_from_jql(self) -> None:
        result = extract_jira_projects(
            "jira_search", {"jql": "project = PROJ AND status = Open"}
        )
        assert "PROJ" in result

    def test_extracts_multiple_from_jql_in_clause(self) -> None:
        result = extract_jira_projects(
            "jira_search", {"jql": "project in (PROJ, DEMO) AND assignee = currentUser()"}
        )
        assert result == frozenset(["PROJ", "DEMO"])

    def test_empty_args_returns_empty(self) -> None:
        result = extract_jira_projects("jira_get_issue", {})
        assert result == frozenset()

    def test_invalid_issue_key_ignored(self) -> None:
        result = extract_jira_projects("jira_get_issue", {"issue_key": "not-a-key"})
        assert result == frozenset()


class TestExtractConfluenceSpaces:
    """Tests for Confluence space key extraction."""

    def test_extracts_space_key(self) -> None:
        result = extract_confluence_spaces(
            "confluence_get_page", {"space_key": "ENG"}
        )
        assert result == frozenset(["ENG"])

    def test_empty_args(self) -> None:
        result = extract_confluence_spaces("confluence_search", {})
        assert result == frozenset()


class TestCheckAccess:
    """Tests for the top-level access control check."""

    def test_allows_read_in_read_only_mode(self) -> None:
        result = check_access(
            "jira_get_issue",
            {"issue_key": "PROJ-1"},
            read_only=True,
            jira_whitelist=frozenset(["PROJ"]),
            confluence_whitelist=frozenset(),
        )
        assert result.allowed is True

    def test_blocks_write_in_read_only_mode(self) -> None:
        result = check_access(
            "jira_create_issue",
            {"project_key": "PROJ"},
            read_only=True,
            jira_whitelist=frozenset(["PROJ"]),
            confluence_whitelist=frozenset(),
        )
        assert result.allowed is False
        assert "read-only" in result.reason

    def test_blocks_out_of_whitelist_project(self) -> None:
        result = check_access(
            "jira_get_issue",
            {"issue_key": "OTHER-1"},
            read_only=False,
            jira_whitelist=frozenset(["PROJ"]),
            confluence_whitelist=frozenset(),
        )
        assert result.allowed is False
        assert "OTHER" in result.reason

    def test_allows_whitelisted_project(self) -> None:
        result = check_access(
            "jira_get_issue",
            {"issue_key": "PROJ-1"},
            read_only=False,
            jira_whitelist=frozenset(["PROJ"]),
            confluence_whitelist=frozenset(),
        )
        assert result.allowed is True

    def test_empty_whitelist_allows_all(self) -> None:
        result = check_access(
            "jira_get_issue",
            {"issue_key": "ANY-123"},
            read_only=False,
            jira_whitelist=frozenset(),
            confluence_whitelist=frozenset(),
        )
        assert result.allowed is True

    def test_blocks_confluence_out_of_whitelist(self) -> None:
        result = check_access(
            "confluence_get_page",
            {"space_key": "HR"},
            read_only=False,
            jira_whitelist=frozenset(),
            confluence_whitelist=frozenset(["ENG"]),
        )
        assert result.allowed is False

    def test_tool_without_project_args_allowed_with_nonempty_whitelist(self) -> None:
        """Tools with no project/space args pass when whitelist is active.

        This is a known limitation: tools like jira_get_user_profile have
        no project-scoped arguments and cannot be filtered by this proxy.
        """
        result = check_access(
            "jira_get_user_profile",
            {"account_id": "abc123"},
            read_only=False,
            jira_whitelist=frozenset(["PROJ"]),
            confluence_whitelist=frozenset(),
        )
        assert result.allowed is True
```

#### Unit tests — `proxy/tests/unit/test_config.py`

```python
"""Unit tests for proxy configuration."""

import os

import pytest
from mcp_proxy.config import ProxyConfig


def test_default_config() -> None:
    config = ProxyConfig()
    assert config.upstream_url == "http://mcp-atlassian:9000"
    assert config.read_only is False
    assert config.jira_projects_set == frozenset()


def test_projects_whitelist_parsed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROXY_JIRA_PROJECTS_WHITELIST", "PROJ, DEMO , OTHER")
    config = ProxyConfig()
    assert config.jira_projects_set == frozenset(["PROJ", "DEMO", "OTHER"])


def test_upstream_url_trailing_slash_stripped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PROXY_UPSTREAM_URL", "http://mcp:9000/")
    config = ProxyConfig()
    assert config.upstream_url == "http://mcp:9000"
```

#### Integration test — `proxy/tests/integration/test_proxy_e2e.py`

```python
"""End-to-end integration tests for the proxy using httpx TestClient.

Requires a running upstream mcp-atlassian instance (controlled via
PROXY_UPSTREAM_URL env var pointing to a real or stub server).
"""

import json

import pytest
from fastapi.testclient import TestClient
from unittest.mock import AsyncMock, patch

from mcp_proxy.main import app


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


def _tools_call_body(tool_name: str, arguments: dict) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": tool_name, "arguments": arguments},
    }


class TestProxyEnforcement:
    """Integration tests verifying enforcement at the HTTP boundary."""

    def test_health_check(self, client: TestClient) -> None:
        response = client.get("/healthz")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}

    @patch("mcp_proxy.main.proxy_request")
    def test_read_only_blocks_create(
        self, mock_forward: AsyncMock, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PROXY_READ_ONLY", "true")
        # Clear lru_cache so new env var is picked up
        from mcp_proxy.config import get_config
        get_config.cache_clear()

        body = _tools_call_body("jira_create_issue", {"project_key": "PROJ"})
        response = client.post("/mcp", json=body)

        assert response.status_code == 403
        assert response.json()["error"]["code"] == -32001
        mock_forward.assert_not_called()
        get_config.cache_clear()

    @patch("mcp_proxy.main.proxy_request")
    def test_whitelisted_project_forwarded(
        self, mock_forward: AsyncMock, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PROXY_JIRA_PROJECTS_WHITELIST", "PROJ")
        monkeypatch.setenv("PROXY_READ_ONLY", "false")
        from mcp_proxy.config import get_config
        get_config.cache_clear()
        mock_forward.return_value = AsyncMock(status_code=200)

        body = _tools_call_body("jira_get_issue", {"issue_key": "PROJ-1"})
        client.post("/mcp", json=body)

        mock_forward.assert_called_once()
        get_config.cache_clear()

    @patch("mcp_proxy.main.proxy_request")
    def test_non_tool_call_forwarded_without_enforcement(
        self, mock_forward: AsyncMock, client: TestClient
    ) -> None:
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/list",
            "params": {},
        }
        client.post("/mcp", json=body)
        mock_forward.assert_called_once()
```

---

### Rollout Plan

#### Phase 1 — Local validation (1 day)

1. Create `proxy/` directory and all files from this plan
2. Run `uv run pytest proxy/tests/unit/ -xvs` — all unit tests must pass
3. Build both images: `docker build -t ka-mcp-atlassian .` and `docker build -t ka-mcp-proxy proxy/`
4. Run `docker compose -f docker-compose.proxy.yml up`
5. Test with `curl` or a local MCP client against `http://localhost:8080/mcp`
6. Verify a denied tool call returns HTTP 403 with correct JSON-RPC error body
7. Verify an allowed tool call reaches mcp-atlassian (check upstream logs)

#### Phase 2 — Feature branch and CI (0.5 day)

1. `git checkout -b feat/proxy-access-control`
2. Add `proxy/` to the repo
3. Add `docker-compose.proxy.yml` to the repo root
4. Add `PROXY_*` variables to `.env.example` with documentation
5. Ensure pre-commit (Ruff, mypy) passes — the proxy has its own `pyproject.toml`
6. Open PR against `main`

#### Phase 3 — Staging deployment

1. Deploy with `PROXY_READ_ONLY=true` and an empty whitelist (allow all projects)
2. Monitor audit logs for 24 hours to establish baseline of tool usage
3. Identify which project keys appear in audit logs
4. Enable whitelist progressively as data privacy approvals are granted

#### Phase 4 — Production

1. Set `PROXY_JIRA_PROJECTS_WHITELIST` to approved projects
2. Set `PROXY_CONFLUENCE_SPACES_WHITELIST` to approved spaces
3. Ensure mcp-atlassian port (9000) is NOT exposed outside the Docker network
4. Route all MCP clients to the proxy port (8080)

---

### Environment Variable Documentation

Add to `.env.example`:

```bash
# =============================================================================
# MCP Access Control Proxy (docker-compose.proxy.yml)
# =============================================================================

# URL of the upstream mcp-atlassian server (internal Docker network address).
PROXY_UPSTREAM_URL=http://mcp-atlassian:9000

# Block all write tools (create, update, delete, etc.) before forwarding.
# This is a proxy-layer enforcement; set READ_ONLY_MODE on mcp-atlassian too
# for defense-in-depth.
PROXY_READ_ONLY=false

# Comma-separated list of Jira project keys to allow.
# Leave empty to allow access to all projects.
# Example: PROJ,DEMO,SUPPORT
PROXY_JIRA_PROJECTS_WHITELIST=

# Comma-separated list of Confluence space keys to allow.
# Leave empty to allow access to all spaces.
# Example: ENG,HR,LEGAL
PROXY_CONFLUENCE_SPACES_WHITELIST=

# Enable structured JSON audit logging (recommended: true in production).
PROXY_AUDIT_LOG_ENABLED=true

# Upstream connection timeout in seconds.
PROXY_UPSTREAM_CONNECT_TIMEOUT=10.0

# Upstream read timeout in seconds (tool calls can be slow).
PROXY_UPSTREAM_READ_TIMEOUT=120.0
```

---

### Verification

End-to-end test scenarios to validate after deployment:

| Scenario | Expected |
|---|---|
| GET `/healthz` on proxy | HTTP 200 `{"status": "ok"}` |
| `tools/list` → pass through | 200; upstream tool list returned |
| `tools/call` `jira_get_issue` `{"issue_key": "PROJ-1"}` with `PROXY_JIRA_PROJECTS_WHITELIST=PROJ` | 200; forwarded to upstream |
| `tools/call` `jira_get_issue` `{"issue_key": "OTHER-1"}` with `PROXY_JIRA_PROJECTS_WHITELIST=PROJ` | 403; JSON-RPC error code -32001 |
| `tools/call` `jira_create_issue` with `PROXY_READ_ONLY=true` | 403; "read-only mode" in error message |
| `tools/call` `jira_search` `{"jql": "project in (PROJ, SENSITIVE)"}` with `PROXY_JIRA_PROJECTS_WHITELIST=PROJ` | 403; SENSITIVE blocked |
| Direct access to mcp-atlassian port 9000 (after Docker network restriction) | Connection refused |
| Audit log after any denied call | JSON line with `"decision": "deny"` |
| Audit log after any allowed call | JSON line with `"decision": "allow"` |

---

## 6. Challenges and Tradeoffs

### Pros

1. **Zero changes to mcp-atlassian** — the upstream server image is unmodified; access control
   is a separate deployment artifact
2. **Universal enforcement** — all 75 tools are protected by a single enforcement point
   regardless of whether the upstream server's tool handlers check the filter themselves
3. **Independent deployment lifecycle** — whitelist rules can be updated by restarting only the
   proxy container, not the MCP server (avoids disrupting active sessions on the server)
4. **Audit trail as first-class feature** — every tool call decision is logged structurally;
   easy to integrate with a SIEM or log aggregation pipeline
5. **Defense-in-depth compatible** — can be combined with `READ_ONLY_MODE=true` on the upstream
   server and `JIRA_PROJECTS_FILTER` for a three-layer defence

### Cons

1. **stdio transport excluded** — the current `.mcp.json` uses stdio (`docker run -i`);
   migrating to HTTP transport requires client-side configuration changes and a persistent
   server process (not ephemeral per-call)
2. **Response content not inspected** — the proxy can only check *what is asked for*, not
   *what is returned*. If an upstream tool returns cross-project data despite receiving an
   approved project key (e.g., a Jira issue linking to another project), the proxy cannot
   block it
3. **JQL/CQL injection is partially mitigated, not fully solved** — the regex-based JQL
   extraction in `_extract_projects_from_jql` catches common patterns but misses aliases,
   sub-queries, and obfuscated references. Full JQL AST parsing is required for a stronger
   guarantee (out of scope for this proxy)
4. **Network segmentation is mandatory** — if an attacker or misconfigured client can reach
   mcp-atlassian:9000 directly (bypassing the proxy), all controls are bypassed. Docker
   networking and firewall rules must enforce this boundary
5. **Additional operational complexity** — two services to monitor, two images to build and
   push, two sets of health checks; proxy startup failures become a full outage for the MCP
   integration

### Key Challenges

1. **Streaming response passthrough**: MCP's `streamable-http` transport sends responses as
   SSE or chunked transfer-encoding streams. The `httpx` streaming approach used in
   `proxy_request()` handles this correctly, but must be tested with a live mcp-atlassian
   instance to confirm no buffering occurs. `uvicorn` with `--http h11` has known issues with
   large chunked responses; use `--http httptools` (default with `uvicorn[standard]`).

2. **Body re-injection after inspection**: HTTP request bodies are single-read streams. The
   `main.py` reads the full body for JSON-RPC parsing, then must inject it back when forwarding.
   The implementation uses a patched `receive` callable on a new `Request` instance — this
   works for Starlette but should be unit-tested explicitly.

3. **MCP session state**: In `streamable-http` mode, the MCP protocol maintains session state
   across multiple HTTP requests (using `mcp-session-id` header). The proxy is stateless and
   simply passes the header through; session management remains in the upstream server. No
   special handling is needed.

4. **READ_ONLY_MODE interaction**: Two layers enforce read-only:
   - Proxy layer: `PROXY_READ_ONLY=true` blocks write tool calls by name matching before they
     reach the server
   - Server layer: `READ_ONLY_MODE=true` blocks write tools at both list-time (hidden from
     `tools/list`) and call-time (via `check_write_access` decorator)

   **Recommended**: Enable both for defence-in-depth. If only the proxy is set to read-only,
   the server will still list write tools to the client — a cosmetic issue since they will be
   blocked by the proxy, but confusing to end users.

5. **Cloud vs Server/DC**: The proxy does not need to distinguish Cloud from Server/DC — that
   is handled entirely by mcp-atlassian's dependency injection. The proxy is transport-agnostic
   with respect to the Atlassian API.

6. **Tools with no project-scoped arguments**: Tools like `jira_get_user_profile`,
   `jira_get_link_types`, `jira_get_agile_boards` have no project key in their arguments.
   When a non-empty Jira whitelist is configured, these tools are allowed through by default
   (since there is nothing to validate against). This is a known limitation — such tools
   inherently return data across the whole Jira instance. The whitelist is only effective
   for tools that take explicit project/issue/space references.

---

## 7. Alternative Approaches (If Proxy Doesn't Fit)

### Alternative A — ASGI Middleware (Approach E)

If the ops overhead of a second container is unacceptable, add an `AccessControlMiddleware`
class directly to `src/mcp_atlassian/servers/main.py` alongside `UserTokenMiddleware`.

**How it works**: The new middleware reads the request body (same JSON-RPC body parsing as the
proxy), applies `check_access()`, and either rejects with a 403 or calls `await
self.app(scope, receive, send)`. The body re-injection challenge is identical.

**Key difference from the proxy**: The middleware has access to the `lifespan_context`
(via `scope`) and can read `MainAppContext.read_only` directly, making the
`PROXY_READ_ONLY` env var unnecessary — it reuses the existing `READ_ONLY_MODE` flag.

**Files to modify**:
- `src/mcp_atlassian/servers/main.py` — add `AccessControlMiddleware` class
- `src/mcp_atlassian/servers/context.py` — add `jira_whitelist` and `confluence_whitelist`
  fields to `MainAppContext`
- `src/mcp_atlassian/utils/enforcement.py` — extract shared enforcement logic (re-usable
  from the proxy's `enforcement.py`)
- `src/mcp_atlassian/servers/main.py` — add the new middleware to `http_app()` after
  `UserTokenMiddleware`

**LoE**: 1–2 days. Maintenance cost is higher (coupled to upstream). Recommended if the team
does not want to operate a second Docker container.

---

### Alternative B — Decorator-based per-tool enforcement (Solution 1)

Described in the original `ds-2031-access-control.md`. Complementary to both the proxy and
middleware approaches. Does not solve the 72-tool coverage gap without patching every tool,
but is the correct long-term approach for the fork since it enforces policy at the tool handler
level (impossible to bypass even from within the same process).

Recommended as a **complement** to the proxy: use the proxy for network-level enforcement
and add decorators to the highest-risk write tools as an additional layer.

---

*Plan generated: 2026-03-11*
*Codebase analysed: `ka-mcp-atlassian` at commit `8e84d74`*
