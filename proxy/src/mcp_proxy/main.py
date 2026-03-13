"""MCP access-control proxy — main application entry point.

This proxy intercepts MCP JSON-RPC ``tools/call`` requests, applies
read-only and project/space whitelist enforcement, emits audit log entries,
and forwards allowed requests to the upstream mcp-atlassian server.

All other MCP protocol messages (initialize, notifications, tools/list, etc.)
are forwarded transparently without modification.

Usage:
    uvicorn mcp_proxy.main:app --host 0.0.0.0 --port 8080
"""

from __future__ import annotations

import base64
import json
import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

import httpx
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response

from mcp_proxy.audit import _safe_arguments_summary, emit
from mcp_proxy.config import get_config
from mcp_proxy.enforcement import check_access
from mcp_proxy.forward import proxy_request

logger = logging.getLogger("mcp-proxy")

_MCP_TOOL_CALL_METHOD = "tools/call"


# ---------------------------------------------------------------------------
# Lifespan: create/destroy the shared httpx client
# ---------------------------------------------------------------------------


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Manage the shared httpx.AsyncClient lifecycle."""
    config = get_config()
    timeout = httpx.Timeout(
        connect=config.upstream_connect_timeout,
        read=config.upstream_read_timeout,
        write=30.0,
        pool=10.0,
    )
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        app.state.http_client = client
        logger.info(
            "MCP proxy started. Upstream: %s | read_only=%s | "
            "jira_whitelist=%s | confluence_whitelist=%s",
            config.upstream_url,
            config.read_only,
            sorted(config.jira_projects_set) or "all",
            sorted(config.confluence_spaces_set) or "all",
        )
        yield
    logger.info("MCP proxy shut down.")


app = FastAPI(title="MCP Access Control Proxy", lifespan=_lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------


@app.get("/healthz", include_in_schema=False)
async def health() -> JSONResponse:
    """Liveness probe endpoint."""
    return JSONResponse({"status": "ok"})


# ---------------------------------------------------------------------------
# Helper: extract user identity from request headers for audit logs
# ---------------------------------------------------------------------------


def _extract_user_identity(request: Request) -> str | None:
    """Extract a redacted user identifier from request headers for audit logs.

    Checks for per-request Atlassian credential headers first (four-header
    multi-tenant model), then falls back to the Authorization header for
    backward compatibility.

    Priority:
        1. ``X-Atlassian-Jira-Personal-Token`` header
        2. ``X-Atlassian-Confluence-Personal-Token`` header
        3. ``Authorization`` header (Basic / Bearer / Token)

    Args:
        request: The incoming Starlette request.

    Returns:
        A redacted identity string, or None if no identifying header is present.
    """
    # --- Four-header credential model (preferred) --------------------------
    jira_pat = request.headers.get("x-atlassian-jira-personal-token", "")
    if jira_pat:
        return (
            f"jira-pat:...{jira_pat[-8:]}"
            if len(jira_pat) > 8
            else "jira-pat:<short>"
        )

    confluence_pat = request.headers.get(
        "x-atlassian-confluence-personal-token", ""
    )
    if confluence_pat:
        return (
            f"confluence-pat:...{confluence_pat[-8:]}"
            if len(confluence_pat) > 8
            else "confluence-pat:<short>"
        )

    # --- Legacy Authorization header (backward compat) ---------------------
    auth = request.headers.get("authorization", "")
    if not auth:
        return None
    if auth.startswith("Basic "):
        try:
            decoded = base64.b64decode(auth[6:]).decode("utf-8")
            email = decoded.split(":", 1)[0]
            return f"basic:{email}"
        except Exception:
            return "basic:decode-error"
    if auth.startswith("Bearer "):
        token = auth[7:].strip()
        return f"bearer:...{token[-8:]}" if len(token) > 8 else "bearer:<short>"
    if auth.startswith("Token "):
        token = auth[6:].strip()
        return f"pat:...{token[-8:]}" if len(token) > 8 else "pat:<short>"
    return "unknown-auth-type"


# ---------------------------------------------------------------------------
# Main catch-all proxy route
# ---------------------------------------------------------------------------


@app.api_route(
    "/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "OPTIONS", "HEAD", "PATCH"],
)
async def proxy_all(request: Request, path: str) -> Response:
    """Intercept all requests; apply enforcement on tools/call; proxy the rest.

    Only POST requests with a ``tools/call`` JSON-RPC method body are subject
    to enforcement. All other requests are forwarded transparently.

    Args:
        request: The incoming Starlette request.
        path: The URL path (used to build the upstream URL).

    Returns:
        Upstream response, or a 403 JSON-RPC error if enforcement denies.
    """
    config = get_config()
    client: httpx.AsyncClient = request.app.state.http_client
    upstream_url = f"{config.upstream_url}/{path}"

    # Only enforce on POST (MCP uses POST for tool calls in streamable-http)
    if request.method != "POST":
        return await proxy_request(request, upstream_url, client)

    # Read and parse the JSON-RPC body
    raw_body = await request.body()
    rpc_body: dict[str, Any] | None = None

    try:
        rpc_body = json.loads(raw_body)
    except (json.JSONDecodeError, ValueError):
        # Not JSON — pass through (could be a ping / health check POST)
        return await proxy_request(request, upstream_url, client)

    method = rpc_body.get("method") if isinstance(rpc_body, dict) else None

    # Only enforce on tools/call
    if method != _MCP_TOOL_CALL_METHOD:
        return await proxy_request(request, upstream_url, client)

    # Extract tool name and arguments
    params = rpc_body.get("params", {})
    tool_name: str = params.get("name", "") if isinstance(params, dict) else ""
    arguments: dict[str, Any] = (
        params.get("arguments", {}) if isinstance(params, dict) else {}
    )
    request_id = rpc_body.get("id")
    user_identity = _extract_user_identity(request)

    if not tool_name:
        # Malformed tool call — forward and let the server handle it
        return await proxy_request(request, upstream_url, client)

    # Enforcement check
    result = check_access(
        tool_name,
        arguments,
        read_only=config.read_only,
        jira_whitelist=config.jira_projects_set,
        confluence_whitelist=config.confluence_spaces_set,
    )

    if config.audit_log_enabled:
        emit(
            decision="allow" if result.allowed else "deny",
            tool_name=tool_name,
            reason=result.reason,
            user_identity=user_identity,
            arguments_summary=_safe_arguments_summary(arguments),
            request_id=str(request_id) if request_id is not None else None,
        )

    if not result.allowed:
        logger.warning(
            "DENIED tool='%s' user='%s' reason='%s'",
            tool_name,
            user_identity or "unknown",
            result.reason,
        )
        # Return a JSON-RPC error response matching the MCP protocol spec.
        # Error code -32600 = Invalid Request; use -32001 for policy violations.
        error_response = {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {
                "code": -32001,
                "message": result.reason,
            },
        }
        return JSONResponse(
            content=error_response,
            status_code=403,
        )

    # Allowed — forward to upstream with original body
    # Re-attach body so proxy_request can read it
    async def _receive() -> dict[str, Any]:
        return {"type": "http.request", "body": raw_body, "more_body": False}

    patched_request = Request(request.scope, receive=_receive)
    return await proxy_request(patched_request, upstream_url, client)
