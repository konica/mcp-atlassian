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

    def test_remove_is_write(self) -> None:
        assert is_write_tool("jira_remove_issue_link") is True

    def test_get_link_types_is_not_write(self) -> None:
        assert is_write_tool("jira_get_link_types") is False

    def test_link_to_epic_is_write(self) -> None:
        assert is_write_tool("jira_link_to_epic") is True


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

    def test_jql_with_order_by_does_not_extract_keywords(self) -> None:
        result = extract_jira_projects(
            "jira_search",
            {"jql": "project = DS ORDER BY created DESC"},
        )
        assert result == frozenset(["DS"])

    def test_jql_order_by_keywords_not_treated_as_projects(self) -> None:
        result = extract_jira_projects(
            "jira_search",
            {"jql": "project = DS ORDER BY updated ASC"},
        )
        for keyword in ("ORDER", "BY", "UPDATED", "ASC", "DESC"):
            assert keyword not in result

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
