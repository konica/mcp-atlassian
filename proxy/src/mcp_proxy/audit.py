"""Structured audit logging for MCP proxy access control decisions."""

from __future__ import annotations

import json
import logging
import time
from typing import Any

logger = logging.getLogger("mcp-proxy.audit")


def emit(
    *,
    decision: str,
    tool_name: str,
    reason: str,
    user_identity: str | None,
    arguments_summary: dict[str, Any] | None = None,
    request_id: str | None = None,
) -> None:
    """Emit a structured audit log entry.

    Output is a single JSON line written to the ``mcp-proxy.audit`` logger.
    Configure handlers externally (e.g., write to file, ship to SIEM).

    Args:
        decision: ``"allow"`` or ``"deny"``.
        tool_name: The MCP tool name that was called.
        reason: Human-readable decision reason.
        user_identity: User email or token fingerprint extracted from request.
        arguments_summary: Sanitized subset of arguments (no secrets).
        request_id: Optional MCP session or request ID for correlation.
    """
    entry: dict[str, Any] = {
        "ts": time.time(),
        "decision": decision,
        "tool": tool_name,
        "reason": reason,
        "user": user_identity or "unknown",
    }
    if request_id:
        entry["request_id"] = request_id
    if arguments_summary:
        entry["args"] = arguments_summary

    logger.info(json.dumps(entry))


def _safe_arguments_summary(arguments: dict[str, Any]) -> dict[str, Any]:
    """Extract a sanitized summary of tool arguments for audit logging.

    Redacts any key whose name suggests it may contain a secret.

    Args:
        arguments: Raw arguments from the MCP tool call.

    Returns:
        Dict with sensitive values replaced by ``"[REDACTED]"``.
    """
    _SENSITIVE_KEYS = frozenset(
        ["token", "password", "secret", "api_key", "api_token", "credential"]
    )
    return {
        k: "[REDACTED]" if any(s in k.lower() for s in _SENSITIVE_KEYS) else v
        for k, v in arguments.items()
    }
