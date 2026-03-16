"""Access control enforcement for MCP tool calls.

Provides project/space whitelist enforcement.
The proxy intercepts the JSON-RPC ``tools/call`` method body and applies
these rules before forwarding to the upstream mcp-atlassian server.

Read-only enforcement is delegated entirely to the upstream server via
``READ_ONLY_MODE=true`` on mcp-atlassian, which removes write tools from
``tools/list`` and blocks them at the handler level.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Project / space key extraction
# ---------------------------------------------------------------------------

# Arguments that directly name a Jira project key.
_JIRA_PROJECT_KEY_ARGS: frozenset[str] = frozenset(
    ["project_key", "project", "board_id"]
)

# Arguments that contain a Jira issue key (e.g., "PROJ-123").
_JIRA_ISSUE_KEY_ARGS: frozenset[str] = frozenset(
    ["issue_key", "issue_id", "source_issue_key", "target_issue_key", "parent_issue_key"]
)

# Arguments that may contain JQL; project keys are extracted with best-effort regex.
_JQL_ARGS: frozenset[str] = frozenset(["jql", "query"])

# Arguments that contain Confluence space keys.
_CONFLUENCE_SPACE_KEY_ARGS: frozenset[str] = frozenset(["space_key", "space"])

# Regex: extract Jira project key from an issue key like "PROJ-123".
_ISSUE_KEY_RE = re.compile(r"^([A-Z][A-Z0-9_]+)-\d+$")

# Regex: best-effort extraction of explicit project references in JQL.
# Two patterns to avoid capturing JQL keywords that follow the project key:
#   1. project = KEY  or  project = "KEY"  (single key, stops at word boundary)
#   2. project in (KEY1, KEY2, ...)        (keys are bounded by the closing paren)
_JQL_PROJECT_EQ_RE = re.compile(
    r"\bproject\s*=\s*['\"]?([A-Z][A-Z0-9_]*)['\"]?",
    re.IGNORECASE,
)
_JQL_PROJECT_IN_RE = re.compile(
    r"\bproject\s+in\s*\(([^)]+)\)",
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

    # project = KEY  (single key)
    for match in _JQL_PROJECT_EQ_RE.finditer(jql):
        clean = match.group(1).strip().upper()
        if clean:
            found.add(clean)

    # project in (KEY1, KEY2, ...)
    for match in _JQL_PROJECT_IN_RE.finditer(jql):
        for part in re.split(r"[,\s\"']+", match.group(1)):
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
    jira_whitelist: frozenset[str],
    confluence_whitelist: frozenset[str],
) -> EnforcementResult:
    """Evaluate whether a tool call should be allowed.

    Applies two sequential checks:
    1. Jira project whitelist: rejects calls referencing out-of-scope projects.
    2. Confluence space whitelist: rejects calls referencing out-of-scope spaces.

    A whitelist that is empty (``frozenset()``) means "allow all" for that
    dimension.

    Read-only enforcement is intentionally omitted here — it is delegated to
    the upstream mcp-atlassian server via ``READ_ONLY_MODE=true``, which
    removes write tools from ``tools/list`` and blocks them at the handler
    level. Duplicating that logic in the proxy would require maintaining a
    parallel list of write-tool name patterns that can drift from the server.

    Args:
        tool_name: The MCP tool name.
        arguments: Parsed tool arguments from the JSON-RPC body.
        jira_whitelist: Allowed Jira project keys; empty = allow all.
        confluence_whitelist: Allowed Confluence space keys; empty = allow all.

    Returns:
        EnforcementResult indicating allow or deny with reason.
    """
    # --- Check 1: Jira project whitelist ---
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

    # --- Check 2: Confluence space whitelist ---
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
