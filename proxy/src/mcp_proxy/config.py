"""Proxy configuration loaded from environment variables."""

from __future__ import annotations

from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ProxyConfig(BaseSettings):
    """Configuration for the MCP access-control proxy.

    All values are loaded from environment variables. Prefix: ``PROXY_``.

    Attributes:
        upstream_url: Full base URL of the upstream mcp-atlassian server.
        listen_host: Host to bind the proxy server to.
        listen_port: Port to bind the proxy server to.
        jira_projects_whitelist: Allowed Jira project keys. Empty set = allow all.
        confluence_spaces_whitelist: Allowed Confluence space keys. Empty set = allow all.
        audit_log_enabled: Whether to emit structured audit log entries.
        upstream_connect_timeout: httpx connect timeout in seconds.
        upstream_read_timeout: httpx read timeout in seconds.
    """

    model_config = SettingsConfigDict(env_prefix="PROXY_", env_file=".env", extra="ignore")

    upstream_url: str = "http://mcp-atlassian:9000"
    listen_host: str = "0.0.0.0"  # noqa: S104
    listen_port: int = 8080
    jira_projects_whitelist: str = ""   # comma-separated, e.g. "PROJ,DEMO"
    confluence_spaces_whitelist: str = ""  # comma-separated, e.g. "ENG,HR"
    audit_log_enabled: bool = True
    upstream_connect_timeout: float = 10.0
    upstream_read_timeout: float = 120.0  # tool calls can be slow

    @field_validator("upstream_url")
    @classmethod
    def validate_upstream_url(cls, v: str) -> str:
        """Ensure upstream URL does not have trailing slash."""
        return v.rstrip("/")

    @property
    def jira_projects_set(self) -> frozenset[str]:
        """Parsed set of allowed Jira project keys (upper-cased)."""
        if not self.jira_projects_whitelist.strip():
            return frozenset()
        return frozenset(
            k.strip().upper()
            for k in self.jira_projects_whitelist.split(",")
            if k.strip()
        )

    @property
    def confluence_spaces_set(self) -> frozenset[str]:
        """Parsed set of allowed Confluence space keys (upper-cased)."""
        if not self.confluence_spaces_whitelist.strip():
            return frozenset()
        return frozenset(
            k.strip().upper()
            for k in self.confluence_spaces_whitelist.split(",")
            if k.strip()
        )


@lru_cache(maxsize=1)
def get_config() -> ProxyConfig:
    """Return the singleton proxy configuration.

    Returns:
        Cached ProxyConfig instance.
    """
    return ProxyConfig()
