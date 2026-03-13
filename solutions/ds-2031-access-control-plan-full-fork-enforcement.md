# Access Control Analysis: DS-2031 — Solution 2: Full Fork with Proper Enforcement

## 1. Existing Solution Summary

### What the current codebase does

`ka-mcp-atlassian` is a fork of `sooperset/mcp-atlassian` operating as a Python ≥ 3.10
FastMCP server. Two environment variables provide the server-side whitelist mechanism:

- `JIRA_PROJECTS_FILTER` — comma-separated project keys stored in `JiraConfig.projects_filter`
- `CONFLUENCE_SPACES_FILTER` — comma-separated space keys stored in
  `ConfluenceConfig.spaces_filter`

Both are read by `from_env()` at startup and stored in the lifespan-scoped
`MainAppContext`. The `_create_user_config_for_fetcher` function in
`servers/dependencies.py` copies the filter from the global config to every
per-user config derived at request time, so the value is technically present on
every `JiraFetcher` / `ConfluenceFetcher` instance.

### Where enforcement actually happens

| Tool / operation | File | Filter applied? | Mechanism |
|---|---|---|---|
| `jira_search` (JQL) | `jira/search.py` `search_issues()` | YES — but bypassable | Appends `AND project IN (...)` to JQL |
| `jira_get_issue` | `jira/issues.py` `get_issue()` | YES | Checks `issue_key` prefix against whitelist |
| `jira_get_all_projects` | `servers/jira.py` | YES | Post-filter on result list |
| `confluence_search` (CQL) | `confluence/search.py` `search()` | YES — but bypassable | Appends `AND (space = ...)` to CQL |
| All other ~44 Jira tools | `servers/jira.py` | **NO** | No project key validation |
| All other ~23 Confluence tools | `servers/confluence.py` | **NO** | No space key validation |

### The bypass vectors

1. **JQL injection in `jira_search`**: The current code only appends a project
   filter when `"project = "` and `"project in"` are absent (case-insensitive
   substring match at line 93–94 of `jira/search.py`). A caller can provide
   `"project = SECRET_PROJ"` directly and the filter is silently skipped.

2. **CQL injection in `confluence_search`**: Same pattern — the filter only
   activates when `"space = "` is not already in the CQL string.

3. **Project-key tools with no guard**: `create_issue`, `batch_create_issues`,
   `update_issue`, `delete_issue`, `transition_issue`, `create_version`,
   `create_sprint`, `add_issues_to_sprint`, `get_project_issues`,
   `get_project_versions`, `get_project_components`, `get_service_desk_for_project`,
   `get_service_desk_queues`, `get_queue_issues`, `batch_create_versions`,
   `get_issue_proforma_forms`, `get_proforma_form_details`,
   `update_proforma_form_answers`, and more — all accept `project_key` or
   `issue_key` parameters with no whitelist check.

4. **Issue-key tools with no guard**: `get_transitions`, `get_worklog`,
   `add_worklog`, `add_comment`, `edit_comment`, `create_issue_link`,
   `create_remote_issue_link`, `remove_issue_link`, `link_to_epic`,
   `get_issue_watchers`, `add_watcher`, `remove_watcher`, `get_issue_images`,
   `download_attachments`, `get_issue_development_info`,
   `get_issues_development_info`, `get_issue_dates`, `get_issue_sla`,
   `batch_get_changelogs`.

5. **Confluence page-ID tools with no guard**: `get_page`, `get_page_children`,
   `get_space_page_tree`, `get_page_history`, `get_page_diff`, `get_page_views`,
   `get_comments`, `get_labels`, `add_label`, `create_page`, `update_page`,
   `delete_page`, `move_page`, `add_comment`, `reply_to_comment`,
   `upload_attachment`, `upload_attachments`, `get_attachments`,
   `download_attachment`, `download_content_attachments`, `delete_attachment`,
   `get_page_images`.

### `READ_ONLY_MODE` interaction

`READ_ONLY_MODE=true` suppresses write tools at the tool-listing level and via
the `@check_write_access` decorator, but it does not restrict which projects or
spaces can be read. Filter enforcement is orthogonal to read-only mode.

---

## 2. Approaches Considered

| # | Approach | Summary |
|---|---|---|
| A | Extend existing bypass-fix in search | Patch the JQL/CQL bypass only; leave other tools unguarded |
| **B** | **Universal project-key guard decorator** (RECOMMENDED) | New `@require_project_access` decorator applied to all project-key and issue-key tool handlers |
| C | JQL/CQL rewrite engine | Parse and rewrite every JQL/CQL query to strip any cross-project clauses |
| D | Policy-as-Code (OPA) | External Open Policy Agent sidecar |
| E | API Gateway layer | AWS API Gateway / Kong in front of the MCP server |
| F | Response-level post-filter | Scrub disallowed projects from every response |

The recommended solution (B) is layered with approach C for query tools — the
two are complementary, not mutually exclusive. D and E are documented as
alternative approaches at the end.

---

## 3. Cost Evaluation

### Approach B — Universal project-key guard decorator

| Dimension | Assessment |
|---|---|
| **Dev cost** | Medium — ~3–5 days. One new decorator + helper; systematic application across ~45 tool handlers and ~25 Confluence handlers. The pattern is repetitive, not complex. |
| **DevOps cost** | Low — no new infrastructure. `JIRA_PROJECTS_FILTER` / `CONFLUENCE_SPACES_FILTER` env vars already exist. No Docker/K8s changes. |
| **Maintenance cost** | Low-Medium — new upstream tools need the decorator applied manually. Risk of missing a new tool on rebase. Mitigated by a CI lint check. |
| **Security cost/risk** | Low — access control lives in the application layer. Audit logging can be added to the same decorator. No new credential surface. |

### Approach C — JQL/CQL rewrite engine

| Dimension | Assessment |
|---|---|
| **Dev cost** | High — ~5–8 days. JQL grammar is complex. Edge cases: subqueries, `NOT IN`, `!= `, nested parentheses, ORDER BY, function calls (`currentUser()`, `membersOf()`). Bugs here silently allow bypass. |
| **DevOps cost** | Low — no infrastructure changes. |
| **Maintenance cost** | High — any Jira/Confluence query language evolution breaks the parser. Testing surface is large. |
| **Security cost/risk** | Medium — parser gaps create bypass paths that are hard to test exhaustively. Recommended only as a defense-in-depth layer on top of B, not as the primary control. |

### Approach D — Policy-as-Code (OPA)

| Dimension | Assessment |
|---|---|
| **Dev cost** | High — ~8–12 days. Requires OPA sidecar, Rego policy authoring, MCP tool-call interceptor middleware. |
| **DevOps cost** | High — new Docker/K8s sidecar, OPA bundle distribution, policy deployment pipeline. |
| **Maintenance cost** | High — dual codebase (Python + Rego), OPA version management, policy testing suite. |
| **Security cost/risk** | Low if properly configured — centralized auditable policy. Overkill for current threat model. |

### Approach E — API Gateway layer

| Dimension | Assessment |
|---|---|
| **Dev cost** | High — ~10–15 days. MCP tool calls use JSON-RPC over HTTP; gateway must parse the JSON-RPC body to extract `tool_name` and `arguments`. Non-trivial. |
| **DevOps cost** | High — new infrastructure component, routing config, TLS termination. |
| **Maintenance cost** | Medium — gateway config drift from tool names as tools are added. |
| **Security cost/risk** | Medium — gateway is a new attack surface. MCP-aware gateways are not mature yet. |

---

## 4. Recommended Approach

**Primary: Approach B — Universal project-key guard decorator**

**Secondary (layered): Approach C — JQL/CQL strict rewrite for search tools only**

The guard decorator handles ~90% of the attack surface with low complexity and
low maintenance cost. The JQL/CQL rewrite is scoped to two specific methods
(`search_issues`, `confluence.search`) rather than a general-purpose parser,
making it tractable.

Audit logging is built into the decorator so every access-controlled call is
recorded.

**Approach B + C together address:**
- All project-key and issue-key tools (guard decorator)
- JQL/CQL bypass in search tools (strict rewrite replaces the current
  substring-match workaround)
- `READ_ONLY_MODE` interaction (decorator is additive, not conflicting)
- Cloud vs Server/DC differences (handled inside the decorator using
  `fetcher.config.is_cloud`)

---

## 5. Comprehensive Implementation Plan

### Pre-requisites

1. `JIRA_PROJECTS_FILTER` must be set to a non-empty comma-separated list of
   uppercase project keys (e.g., `JIRA_PROJECTS_FILTER=MYPROJ,OTHERPROJ`).
   Empty or absent means "allow all" — the decorator must treat this as
   pass-through to preserve backward compatibility with unrestricted deployments.

2. `CONFLUENCE_SPACES_FILTER` must be set similarly
   (e.g., `CONFLUENCE_SPACES_FILTER=TEAM,DEV`).

3. No new Python dependencies are required. The implementation uses only the
   standard library and existing project utilities.

4. Run `uv sync --frozen --all-extras --dev` before starting to ensure all dev
   dependencies are current.

### Step-by-Step

#### Step 1 — Create `src/mcp_atlassian/utils/access_control.py`

This module contains:
- `AccessDeniedError` — a new exception for clean error signaling
- `parse_filter_set()` — normalizes a comma-separated filter string to a
  `frozenset[str]` of uppercase keys
- `extract_project_from_issue_key()` — splits `"PROJ-123"` to `"PROJ"`
- `check_project_access()` — validates a project key against the filter set
- `check_space_access()` — validates a space key against the filter set
- `rewrite_jql_project_clause()` — replaces any `project =` / `project IN ()`
  clause in a JQL string with one derived strictly from the whitelist
- `rewrite_cql_space_clause()` — same for CQL `space =` / `space IN ()`

See sample code section below for full implementation.

#### Step 2 — Create `src/mcp_atlassian/utils/audit.py`

A thin structured-logging wrapper that emits a JSON audit record for each
access-controlled call, including: tool name, project/space accessed, user
identity (from request state), outcome (allowed/denied), and timestamp.

#### Step 3 — Add `@enforce_project_filter` and `@enforce_space_filter` decorators to `src/mcp_atlassian/utils/decorators.py`

These decorators:
1. Extract the project/space key from the tool arguments by inspecting the
   function signature (`project_key`, `issue_key`, `space_key`, or `page_id`
   parameters — for `page_id` the check is deferred to post-fetch response
   validation).
2. Call `check_project_access()` / `check_space_access()`.
3. Emit an audit log entry.
4. Raise `ToolError` (not `AccessDeniedError`) if access is denied, so the
   error message reaches the MCP client per FastMCP's error propagation
   contract.

The decorator reads the filter from `fetcher.config.projects_filter` (already
propagated to per-user configs by `_create_user_config_for_fetcher`), not from
a global environment variable, so it respects multi-tenant token isolation.

#### Step 4 — Fix the JQL/CQL bypass in `jira/search.py` and `confluence/search.py`

Replace the current substring-match bypass detection with `rewrite_jql_project_clause()`:
- If no filter is configured, pass JQL through unchanged.
- If a filter is configured, rewrite the JQL to enforce the project constraint
  regardless of what the caller provided.
  - This means a caller providing `project = SECRET_PROJ` will have their
    clause replaced, not skipped.

Same treatment for `confluence/search.py`.

#### Step 5 — Apply `@enforce_project_filter` to all Jira tool handlers

Apply the decorator to every tool in `servers/jira.py` that takes a
`project_key` or `issue_key` argument. The decorator parameter specifies which
argument carries the key:

```python
@enforce_project_filter(key_arg="project_key")
async def create_issue(ctx, project_key, ...): ...

@enforce_project_filter(key_arg="issue_key")
async def get_issue(ctx, issue_key, ...): ...
```

For `batch_create_issues`, enforcement happens inside the batch loop (each
issue's `project_key` is validated before the batch proceeds).

Tools that are project-agnostic (`search_fields`, `get_link_types`,
`get_agile_boards`, `get_user_profile`) do not receive the decorator.

Full tool matrix:

**Jira tools — `project_key` enforcement needed:**
`create_issue`, `batch_create_issues`, `update_issue` (derives from issue_key),
`delete_issue` (issue_key), `get_project_issues`, `get_transitions` (issue_key),
`get_worklog` (issue_key), `add_worklog` (issue_key), `add_comment` (issue_key),
`edit_comment` (issue_key), `transition_issue` (issue_key), `create_issue_link`
(issue_key), `create_remote_issue_link` (issue_key), `remove_issue_link`
(link_id — no project context; skip, covered by issue-level fetch),
`link_to_epic` (issue_key), `create_sprint` (board_id — no project context;
skip), `update_sprint` (sprint_id — skip), `add_issues_to_sprint`
(issue_keys — each validated), `get_board_issues` (board_id — skip; filter
applies via search_issues), `get_sprint_issues` (sprint_id — skip; filter
applies via search_issues), `get_issue_watchers` (issue_key), `add_watcher`
(issue_key), `remove_watcher` (issue_key), `get_project_versions` (project_key),
`get_project_components` (project_key), `get_service_desk_for_project`
(project_key), `get_service_desk_queues` (service_desk_id — skip), `get_queue_issues`
(service_desk_id — skip), `create_version` (project_key), `batch_create_versions`
(project_key per item), `get_issue_proforma_forms` (issue_key),
`get_proforma_form_details` (issue_key), `update_proforma_form_answers`
(issue_key), `get_issue_dates` (issue_key), `get_issue_sla` (issue_key),
`get_issue_development_info` (issue_key), `get_issues_development_info`
(issue_keys — each validated), `get_field_options` (project_key when present),
`download_attachments` (issue_key), `get_issue_images` (issue_key),
`batch_get_changelogs` (issue_keys — each validated).

#### Step 6 — Apply `@enforce_space_filter` to all Confluence tool handlers

**Confluence tools — `space_key` enforcement needed:**
`get_page` (space_key parameter; also check response's space key when
`page_id` is provided), `get_page_children` (parent_page_id — post-fetch
check), `get_space_page_tree` (space_key), `get_comments` (page_id — post-fetch),
`get_labels` (page_id — post-fetch), `add_label` (page_id — post-fetch),
`create_page` (space_key), `update_page` (page_id — post-fetch), `delete_page`
(page_id — post-fetch), `move_page` (page_id — post-fetch), `add_comment`
(page_id — post-fetch), `reply_to_comment` (comment_id — skip; permission
enforced by Confluence itself), `get_page_history` (page_id — post-fetch),
`get_page_diff` (page_id — post-fetch), `get_page_views` (page_id — post-fetch),
`upload_attachment` (page_id — post-fetch), `upload_attachments` (page_id —
post-fetch), `get_attachments` (page_id — post-fetch), `download_attachment`
(page_id — post-fetch), `download_content_attachments` (page_id — post-fetch),
`delete_attachment` (attachment_id — skip), `get_page_images` (page_id —
post-fetch).

For page-ID-based tools, the decorator fetches the page metadata to resolve
the space key before allowing the operation. This incurs one extra API call per
invocation; caching at the `TTLCache` level in `servers/main.py` is not
applicable here since the cache is for token validation, not page metadata.
Acceptable trade-off for security. An optional `page_id_space_cache` can be
added as a follow-on optimization.

#### Step 7 — Add `MCPAtlassianAccessDeniedError` to `src/mcp_atlassian/exceptions.py`

Keep it distinct from `MCPAtlassianAuthenticationError` so log aggregation can
distinguish 401/403 auth failures from policy-enforced access denials.

#### Step 8 — Update `MainAppContext` in `servers/context.py`

Add a `projects_filter_set: frozenset[str] | None` and
`spaces_filter_set: frozenset[str] | None` field precomputed at startup. This
avoids re-parsing the comma-separated string on every tool call.

#### Step 9 — Write tests

See Testing Strategy section.

#### Step 10 — Add CI lint check for missing decorator

Add a Ruff custom rule or a simple `scripts/check_access_control.py` that
inspects `servers/jira.py` and `servers/confluence.py` and warns if any tool
handler that takes `project_key` or `issue_key` lacks the decorator. Run this
in CI as part of `pre-commit`.

#### Step 11 — Document new env vars and behavior

Update `.env.example` with enforcement semantics and update `AGENTS.md` with
the access control pattern.

---

### Sample Code

#### `src/mcp_atlassian/utils/access_control.py`

```python
"""Server-side access control utilities for project and space whitelists.

Provides project-key and space-key validation against the configured
JIRA_PROJECTS_FILTER / CONFLUENCE_SPACES_FILTER whitelists, and strict
JQL/CQL rewrite logic to prevent cross-project query bypass.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger("mcp-atlassian.access_control")


class AccessDeniedError(ValueError):
    """Raised when a tool call is denied by the project/space whitelist.

    Inherits from ValueError so FastMCP raises it as a ToolError.
    """


def parse_filter_set(filter_str: str | None) -> frozenset[str] | None:
    """Parse a comma-separated filter string into a normalized frozenset.

    Args:
        filter_str: Comma-separated project/space keys, or None.

    Returns:
        Frozenset of uppercase keys, or None if filter_str is empty/None.
        None means "allow all" (no restriction).
    """
    if not filter_str or not filter_str.strip():
        return None
    return frozenset(k.strip().upper() for k in filter_str.split(",") if k.strip())


def extract_project_from_issue_key(issue_key: str) -> str:
    """Extract the project key prefix from a Jira issue key.

    Args:
        issue_key: Issue key like "PROJ-123" or "ACV2-42".

    Returns:
        Uppercase project key prefix (e.g., "PROJ").

    Raises:
        ValueError: If the issue key does not contain a dash.
    """
    parts = issue_key.upper().split("-", 1)
    if len(parts) < 2:
        raise ValueError(f"Invalid issue key format: '{issue_key}'")
    return parts[0]


def check_project_access(
    project_key: str,
    allowed: frozenset[str] | None,
    *,
    context: str = "",
) -> None:
    """Assert that project_key is within the allowed set.

    Args:
        project_key: The project key to validate (case-insensitive).
        allowed: Frozenset of permitted uppercase keys, or None (allow all).
        context: Optional context string for the audit log message.

    Raises:
        AccessDeniedError: If the project key is not in the allowed set.
    """
    if allowed is None:
        return
    normalized = project_key.strip().upper()
    if normalized not in allowed:
        msg = (
            f"Access denied: project '{normalized}' is not in the "
            f"configured whitelist. {context}".strip()
        )
        logger.warning(msg)
        raise AccessDeniedError(msg)


def check_space_access(
    space_key: str,
    allowed: frozenset[str] | None,
    *,
    context: str = "",
) -> None:
    """Assert that space_key is within the allowed set.

    Args:
        space_key: The Confluence space key to validate (case-insensitive).
        allowed: Frozenset of permitted uppercase keys, or None (allow all).
        context: Optional context string for the audit log message.

    Raises:
        AccessDeniedError: If the space key is not in the allowed set.
    """
    if allowed is None:
        return
    normalized = space_key.strip().upper()
    if normalized not in allowed:
        msg = (
            f"Access denied: space '{normalized}' is not in the "
            f"configured whitelist. {context}".strip()
        )
        logger.warning(msg)
        raise AccessDeniedError(msg)


# ---------------------------------------------------------------------------
# JQL rewrite
# ---------------------------------------------------------------------------

_JQL_PROJECT_SINGLE = re.compile(
    r"""(?i)project\s*(?:!=|=)\s*(?:"[^"]*"|'[^']*'|\w+)""",
)
_JQL_PROJECT_IN = re.compile(
    r"""(?i)project\s+(?:NOT\s+)?IN\s*\([^)]*\)""",
)
_JQL_ORDER_BY = re.compile(r"(?i)\s+ORDER\s+BY\s+.+$")


def rewrite_jql_project_clause(
    jql: str,
    allowed: frozenset[str] | None,
) -> str:
    """Rewrite a JQL query to strictly enforce the project whitelist.

    Unlike the old substring-match approach, this function removes *any*
    caller-supplied project clause and replaces it with one derived entirely
    from the allowed set. If no filter is configured, the JQL is unchanged.

    Strategy:
    1. Strip all ``project =``, ``project !=``, ``project IN``,
       ``project NOT IN`` clauses and surrounding AND/OR connectives.
    2. Append ``AND project IN (A, B, ...)`` (or ``project = A`` for
       single-project whitelists).
    3. Re-attach any ORDER BY clause.

    Args:
        jql: The original JQL query string.
        allowed: Frozenset of allowed project keys, or None (no rewrite).

    Returns:
        Rewritten JQL string with enforced project filter.
    """
    if allowed is None:
        return jql

    # Extract and preserve ORDER BY
    order_match = _JQL_ORDER_BY.search(jql)
    order_clause = order_match.group(0) if order_match else ""
    jql_body = jql[: order_match.start()] if order_match else jql

    # Remove all project clauses
    cleaned = _JQL_PROJECT_IN.sub("", jql_body)
    cleaned = _JQL_PROJECT_SINGLE.sub("", cleaned)

    # Remove orphaned AND / OR connectives
    cleaned = re.sub(r"(?i)\b(AND|OR)\s+(AND|OR)\b", r"\2", cleaned)
    cleaned = re.sub(r"(?i)^\s*(AND|OR)\s*", "", cleaned)
    cleaned = re.sub(r"(?i)\s*(AND|OR)\s*$", "", cleaned)
    cleaned = cleaned.strip()

    # Build replacement project clause
    sorted_keys = sorted(allowed)
    if len(sorted_keys) == 1:
        project_clause = f'project = "{sorted_keys[0]}"'
    else:
        quoted = ", ".join(f'"{k}"' for k in sorted_keys)
        project_clause = f"project IN ({quoted})"

    if cleaned:
        rewritten = f"({cleaned}) AND {project_clause}"
    else:
        rewritten = project_clause

    return f"{rewritten}{order_clause}"


# ---------------------------------------------------------------------------
# CQL rewrite
# ---------------------------------------------------------------------------

_CQL_SPACE_SINGLE = re.compile(
    r"""(?i)space\s*(?:!=|=)\s*(?:"[^"]*"|'[^']*'|~[^\s]+|\w+)""",
)
_CQL_SPACE_IN = re.compile(
    r"""(?i)space\s+(?:NOT\s+)?IN\s*\([^)]*\)""",
)
_CQL_ORDER_BY = re.compile(r"(?i)\s+ORDER\s+BY\s+.+$")


def rewrite_cql_space_clause(
    cql: str,
    allowed: frozenset[str] | None,
) -> str:
    """Rewrite a CQL query to strictly enforce the space whitelist.

    Mirrors the JQL logic but for Confluence CQL space predicates.

    Args:
        cql: The original CQL query string.
        allowed: Frozenset of allowed space keys, or None (no rewrite).

    Returns:
        Rewritten CQL string with enforced space filter.
    """
    if allowed is None:
        return cql

    order_match = _CQL_ORDER_BY.search(cql)
    order_clause = order_match.group(0) if order_match else ""
    cql_body = cql[: order_match.start()] if order_match else cql

    cleaned = _CQL_SPACE_IN.sub("", cql_body)
    cleaned = _CQL_SPACE_SINGLE.sub("", cleaned)
    cleaned = re.sub(r"(?i)\b(AND|OR)\s+(AND|OR)\b", r"\2", cleaned)
    cleaned = re.sub(r"(?i)^\s*(AND|OR)\s*", "", cleaned)
    cleaned = re.sub(r"(?i)\s*(AND|OR)\s*$", "", cleaned)
    cleaned = cleaned.strip()

    sorted_keys = sorted(allowed)
    if len(sorted_keys) == 1:
        space_clause = f"space = {sorted_keys[0]}"
    else:
        space_list = " OR ".join(f"space = {k}" for k in sorted_keys)
        space_clause = f"({space_list})"

    if cleaned:
        rewritten = f"({cleaned}) AND {space_clause}"
    else:
        rewritten = space_clause

    return f"{rewritten}{order_clause}"
```

#### `src/mcp_atlassian/utils/audit.py`

```python
"""Structured audit logging for MCP Atlassian access control decisions.

All access control outcomes (allowed or denied) are emitted as structured
JSON log records at INFO level on the ``mcp-atlassian.audit`` logger. This
allows the operator to ship audit logs to SIEM systems independently of
application-level DEBUG logs.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

_audit_logger = logging.getLogger("mcp-atlassian.audit")


def emit_access_event(
    *,
    tool_name: str,
    resource_type: str,
    resource_key: str,
    outcome: str,
    user_identity: str | None,
    reason: str = "",
    extra: dict[str, Any] | None = None,
) -> None:
    """Emit a structured audit log record for an access control decision.

    Args:
        tool_name: Name of the MCP tool being called (e.g., "create_issue").
        resource_type: "project" or "space".
        resource_key: The key being accessed (e.g., "MYPROJ", "DEV").
        outcome: "ALLOWED" or "DENIED".
        user_identity: User email or account identifier, if available.
        reason: Human-readable reason for DENIED outcomes.
        extra: Optional additional context fields.
    """
    record: dict[str, Any] = {
        "event": "access_control",
        "ts": time.time(),
        "tool": tool_name,
        "resource_type": resource_type,
        "resource_key": resource_key,
        "outcome": outcome,
        "user": user_identity or "unknown",
    }
    if reason:
        record["reason"] = reason
    if extra:
        record.update(extra)

    _audit_logger.info(json.dumps(record))
```

#### New decorators in `src/mcp_atlassian/utils/decorators.py`

```python
from mcp_atlassian.utils.access_control import (
    AccessDeniedError,
    check_project_access,
    extract_project_from_issue_key,
    parse_filter_set,
)
from mcp_atlassian.utils.audit import emit_access_event


def enforce_project_filter(
    key_arg: str,
    key_type: str = "project_key",
) -> Callable[[F], F]:
    """Decorator that enforces JIRA_PROJECTS_FILTER on a tool handler.

    Reads the project key from the named argument, derives the project key
    from an issue key if key_type=="issue_key", and raises ToolError if the
    project is outside the configured whitelist.

    Args:
        key_arg: Name of the function argument carrying the key
            (e.g., "project_key" or "issue_key").
        key_type: Either "project_key" (direct) or "issue_key" (derive
            project from the key prefix).

    Returns:
        Decorator function.

    Example:
        @enforce_project_filter(key_arg="project_key")
        async def create_issue(ctx, project_key, ...): ...

        @enforce_project_filter(key_arg="issue_key", key_type="issue_key")
        async def get_issue(ctx, issue_key, ...): ...
    """

    def decorator(func: F) -> F:
        tool_name = func.__name__

        @wraps(func)
        async def wrapper(ctx: Context, *args: Any, **kwargs: Any) -> Any:
            raw_key = kwargs.get(key_arg)
            if raw_key is None and args:
                # Positional arg: introspect signature to find position
                import inspect
                sig = inspect.signature(func)
                param_names = list(sig.parameters.keys())
                # Skip 'ctx' which is index 0
                try:
                    idx = param_names.index(key_arg)
                    # args[0] == ctx already consumed, so adjust
                    raw_key = args[idx - 1] if idx - 1 < len(args) else None
                except ValueError:
                    raw_key = None

            if raw_key is not None:
                try:
                    project_key: str
                    if key_type == "issue_key":
                        project_key = extract_project_from_issue_key(str(raw_key))
                    else:
                        project_key = str(raw_key).strip().upper()

                    # Resolve fetcher to get per-user filter
                    from mcp_atlassian.servers.dependencies import get_jira_fetcher
                    jira = await get_jira_fetcher(ctx)
                    allowed = parse_filter_set(jira.config.projects_filter)

                    user_identity = None
                    try:
                        from fastmcp.server.dependencies import get_http_request
                        req = get_http_request()
                        user_identity = getattr(
                            req.state, "user_atlassian_email", None
                        )
                    except RuntimeError:
                        pass

                    try:
                        check_project_access(project_key, allowed)
                        emit_access_event(
                            tool_name=tool_name,
                            resource_type="project",
                            resource_key=project_key,
                            outcome="ALLOWED",
                            user_identity=user_identity,
                        )
                    except AccessDeniedError as exc:
                        emit_access_event(
                            tool_name=tool_name,
                            resource_type="project",
                            resource_key=project_key,
                            outcome="DENIED",
                            user_identity=user_identity,
                            reason=str(exc),
                        )
                        raise ToolError(str(exc)) from exc
                except AccessDeniedError:
                    raise
                except ToolError:
                    raise
                except Exception as extraction_err:
                    logger.warning(
                        f"enforce_project_filter: Could not validate key "
                        f"'{raw_key}' for tool '{tool_name}': {extraction_err}"
                    )

            return await func(ctx, *args, **kwargs)

        return wrapper  # type: ignore[return-value]

    return decorator


def enforce_space_filter(
    key_arg: str = "space_key",
) -> Callable[[F], F]:
    """Decorator that enforces CONFLUENCE_SPACES_FILTER on a tool handler.

    For direct space_key args. For page_id-based tools, space validation
    must be done inside the tool after fetching page metadata.

    Args:
        key_arg: Name of the function argument carrying the space key.

    Returns:
        Decorator function.
    """

    def decorator(func: F) -> F:
        tool_name = func.__name__

        @wraps(func)
        async def wrapper(ctx: Context, *args: Any, **kwargs: Any) -> Any:
            raw_key = kwargs.get(key_arg)
            if raw_key is not None:
                try:
                    space_key = str(raw_key).strip().upper()
                    from mcp_atlassian.servers.dependencies import (
                        get_confluence_fetcher,
                    )
                    confluence = await get_confluence_fetcher(ctx)
                    allowed = parse_filter_set(confluence.config.spaces_filter)

                    user_identity = None
                    try:
                        from fastmcp.server.dependencies import get_http_request
                        req = get_http_request()
                        user_identity = getattr(
                            req.state, "user_atlassian_email", None
                        )
                    except RuntimeError:
                        pass

                    try:
                        check_space_access(space_key, allowed)
                        emit_access_event(
                            tool_name=tool_name,
                            resource_type="space",
                            resource_key=space_key,
                            outcome="ALLOWED",
                            user_identity=user_identity,
                        )
                    except AccessDeniedError as exc:
                        emit_access_event(
                            tool_name=tool_name,
                            resource_type="space",
                            resource_key=space_key,
                            outcome="DENIED",
                            user_identity=user_identity,
                            reason=str(exc),
                        )
                        raise ToolError(str(exc)) from exc
                except AccessDeniedError:
                    raise
                except ToolError:
                    raise
                except Exception as extraction_err:
                    logger.warning(
                        f"enforce_space_filter: Could not validate key "
                        f"'{raw_key}' for tool '{tool_name}': {extraction_err}"
                    )

            return await func(ctx, *args, **kwargs)

        return wrapper  # type: ignore[return-value]

    return decorator
```

#### Updated `jira/search.py` — strict JQL rewrite

Replace lines 64–109 of `search_issues()` with:

```python
from mcp_atlassian.utils.access_control import parse_filter_set, rewrite_jql_project_clause

# ...inside search_issues():
filter_str = projects_filter or self.config.projects_filter
allowed_set = parse_filter_set(filter_str)
# Strict rewrite: removes any caller-supplied project clause and
# replaces it with one built from the whitelist.
jql = rewrite_jql_project_clause(jql, allowed_set)
if allowed_set:
    logger.info(f"Applied strict projects filter to query: {jql}")
```

#### Updated `confluence/search.py` — strict CQL rewrite

Replace the current spaces-filter block in `search()` with:

```python
from mcp_atlassian.utils.access_control import parse_filter_set, rewrite_cql_space_clause

filter_str = spaces_filter or self.config.spaces_filter
allowed_set = parse_filter_set(filter_str)
cql = rewrite_cql_space_clause(cql, allowed_set)
if allowed_set:
    logger.info(f"Applied strict spaces filter to query: {cql}")
```

#### Adding `MCPAtlassianAccessDeniedError` to `exceptions.py`

```python
class MCPAtlassianAccessDeniedError(Exception):
    """Raised when a tool call is denied by the project/space whitelist.

    Distinct from MCPAtlassianAuthenticationError (401/403) — this is a
    policy-level denial at the application layer, not a credential failure.
    """
```

#### Example application to `servers/jira.py`

```python
from mcp_atlassian.utils.decorators import check_write_access, enforce_project_filter

@jira_mcp.tool(
    tags={"jira", "write", "toolset:jira_issues"},
    annotations={"title": "Create Issue", "destructiveHint": True},
)
@check_write_access
@enforce_project_filter(key_arg="project_key")
async def create_issue(
    ctx: Context,
    project_key: Annotated[str, Field(...)],
    summary: Annotated[str, Field(...)],
    # ... remaining args unchanged ...
) -> str:
    """Create a new Jira issue with optional Epic link or parent for subtasks."""
    jira = await get_jira_fetcher(ctx)
    # ... existing body unchanged ...
```

Note on decorator ordering: `@check_write_access` wraps the outermost layer
(it needs `ctx` as first positional arg and handles the `read_only` check).
`@enforce_project_filter` wraps next. The innermost call is the actual
implementation. Both decorators preserve `ctx` as the first arg via `@wraps`.

---

### Testing Strategy

#### Unit tests — `tests/unit/utils/test_access_control.py` (new file)

```python
"""Unit tests for access_control utilities."""

import pytest
from mcp_atlassian.utils.access_control import (
    AccessDeniedError,
    check_project_access,
    check_space_access,
    extract_project_from_issue_key,
    parse_filter_set,
    rewrite_cql_space_clause,
    rewrite_jql_project_clause,
)


class TestParseFilterSet:
    def test_none_returns_none(self) -> None:
        assert parse_filter_set(None) is None

    def test_empty_string_returns_none(self) -> None:
        assert parse_filter_set("") is None

    def test_normalizes_to_uppercase_frozenset(self) -> None:
        result = parse_filter_set("myproj, OTHER,  THIRD")
        assert result == frozenset({"MYPROJ", "OTHER", "THIRD"})

    def test_whitespace_only_returns_none(self) -> None:
        assert parse_filter_set("   ") is None


class TestExtractProject:
    def test_standard_key(self) -> None:
        assert extract_project_from_issue_key("PROJ-123") == "PROJ"

    def test_lowercase_normalised(self) -> None:
        assert extract_project_from_issue_key("proj-1") == "PROJ"

    def test_invalid_format_raises(self) -> None:
        with pytest.raises(ValueError, match="Invalid issue key"):
            extract_project_from_issue_key("NOHYPHEN")


class TestCheckProjectAccess:
    def test_no_filter_always_passes(self) -> None:
        check_project_access("ANY", None)

    def test_allowed_key_passes(self) -> None:
        check_project_access("MYPROJ", frozenset({"MYPROJ", "OTHER"}))

    def test_case_insensitive(self) -> None:
        check_project_access("myproj", frozenset({"MYPROJ"}))

    def test_denied_key_raises(self) -> None:
        with pytest.raises(AccessDeniedError, match="SECRET"):
            check_project_access("SECRET", frozenset({"MYPROJ"}))


class TestRewriteJQL:
    def test_no_filter_passthrough(self) -> None:
        jql = "project = SECRET AND status = Open"
        assert rewrite_jql_project_clause(jql, None) == jql

    def test_single_allowed_project(self) -> None:
        result = rewrite_jql_project_clause(
            "status = Open", frozenset({"MYPROJ"})
        )
        assert 'project = "MYPROJ"' in result
        assert "status = Open" in result

    def test_replaces_existing_project_clause(self) -> None:
        # Caller tries to bypass by supplying project = SECRET
        result = rewrite_jql_project_clause(
            "project = SECRET AND status = Open",
            frozenset({"MYPROJ"}),
        )
        assert "SECRET" not in result
        assert 'project = "MYPROJ"' in result

    def test_preserves_order_by(self) -> None:
        result = rewrite_jql_project_clause(
            "status = Open ORDER BY created DESC",
            frozenset({"MYPROJ"}),
        )
        assert result.endswith("ORDER BY created DESC")

    def test_multi_project_uses_in_clause(self) -> None:
        result = rewrite_jql_project_clause(
            "status = Open",
            frozenset({"A", "B"}),
        )
        assert "project IN" in result
        assert '"A"' in result
        assert '"B"' in result

    def test_project_in_bypass_replaced(self) -> None:
        result = rewrite_jql_project_clause(
            "project IN (SECRET, ANOTHER) AND assignee = me",
            frozenset({"MYPROJ"}),
        )
        assert "SECRET" not in result
        assert "ANOTHER" not in result


class TestRewriteCQL:
    def test_no_filter_passthrough(self) -> None:
        cql = "space = SECRET AND type = page"
        assert rewrite_cql_space_clause(cql, None) == cql

    def test_replaces_caller_space_clause(self) -> None:
        result = rewrite_cql_space_clause(
            "space = SECRET AND type = page",
            frozenset({"DEV"}),
        )
        assert "SECRET" not in result
        assert "space = DEV" in result
```

#### Unit tests for decorators — `tests/unit/servers/test_access_control_decorators.py` (new file)

```python
"""Unit tests for @enforce_project_filter and @enforce_space_filter decorators."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from fastmcp.exceptions import ToolError

from mcp_atlassian.utils.decorators import enforce_project_filter


@pytest.mark.asyncio
async def test_enforce_project_filter_blocks_denied_project() -> None:
    mock_jira = MagicMock()
    mock_jira.config.projects_filter = "MYPROJ"

    @enforce_project_filter(key_arg="project_key")
    async def fake_tool(ctx, project_key: str) -> str:
        return "ok"

    mock_ctx = MagicMock()
    with (
        patch(
            "mcp_atlassian.utils.decorators.get_jira_fetcher",
            new=AsyncMock(return_value=mock_jira),
        ),
        patch(
            "mcp_atlassian.utils.decorators.get_http_request",
            side_effect=RuntimeError("not HTTP"),
        ),
    ):
        with pytest.raises(ToolError, match="ACCESS DENIED|not in the configured"):
            await fake_tool(mock_ctx, project_key="SECRET")


@pytest.mark.asyncio
async def test_enforce_project_filter_allows_whitelisted_project() -> None:
    mock_jira = MagicMock()
    mock_jira.config.projects_filter = "MYPROJ"

    @enforce_project_filter(key_arg="project_key")
    async def fake_tool(ctx, project_key: str) -> str:
        return "ok"

    mock_ctx = MagicMock()
    with (
        patch(
            "mcp_atlassian.utils.decorators.get_jira_fetcher",
            new=AsyncMock(return_value=mock_jira),
        ),
        patch(
            "mcp_atlassian.utils.decorators.get_http_request",
            side_effect=RuntimeError("not HTTP"),
        ),
    ):
        result = await fake_tool(mock_ctx, project_key="MYPROJ")
        assert result == "ok"


@pytest.mark.asyncio
async def test_enforce_project_filter_no_filter_allows_all() -> None:
    mock_jira = MagicMock()
    mock_jira.config.projects_filter = None  # No filter = allow all

    @enforce_project_filter(key_arg="project_key")
    async def fake_tool(ctx, project_key: str) -> str:
        return "ok"

    mock_ctx = MagicMock()
    with (
        patch(
            "mcp_atlassian.utils.decorators.get_jira_fetcher",
            new=AsyncMock(return_value=mock_jira),
        ),
        patch(
            "mcp_atlassian.utils.decorators.get_http_request",
            side_effect=RuntimeError("not HTTP"),
        ),
    ):
        result = await fake_tool(mock_ctx, project_key="ANYTHING")
        assert result == "ok"
```

#### Integration tests for JQL rewrite — extend `tests/unit/jira/test_search.py`

Add test cases that configure `projects_filter = "MYPROJ"` on a mock config
and assert that calls to `search_issues(jql="project = SECRET AND ...")` have
"SECRET" removed and "MYPROJ" present in the JQL sent to the API.

#### Regression test for filter propagation

Extend `tests/unit/servers/test_dependencies.py` to assert that
`_create_user_config_for_fetcher` correctly propagates `projects_filter` and
`spaces_filter` from global config to per-user configs for all three auth
types (oauth, pat, basic).

---

### Rollout Plan

#### Phase 1 — Branch and utilities (1–2 days)
1. `git checkout -b feat/ds-2031-access-control-enforcement`
2. Implement `utils/access_control.py` and `utils/audit.py`
3. Add `MCPAtlassianAccessDeniedError` to `exceptions.py`
4. Write and pass all unit tests for the new utilities
5. Run `pre-commit run --all-files` to ensure lint/type checks pass

#### Phase 2 — Fix JQL/CQL bypass (0.5 days)
1. Update `jira/search.py` to use `rewrite_jql_project_clause`
2. Update `confluence/search.py` to use `rewrite_cql_space_clause`
3. Add `tests/unit/jira/test_search.py` rewrite test cases
4. Run existing search tests to verify no regressions

#### Phase 3 — Decorator implementation and application (2–3 days)
1. Add `enforce_project_filter` and `enforce_space_filter` to `utils/decorators.py`
2. Apply decorators systematically to all target tools in `servers/jira.py`
3. Apply decorators to all target tools in `servers/confluence.py`
4. Write decorator unit tests
5. Run full test suite: `uv run pytest -xvs`

#### Phase 4 — Audit logging verification (0.5 days)
1. Manually test with `JIRA_PROJECTS_FILTER=TESTPROJ` and attempt access to
   both allowed and denied projects
2. Verify `mcp-atlassian.audit` logger emits structured JSON records with
   correct `outcome` values
3. Check that denied requests return `ToolError` with a clear message

#### Phase 5 — CI guard (0.5 days)
1. Add `scripts/check_access_control.py` to scan server files for tools missing
   the decorator
2. Register the check in `.pre-commit-config.yaml` as a local hook

#### Environment variable documentation additions

```bash
# Access control: project whitelist for Jira
# Comma-separated project keys. If set, all Jira tool calls are restricted
# to these projects. JQL/CQL queries are rewritten to enforce this.
# Example: JIRA_PROJECTS_FILTER=MYPROJ,OTHERAPP
JIRA_PROJECTS_FILTER=

# Access control: space whitelist for Confluence
# Example: CONFLUENCE_SPACES_FILTER=TEAM,DEV,DOCS
CONFLUENCE_SPACES_FILTER=
```

---

### Verification

#### End-to-end test scenarios

| Scenario | Setup | Expected outcome |
|---|---|---|
| Jira `create_issue` with allowed project | `JIRA_PROJECTS_FILTER=MYPROJ`; call with `project_key=MYPROJ` | Issue created; audit log shows ALLOWED |
| Jira `create_issue` with denied project | `JIRA_PROJECTS_FILTER=MYPROJ`; call with `project_key=SECRET` | ToolError "Access denied: project 'SECRET'"; audit log shows DENIED |
| Jira `search` with bypass JQL | `JIRA_PROJECTS_FILTER=MYPROJ`; call with `jql="project = SECRET"` | JQL rewritten to `project = "MYPROJ"`; search executes against MYPROJ only |
| Jira `search` with no filter | `JIRA_PROJECTS_FILTER` unset; any JQL | JQL unchanged; search executes normally |
| Jira `get_issue` with allowed project | `JIRA_PROJECTS_FILTER=MYPROJ`; `issue_key=MYPROJ-1` | Issue returned; audit log shows ALLOWED |
| Jira `get_issue` with denied project | `JIRA_PROJECTS_FILTER=MYPROJ`; `issue_key=SECRET-1` | ToolError; audit log shows DENIED |
| Confluence `create_page` with allowed space | `CONFLUENCE_SPACES_FILTER=DEV`; `space_key=DEV` | Page created |
| Confluence `create_page` with denied space | `CONFLUENCE_SPACES_FILTER=DEV`; `space_key=HR` | ToolError "Access denied: space 'HR'" |
| Confluence `search` bypass | `CONFLUENCE_SPACES_FILTER=DEV`; `cql="space = SECRET"` | CQL rewritten; query runs in DEV space |
| Per-user token with filter | Multi-tenant: global config has filter; user provides PAT | Filter from global config propagated to per-user config; all checks apply |

---

## 6. Challenges and Tradeoffs

### Pros

**Security:**
- Eliminates all direct project/space bypass vectors (JQL injection, direct
  parameter access to out-of-scope resources)
- Audit log provides a complete trail of all access decisions, enabling
  SIEM integration and compliance reporting
- Defense-in-depth: query rewrite at the data layer + decorator at the tool
  layer means bypass requires defeating two independent mechanisms
- Filter propagation to per-user configs (already implemented in
  `_create_user_config_for_fetcher`) means multi-tenant deployments are
  correct by default

**Developer experience:**
- Decorator pattern is idiomatic Python — easy to apply, review, and reason
  about
- `check_write_access` pattern is already established in the codebase; this
  follows the same convention
- `parse_filter_set` returns `None` when no filter is set, so all 73 tools
  continue to work unchanged for deployments without a filter

**Maintenance:**
- The decorator is the single source of truth for project enforcement; fixing
  a bypass bug in the decorator fixes all tools simultaneously
- JQL/CQL rewrite is isolated to two methods, not a general-purpose parser

### Cons

**Page-ID tools (Confluence):**
- Page-ID-based tools cannot validate the space key before the API call — they
  must fetch page metadata first and validate the space key from the response
- This costs one extra API call per page-ID-based write operation when a filter
  is configured
- Mitigation: a `TTLCache` on page-ID → space-key lookups can reduce this to
  near-zero for repeat accesses within a session

**Board/Sprint tools (Jira):**
- Tools like `get_board_issues`, `get_sprint_issues`, `create_sprint`, and
  `update_sprint` are scoped to board/sprint IDs, not project keys
- These IDs can belong to projects outside the whitelist
- Mitigation: `get_board_issues` and `get_sprint_issues` route through
  `search_issues`, which applies the JQL rewrite — so results are filtered
  even if the board itself is not
- Sprint creation and sprint updates do not expose issue data; the risk is
  low (sprint metadata only), and these are write tools behind `@check_write_access`
- A follow-on improvement can map board_id → project_key via the Jira Agile
  API and validate accordingly

**Upstream rebase burden:**
- Every rebase from `sooperset/mcp-atlassian` requires checking new tool
  handlers for the decorator
- Mitigated by the CI lint guard (Step 10) which will fail the build if a
  new tool handler is added without the decorator

**JQL/CQL parser limitations:**
- The rewrite uses regex, not a full grammar parser
- Edge cases: nested parentheses beyond one level, JQL function calls that
  reference project lists, custom fields that contain "project"
- The approach is conservative: when in doubt, the rewrite enforces the
  whitelist, which may over-restrict rather than under-restrict
- A future iteration could use a proper JQL AST parser (there are no
  maintained Python JQL parser libraries as of 2025; a custom one would need
  to be written or the Jira Cloud JQL parse API used)

**Audit log performance:**
- Structured JSON serialization on every tool call adds a small overhead
  (~microseconds per call)
- Use `logging.getLogger("mcp-atlassian.audit").isEnabledFor(INFO)` as a
  guard in high-frequency paths if this becomes a concern

### Key Challenges

#### Cloud vs Server/DC divergence

- Cloud: issue keys follow the `[A-Z]{2,10}-\d+` format strictly
- Server/DC: project keys can be up to 10 characters and include underscores
  (e.g., `MY_PROJ-1`) — the `PROJECT_KEY_PATTERN` regex in `servers/jira.py`
  already handles this with `[A-Z][A-Z0-9_]+`
- `extract_project_from_issue_key` must use `split("-", 1)` not `split("-")` to
  handle issue keys where the project key itself might contain unusual characters
- Space keys in Confluence are case-sensitive in some API versions — always
  normalize to uppercase in both the filter and the incoming key

#### `READ_ONLY_MODE` interaction

- `@check_write_access` and `@enforce_project_filter` are independent decorators
- Order matters: `@check_write_access` should be outermost (applied first) so
  that a read-only mode rejection happens before the project filter check
- This keeps error messages clear: "cannot create_issue in read-only mode" vs
  "access denied: project 'X'"

#### Multi-tenant per-user PAT header flow

- In the header-PAT branch (`Branch 1` in `_get_fetcher`), the fetcher is
  created with `**spec.filter_kwargs` which sets `projects_filter=None`
  (see `servers/dependencies.py` line 554)
- This means **the per-user header-PAT config has no filter** — the decorator
  would be a no-op for header-PAT users
- Fix: change `filter_kwargs={"projects_filter": None}` in `_jira_spec()` to
  `filter_kwargs={"projects_filter": os.getenv("JIRA_PROJECTS_FILTER")}` — or
  better, read the filter from the global config when building the header-PAT
  config. This is a critical bug that must be fixed in Phase 1.

#### Upstream rebase strategy

The key decision for maintaining the fork is **minimizing diff surface area**:

1. Keep all access-control code in `utils/access_control.py`,
   `utils/audit.py`, and the two new decorators — files that do not exist
   upstream
2. Changes to `jira/search.py` and `confluence/search.py` (the JQL/CQL
   rewrite) will conflict on rebase — these are the only upstream files that
   need material changes
3. The decorator applications in `servers/jira.py` and `servers/confluence.py`
   add one or two lines per tool — these are line-level additions that are
   low-conflict risk on rebase
4. Use a `git rerere` cache for the known conflict patterns in `search.py`
5. Maintain a `CHANGELOG-ka.md` in the repo root to track all fork-specific
   changes with the commit SHAs that introduced them — this is the rebase
   runbook

---

## 7. Alternative Approaches (If Recommendation Doesn't Fit)

### Alternative A — OPA Policy-as-Code

**When to use:** If the number of distinct access policies grows (e.g., per-user
role-based project access rather than a single global whitelist), OPA becomes
more maintainable than Python decorators.

**Pre-requisites:**
- OPA sidecar or embedded via `python-opa-client`
- Policy bundle deployment pipeline

**Implementation sketch:**
1. Deploy OPA as a sidecar in Docker Compose / Kubernetes
2. Author a Rego policy: `allow if input.project_key in data.allowed_projects`
3. Add a FastMCP middleware that calls the OPA decision endpoint before
   forwarding each tool call
4. Bundle the allowed projects list as OPA data, updated via CI when the whitelist changes

**Trade-off:** 3x the infrastructure, 3x the maintenance surface, but audit
logging is built into OPA's decision log format, and policy changes do not
require redeployment of the Python server.

### Alternative B — API Gateway (Kong or AWS API GW)

**When to use:** If the MCP server is already behind an HTTP-transport gateway
for other reasons (rate limiting, mTLS, authentication), the gateway can
additionally parse the JSON-RPC body and enforce project key restrictions.

**Implementation sketch:**
1. Add a Kong custom plugin (Lua) or AWS API GW Lambda authorizer that:
   - Parses the `application/json` request body
   - Extracts `params.arguments.project_key` from the JSON-RPC method call
   - Returns 403 if the project key is not in the allowed list
2. The MCP server continues to run unmodified

**Trade-off:** Only works for HTTP transport (`--transport streamable-http`),
not `stdio`. The `stdio` transport (used with Claude Desktop locally) bypasses
the gateway entirely, leaving a gap. Requires mature JSON-RPC awareness in the
gateway, which is not standard.

### Alternative C — Decorator-only without JQL rewrite

**When to use:** Faster implementation if JQL bypass via `jira_search` is
acceptable in the short term (e.g., `READ_ONLY_MODE=true` is active and all
search tools are already restricted).

**What to skip:** Steps 4 and related CQL tests.

**Risk:** The `search` tool and `get_board_issues` tool remain bypassable for
read-only data exfiltration across project boundaries. Acceptable only if
`READ_ONLY_MODE=true` is enforced at all times.
