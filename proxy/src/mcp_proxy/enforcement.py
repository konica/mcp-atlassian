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
        "remove",
        "link",
        "reply",
    ]
)

# Read-only tools that contain write-pattern segments in their name.
# These are explicitly excluded from write classification to avoid
# false positives in read-only mode (e.g., "link" in "jira_get_link_types").
_READ_ONLY_OVERRIDES: frozenset[str] = frozenset(
    [
        "jira_get_link_types",
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
    if tool_name in _READ_ONLY_OVERRIDES:
        return False
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
