"""Unit tests for proxy configuration."""

import pytest
from mcp_proxy.config import ProxyConfig


def test_default_config() -> None:
    # _env_file=None isolates the test from any local proxy/.env file
    config = ProxyConfig(_env_file=None)
    assert config.upstream_url == "http://mcp-atlassian:9000"
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
