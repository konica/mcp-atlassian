"""End-to-end integration tests for the proxy using httpx TestClient.

Requires a running upstream mcp-atlassian instance (controlled via
PROXY_UPSTREAM_URL env var pointing to a real or stub server).
"""

import json
from collections.abc import Iterator

import pytest
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from unittest.mock import AsyncMock, patch

from mcp_proxy.main import app


# Common four-header credential sets for tests
_JIRA_HEADERS = {
    "X-Atlassian-Jira-Url": "https://jira.example.com/jira",
    "X-Atlassian-Jira-Personal-Token": "ABCDEFGH12345678",
}
_CONFLUENCE_HEADERS = {
    "X-Atlassian-Confluence-Url": "https://wiki.example.com/confluence",
    "X-Atlassian-Confluence-Personal-Token": "ZYXWVUTS87654321",
}
_ALL_HEADERS = {**_JIRA_HEADERS, **_CONFLUENCE_HEADERS}


@pytest.fixture()
def client() -> Iterator[TestClient]:
    # raise_server_exceptions=False would hide errors; instead we use the
    # lifespan context so app.state.http_client is initialised properly.
    with TestClient(app) as c:
        yield c


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
        response = client.post("/mcp", json=body, headers=_ALL_HEADERS)

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

        mock_forward.return_value = JSONResponse(content={"result": "ok"})

        body = _tools_call_body("jira_get_issue", {"issue_key": "PROJ-1"})
        client.post("/mcp", json=body, headers=_JIRA_HEADERS)

        mock_forward.assert_called_once()
        get_config.cache_clear()

    @patch("mcp_proxy.main.proxy_request")
    def test_out_of_whitelist_blocked(
        self, mock_forward: AsyncMock, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PROXY_JIRA_PROJECTS_WHITELIST", "PROJ")
        monkeypatch.setenv("PROXY_READ_ONLY", "false")
        from mcp_proxy.config import get_config
        get_config.cache_clear()

        body = _tools_call_body("jira_get_issue", {"issue_key": "OTHER-1"})
        response = client.post("/mcp", json=body, headers=_JIRA_HEADERS)

        assert response.status_code == 403
        assert "OTHER" in response.json()["error"]["message"]
        mock_forward.assert_not_called()
        get_config.cache_clear()

    @patch("mcp_proxy.main.proxy_request")
    def test_non_tool_call_forwarded_without_enforcement(
        self, mock_forward: AsyncMock, client: TestClient
    ) -> None:
        mock_forward.return_value = JSONResponse(content={"result": "ok"})

        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/list",
            "params": {},
        }
        client.post("/mcp", json=body, headers=_ALL_HEADERS)
        mock_forward.assert_called_once()

    @patch("mcp_proxy.main.proxy_request")
    def test_confluence_only_headers_identity_fallback(
        self, mock_forward: AsyncMock, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When only Confluence headers are present, identity uses confluence-pat."""
        monkeypatch.setenv("PROXY_READ_ONLY", "false")
        monkeypatch.setenv("PROXY_AUDIT_LOG_ENABLED", "true")
        from mcp_proxy.config import get_config
        get_config.cache_clear()

        mock_forward.return_value = JSONResponse(content={"result": "ok"})

        body = _tools_call_body(
            "confluence_get_page", {"page_id": "12345"}
        )
        with patch("mcp_proxy.main.emit") as mock_emit:
            client.post("/mcp", json=body, headers=_CONFLUENCE_HEADERS)
            mock_emit.assert_called_once()
            call_kwargs = mock_emit.call_args[1]
            identity = call_kwargs.get("user_identity", "")
            assert identity.startswith("confluence-pat:...")

        mock_forward.assert_called_once()
        get_config.cache_clear()
