"""Unit tests for MCP proxy audit logging."""

import json
import logging

import pytest

from mcp_proxy.audit import _safe_arguments_summary, emit


class TestSafeArgumentsSummary:
    """Tests for argument sanitization."""

    def test_redacts_token_key(self) -> None:
        result = _safe_arguments_summary({"api_token": "secret123", "issue_key": "PROJ-1"})
        assert result["api_token"] == "[REDACTED]"
        assert result["issue_key"] == "PROJ-1"

    def test_redacts_password_key(self) -> None:
        result = _safe_arguments_summary({"password": "hunter2"})
        assert result["password"] == "[REDACTED]"

    def test_passes_safe_keys(self) -> None:
        result = _safe_arguments_summary({"project_key": "PROJ", "jql": "status = Open"})
        assert result == {"project_key": "PROJ", "jql": "status = Open"}

    def test_empty_arguments(self) -> None:
        result = _safe_arguments_summary({})
        assert result == {}


class TestEmit:
    """Tests for audit log emission."""

    def test_emit_writes_json_log(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.INFO, logger="mcp-proxy.audit"):
            emit(
                decision="deny",
                tool_name="jira_create_issue",
                reason="read-only mode",
                user_identity="basic:user@example.com",
                request_id="req-1",
            )
        assert len(caplog.records) == 1
        entry = json.loads(caplog.records[0].message)
        assert entry["decision"] == "deny"
        assert entry["tool"] == "jira_create_issue"
        assert entry["user"] == "basic:user@example.com"
        assert entry["request_id"] == "req-1"

    def test_emit_defaults_unknown_user(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.INFO, logger="mcp-proxy.audit"):
            emit(
                decision="allow",
                tool_name="jira_search",
                reason="allowed",
                user_identity=None,
            )
        entry = json.loads(caplog.records[0].message)
        assert entry["user"] == "unknown"
