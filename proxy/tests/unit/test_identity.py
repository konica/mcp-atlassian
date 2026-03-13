"""Unit tests for _extract_user_identity in mcp_proxy.main."""

from starlette.requests import Request

from mcp_proxy.main import _extract_user_identity


def _make_request(headers: dict[str, str] | None = None) -> Request:
    """Build a minimal Starlette Request with the given headers."""
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/mcp",
        "headers": [
            (k.lower().encode(), v.encode())
            for k, v in (headers or {}).items()
        ],
    }
    return Request(scope)


class TestExtractUserIdentity:
    """Tests for the four-header credential identity extraction."""

    def test_jira_pat_header_present(self) -> None:
        request = _make_request({
            "X-Atlassian-Jira-Personal-Token": "ABCDEFGHIJKLMNOP",
        })
        result = _extract_user_identity(request)
        assert result == "jira-pat:...IJKLMNOP"

    def test_jira_pat_short_token(self) -> None:
        request = _make_request({
            "X-Atlassian-Jira-Personal-Token": "SHORT",
        })
        result = _extract_user_identity(request)
        assert result == "jira-pat:<short>"

    def test_confluence_pat_header_only(self) -> None:
        request = _make_request({
            "X-Atlassian-Confluence-Personal-Token": "ZYXWVUTSRQPONMLK",
        })
        result = _extract_user_identity(request)
        assert result == "confluence-pat:...RQPONMLK"

    def test_confluence_pat_short_token(self) -> None:
        request = _make_request({
            "X-Atlassian-Confluence-Personal-Token": "TINY",
        })
        result = _extract_user_identity(request)
        assert result == "confluence-pat:<short>"

    def test_jira_pat_takes_priority_over_confluence(self) -> None:
        request = _make_request({
            "X-Atlassian-Jira-Personal-Token": "JIRA_TOKEN_12345678",
            "X-Atlassian-Confluence-Personal-Token": "CONF_TOKEN_87654321",
        })
        result = _extract_user_identity(request)
        assert result is not None
        assert result.startswith("jira-pat:...")

    def test_authorization_token_fallback(self) -> None:
        request = _make_request({
            "Authorization": "Token my-long-personal-access-token",
        })
        result = _extract_user_identity(request)
        assert result is not None
        assert result.startswith("pat:...")
        assert result == "pat:...ss-token"

    def test_authorization_basic_fallback(self) -> None:
        import base64
        creds = base64.b64encode(b"user@example.com:apitoken").decode()
        request = _make_request({
            "Authorization": f"Basic {creds}",
        })
        result = _extract_user_identity(request)
        assert result == "basic:user@example.com"

    def test_authorization_bearer_fallback(self) -> None:
        request = _make_request({
            "Authorization": "Bearer eyJhbGciOiJIUzI1NiJ9.long-jwt-token",
        })
        result = _extract_user_identity(request)
        assert result is not None
        assert result.startswith("bearer:...")

    def test_no_headers_returns_none(self) -> None:
        request = _make_request({})
        result = _extract_user_identity(request)
        assert result is None
