"""Streaming HTTP reverse proxy for forwarding MCP requests to upstream server."""

from __future__ import annotations

import logging
from typing import AsyncIterator

import httpx
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse

logger = logging.getLogger(__name__)

# Headers that must not be forwarded to upstream (hop-by-hop).
_HOP_BY_HOP = frozenset(
    [
        "connection",
        "host",  # rewritten by httpx from the upstream URL
        "keep-alive",
        "transfer-encoding",
        "te",
        "trailers",
        "upgrade",
        "proxy-authorization",
        "proxy-authenticate",
    ]
)


def _forward_headers(request: Request) -> dict[str, str]:
    """Build headers to forward to upstream, excluding hop-by-hop headers.

    Args:
        request: Incoming Starlette request.

    Returns:
        Dict of headers to include in the upstream request.
    """
    return {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in _HOP_BY_HOP
    }


async def proxy_request(
    request: Request,
    upstream_url: str,
    client: httpx.AsyncClient,
) -> Response:
    """Forward a request to the upstream server and stream the response back.

    Handles both regular JSON responses and SSE / chunked streaming responses.
    The request body is read in full before forwarding (required for JSON-RPC
    body inspection by the enforcement layer).

    Args:
        request: The incoming Starlette request (body already consumed by
            the calling middleware and re-injected).
        upstream_url: The full upstream URL (base URL + path).
        client: Shared httpx.AsyncClient instance.

    Returns:
        Starlette Response (streaming if upstream uses SSE/chunked).
    """
    body = await request.body()
    headers = _forward_headers(request)

    try:
        upstream_response = await client.send(
            client.build_request(
                method=request.method,
                url=upstream_url,
                headers=headers,
                content=body,
                params=dict(request.query_params),
            ),
            stream=True,
        )
    except httpx.ConnectError as e:
        logger.error("Upstream connect error: %s", e)
        return Response(
            content='{"error": "Upstream server unavailable"}',
            status_code=502,
            media_type="application/json",
        )
    except httpx.TimeoutException as e:
        logger.error("Upstream timeout: %s", e)
        return Response(
            content='{"error": "Upstream request timed out"}',
            status_code=504,
            media_type="application/json",
        )

    response_headers = {
        k: v
        for k, v in upstream_response.headers.items()
        if k.lower() not in _HOP_BY_HOP
    }

    async def _stream_body() -> AsyncIterator[bytes]:
        try:
            async for chunk in upstream_response.aiter_bytes(chunk_size=4096):
                yield chunk
        finally:
            await upstream_response.aclose()

    return StreamingResponse(
        content=_stream_body(),
        status_code=upstream_response.status_code,
        headers=response_headers,
        media_type=upstream_response.headers.get("content-type"),
    )
