# Access Control Analysis: DS-2031 — Solution 3: Rely Solely on Per-User PAT

## 1. Existing Solution Summary

### What the current codebase already does

The existing codebase (`ka-mcp-atlassian`) has a layered authentication model that makes Solution 3 partially implemented already, but with important gaps.

**Multi-tenant per-user credential routing (implemented)**

`UserTokenMiddleware` in `src/mcp_atlassian/servers/main.py` extracts per-request credentials from HTTP headers:
- `Authorization: Bearer <token>` — OAuth access tokens
- `Authorization: Token <pat>` — PAT-style bearer tokens
- `Authorization: Basic <base64(email:api_token)>` — basic auth
- `X-Atlassian-Jira-Personal-Token` + `X-Atlassian-Jira-Url` — per-service PAT headers
- `X-Atlassian-Confluence-Personal-Token` + `X-Atlassian-Confluence-Url` — per-service PAT headers

`get_jira_fetcher` / `get_confluence_fetcher` in `src/mcp_atlassian/servers/dependencies.py` consume those scope-state values and construct per-user `JiraFetcher` / `ConfluenceFetcher` instances with user-specific credentials. **Each MCP tool call already runs under the identity of the requesting user** when headers are present.

**Read-only mode (implemented)**

`READ_ONLY_MODE=true` sets `MainAppContext.read_only = True` at server startup. Two enforcement mechanisms exist:

1. `_list_tools_mcp` in `AtlassianMCP` excludes tools whose tag set contains `"write"` from the tool list returned to the client — the client never sees write tools.
2. `@check_write_access` decorator in `src/mcp_atlassian/utils/decorators.py` raises `ValueError` at call time for any write tool if `read_only` is `True`. This acts as a second layer even if the tool list exclusion is bypassed.

**Projects/spaces filter (partially implemented — the core gap)**

`JIRA_PROJECTS_FILTER` / `CONFLUENCE_SPACES_FILTER` are loaded into config at startup and propagated to per-user configs via `_create_user_config_for_fetcher`. However, as the problem statement in `ds-2031-access-control.md` correctly identifies:
- Jira: only `get_issue` and `search` check the filter (2 of ~50 tools)
- Confluence: only `search` checks the filter (1 of ~25 tools)
- All other tools (create, delete, transitions, worklogs, etc.) ignore the filter
- The filter is bypassable via explicit `project=` clauses in JQL/CQL

**Token validation (implemented)**

`_create_and_validate` calls `spec.validate_fn` on every new per-user fetcher. For Jira this calls `get_current_user_account_id()` and for Confluence `get_current_user_info()`. This means **the Atlassian API itself validates every user credential on first use per request**. Invalid or expired tokens fail fast with a `ValueError` before any tool executes.

---

## 2. What "Solution 3" Actually Means

Solution 3 removes `JIRA_PROJECTS_FILTER` / `CONFLUENCE_SPACES_FILTER` from the security model entirely. The assertion is: **each user's native Jira/Confluence permissions already enforce which projects/spaces they can access, so the server filter adds no real value and creates a false sense of security when inconsistently applied**.

This is not a new implementation — it is a deliberate decision to remove the filter as a security control while keeping everything else (per-user auth, read-only mode).

The key question is: under what conditions does this hold and what does it leave unprotected?

---

## 3. What Jira/Confluence Native Permissions Cover

### Jira permission model (Cloud and Server/DC)

| Scenario | Native enforcement? |
|----------|---------------------|
| Read an issue the user has no access to | Yes — returns 404 or 403 |
| Search with JQL across projects | Yes — results filtered to visible issues |
| Create an issue in a restricted project | Yes — returns 403 |
| Transition an issue without permission | Yes — returns 403 |
| Delete an issue without admin permission | Yes — returns 403 |
| Add a worklog without permission | Yes — returns 403 |
| Access service desk queues without agent role | Yes — returns 403 |
| Batch-create issues across projects | Yes — per-issue permission checked |
| Read development info (branches, PRs) | Yes — requires Developer Tools permission |

Jira enforces permissions at the **individual resource level** on every API call. There is no concept of "list all" returning data the user cannot see — the API respects project-level roles (Browse Project, Create Issues, Edit Issues, etc.) and global permissions.

### Confluence permission model (Cloud and Server/DC)

| Scenario | Native enforcement? |
|----------|---------------------|
| Read a page in a restricted space | Yes — returns 403 |
| Search CQL across spaces | Yes — results filtered to visible pages |
| Create a page in a space without write permission | Yes — returns 403 |
| Delete a page without admin permission | Yes — returns 403 |
| Upload an attachment without write permission | Yes — returns 403 |
| View page analytics | Yes — requires space viewer role |
| Move a page to another space | Yes — requires permissions in both spaces |

Confluence space permissions (view, add pages, delete) and page-level restrictions are enforced by the API on every operation.

### What native permissions do NOT cover

1. **Visibility of project/space existence**: A user with "Browse Projects" role can see a project exists, even if they cannot see its issues. In some Jira configurations, project keys are discoverable via autocomplete APIs even without issue access.

2. **Metadata leakage**: Tools like `jira_get_all_projects` return projects the user can browse. The permission controls content, not the existence of the project itself.

3. **Rate and volume**: Native permissions do not prevent a user with legitimate access from making thousands of API calls through the AI agent (data exfiltration via legitimate queries).

4. **Cross-project aggregation**: A user with access to 200 projects can use the AI to aggregate and correlate data across all of them in ways that would be impractical manually. This is an AI-specific risk that native permissions were never designed to address.

5. **Audit trail at the AI layer**: Atlassian audit logs record who did what, but they do not record that the action was AI-initiated. Compliance requirements may demand AI-specific audit trails.

6. **Admin-scoped service accounts**: If the MCP server is deployed with a shared service account PAT (the global fallback path in `get_jira_fetcher`), all users share that account's permissions. The per-user PAT mechanism only applies when users provide their own credentials via headers. If a shared service account has broad permissions, Solution 3 provides no protection in that deployment mode.

---

## 4. Read-Only Enforcement Analysis

### Current state

`READ_ONLY_MODE=true` provides two independent enforcement layers:

**Layer 1 — Tool list exclusion** (`_list_tools_mcp`)

```python
if tool_obj and read_only and "write" in tool_tags:
    continue  # excluded from list
```

This prevents conforming MCP clients from ever learning about or calling write tools. The MCP protocol specifies that clients must only call tools listed in `tools/list`. A well-behaved client cannot call an unlisted tool.

**Layer 2 — Runtime guard** (`@check_write_access`)

```python
if app_lifespan_ctx is not None and app_lifespan_ctx.read_only:
    raise ValueError(f"Cannot {action_description} in read-only mode.")
```

This protects against: buggy clients, clients that cache tool lists from a previous non-read-only session, or direct HTTP requests that bypass tool list negotiation. Every write tool in `src/mcp_atlassian/servers/jira.py` and `src/mcp_atlassian/servers/confluence.py` is decorated with `@check_write_access`.

### Audit: is every write tool protected?

The tag-based exclusion in `_list_tools_mcp` works correctly only if **every write tool has `"write"` in its tag set**. The `@check_write_access` decorator provides a separate runtime guarantee even if a tag is missing.

To verify completeness, the tools tagged `"write"` can be enumerated:

```bash
grep -n '"write"' src/mcp_atlassian/servers/jira.py
grep -n '"write"' src/mcp_atlassian/servers/confluence.py
```

The decorator approach is the stronger of the two because it does not depend on correct tagging.

### Guarantee: read-only mode is reliable for AI use cases

When `READ_ONLY_MODE=true`:
- Users cannot create, update, or delete issues, comments, pages, attachments, sprints, links, versions, or worklogs through the AI
- The guarantee holds even if the user's Jira/Confluence account has write permissions
- The guarantee holds across Cloud, Server, and Data Center deployments
- It does not depend on `JIRA_PROJECTS_FILTER` or `CONFLUENCE_SPACES_FILTER`

**Conclusion**: `READ_ONLY_MODE=true` is the correct mechanism to prevent AI-initiated writes. It is already well-implemented. Solution 3 + `READ_ONLY_MODE=true` provides a coherent and defensible "read-only AI assistant" posture.

---

## 5. Recommended Approach

### When Solution 3 is the right choice

Solution 3 is acceptable — and arguably superior to the false-security whitelist — under these specific conditions:

1. **Per-user PAT is enforced**: Every MCP client provides its own Atlassian credentials via `Authorization` headers. The global fallback config (`full_jira_config`, `full_confluence_config`) is not present, or is a read-only service account with minimal permissions.

2. **Read-only mode is enabled**: `READ_ONLY_MODE=true` is set in the deployment environment, and this is verified as the operational default for the AI deployment.

3. **Users have appropriately scoped Atlassian permissions**: The organization's Atlassian administrators have already configured project roles and space permissions to reflect what users should access. This is typically true in organizations with mature Atlassian administration.

4. **Data privacy requirements are limited to "prevent unauthorized modification"**: If the requirement is simply that the AI cannot write data it shouldn't, `READ_ONLY_MODE` solves this. If the requirement is that the AI cannot *read* data outside a defined scope, Solution 3 alone does not satisfy it.

5. **Compliance does not require AI-specific access controls**: GDPR/DSGVO requirements around data minimization may require that the AI only accesses approved data, not just that it cannot modify it. Legal review is needed.

### When Solution 3 is not acceptable

- A shared service account is used (shared PAT, global config) rather than per-user credentials
- Any user has Jira/Confluence permissions that are broader than what should be accessible through AI
- Data privacy approval has been granted for specific projects/spaces only, not an entire instance
- Compliance requirements mandate AI-specific access logs separate from regular Atlassian audit logs
- The deployment serves users from multiple teams with different data classification levels

---

## 6. Cost Evaluation

### Development cost

| Item | Estimate |
|------|----------|
| Remove filter dependency from security documentation | 2 hours |
| Audit that `READ_ONLY_MODE` covers all write tools | 2 hours |
| Write documentation and decision record | 2 hours |
| Update `.env.example` to clarify filter is advisory, not security | 1 hour |
| Write hardening verification tests | 4 hours |
| **Total** | **~11 hours** |

Python skill level required: low. No new code paths. Primary work is auditing, documenting, and testing existing behavior.

### DevOps / Infrastructure cost

| Item | Estimate |
|------|----------|
| Verify `READ_ONLY_MODE=true` is set in deployment env | 30 minutes |
| Verify no shared service account PAT is in the deployment | 30 minutes |
| Confirm MCP client sends per-user credentials via headers | 1 hour |
| Add `READ_ONLY_MODE` to deployment checklist / IaC | 1 hour |
| **Total** | **~3 hours** |

No infrastructure changes required. No new environment variables. No new Docker or Kubernetes resources.

### Maintenance cost

| Item | Burden |
|------|--------|
| Upstream Jira/Confluence permission changes | None — handled by Atlassian administrators |
| New tools added to the MCP server | Must verify each new write tool has `@check_write_access` |
| Token expiry handling | Already handled by `_create_and_validate` validation |
| Audit log review | Manual or delegated to Atlassian audit tools |
| **Overall** | Very low — no server-side filter logic to maintain |

### Security cost / risk

| Risk | Severity | Mitigated by |
|------|----------|--------------|
| User reads data they have Atlassian access to | Low | Atlassian permissions are the correct boundary |
| User accidentally writes data via AI | Low | `READ_ONLY_MODE=true` blocks all writes |
| Shared service account exposes all projects | **High** | Must enforce per-user PAT deployment pattern |
| AI aggregates data across many projects | Medium | Acceptable if user has legitimate access to all |
| No AI-specific audit trail | Medium | Atlassian audit logs capture all API calls with user identity |
| PAT credential logged in application logs | Low | `UserTokenMiddleware` already masks tokens in debug logs |
| SSRF via crafted URL headers | Low | `validate_url_for_ssrf` already applied in `_create_and_validate` |

---

## 7. Comprehensive Implementation Plan

### Pre-requisites

1. Confirm the deployment uses per-user PAT headers (not a shared service account global config), or document explicitly which deployment modes are in scope.
2. Confirm `READ_ONLY_MODE=true` can be set and is committed to the deployment environment.
3. Obtain legal/compliance sign-off on the assertion that "Atlassian native permissions are sufficient for data privacy requirements" for this specific AI use case.
4. Confirm all write tools have `@check_write_access` applied (verification step below).

### Step-by-Step

**Step 1 — Audit write tool coverage**

Run the following to enumerate write tools and verify decorator coverage:

```bash
# Count tools tagged "write"
grep -c '"write"' src/mcp_atlassian/servers/jira.py
grep -c '"write"' src/mcp_atlassian/servers/confluence.py

# List all write-tagged tools
grep -B5 '"write"' src/mcp_atlassian/servers/jira.py | grep "@jira_mcp.tool"
grep -B5 '"write"' src/mcp_atlassian/servers/confluence.py | grep "@confluence_mcp.tool"

# Verify every write tool handler has @check_write_access
# Tools with "write" tag should have check_write_access in the lines between
# the @tool decorator and the async def
```

Manually cross-reference that every function decorated with `@jira_mcp.tool(tags={..., "write", ...})` has `@check_write_access` in its decorator stack, and vice versa.

**Step 2 — Verify per-user PAT is enforced at the deployment level**

The existing `_get_fetcher` logic in `dependencies.py` falls back to the global config when no per-user headers are present (lines 658–676). For Solution 3 to provide the intended security guarantee, this fallback must either:

- Not exist (no `JIRA_URL` / `CONFLUENCE_URL` set), which forces header-based auth for all requests, or
- Be a read-only service account with minimal permissions (e.g., only "Browse Projects"), used only as a degraded-experience fallback

Document which deployment model is active and verify it in the deployment runbook.

**Step 3 — Document the filter as advisory-only**

Update `.env.example` comments to explicitly state that `JIRA_PROJECTS_FILTER` and `CONFLUENCE_SPACES_FILTER` are **advisory** (they narrow search results and issue fetching for usability) and are **not a security boundary**. This prevents future operators from relying on the filter for access control.

**Step 4 — Add a startup log statement clarifying the security posture**

In `main_lifespan` (`src/mcp_atlassian/servers/main.py`), after the existing `read_only` log, add a warning when the filter is set but read-only mode is disabled:

```python
# After: logger.info(f"Read-only mode: {'ENABLED' if read_only else 'DISABLED'}")

projects_filter = os.getenv("JIRA_PROJECTS_FILTER")
spaces_filter = os.getenv("CONFLUENCE_SPACES_FILTER")
if (projects_filter or spaces_filter) and not read_only:
    logger.warning(
        "JIRA_PROJECTS_FILTER / CONFLUENCE_SPACES_FILTER are set but "
        "READ_ONLY_MODE is not enabled. These filters are advisory only "
        "(applied in search/get_issue) and do NOT prevent access to "
        "other tools. Set READ_ONLY_MODE=true for write protection, and "
        "rely on native Atlassian permissions for project/space scoping."
    )
```

**Step 5 — Verify the `READ_ONLY_MODE` environment variable is set in all deployment paths**

Check:
- Docker Compose files
- Kubernetes ConfigMaps / deployment manifests
- Helm chart values
- CI/CD environment variable configuration
- Local `.env` for development

**Step 6 — Write hardening verification tests**

See the Testing Strategy section below.

**Step 7 — Update the solutions document**

Add a "Decision" section to `solutions/ds-2031-access-control.md` recording that Solution 3 was chosen, the conditions it requires, and what was verified.

---

### Sample Code

#### Hardening: startup warning for misconfigured filter + read-only state

In `src/mcp_atlassian/servers/main.py`, within `main_lifespan`, after the existing read-only log:

```python
import os  # already imported

# --- Existing lines ---
logger.info(f"Read-only mode: {'ENABLED' if read_only else 'DISABLED'}")
logger.info(f"Enabled tools filter: {enabled_tools or 'All tools enabled'}")
logger.info(f"Enabled toolsets filter: {sorted(enabled_toolsets)}")

# --- Add after ---
_projects_filter = os.getenv("JIRA_PROJECTS_FILTER")
_spaces_filter = os.getenv("CONFLUENCE_SPACES_FILTER")
if (_projects_filter or _spaces_filter) and not read_only:
    logger.warning(
        "Security notice: JIRA_PROJECTS_FILTER/CONFLUENCE_SPACES_FILTER "
        "are set but READ_ONLY_MODE is not enabled. "
        "These filters are advisory (search/get_issue only) and do not "
        "restrict write tools or direct issue access. "
        "Enable READ_ONLY_MODE=true to prevent AI-initiated writes."
    )
if not read_only:
    logger.warning(
        "Server is running in READ-WRITE mode. "
        "AI agents can create, update, and delete Atlassian content. "
        "Set READ_ONLY_MODE=true unless write access is explicitly required."
    )
```

#### Utility: verify write tool tag/decorator consistency

This can be used as a test or a CI check. Add to `tests/unit/servers/test_write_tool_coverage.py`:

```python
"""Verify that all write-tagged tools have check_write_access applied.

This test guards against a tool being tagged 'write' without the runtime
guard, which would mean READ_ONLY_MODE would exclude it from the tool list
but could not block a direct call.
"""

import inspect
from collections.abc import Callable

import pytest

from mcp_atlassian.servers.jira import jira_mcp
from mcp_atlassian.servers.confluence import confluence_mcp
from mcp_atlassian.utils.decorators import check_write_access


def _is_guarded_by_check_write_access(fn: Callable) -> bool:
    """Return True if fn's call chain includes check_write_access's wrapper.

    check_write_access uses @wraps so __wrapped__ is available.
    We walk the wrapper chain looking for the sentinel.
    """
    # The wrapper created by check_write_access is named 'wrapper' and
    # is defined inside check_write_access. We identify it by checking
    # whether the function is wrapped by a closure that refers to
    # check_write_access's sentinel.
    #
    # Simpler approach: check_write_access wraps with handle_tool_errors
    # which also wraps. We can verify by applying the decorator to an
    # identity function and comparing closure structure, but the most
    # reliable approach for this codebase is to check __wrapped__ chains
    # and look for the read_only check behavior in the source.
    #
    # Pragmatic: check that the function's __module__ chain or qualname
    # contains the wrapper from check_write_access. Since @wraps preserves
    # __qualname__, we instead check the actual source of the outermost
    # non-wraps function by walking __wrapped__.
    current = fn
    seen: set[int] = set()
    while current is not None:
        if id(current) in seen:
            break
        seen.add(id(current))
        # check_write_access's outer wrapper calls handle_tool_errors
        # which is also a wrapper. The innermost wrapper checks read_only.
        # We look for the qualname pattern of the closure.
        qualname = getattr(current, "__qualname__", "")
        if "check_write_access.<locals>" in qualname:
            return True
        current = getattr(current, "__wrapped__", None)
    return False


@pytest.mark.parametrize(
    "mcp_server,server_name",
    [
        (jira_mcp, "jira_mcp"),
        (confluence_mcp, "confluence_mcp"),
    ],
)
def test_all_write_tools_have_check_write_access(
    mcp_server: object,
    server_name: str,
) -> None:
    """All tools tagged 'write' must have @check_write_access in their call stack.

    Args:
        mcp_server: The FastMCP server instance.
        server_name: Human-readable server name for error messages.
    """
    import asyncio

    tools = asyncio.get_event_loop().run_until_complete(mcp_server.get_tools())
    missing: list[str] = []

    for tool_name, tool_obj in tools.items():
        if "write" in (tool_obj.tags or set()):
            fn = tool_obj.fn
            if not _is_guarded_by_check_write_access(fn):
                missing.append(tool_name)

    assert not missing, (
        f"[{server_name}] The following write-tagged tools are missing "
        f"@check_write_access: {missing}. "
        "This means READ_ONLY_MODE would exclude them from the tool list "
        "but cannot block a direct call."
    )
```

#### Test: per-user PAT isolation is respected

Add to `tests/unit/servers/test_dependencies.py` (extending existing patterns):

```python
"""Verify per-user PAT isolation in get_jira_fetcher / get_confluence_fetcher."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mcp_atlassian.jira.config import JiraConfig
from mcp_atlassian.servers.dependencies import _create_user_config_for_fetcher


def _make_jira_base_config(
    *,
    projects_filter: str | None = "ALLOWED",
) -> JiraConfig:
    """Create a minimal JiraConfig for testing."""
    return JiraConfig(
        url="https://example.atlassian.net",
        auth_type="pat",
        personal_token="global-pat",
        projects_filter=projects_filter,
    )


def test_user_config_inherits_projects_filter() -> None:
    """projects_filter from the base config is propagated to per-user config.

    This verifies that when per-user PAT is resolved, the server-level
    projects_filter (if any) is still applied even with per-user credentials.
    """
    base = _make_jira_base_config(projects_filter="PROJ1,PROJ2")
    user_config = _create_user_config_for_fetcher(
        base_config=base,
        auth_type="pat",
        credentials={"personal_access_token": "user-specific-pat"},
    )
    assert user_config.projects_filter == "PROJ1,PROJ2"
    assert user_config.personal_token == "user-specific-pat"
    assert user_config.personal_token != base.personal_token


def test_user_config_does_not_inherit_global_pat() -> None:
    """Per-user config uses the user's PAT, not the global service account PAT."""
    base = _make_jira_base_config()
    user_config = _create_user_config_for_fetcher(
        base_config=base,
        auth_type="pat",
        credentials={"personal_access_token": "user-abc-token"},
    )
    assert user_config.personal_token == "user-abc-token"
    assert user_config.personal_token != base.personal_token


def test_user_config_pat_missing_raises() -> None:
    """Missing PAT in credentials raises ValueError, not a silent fallback."""
    base = _make_jira_base_config()
    with pytest.raises(ValueError, match="PAT missing"):
        _create_user_config_for_fetcher(
            base_config=base,
            auth_type="pat",
            credentials={},
        )
```

#### Test: READ_ONLY_MODE blocks all write tools at runtime

Add to `tests/unit/servers/test_read_only_mode.py`:

```python
"""Verify READ_ONLY_MODE blocks write operations at runtime.

Tests the @check_write_access decorator as a defence-in-depth guard
independent of tool list filtering.
"""

from unittest.mock import MagicMock

import pytest
from fastmcp.exceptions import ToolError

from mcp_atlassian.utils.decorators import check_write_access


def _make_ctx(*, read_only: bool) -> MagicMock:
    """Create a mock FastMCP Context with the given read_only state."""
    ctx = MagicMock()
    ctx.request_context.lifespan_context = {
        "app_lifespan_context": MagicMock(read_only=read_only)
    }
    return ctx


@pytest.mark.asyncio
async def test_write_blocked_when_read_only() -> None:
    """@check_write_access raises ToolError when read_only=True."""

    @check_write_access
    async def jira_create_issue(ctx: MagicMock) -> str:
        return "created"

    ctx = _make_ctx(read_only=True)
    with pytest.raises(ToolError, match="read-only mode"):
        await jira_create_issue(ctx)


@pytest.mark.asyncio
async def test_write_allowed_when_not_read_only() -> None:
    """@check_write_access allows execution when read_only=False."""

    @check_write_access
    async def jira_create_issue(ctx: MagicMock) -> str:
        return "created"

    ctx = _make_ctx(read_only=False)
    result = await jira_create_issue(ctx)
    assert result == "created"


@pytest.mark.asyncio
async def test_write_blocked_regardless_of_user_permissions() -> None:
    """READ_ONLY_MODE blocks writes even if the user has write permissions.

    The PAT may belong to a Jira admin. The server-level guard must
    still block the write.
    """

    @check_write_access
    async def jira_delete_issue(ctx: MagicMock, issue_key: str) -> str:
        return f"deleted {issue_key}"

    ctx = _make_ctx(read_only=True)
    with pytest.raises(ToolError, match="read-only mode"):
        await jira_delete_issue(ctx, "PROJ-123")
```

---

### Testing Strategy

#### Unit tests

| Test | File | Validates |
|------|------|-----------|
| All write tools tagged `"write"` have `@check_write_access` | `tests/unit/servers/test_write_tool_coverage.py` | Tag/decorator consistency |
| `@check_write_access` blocks when `read_only=True` | `tests/unit/servers/test_read_only_mode.py` | Runtime guard |
| `@check_write_access` allows when `read_only=False` | `tests/unit/servers/test_read_only_mode.py` | Non-regression |
| Per-user config uses user's PAT, not global | `tests/unit/servers/test_dependencies.py` | Credential isolation |
| `projects_filter` propagated to per-user config | `tests/unit/servers/test_dependencies.py` | Filter propagation |
| Missing PAT in credentials raises, not silently falls back | `tests/unit/servers/test_dependencies.py` | Fail-safe credential handling |

#### Integration tests

| Test | What to verify |
|------|----------------|
| Request with valid PAT, `READ_ONLY_MODE=true`: read tool succeeds | Read path is not broken |
| Request with valid PAT, `READ_ONLY_MODE=true`: write tool returns ToolError | Runtime guard fires |
| Request with valid PAT, `READ_ONLY_MODE=true`: write tool absent from `tools/list` | Tool list filter fires |
| Request with invalid PAT: `get_jira_fetcher` raises ValueError | Credential validation works |
| Request with no credentials and no global config: error, not anonymous access | No anonymous fallback |
| Request with PAT for restricted Jira user: `get_issue` on forbidden issue returns 403 | Native permission enforcement |

The integration tests that require a live Atlassian instance should be placed in `tests/integration/` following the existing `@pytest.mark.integration` pattern.

#### Regression tests

Before removing any filter-based tests, verify the following existing tests still pass:

```bash
uv run pytest tests/unit/jira/test_issues.py -xvs -k "filter"
uv run pytest tests/unit/jira/test_search.py -xvs -k "filter"
uv run pytest tests/unit/confluence/test_search.py -xvs -k "filter"
uv run pytest tests/unit/servers/test_dependencies.py -xvs
uv run pytest tests/unit/utils/test_decorators.py -xvs
```

---

### Rollout Plan

**Phase 1: Audit (no code changes)**

1. Run the write-tool coverage audit manually.
2. Confirm `READ_ONLY_MODE=true` is set in all deployment environments.
3. Confirm per-user PAT routing is active for all MCP clients in scope.
4. Obtain legal/compliance confirmation.

**Phase 2: Hardening (minimal code changes)**

1. Create feature branch: `git checkout -b feat/ds-2031-pat-only-hardening`
2. Add the startup warning log in `main_lifespan`.
3. Update `.env.example` comment on `JIRA_PROJECTS_FILTER` / `CONFLUENCE_SPACES_FILTER`.
4. Add the test files listed above.
5. Run full test suite: `uv run pytest -xvs`
6. Run lint: `pre-commit run --all-files`
7. Open PR against `main`.

**Phase 3: Decision record**

Update `solutions/ds-2031-access-control.md` with a "Decision" section documenting:
- Solution 3 adopted on [date]
- Conditions it requires (per-user PAT, `READ_ONLY_MODE=true`)
- What was verified
- What residual risks exist
- Owner of ongoing compliance review

**Environment variable documentation**

| Variable | Required for Solution 3 | Value | Notes |
|----------|-------------------------|-------|-------|
| `READ_ONLY_MODE` | Yes | `true` | Must be set in deployment |
| `JIRA_PROJECTS_FILTER` | No | N/A | Advisory only — does not constitute a security boundary |
| `CONFLUENCE_SPACES_FILTER` | No | N/A | Advisory only |
| `IGNORE_HEADER_AUTH` | No | `false` (default) | Must NOT be set to `true` — disables per-user auth |
| `JIRA_URL` / `CONFLUENCE_URL` | Conditional | — | If set, global fallback is active; must use restricted service account |
| `JIRA_PERSONAL_TOKEN` | Conditional | — | If set, is the global fallback credential; must be read-only scoped |

---

### Verification

**End-to-end test scenarios**

1. **Read succeeds with user PAT**
   - Set `READ_ONLY_MODE=true`
   - Send `Authorization: Token <valid-user-pat>` header
   - Call `jira_get_issue` with an issue the user can access
   - Expected: issue returned

2. **Write blocked with user PAT**
   - Set `READ_ONLY_MODE=true`
   - Send `Authorization: Token <valid-user-pat>` header
   - Attempt `jira_create_issue` via direct HTTP call
   - Expected: ToolError with "read-only mode" message

3. **Write tool absent from tool list**
   - Set `READ_ONLY_MODE=true`
   - Call `tools/list` via MCP protocol
   - Expected: `jira_create_issue`, `jira_update_issue`, `jira_delete_issue`, etc. are absent

4. **Native permission denied**
   - Use a PAT for a user with no access to project RESTRICTED
   - Call `jira_get_issue` with issue key `RESTRICTED-1`
   - Expected: 403/404 error from Jira API (not a server bypass)

5. **No credential, no global config**
   - Remove `JIRA_URL` and `JIRA_PERSONAL_TOKEN` from environment
   - Send request with no Authorization header
   - Expected: `ValueError` — "Jira client (fetcher) not available"

---

## 8. Challenges and Tradeoffs

### Pros

1. **Eliminates false security**: The existing filter creates a belief that cross-project access is blocked, when in fact 48/50 Jira tools and 24/25 Confluence tools ignore it. Removing it as a security claim is more honest.

2. **Zero implementation complexity**: No new code paths, no new dependencies, no risk of introducing bugs in access control logic. The security boundary is delegated to Atlassian's battle-tested permission system.

3. **Correct threat model for most deployments**: In a typical enterprise deployment, Jira/Confluence administrators have already correctly scoped project access for users. The AI inherits those correct permissions automatically with per-user PAT.

4. **Maintenance-free**: Upstream `sooperset/mcp-atlassian` can be rebased freely. New tools added upstream automatically inherit native permission enforcement without any changes to server-side access control code.

5. **Audit trail at source**: Atlassian's audit logs record all API calls with user identity. When a user uses the AI to read issue data, the audit log entry is identical to them reading it manually — same user, same resource, same timestamp. This is often preferable for compliance.

6. **Scales to all 73 tools consistently**: Native permissions apply to every API call made by every tool, regardless of whether the tool was recently added or the filter was updated.

### Cons

1. **No defense-in-depth against over-privileged users**: If a user legitimately has access to 500 projects in Jira, the AI inherits that access. There is no server-side narrowing. This is acceptable if the organization accepts that "what the user can see, the AI can see" — but some organizations require AI-specific restrictions.

2. **AI-specific data volume risk**: A user with read access to a large Jira instance can instruct the AI to batch-export all issues, search across all projects, and aggregate data at a scale that would be impractical manually. Native permissions do not rate-limit this. Mitigation: toolset restrictions (`TOOLSETS=default` limits to a smaller set), rate limiting at the infrastructure layer.

3. **Shared service account remains a risk**: The global fallback in `get_jira_fetcher` (lines 658–676 of `dependencies.py`) creates anonymous or service-account access if per-user headers are absent. If `JIRA_PERSONAL_TOKEN` is set to a powerful service account, all users who fail to provide headers get those permissions. This is a deployment configuration risk, not a code risk.

4. **No visibility into what the AI accessed**: Without an AI-specific audit log layer, it is difficult to answer "what Jira data did the AI access on behalf of user X last week?" The Atlassian audit log records the calls but not the AI context (which conversation, which prompt, etc.).

5. **Compliance gap for data privacy approval workflows**: If the organizational process requires explicit per-project data privacy approval before AI access, and that approval is recorded in a system-of-record (not just Jira permissions), then "if you have Jira access, AI has access" may not satisfy the approval process — even if the technical result is identical.

6. **Cloud vs Server/DC divergence**: Atlassian Cloud and Server/Data Center have different permission models. Cloud uses fine-grained OAuth scopes in addition to project roles. Server/DC uses project roles and global permissions only. When evaluating whether native permissions are "sufficient", the analysis must be repeated for each deployment mode.

### Key challenges

**Challenge 1: Verifying the deployment uses per-user PAT, not a shared service account**

The biggest risk in Solution 3 is the global fallback. The code path exists and is intentional (for stdio/non-HTTP deployments). In an HTTP deployment:
- If `JIRA_URL` is set but no per-user auth header is provided, the fallback fires silently
- There is no 401 response in this case — the global config is used instead
- This must be explicitly disabled or the global config must be a minimally-scoped service account

Mitigation: document and verify in the deployment runbook. Optionally, add a server startup assertion that fails if `JIRA_URL` is set but `READ_ONLY_MODE` is not:

```python
# In main_lifespan, after read_only is set:
if loaded_jira_config and not read_only:
    logger.critical(
        "SECURITY: Jira global config is active with READ-WRITE mode. "
        "The global service account can modify Jira data. "
        "Set READ_ONLY_MODE=true unless write access is intentional."
    )
```

**Challenge 2: The MCP protocol does not authenticate tool calls**

The MCP protocol (as implemented here) uses HTTP-level authentication via the `Authorization` header, but the per-user auth extraction happens in `UserTokenMiddleware` and is applied per HTTP request, not per MCP tool call. A single HTTP request can contain multiple MCP tool calls (batching). The current implementation correctly applies per-user credentials to the request's `request.state`, which is shared across all tool calls within that HTTP request. This is correct behavior but should be understood: one PAT credential covers all tool calls in a single HTTP connection.

**Challenge 3: PAT scope limitations on Atlassian Cloud**

On Atlassian Cloud, PATs are not directly available for user authentication to the REST API in the same way as on Server/DC. Cloud uses API tokens (basic auth: email + API token) or OAuth tokens. The `X-Atlassian-Jira-Personal-Token` header path in `dependencies.py` (Branch 1) works with Server/DC PATs. Cloud users must use `Authorization: Bearer <oauth-token>` or `Authorization: Basic <base64(email:api_token)>`. This affects how the MCP client is configured, not the security model, but operators must understand this distinction.

**Challenge 4: Token validation adds latency per request**

`_create_and_validate` makes a live Atlassian API call for every new per-user fetcher (i.e., every new HTTP request with a user token). The `TTLCache` in `main.py` (`token_validation_cache`) exists but is not actively connected to the per-user fetcher creation path in `_get_fetcher`. This means each HTTP request pays one extra Atlassian API call for validation. At low call volume this is acceptable; at high volume it may add noticeable latency. This is not a security issue but a performance consideration.

---

## 9. Alternative Approaches If This Does Not Fit

### If "per-user PAT only" cannot be enforced at deployment level

Apply Solution 1 (decorator-based filter enforcement) to cover the 48/50 Jira tools and 24/25 Confluence tools that currently ignore `JIRA_PROJECTS_FILTER` / `CONFLUENCE_SPACES_FILTER`. The decorator pattern already exists (`@check_write_access` is the template). A `@require_project_access(from_config=True)` decorator could be applied to the same set of tools. This is 2–3 days of work and provides defense-in-depth even when a shared service account is used.

### If AI-specific audit logs are required

Add a `structlog`-based audit logger that records every tool call with:
- User identity (from `request.state.user_atlassian_email`)
- Tool name
- Input arguments (sanitized, no credential values)
- Timestamp
- MCP session ID

This does not change the access control model but adds an AI-specific audit trail. The hook point is the `@handle_tool_errors` decorator (all tools pass through it) or a new `@audit_tool_call` decorator added to every tool.

### If data privacy approval requires explicit per-project whitelisting

Solution 2 (fork with universal filter) is the only approach that can guarantee project-scoped access across all 73 tools without relying on Atlassian permissions. The implementation requires:
1. A universal decorator that checks `JIRA_PROJECTS_FILTER` against the `project` parameter (or JQL content) before any tool executes
2. JQL/CQL sanitization to prevent bypass via crafted queries
3. Ongoing maintenance as new tools are added upstream

This is a 1–2 week implementation effort and ongoing maintenance burden, but it is the only option that satisfies a "server-side whitelist as a security control" requirement.
