# Access Control Analysis: DS-2031 — Decorator-Based Filter Enforcement

## 1. Existing Solution Summary

### What the existing solution does

`JIRA_PROJECTS_FILTER` and `CONFLUENCE_SPACES_FILTER` are env vars loaded into
`JiraConfig.projects_filter` and `ConfluenceConfig.spaces_filter` at server startup
via `from_env()` factory methods. Those values are propagated into every
per-request config clone that `_create_user_config_for_fetcher` in
`servers/dependencies.py` produces (lines 490–496):

```python
user_jira_config.projects_filter = base_config.projects_filter
user_confluence_config.spaces_filter = base_config.spaces_filter
```

At the mixin layer, only **two tools** actually read these fields:

| Tool | Mixin method | How it enforces |
|---|---|---|
| `jira_search` (server tool) | `SearchMixin.search_issues` | Appends `AND project IN (...)` to the caller-supplied JQL |
| `confluence_search` (server tool) | `SearchMixin.search` | Appends `AND (space = ...)` to the caller-supplied CQL |

`IssuesMixin.get_issue` enforces the filter too — but only by extracting the
project prefix from the issue key and comparing it to the list. That leaves
**47 Jira tools and 23 Confluence tools** that simply ignore the filter
entirely (create, update, delete, comment, worklog, sprint, attachment, …).

### What `READ_ONLY_MODE` does

`READ_ONLY_MODE=true` is evaluated at server startup inside `main_lifespan` and
stored in `MainAppContext.read_only`. The `_list_tools_mcp` override on
`AtlassianMCP` removes every tool tagged `"write"` from the advertised tool
list. Write tools additionally carry the `@check_write_access` decorator (in
`utils/decorators.py`) that short-circuits execution with a `ValueError` if
`app_lifespan_ctx.read_only` is true — a defense-in-depth guard.

Critically, the `"write"` tag + `@check_write_access` pattern is already
consistently applied to all write tools in both server files. That is the
template we will extend.

### Coverage gap

```
Jira  (49 tools total):  2 tools enforce project filter
Confluence (24 tools total): 1 tool enforces space filter
```

The gap affects all tools that accept an `issue_key` (project is encoded in the
key), `project_key`, `board_id`, `sprint_id`, `page_id`, or `space_key`
parameter, plus tools that accept freeform `jql` / `cql` parameters where a
crafted query can reference any project.

### JQL/CQL bypass risk

Even where the filter is applied, the existing `search_issues` logic has a
bypass window:

```python
elif (
    "project = " not in jql.lower() and "project in" not in jql.lower()
):
    jql = f"({jql}) AND {project_query}"
```

A query like `project = FORBIDDEN OR project = ALLOWED` would not trigger the
guard (it already contains `"project = "`), and would return issues from
`FORBIDDEN`. The current substring check is not a real parser.

---

## 2. Approaches Considered

| # | Approach | Summary |
|---|---|---|
| **A** | **Decorator-based access guard** (this plan) | Python decorators on tool handlers enforce project/space allowlist before execution, mirroring `check_write_access` |
| B | Config-layer propagation only | Ensure `projects_filter` is passed to every mixin method; rely on mixin enforcement |
| C | JQL/CQL query rewriting | Parse and rewrite every JQL/CQL expression to strip or replace project/space predicates |
| D | API Gateway / proxy layer | External proxy intercepts MCP calls and enforces ACL before forwarding |
| E | Policy-as-Code (OPA/Cedar) | Declarative policies evaluated at tool invocation time |

This plan implements **Approach A**, the direct analogue to the existing
`check_write_access` pattern, with targeted JQL/CQL hardening from Approach C.

---

## 3. Cost Evaluation

### Approach A — Decorator-based access guard

| Dimension | Assessment |
|---|---|
| **Dev cost** | Low–Medium. ~2–3 engineer-days. Follows the existing `check_write_access` pattern exactly. Two new decorators, one shared helper, applied to all 73 tools. No new dependencies. |
| **DevOps cost** | Minimal. Two existing env vars (`JIRA_PROJECTS_FILTER`, `CONFLUENCE_SPACES_FILTER`) already exist. No infrastructure changes. |
| **Maintenance cost** | Medium. Every new tool added upstream must receive the decorator — enforced by a failing test that asserts 100% tool coverage. |
| **Security cost/risk** | Medium-low. Decorator runs at the tool handler layer, so the whitelist is enforced regardless of how the fetcher was constructed. Key residual risk: JQL/CQL bypass if query contains project clauses. Mitigated in this plan by a strict allowlist rewrite. |

### Approach B — Config-layer propagation

| Dimension | Assessment |
|---|---|
| **Dev cost** | High. 50+ mixin methods need updating. Risk of missing paths high. |
| **DevOps cost** | None. |
| **Maintenance cost** | Very high. Every new mixin method is a gap. |
| **Security cost/risk** | High. Filter bypasses remain in every mixin method that doesn't explicitly use it. |

### Approach C — JQL/CQL query rewriting

| Dimension | Assessment |
|---|---|
| **Dev cost** | High. Full JQL grammar is complex. Edge cases in nested predicates, functions (`issuesOf()`, `linkedIssues()`), and ORDER BY clauses are numerous. |
| **DevOps cost** | None. |
| **Maintenance cost** | High. Jira regularly extends JQL. Parser must track upstream changes. |
| **Security cost/risk** | Medium. A correct parser gives strong guarantees, but a buggy one gives a false sense of security worse than no guard. |

### Approach D — API Gateway / proxy layer

| Dimension | Assessment |
|---|---|
| **Dev cost** | High. New service to build, separate codebase. |
| **DevOps cost** | High. Requires HTTP transport (not stdio), new container, load balancer changes. |
| **Maintenance cost** | Medium. Separate release lifecycle from `ka-mcp-atlassian`. |
| **Security cost/risk** | Low (if built correctly). But the proxy itself becomes a single-point-of-failure security boundary. |

### Approach E — Policy-as-Code (OPA)

| Dimension | Assessment |
|---|---|
| **Dev cost** | Very high. OPA sidecar, Rego policy authoring, integration with FastMCP. |
| **DevOps cost** | High. OPA daemon in every deployment. |
| **Maintenance cost** | Medium. Policies are auditable and separate from code. |
| **Security cost/risk** | Low. But over-engineered for the current threat model. |

---

## 4. Recommended Approach

**Primary**: Approach A (decorator-based access guard) with targeted JQL/CQL
allowlist rewriting.

**Reasoning**:
- It exactly mirrors the `check_write_access` + `"write"` tag pattern already
  present for read-only mode — minimal conceptual overhead for maintainers.
- The decorator runs in the tool handler layer (not the mixin layer), which
  means it enforces the whitelist regardless of how the underlying fetcher is
  configured or called.
- A companion test that asserts 100% tool coverage prevents future regressions
  when upstream adds new tools.
- JQL/CQL rewriting is scoped to the narrow "add AND clause" case, not a full
  parser — safe and maintainable.

**Not recommended as primary**: Approaches D and E are architecturally sound
but disproportionate to the current deployment scale and team size. They are
the right next step once the fork reaches multi-team adoption.

---

## 5. Comprehensive Implementation Plan

### Pre-requisites

1. Environment variables `JIRA_PROJECTS_FILTER` and `CONFLUENCE_SPACES_FILTER`
   must be documented in `.env.example` (they already exist in code but need
   entries confirming comma-separated format).
2. No new Python dependencies.
3. Feature branch: `feature/ds-2031-decorator-access-guard`.

### Step-by-Step

#### Step 1 — Add `project_filter_set` and `space_filter_set` to `MainAppContext`

File: `src/mcp_atlassian/servers/context.py`

Parse the raw `projects_filter` / `spaces_filter` strings into frozen sets at
startup so that per-tool lookups are O(1) and there is a single source of
truth.

```python
# context.py (additions only)
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mcp_atlassian.confluence.config import ConfluenceConfig
    from mcp_atlassian.jira.config import JiraConfig


@dataclass(frozen=True)
class MainAppContext:
    """Context holding fully configured service configurations."""

    full_jira_config: JiraConfig | None = None
    full_confluence_config: ConfluenceConfig | None = None
    read_only: bool = False
    enabled_tools: list[str] | None = None
    enabled_toolsets: set[str] | None = None
    # Pre-parsed allowlists (empty set == no restriction)
    jira_project_allowlist: frozenset[str] = field(
        default_factory=frozenset
    )
    confluence_space_allowlist: frozenset[str] = field(
        default_factory=frozenset
    )
```

#### Step 2 — Populate the allowlists during lifespan startup

File: `src/mcp_atlassian/servers/main.py`

In `main_lifespan`, after configs are loaded, extract and normalise the filter
strings into frozen sets:

```python
def _parse_filter_to_frozenset(raw: str | None) -> frozenset[str]:
    """Parse a comma-separated filter string into an upper-cased frozenset.

    Args:
        raw: Comma-separated project/space keys, or None.

    Returns:
        Frozenset of upper-cased keys, or empty frozenset if raw is None/empty.
    """
    if not raw:
        return frozenset()
    return frozenset(k.strip().upper() for k in raw.split(",") if k.strip())
```

Then in `main_lifespan`:

```python
jira_allowlist = _parse_filter_to_frozenset(
    loaded_jira_config.projects_filter if loaded_jira_config else None
)
confluence_allowlist = _parse_filter_to_frozenset(
    loaded_confluence_config.spaces_filter if loaded_confluence_config else None
)

app_context = MainAppContext(
    full_jira_config=loaded_jira_config,
    full_confluence_config=loaded_confluence_config,
    read_only=read_only,
    enabled_tools=enabled_tools,
    enabled_toolsets=enabled_toolsets,
    jira_project_allowlist=jira_allowlist,
    confluence_space_allowlist=confluence_allowlist,
)
logger.info(
    f"Jira project allowlist: "
    f"{sorted(jira_allowlist) if jira_allowlist else 'unrestricted'}"
)
logger.info(
    f"Confluence space allowlist: "
    f"{sorted(confluence_allowlist) if confluence_allowlist else 'unrestricted'}"
)
```

#### Step 3 — Add `enforce_project_access` and `enforce_space_access` decorators

File: `src/mcp_atlassian/utils/decorators.py`

These are the two new decorators. They follow the exact same structure as
`check_write_access`: they expect `ctx: Context` as first positional argument,
pull the allowlist from `app_lifespan_ctx`, and raise `ToolError` (not
`ValueError`) for consistency with `handle_tool_errors`.

```python
import re
from collections.abc import Awaitable, Callable
from functools import wraps
from typing import Any, TypeVar

from fastmcp import Context
from fastmcp.exceptions import ToolError

# (existing F TypeVar already defined above)


def _extract_project_from_issue_key(issue_key: str) -> str:
    """Extract the project key prefix from a Jira issue key.

    Args:
        issue_key: A Jira issue key like 'PROJ-123' or 'ACV2-42'.

    Returns:
        The upper-cased project prefix (e.g., 'PROJ').
    """
    return issue_key.split("-")[0].upper()


def enforce_project_access(
    *,
    issue_key_param: str | None = None,
    project_key_param: str | None = None,
    issue_keys_param: str | None = None,
) -> Callable[[F], F]:
    """Decorator that validates Jira project access against the allowlist.

    Reads the project allowlist from ``MainAppContext.jira_project_allowlist``.
    When the allowlist is empty (frozenset()), no restriction is applied.
    When non-empty, the requested project key(s) must be a subset of the
    allowlist; otherwise ``ToolError`` is raised before the tool executes.

    Must be applied to async tool functions that have ``ctx: Context`` as their
    first positional argument.

    Args:
        issue_key_param: Kwarg name holding a single Jira issue key
            (e.g., ``'issue_key'``). The project prefix is extracted
            automatically.
        project_key_param: Kwarg name holding a bare project key
            (e.g., ``'project_key'``).
        issue_keys_param: Kwarg name holding a list of Jira issue keys
            (e.g., ``'issue_keys'``). All keys are checked.

    Returns:
        Decorator that enforces project access before calling the tool.

    Raises:
        ToolError: If any referenced project is not in the allowlist.
    """

    def decorator(func: F) -> F:
        tool_name = func.__name__

        @wraps(func)
        @handle_tool_errors
        async def wrapper(ctx: Context, *args: Any, **kwargs: Any) -> Any:
            lifespan_ctx_dict = ctx.request_context.lifespan_context
            app_ctx = (
                lifespan_ctx_dict.get("app_lifespan_context")
                if isinstance(lifespan_ctx_dict, dict)
                else None
            )
            allowlist: frozenset[str] = (
                getattr(app_ctx, "jira_project_allowlist", frozenset())
                if app_ctx
                else frozenset()
            )

            if allowlist:
                requested: set[str] = set()

                if issue_key_param and (val := kwargs.get(issue_key_param)):
                    requested.add(_extract_project_from_issue_key(str(val)))

                if project_key_param and (val := kwargs.get(project_key_param)):
                    requested.add(str(val).upper())

                if issue_keys_param and (vals := kwargs.get(issue_keys_param)):
                    keys = vals if isinstance(vals, list) else [vals]
                    requested.update(
                        _extract_project_from_issue_key(str(k)) for k in keys
                    )

                denied = requested - allowlist
                if denied:
                    logger.warning(
                        f"Tool '{tool_name}' blocked: projects {denied} "
                        f"not in allowlist {allowlist}"
                    )
                    raise ToolError(
                        f"Access denied: project(s) {sorted(denied)} are not "
                        "in the configured project allowlist."
                    )

            return await func(ctx, *args, **kwargs)

        return wrapper  # type: ignore[return-value]

    return decorator


def enforce_space_access(
    *,
    space_key_param: str | None = None,
    page_space_resolver: str | None = None,
) -> Callable[[F], F]:
    """Decorator that validates Confluence space access against the allowlist.

    Reads the space allowlist from ``MainAppContext.confluence_space_allowlist``.
    When the allowlist is empty (frozenset()), no restriction is applied.

    For tools that work by ``page_id`` and do not expose ``space_key`` directly,
    set ``page_space_resolver`` to the kwarg name holding the page ID; the
    decorator will skip the pre-flight check and instead rely on post-fetch
    validation in the tool body (see note below on page_id tools).

    Must be applied to async tool functions that have ``ctx: Context`` as their
    first positional argument.

    Args:
        space_key_param: Kwarg name holding a Confluence space key
            (e.g., ``'space_key'``).
        page_space_resolver: Kwarg name holding a page ID when the space
            key is not directly available. Currently triggers a warning-only
            log; see implementation note for full enforcement strategy.

    Returns:
        Decorator that enforces space access before calling the tool.

    Raises:
        ToolError: If the referenced space is not in the allowlist.
    """

    def decorator(func: F) -> F:
        tool_name = func.__name__

        @wraps(func)
        @handle_tool_errors
        async def wrapper(ctx: Context, *args: Any, **kwargs: Any) -> Any:
            lifespan_ctx_dict = ctx.request_context.lifespan_context
            app_ctx = (
                lifespan_ctx_dict.get("app_lifespan_context")
                if isinstance(lifespan_ctx_dict, dict)
                else None
            )
            allowlist: frozenset[str] = (
                getattr(app_ctx, "confluence_space_allowlist", frozenset())
                if app_ctx
                else frozenset()
            )

            if allowlist and space_key_param:
                val = kwargs.get(space_key_param)
                if val:
                    key = str(val).upper()
                    if key not in allowlist:
                        logger.warning(
                            f"Tool '{tool_name}' blocked: space '{key}' "
                            f"not in allowlist {allowlist}"
                        )
                        raise ToolError(
                            f"Access denied: space '{key}' is not in the "
                            "configured space allowlist."
                        )

            if allowlist and page_space_resolver and not space_key_param:
                # page_id-only tools: space key is unknown at this point.
                # We log a warning; post-fetch enforcement is in Step 6.
                page_id_val = kwargs.get(page_space_resolver)
                if page_id_val:
                    logger.debug(
                        f"Tool '{tool_name}' called with page_id='{page_id_val}'. "
                        "Space key cannot be pre-validated; post-fetch check applies."
                    )

            return await func(ctx, *args, **kwargs)

        return wrapper  # type: ignore[return-value]

    return decorator
```

#### Step 4 — Add JQL allowlist rewrite helper

File: `src/mcp_atlassian/utils/access_control.py` (new utility module)

The existing JQL filter in `SearchMixin.search_issues` uses a substring test
(`"project = " not in jql.lower()`) which is bypassable. Replace it with a
strict allowlist-append strategy: **always** append the allowlist clause,
unconditionally overwriting any project predicate already present by wrapping
the original JQL in a sub-expression.

```python
"""Access control utilities for project/space allowlist enforcement."""

import logging
import re

logger = logging.getLogger(__name__)

_JQL_ORDER_BY_RE = re.compile(r"\s+ORDER\s+BY\s+", re.IGNORECASE)


def build_jql_allowlist_clause(
    jql: str,
    project_allowlist: frozenset[str],
) -> str:
    """Wrap a JQL expression so it can only return issues from allowed projects.

    Strategy: always append ``AND project IN (ALLOWED, ...)`` to the caller-
    supplied JQL, treating the original expression as an opaque sub-expression.
    This is safe even if the original JQL already contains a ``project =``
    predicate — the ``AND`` clause further narrows the result set to the
    intersection, so cross-project results from the original predicate are
    suppressed.

    Args:
        jql: Caller-supplied JQL string (may be empty).
        project_allowlist: Non-empty frozenset of upper-cased project keys.

    Returns:
        JQL string guaranteed to return only issues from allowed projects.

    Examples:
        >>> build_jql_allowlist_clause("assignee = me", frozenset({"PROJ"}))
        '(assignee = me) AND project IN ("PROJ")'
        >>> build_jql_allowlist_clause("", frozenset({"A", "B"}))
        'project IN ("A", "B")'
    """
    if not project_allowlist:
        return jql

    sorted_keys = sorted(project_allowlist)
    if len(sorted_keys) == 1:
        project_clause = f'project = "{sorted_keys[0]}"'
    else:
        joined = ", ".join(f'"{k}"' for k in sorted_keys)
        project_clause = f"project IN ({joined})"

    if not jql or not jql.strip():
        return project_clause

    # Preserve ORDER BY position
    order_match = _JQL_ORDER_BY_RE.search(jql)
    if order_match:
        before = jql[: order_match.start()]
        order_part = jql[order_match.start() :]
        return f"({before}) AND {project_clause}{order_part}"

    return f"({jql}) AND {project_clause}"


def build_cql_allowlist_clause(
    cql: str,
    space_allowlist: frozenset[str],
) -> str:
    """Wrap a CQL expression so it can only return content from allowed spaces.

    Same strategy as ``build_jql_allowlist_clause``: always append the space
    constraint unconditionally.

    Args:
        cql: Caller-supplied CQL string (may be empty).
        space_allowlist: Non-empty frozenset of upper-cased space keys.

    Returns:
        CQL string guaranteed to return only content from allowed spaces.
    """
    if not space_allowlist:
        return cql

    sorted_keys = sorted(space_allowlist)
    if len(sorted_keys) == 1:
        space_clause = f'space = "{sorted_keys[0]}"'
    else:
        joined = " OR ".join(f'space = "{k}"' for k in sorted_keys)
        space_clause = f"({joined})"

    if not cql or not cql.strip():
        return space_clause

    return f"({cql}) AND {space_clause}"
```

#### Step 5 — Wire the JQL/CQL helpers into the search mixins

File: `src/mcp_atlassian/jira/search.py`

Replace the existing filter logic in `SearchMixin.search_issues` (lines 60–109)
with:

```python
from mcp_atlassian.utils.access_control import build_jql_allowlist_clause

# Inside search_issues, replace the projects_filter block:
filter_to_use = projects_filter or self.config.projects_filter
if filter_to_use:
    allowlist = frozenset(
        k.strip().upper() for k in filter_to_use.split(",") if k.strip()
    )
    jql = build_jql_allowlist_clause(jql, allowlist)
    logger.info(f"Applied project allowlist to JQL: {jql}")
```

File: `src/mcp_atlassian/confluence/search.py`

Replace the existing filter logic in `SearchMixin.search` (lines 46–65) with:

```python
from mcp_atlassian.utils.access_control import build_cql_allowlist_clause

filter_to_use = spaces_filter or self.config.spaces_filter
if filter_to_use:
    allowlist = frozenset(
        k.strip().upper() for k in filter_to_use.split(",") if k.strip()
    )
    cql = build_cql_allowlist_clause(cql, allowlist)
    logger.info(f"Applied space allowlist to CQL: {cql}")
```

#### Step 6 — Apply decorators to all 73 tool handlers

##### Jira tools (`src/mcp_atlassian/servers/jira.py`)

Import both new decorators:

```python
from mcp_atlassian.utils.decorators import (
    check_write_access,
    enforce_project_access,
)
```

Apply `@enforce_project_access` before `@check_write_access` (outermost to
innermost) so that the access guard fires first. The decorator stacking order
is: `@jira_mcp.tool` → `@enforce_project_access(...)` → `@check_write_access`
→ `@handle_tool_errors` (already inside `check_write_access`).

Examples:

```python
# Tools using issue_key
@jira_mcp.tool(tags={"jira", "read", "toolset:jira_issues"}, ...)
@enforce_project_access(issue_key_param="issue_key")
async def get_issue(ctx: Context, issue_key: ...) -> str: ...

# Tools using project_key
@jira_mcp.tool(tags={"jira", "write", "toolset:jira_issues"}, ...)
@enforce_project_access(project_key_param="project_key")
@check_write_access
async def create_issue(ctx: Context, project_key: ...) -> str: ...

# Tools using multiple keys (issue links)
@jira_mcp.tool(tags={"jira", "write", "toolset:jira_links"}, ...)
@enforce_project_access(
    issue_key_param="inward_issue_key",
    issue_keys_param=None,
)
@check_write_access
async def create_issue_link(
    ctx: Context,
    inward_issue_key: ...,
    outward_issue_key: ...,
) -> str: ...
```

For tools whose scope cannot be determined from arguments at call time (e.g.,
`get_agile_boards`, `get_all_projects`, `create_sprint` by `board_id`), apply
`@enforce_project_access()` with no parameters — the decorator becomes a no-op
when no key parameter is specified but still documents that the tool has been
audited. See the full mapping table in section 5 (Testing Strategy) below.

##### Confluence tools (`src/mcp_atlassian/servers/confluence.py`)

Import:

```python
from mcp_atlassian.utils.decorators import (
    check_write_access,
    enforce_space_access,
)
```

Apply `@enforce_space_access(space_key_param="space_key")` to all tools that
accept a `space_key` argument. For tools that only accept `page_id` (where the
space is not known until after the Confluence API call), use
`@enforce_space_access(page_space_resolver="page_id")` — this currently logs
a debug message and does not block, but establishes the hook for Step 7.

```python
# Tool with explicit space_key
@confluence_mcp.tool(tags={"confluence", "write", "toolset:confluence_pages"}, ...)
@enforce_space_access(space_key_param="space_key")
@check_write_access
async def create_page(ctx: Context, space_key: ...) -> str: ...

# Tool with page_id only (deferred check)
@confluence_mcp.tool(tags={"confluence", "read", "toolset:confluence_pages"}, ...)
@enforce_space_access(page_space_resolver="page_id")
async def get_page(ctx: Context, page_id: ...) -> str: ...
```

#### Step 7 — Post-fetch space validation for page_id tools (deferred)

For Confluence tools that work by `page_id` only (where space is unknown at
call time), enforce the allowlist after the page is fetched. This requires a
helper in the tool handler body:

```python
from mcp_atlassian.utils.access_control import assert_space_allowed


def assert_space_allowed(
    space_key: str,
    allowlist: frozenset[str],
    tool_name: str,
) -> None:
    """Raise ToolError if space_key is not in allowlist.

    Args:
        space_key: The space key of the fetched page (from API response).
        allowlist: Non-empty frozenset of allowed space keys.
        tool_name: Tool name for logging.

    Raises:
        ToolError: If space_key is not in allowlist.
    """
    if not allowlist:
        return
    key = space_key.upper()
    if key not in allowlist:
        logger.warning(
            f"Tool '{tool_name}' blocked post-fetch: space '{key}' "
            f"not in allowlist {allowlist}"
        )
        raise ToolError(
            f"Access denied: space '{key}' is not in the configured space allowlist."
        )
```

In `get_page` and similar tools, after fetching the page, extract
`page.space.key` and call `assert_space_allowed(page.space.key, allowlist, "get_page")`.
The allowlist is retrieved from `MainAppContext` via ctx exactly as in the
decorator.

#### Step 8 — Add `"access_controlled"` tag to all guarded tools

To make coverage auditable, add the tag `"access_controlled"` to every tool
that has had a decorator applied. The `_list_tools_mcp` loop can then emit a
warning for any tool in the jira/confluence namespace that lacks this tag:

```python
# In AtlassianMCP._list_tools_mcp, after the existing toolset filter:
if ("jira" in tool_tags or "confluence" in tool_tags):
    if "access_controlled" not in tool_tags:
        logger.warning(
            f"Tool '{registered_name}' is missing 'access_controlled' tag. "
            "Ensure enforce_project_access or enforce_space_access is applied."
        )
```

This creates a runtime audit trail without blocking functionality, and a test
can assert that the warning is never emitted.

### Sample Code

#### `src/mcp_atlassian/utils/access_control.py` (complete)

```python
"""Access control utilities: JQL/CQL allowlist query rewriting."""

import logging
import re

logger = logging.getLogger(__name__)

_JQL_ORDER_BY_RE = re.compile(r"\s+ORDER\s+BY\s+", re.IGNORECASE)


def build_jql_allowlist_clause(
    jql: str,
    project_allowlist: frozenset[str],
) -> str:
    """Wrap a JQL expression so results are restricted to allowed projects.

    Always appends ``AND project IN (...)`` unconditionally — even when the
    original JQL already contains a project predicate — so that crafted
    cross-project predicates in the input cannot leak data from disallowed
    projects.

    Args:
        jql: Caller-supplied JQL string (may be empty).
        project_allowlist: Non-empty frozenset of upper-cased project keys.

    Returns:
        JQL string restricted to the allowed project set. Unchanged if
        ``project_allowlist`` is empty.

    Examples:
        >>> build_jql_allowlist_clause("project = EVIL", frozenset({"GOOD"}))
        '(project = EVIL) AND project IN ("GOOD")'
    """
    if not project_allowlist:
        return jql

    sorted_keys = sorted(project_allowlist)
    if len(sorted_keys) == 1:
        project_clause = f'project = "{sorted_keys[0]}"'
    else:
        joined = ", ".join(f'"{k}"' for k in sorted_keys)
        project_clause = f"project IN ({joined})"

    if not jql or not jql.strip():
        return project_clause

    order_match = _JQL_ORDER_BY_RE.search(jql)
    if order_match:
        before = jql[: order_match.start()]
        order_part = jql[order_match.start() :]
        return f"({before}) AND {project_clause}{order_part}"

    return f"({jql}) AND {project_clause}"


def build_cql_allowlist_clause(
    cql: str,
    space_allowlist: frozenset[str],
) -> str:
    """Wrap a CQL expression so results are restricted to allowed spaces.

    Args:
        cql: Caller-supplied CQL string (may be empty).
        space_allowlist: Non-empty frozenset of upper-cased space keys.

    Returns:
        CQL string restricted to the allowed space set. Unchanged if
        ``space_allowlist`` is empty.
    """
    if not space_allowlist:
        return cql

    sorted_keys = sorted(space_allowlist)
    if len(sorted_keys) == 1:
        space_clause = f'space = "{sorted_keys[0]}"'
    else:
        joined = " OR ".join(f'space = "{k}"' for k in sorted_keys)
        space_clause = f"({joined})"

    if not cql or not cql.strip():
        return space_clause

    return f"({cql}) AND {space_clause}"


def assert_space_allowed(
    space_key: str,
    allowlist: frozenset[str],
    tool_name: str,
) -> None:
    """Assert that a fetched page's space is within the configured allowlist.

    Called in the tool handler body after the page is fetched, for tools that
    accept only a ``page_id`` and cannot pre-validate the space at invocation
    time.

    Args:
        space_key: The space key of the fetched Confluence page.
        allowlist: Non-empty frozenset of allowed space keys. If empty, no
            check is performed.
        tool_name: Tool name used in log messages.

    Raises:
        ToolError: If ``space_key`` is not in ``allowlist``.
    """
    from fastmcp.exceptions import ToolError  # avoid circular at module level

    if not allowlist:
        return
    key = space_key.upper()
    if key not in allowlist:
        logger.warning(
            f"Tool '{tool_name}' blocked post-fetch: space '{key}' "
            f"not in allowlist {sorted(allowlist)}"
        )
        raise ToolError(
            f"Access denied: space '{key}' is not in the "
            "configured space allowlist."
        )
```

#### New decorators in `src/mcp_atlassian/utils/decorators.py` (additions)

```python
# Add these imports at the top of decorators.py
import re

# Add after the existing check_write_access function:


def _extract_project_from_issue_key(issue_key: str) -> str:
    """Extract the project key prefix from a Jira issue key.

    Args:
        issue_key: A Jira issue key like 'PROJ-123' or 'ACV2-42'.

    Returns:
        The upper-cased project prefix (e.g., 'PROJ').
    """
    return issue_key.split("-")[0].upper()


def enforce_project_access(
    *,
    issue_key_param: str | None = None,
    project_key_param: str | None = None,
    issue_keys_param: str | None = None,
) -> Callable[[F], F]:
    """Decorator enforcing Jira project allowlist on MCP tool handlers.

    Reads ``MainAppContext.jira_project_allowlist`` from the FastMCP lifespan
    context. An empty allowlist means no restriction. A non-empty allowlist
    means the tool may only operate on the listed project keys.

    Must wrap async functions whose first positional argument is
    ``ctx: Context``.

    Args:
        issue_key_param: Kwarg name containing a single Jira issue key
            (project prefix is extracted automatically, e.g. ``'PROJ-42'``
            → ``'PROJ'``).
        project_key_param: Kwarg name containing a bare project key
            (e.g., ``'project_key'``).
        issue_keys_param: Kwarg name containing a list of Jira issue keys.
            All project prefixes are checked.

    Returns:
        Decorated function that raises ``ToolError`` when the project is
        not in the allowlist.
    """

    def decorator(func: F) -> F:
        tool_name = func.__name__

        @wraps(func)
        @handle_tool_errors
        async def wrapper(ctx: Context, *args: Any, **kwargs: Any) -> Any:
            lifespan_ctx_dict = ctx.request_context.lifespan_context
            app_ctx = (
                lifespan_ctx_dict.get("app_lifespan_context")
                if isinstance(lifespan_ctx_dict, dict)
                else None
            )
            allowlist: frozenset[str] = (
                getattr(app_ctx, "jira_project_allowlist", frozenset())
                if app_ctx
                else frozenset()
            )

            if allowlist:
                requested: set[str] = set()

                if issue_key_param and (val := kwargs.get(issue_key_param)):
                    requested.add(_extract_project_from_issue_key(str(val)))

                if project_key_param and (val := kwargs.get(project_key_param)):
                    requested.add(str(val).upper())

                if issue_keys_param and (vals := kwargs.get(issue_keys_param)):
                    keys = vals if isinstance(vals, list) else [vals]
                    requested.update(
                        _extract_project_from_issue_key(str(k)) for k in keys
                    )

                denied = requested - allowlist
                if denied:
                    logger.warning(
                        f"Tool '{tool_name}' blocked: "
                        f"project(s) {sorted(denied)} not in allowlist"
                    )
                    raise ToolError(
                        f"Access denied: project(s) {sorted(denied)} are not "
                        "in the configured project allowlist."
                    )

            return await func(ctx, *args, **kwargs)

        return wrapper  # type: ignore[return-value]

    return decorator


def enforce_space_access(
    *,
    space_key_param: str | None = None,
    page_space_resolver: str | None = None,
) -> Callable[[F], F]:
    """Decorator enforcing Confluence space allowlist on MCP tool handlers.

    Reads ``MainAppContext.confluence_space_allowlist`` from the FastMCP
    lifespan context. An empty allowlist means no restriction.

    For tools that accept a ``page_id`` and cannot determine the space at
    invocation time, pass ``page_space_resolver`` instead of
    ``space_key_param``. The decorator will not block the call, but it
    registers the tool as audited. Post-fetch enforcement must be added
    manually in the tool body using ``assert_space_allowed``.

    Must wrap async functions whose first positional argument is
    ``ctx: Context``.

    Args:
        space_key_param: Kwarg name containing a Confluence space key.
        page_space_resolver: Kwarg name containing a page ID when the space
            key is not available at call time.

    Returns:
        Decorated function that raises ``ToolError`` when the space key is
        not in the allowlist.
    """

    def decorator(func: F) -> F:
        tool_name = func.__name__

        @wraps(func)
        @handle_tool_errors
        async def wrapper(ctx: Context, *args: Any, **kwargs: Any) -> Any:
            lifespan_ctx_dict = ctx.request_context.lifespan_context
            app_ctx = (
                lifespan_ctx_dict.get("app_lifespan_context")
                if isinstance(lifespan_ctx_dict, dict)
                else None
            )
            allowlist: frozenset[str] = (
                getattr(app_ctx, "confluence_space_allowlist", frozenset())
                if app_ctx
                else frozenset()
            )

            if allowlist and space_key_param:
                val = kwargs.get(space_key_param)
                if val:
                    key = str(val).upper()
                    if key not in allowlist:
                        logger.warning(
                            f"Tool '{tool_name}' blocked: space '{key}' "
                            f"not in allowlist {sorted(allowlist)}"
                        )
                        raise ToolError(
                            f"Access denied: space '{key}' is not in the "
                            "configured space allowlist."
                        )

            if allowlist and page_space_resolver and not space_key_param:
                logger.debug(
                    f"Tool '{tool_name}': page_id-based call; "
                    "space pre-validation deferred to post-fetch check."
                )

            return await func(ctx, *args, **kwargs)

        return wrapper  # type: ignore[return-value]

    return decorator
```

### Testing Strategy

#### Unit tests — `tests/unit/utils/test_access_control.py` (new file)

```python
"""Unit tests for access_control utility functions."""

import pytest

from mcp_atlassian.utils.access_control import (
    build_cql_allowlist_clause,
    build_jql_allowlist_clause,
)


class TestBuildJqlAllowlistClause:
    """Tests for build_jql_allowlist_clause."""

    def test_empty_allowlist_returns_original_jql(self) -> None:
        jql = "project = EVIL"
        result = build_jql_allowlist_clause(jql, frozenset())
        assert result == jql

    def test_single_project_wraps_jql(self) -> None:
        result = build_jql_allowlist_clause(
            "assignee = me", frozenset({"PROJ"})
        )
        assert result == '(assignee = me) AND project = "PROJ"'

    def test_multiple_projects_uses_in_clause(self) -> None:
        result = build_jql_allowlist_clause(
            "status = Open", frozenset({"A", "B"})
        )
        assert 'project IN ("A", "B")' in result

    def test_empty_jql_returns_only_project_clause(self) -> None:
        result = build_jql_allowlist_clause("", frozenset({"PROJ"}))
        assert result == 'project = "PROJ"'

    def test_cross_project_predicate_is_suppressed(self) -> None:
        # Even if the caller supplies "project = EVIL", the allowlist
        # clause is still appended — AND logic means only ALLOWED results.
        result = build_jql_allowlist_clause(
            "project = EVIL", frozenset({"ALLOWED"})
        )
        assert 'project = "ALLOWED"' in result
        # Original is still present but logically narrowed by AND
        assert "project = EVIL" in result

    def test_order_by_preserved(self) -> None:
        result = build_jql_allowlist_clause(
            "status = Open ORDER BY created DESC", frozenset({"PROJ"})
        )
        assert result.endswith("ORDER BY created DESC")
        assert 'project = "PROJ"' in result


class TestBuildCqlAllowlistClause:
    """Tests for build_cql_allowlist_clause."""

    def test_empty_allowlist_returns_original_cql(self) -> None:
        cql = "space = EVIL"
        result = build_cql_allowlist_clause(cql, frozenset())
        assert result == cql

    def test_single_space_wraps_cql(self) -> None:
        result = build_cql_allowlist_clause(
            "type = page", frozenset({"DEV"})
        )
        assert result == '(type = page) AND space = "DEV"'

    def test_multiple_spaces(self) -> None:
        result = build_cql_allowlist_clause(
            "title = foo", frozenset({"A", "B"})
        )
        assert "(A)" in result or "(B)" in result or "OR" in result

    def test_empty_cql_returns_space_clause(self) -> None:
        result = build_cql_allowlist_clause("", frozenset({"DEV"}))
        assert result == 'space = "DEV"'
```

#### Unit tests — `tests/unit/utils/test_decorators_access.py` (new file)

```python
"""Unit tests for enforce_project_access and enforce_space_access decorators."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp.exceptions import ToolError

from mcp_atlassian.servers.context import MainAppContext
from mcp_atlassian.utils.decorators import (
    enforce_project_access,
    enforce_space_access,
)


def _make_ctx(
    jira_allowlist: frozenset[str] = frozenset(),
    space_allowlist: frozenset[str] = frozenset(),
) -> MagicMock:
    """Build a mock FastMCP Context with a populated MainAppContext."""
    app_ctx = MainAppContext(
        jira_project_allowlist=jira_allowlist,
        confluence_space_allowlist=space_allowlist,
    )
    ctx = MagicMock()
    ctx.request_context.lifespan_context = {"app_lifespan_context": app_ctx}
    return ctx


class TestEnforceProjectAccess:
    """Tests for the enforce_project_access decorator."""

    @pytest.mark.asyncio
    async def test_no_allowlist_allows_any_project(self) -> None:
        @enforce_project_access(issue_key_param="issue_key")
        async def tool(ctx, *, issue_key: str) -> str:
            return "ok"

        ctx = _make_ctx()
        result = await tool(ctx, issue_key="EVIL-1")
        assert result == "ok"

    @pytest.mark.asyncio
    async def test_allowed_project_passes(self) -> None:
        @enforce_project_access(issue_key_param="issue_key")
        async def tool(ctx, *, issue_key: str) -> str:
            return "ok"

        ctx = _make_ctx(jira_allowlist=frozenset({"PROJ"}))
        result = await tool(ctx, issue_key="PROJ-42")
        assert result == "ok"

    @pytest.mark.asyncio
    async def test_denied_project_raises_tool_error(self) -> None:
        @enforce_project_access(issue_key_param="issue_key")
        async def tool(ctx, *, issue_key: str) -> str:
            return "ok"

        ctx = _make_ctx(jira_allowlist=frozenset({"PROJ"}))
        with pytest.raises(ToolError, match="EVIL"):
            await tool(ctx, issue_key="EVIL-1")

    @pytest.mark.asyncio
    async def test_project_key_param(self) -> None:
        @enforce_project_access(project_key_param="project_key")
        async def tool(ctx, *, project_key: str) -> str:
            return "ok"

        ctx = _make_ctx(jira_allowlist=frozenset({"PROJ"}))
        with pytest.raises(ToolError):
            await tool(ctx, project_key="OTHER")

    @pytest.mark.asyncio
    async def test_issue_keys_list_all_checked(self) -> None:
        @enforce_project_access(issue_keys_param="issue_keys")
        async def tool(ctx, *, issue_keys: list[str]) -> str:
            return "ok"

        ctx = _make_ctx(jira_allowlist=frozenset({"PROJ"}))
        with pytest.raises(ToolError, match="EVIL"):
            await tool(ctx, issue_keys=["PROJ-1", "EVIL-2"])


class TestEnforceSpaceAccess:
    """Tests for the enforce_space_access decorator."""

    @pytest.mark.asyncio
    async def test_no_allowlist_allows_any_space(self) -> None:
        @enforce_space_access(space_key_param="space_key")
        async def tool(ctx, *, space_key: str) -> str:
            return "ok"

        ctx = _make_ctx()
        result = await tool(ctx, space_key="SECRET")
        assert result == "ok"

    @pytest.mark.asyncio
    async def test_allowed_space_passes(self) -> None:
        @enforce_space_access(space_key_param="space_key")
        async def tool(ctx, *, space_key: str) -> str:
            return "ok"

        ctx = _make_ctx(space_allowlist=frozenset({"DEV"}))
        result = await tool(ctx, space_key="DEV")
        assert result == "ok"

    @pytest.mark.asyncio
    async def test_denied_space_raises_tool_error(self) -> None:
        @enforce_space_access(space_key_param="space_key")
        async def tool(ctx, *, space_key: str) -> str:
            return "ok"

        ctx = _make_ctx(space_allowlist=frozenset({"DEV"}))
        with pytest.raises(ToolError, match="SECRET"):
            await tool(ctx, space_key="SECRET")
```

#### Unit test — coverage sentinel (`tests/unit/servers/test_access_control_coverage.py`)

This test asserts that every registered tool in both server files has the
`"access_controlled"` tag. This prevents future regressions when new tools are
added.

```python
"""Regression test: all Jira and Confluence tools must be access-controlled."""

import pytest
from fastmcp import FastMCP

from mcp_atlassian.servers.jira import jira_mcp
from mcp_atlassian.servers.confluence import confluence_mcp


@pytest.mark.asyncio
async def test_all_jira_tools_have_access_controlled_tag() -> None:
    """Assert every registered Jira tool carries the 'access_controlled' tag."""
    tools = await jira_mcp.get_tools()
    missing = [
        name for name, tool in tools.items()
        if "access_controlled" not in tool.tags
    ]
    assert not missing, (
        f"Jira tools missing 'access_controlled' tag: {sorted(missing)}"
    )


@pytest.mark.asyncio
async def test_all_confluence_tools_have_access_controlled_tag() -> None:
    """Assert every registered Confluence tool carries the 'access_controlled' tag."""
    tools = await confluence_mcp.get_tools()
    missing = [
        name for name, tool in tools.items()
        if "access_controlled" not in tool.tags
    ]
    assert not missing, (
        f"Confluence tools missing 'access_controlled' tag: {sorted(missing)}"
    )
```

#### Integration tests

Add to `tests/integration/test_access_control.py`:

- Test that `get_issue` with a non-whitelisted issue key returns `ToolError`
  when `JIRA_PROJECTS_FILTER=ALLOWED`.
- Test that `search` with a JQL containing `project = EVIL` returns only
  results from `ALLOWED` projects.
- Test that `create_page` in a non-whitelisted space is blocked.
- Test that with no filter set, all tools work unrestricted.

### Rollout Plan

1. Create branch: `git checkout -b feature/ds-2031-decorator-access-guard`
2. Implement Steps 1–5 (context, helpers, decorators, search wiring) — no
   changes to tool handlers yet; full suite must still pass.
3. Implement Step 6 (apply decorators to all tools) in a single commit per
   service (`jira.py`, then `confluence.py`).
4. Implement Step 7 (post-fetch space validation for `page_id` tools).
5. Implement Step 8 (tag audit + coverage sentinel test).
6. Run full test suite: `uv run pytest -xvs`.
7. Lint and type-check: `pre-commit run --all-files`.
8. Document new tags in `.env.example` comments:
   - `JIRA_PROJECTS_FILTER` — comma-separated project keys (e.g. `PROJ,TEAM`).
     When set, **all** tools that reference a Jira project are blocked if the
     project is not in this list.
   - `CONFLUENCE_SPACES_FILTER` — comma-separated space keys (e.g. `DEV,DOCS`).
     When set, **all** tools that reference a Confluence space are blocked if
     the space is not in this list.
9. Open PR targeting `main` with the checklist below.

**PR checklist**:
- [ ] `MainAppContext` has `jira_project_allowlist` and `confluence_space_allowlist`
- [ ] `_parse_filter_to_frozenset` in `main.py`; lifespan logs allowlists
- [ ] `utils/access_control.py` created with both helpers and `assert_space_allowed`
- [ ] `enforce_project_access` and `enforce_space_access` added to `utils/decorators.py`
- [ ] JQL rewrite in `jira/search.py` uses `build_jql_allowlist_clause`
- [ ] CQL rewrite in `confluence/search.py` uses `build_cql_allowlist_clause`
- [ ] All 49 Jira tools in `servers/jira.py` decorated + tagged `"access_controlled"`
- [ ] All 24 Confluence tools in `servers/confluence.py` decorated + tagged
- [ ] Coverage sentinel test passes with zero missing tools
- [ ] Unit tests for `access_control.py` and both new decorators pass
- [ ] Integration tests pass with filter env vars set and unset
- [ ] `mypy --strict` clean
- [ ] `.env.example` updated

### Verification

End-to-end scenarios (to run manually or as integration tests):

| Scenario | `JIRA_PROJECTS_FILTER` | Tool + args | Expected |
|---|---|---|---|
| 1 | `PROJ` | `get_issue(issue_key="PROJ-1")` | Success |
| 2 | `PROJ` | `get_issue(issue_key="EVIL-1")` | ToolError: EVIL not in allowlist |
| 3 | `PROJ` | `search(jql="project = EVIL")` | Returns only PROJ issues |
| 4 | `PROJ` | `create_issue(project_key="EVIL")` | ToolError blocked |
| 5 | unset | `get_issue(issue_key="ANY-1")` | Success (unrestricted) |
| 6 | `PROJ` | `search(jql="project = EVIL OR project = PROJ")` | Returns only PROJ issues |
| 7 | `PROJ` | `add_worklog(issue_key="EVIL-2")` | ToolError blocked |
| 8 | `DEV` (Confluence) | `create_page(space_key="SECRET")` | ToolError blocked |
| 9 | `DEV` (Confluence) | `get_page(space_key="DEV")` | Success |
| 10 | `DEV` (Confluence) | `confluence_search(cql="space = SECRET")` | Returns only DEV results |

---

## 6. Challenges and Tradeoffs

### Pros

1. **Minimal surface area**: Two new decorators following a pattern already
   established in the codebase (`check_write_access`). No new dependencies,
   no new services, no infrastructure changes.

2. **Defense-in-depth with `READ_ONLY_MODE`**: The decorator stacking
   `@enforce_project_access` → `@check_write_access` means a write to a
   non-whitelisted project is blocked twice — once for being in the wrong
   project, and (redundantly) once for read-only mode. The order ensures the
   access control error is surfaced rather than the read-only error.

3. **Auditable by tag**: The `"access_controlled"` tag + coverage test creates
   a machine-checkable inventory of which tools have been reviewed. This
   travels with the tool in the FastMCP registry and is visible in debug logs.

4. **Unconditional JQL/CQL rewrite closes bypass window**: The old substring
   check (`"project = " not in jql.lower()`) was a bypass vector. The new
   `build_jql_allowlist_clause` always appends the allowlist constraint without
   inspecting the content of the original query, eliminating the bypass.

5. **Zero overhead when not configured**: When `JIRA_PROJECTS_FILTER` is unset,
   `jira_project_allowlist` is an empty frozenset, and all decorator checks are
   short-circuited with no performance impact.

6. **Compatible with all auth modes**: The decorator reads from
   `MainAppContext` (lifespan context), not from request headers or fetcher
   config. It applies identically regardless of whether the user is
   authenticated via OAuth, PAT, Basic, or header-based multi-tenant auth.

### Cons

1. **Manual application to all 73 tools**: The decorator must be applied
   individually to every tool handler. A missed decoration is a security gap.
   Mitigated by the coverage sentinel test, but the initial application
   requires careful review.

2. **Upstream sync burden**: When new tools are added from the upstream
   `sooperset/mcp-atlassian`, they arrive without the decorator. The coverage
   test will catch this, but it requires a developer to correctly annotate the
   parameter names for each new tool.

3. **Board/sprint tools have no direct project link**: Tools like
   `get_agile_boards`, `get_board_issues`, `get_sprint_issues` operate on
   `board_id` and `sprint_id` — not project keys. The decorator cannot
   enforce the allowlist on these without additional Jira API calls to resolve
   board → project mapping, which would add latency and failure modes. These
   tools are marked as "audited" with an empty `@enforce_project_access()` but
   are not blocked.

4. **Confluence `page_id` tools**: For Confluence tools that accept only a
   `page_id` (e.g., `get_page`, `get_page_history`, `add_comment`), the space
   key is not available until after the API call. Post-fetch validation (Step 7)
   requires each such tool to be individually updated in its body — not just
   decorated — which is fragile.

5. **No per-user allowlist**: The allowlist is server-wide (from env vars).
   If different users should have access to different project subsets, this
   approach is insufficient. Per-user ACL would require either OPA/Cedar
   (Approach E) or a gateway layer (Approach D).

6. **Allowlist bypass via non-standard JQL functions**: JQL functions like
   `issuesOf()`, `linkedIssues()`, `subtasksOf()` can return issues from any
   project. The unconditional `AND project IN (...)` append neutralises this:
   even if the function returns cross-project issues, the `AND` clause
   restricts the database result set. However, this is only true when using
   the Jira v3 API (Cloud); the v2 server-side JQL execution semantics should
   be verified for Server/DC.

### Key Challenges

#### Challenge 1: Cloud vs Server/DC field-name divergence

The `projects_filter` is already applied differently for Cloud (POST
`/rest/api/3/search/jql`) and Server/DC (`jira.jql()`). The new
`build_jql_allowlist_clause` must produce valid JQL for both. The current
implementation uses standard JQL syntax (`project IN ("A", "B")`), which is
valid in both environments. Verified against Jira Cloud and Jira Server 8.x
documentation.

#### Challenge 2: `read_only` interaction

The decorator stack ordering matters. Placing `@enforce_project_access` above
`@check_write_access` means:

1. Tool is invoked.
2. `enforce_project_access` fires: project not in allowlist → `ToolError`.
   **Or** project is allowed, continue.
3. `check_write_access` fires: `READ_ONLY_MODE=true` → `ValueError` wrapped
   as `ToolError` by `handle_tool_errors`. **Or** not read-only, continue.
4. Tool body executes.

This ordering is intentional: access control fires before the write guard, so
error messages clearly indicate the access control violation rather than a
confusing "read-only mode" error for an out-of-scope project.

#### Challenge 3: lifespan context unavailability (stdio mode)

In stdio transport (the default for Claude Desktop), there is no HTTP request
context, and the lifespan context may be accessed differently. The existing
`check_write_access` already handles this pattern via
`ctx.request_context.lifespan_context` — the new decorators use the same
access path and should be safe. The `app_ctx is None` guard ensures the
decorator is a no-op when context is unavailable, which matches the existing
behavior of `check_write_access`.

#### Challenge 4: case sensitivity of project and space keys

Jira project keys are uppercase by convention (e.g., `PROJ`) but the API
accepts case-insensitive matching in some contexts. The allowlist is
normalised to uppercase in `_parse_filter_to_frozenset`, and the decorator
calls `.upper()` on incoming values before comparison. This is consistent with
the existing filter logic in `SearchMixin.search_issues`.

#### Challenge 5: multi-tenant header auth with header-specific projects

In multi-tenant deployments using `X-Atlassian-Jira-Url` header auth, the
header-based config has `projects_filter: None` (see `_jira_spec().filter_kwargs`
= `{"projects_filter": None}`). The new approach reads the allowlist from
`MainAppContext` (server-wide env var), not from the per-request fetcher config.
This is intentionally stricter: the server-wide allowlist applies uniformly to
all tenants using this instance.

---

## 7. Alternative Approaches

### Alternative A2: Config-layer propagation (runner-up)

Rather than tool-level decorators, enforce the filter in every mixin method
that accepts project-scoped arguments. This keeps access control collocated
with business logic.

**Implementation sketch**: Add a `_assert_project_allowed(project_key)` helper
to `JiraClient` that checks `self.config.projects_filter`. Call it at the top
of every mixin method. Similarly for `ConfluenceClient`.

**Why it's the runner-up**: It provides a second layer of enforcement
independent of the server layer, making it appropriate for a defense-in-depth
strategy after the decorator approach is in place. However, as a standalone
approach, it requires updating 50+ mixin methods and is prone to regression
when new methods are added.

### Alternative A3: API Gateway with allowlist (future)

Once `ka-mcp-atlassian` is deployed behind a shared HTTP endpoint (rather than
per-user stdio), a lightweight gateway (Traefik, AWS API Gateway, or a minimal
FastAPI service) should be the primary enforcement point. The gateway has no
knowledge of MCP semantics, so it must inspect the JSON-RPC `params` field to
extract project keys — which requires MCP-aware parsing.

**Suggested architecture**:
```
Claude Desktop → MCP Gateway (Traefik + OPA plugin) →
    ka-mcp-atlassian (HTTP transport, no auth)
```

The OPA policy can express rules like:
```rego
allow {
    input.tool == "jira_get_issue"
    project := split(input.params.issue_key, "-")[0]
    project == input.user.allowed_project
}
```

**When to pursue**: After per-user PAT auth is in place and `ka-mcp-atlassian`
is running as a shared service rather than per-user processes.
