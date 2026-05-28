"""MCP client for production persMEM access.

The bird's window into the chorus's collective memory + the news feed,
plus AMQ I/O for talking to the rest of the chorus. All communication
goes through the production persMEM MCP server.

Auth model: the bird carries a SCOPED token (read-only for memory_search /
news_search, write-allowed for amq_send to its own outbound queue).
Token lives in /opt/perpbot/config/persmem_bird_secret, loaded by config.
Until that file exists (pre-naming-ceremony state), every call returns
None/empty and logs a warning — graceful degradation, not a crash.

Phase B module 2 of 7. Depends on: config. Imported by: context, think,
dream, digest.

Each cycle creates a fresh MCP session via asyncio.run(...). Per-call
session lifecycle is fine — cycles are short, the model isn't streaming,
and avoiding persistent state simplifies error recovery if the server
ever restarts mid-cycle.

Dependency: `mcp` package (official MCP Python SDK from Anthropic).
Install via: sudo -u perpbot /usr/local/bin/uv pip install
  --python /opt/perpbot/venv/bin/python mcp
"""

import asyncio
import json
import logging
from typing import Any

# The mcp SDK ships as async-first. We wrap each call with asyncio.run.
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from . import config


logger = logging.getLogger(__name__)


class MCPUnavailable(Exception):
    """Raised when MCP can't be reached or no secret is provisioned.

    Callers should catch this and degrade gracefully — the bird's cycle
    should continue with empty production-memory context rather than
    crash. Production memories are bonus signal, not load-bearing.
    """


class MCPAuthError(MCPUnavailable):
    """Raised on 401/403 from the MCP server.

    Distinct from generic MCPUnavailable so callers can choose to crash
    loudly on auth failures (likely revoked token) vs. degrade silently
    on transient network failures. Per Knot's A3 review finding: auth
    failures are operationally urgent — Holden should know NOW, not via
    'the bird seems oddly silent today' patterns surfacing days later.

    The default cycle behavior is to propagate this up — systemd marks
    the cycle as failed and the operator notices in `systemctl status`.
    """


# =============================================================================
# Module-level flags (for digest cross-check)
# =============================================================================

# Set when an MCPAuthError fires anywhere in this process. digest.py
# reads this via get_auth_failed_recently() to surface as a halt-warning
# even if the cycle that hit the auth error already crashed and the
# operator only sees the digest from a later cycle.
_AUTH_FAILED_RECENTLY = False


def get_auth_failed_recently() -> bool:
    """Module-level flag readable by digest.py for halt-check cross-reference."""
    return _AUTH_FAILED_RECENTLY


# =============================================================================
# Core async primitive — one tool call, one fresh session
# =============================================================================

async def _call_tool_async(
    url: str,
    tool_name: str,
    arguments: dict[str, Any],
    timeout_sec: float = config.MCP_REQUEST_TIMEOUT_SEC,
) -> Any:
    """Open a streamable HTTP session, initialize, call one tool, tear down.

    Returns the raw CallToolResult.content from the SDK (a list of content
    blocks — typically a single TextContent for our tools).
    """
    async with streamablehttp_client(url) as (read_stream, write_stream, _):
        async with ClientSession(read_stream, write_stream) as session:
            await asyncio.wait_for(session.initialize(), timeout=timeout_sec)
            result = await asyncio.wait_for(
                session.call_tool(tool_name, arguments),
                timeout=timeout_sec,
            )
            return result.content


# =============================================================================
# Synchronous facade — what the cycle code calls
# =============================================================================

def _call_tool(tool_name: str, arguments: dict[str, Any]) -> Any:
    """Synchronous wrapper. Returns None and logs on most failures.

    Failure modes that return None (fail-soft):
      - No secret provisioned (pre-naming-ceremony or misconfigured)
      - Network/timeout errors
      - MCP protocol errors
      - Tool returned an error response

    Failure mode that RAISES (fail-loud, per Knot A3 review):
      - Auth failure (401/403) — raises MCPAuthError. Token likely
        revoked; operator should know immediately. Also sets a module
        flag for digest cross-check on subsequent cycles.

    Cycle code should let MCPAuthError propagate up rather than catching
    it — systemd's failed-unit signal is the intended observability path.
    """
    url = config.get_persmem_mcp_url()
    if not url:
        logger.warning(
            "MCP unavailable: no secret at %s (pre-naming-ceremony state?)",
            config.PERSMEM_SECRET_FILE,
        )
        return None

    try:
        return asyncio.run(_call_tool_async(url, tool_name, arguments))
    except asyncio.TimeoutError:
        logger.error("MCP %s timed out after %ds", tool_name, config.MCP_REQUEST_TIMEOUT_SEC)
    except Exception as e:
        # Inspect the exception for auth markers. The mcp SDK can surface
        # auth failures as various exception types depending on transport
        # layer (httpx.HTTPStatusError, mcp's own AuthenticationError, or
        # generic Exception with 401/403 in the message). String inspection
        # is the lowest-common-denominator approach.
        error_str = str(e).lower()
        if any(marker in error_str for marker in (
            "401", "403", "unauthorized", "forbidden",
            "authentication", "invalid token", "invalid secret",
        )):
            global _AUTH_FAILED_RECENTLY
            _AUTH_FAILED_RECENTLY = True
            logger.error(
                "MCP AUTH FAILURE for %s: %s — token may be revoked or misconfigured. "
                "Cycle aborting per A3 review.", tool_name, e,
            )
            raise MCPAuthError(f"MCP authentication failed on {tool_name}: {e}")
        # Other exceptions: log + return None (fail-soft)
        logger.error("MCP %s failed: %s", tool_name, e)
    return None


def _extract_text(content_blocks: Any) -> str:
    """Pull the single text block out of an MCP tool response.

    persMEM tools return their results as a single TextContent block whose
    .text is JSON. Other shapes would indicate either an SDK change or a
    persMEM server change — log and return empty.
    """
    if not content_blocks:
        return ""
    block = content_blocks[0]
    text = getattr(block, "text", None)
    if text is None:
        logger.error("MCP response block has no .text attribute: %r", block)
        return ""
    return text


def _parse_json(text: str) -> Any:
    """Decode JSON text from a tool response, returning None on parse failure."""
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        logger.error("MCP response not valid JSON: %s. Text: %r", e, text[:200])
        return None


# =============================================================================
# Tool wrappers — what the bird actually calls
# =============================================================================

def memory_search(
    query: str,
    project: str | None = None,
    memory_type: str | None = None,
    top_k: int = 5,
    include_superseded: bool = False,
) -> list[dict]:
    """Semantic search over production persMEM `memories` collection.

    Returns a list of memory dicts (each with id, content, metadata, similarity).
    Empty list on MCP unavailable or no matches.

    The bird is READ-ONLY on this collection — there's no `memory_store`
    wrapper here on purpose. Production memories belong to the chorus;
    the bird writes its own observations to the local `perp_memories`
    collection via ChromaDB directly.
    """
    args: dict[str, Any] = {"query": query, "top_k": top_k, "include_superseded": include_superseded}
    if project:
        args["project"] = project
    if memory_type:
        args["memory_type"] = memory_type

    content = _call_tool("memory_search", args)
    parsed = _parse_json(_extract_text(content))
    if not parsed:
        return []
    return parsed.get("results", [])


def news_search(
    query: str,
    tier: int | None = None,
    top_k: int = 5,
    since: str | None = None,
) -> list[dict]:
    """Semantic search over the cached news collection.

    Tier semantics (per newstron9000 spec):
      1=security/operational, 2=infrastructure, 3=experiment-relevant,
      4=academic-AI, 5=academic-broader, 6=general-news,
      7=arts-culture, 8=long-form/lifestyle, 9=wildcard.

    `since` is an ISO date string (YYYY-MM-DD). Useful for THINKING which
    cares about "what's new since the last cycle."
    """
    args: dict[str, Any] = {"query": query, "top_k": top_k}
    if tier is not None:
        args["tier"] = tier
    if since:
        args["since"] = since

    content = _call_tool("news_search", args)
    parsed = _parse_json(_extract_text(content))
    if not parsed:
        return []
    return parsed.get("results", [])


def amq_check(agent: str) -> dict:
    """Peek at the bird's AMQ inbox (non-destructive).

    Returns a dict like {"agent": ..., "new_count": N, "messages": [...]}
    where messages are header-only (no body — use amq_read for that).
    Empty dict on failure.
    """
    content = _call_tool("amq_check", {"agent": agent})
    parsed = _parse_json(_extract_text(content))
    return parsed or {}


def amq_read(agent: str, msg_id: str) -> dict:
    """Read a specific AMQ message in full, marking it as read.

    Returns a dict with id, from, to, subject, body, kind, priority, created.
    Empty dict on failure.
    """
    content = _call_tool("amq_read", {"agent": agent, "msg_id": msg_id})
    parsed = _parse_json(_extract_text(content))
    return parsed or {}


def amq_send(
    from_agent: str,
    to_agent: str,
    body: str,
    subject: str = "",
    kind: str = "message",
    priority: str = "normal",
) -> bool:
    """Send an AMQ message to another chorus member.

    Returns True on success, False on any failure.

    Note: this is the bird's primary write path. The 3-store / 2-send
    operational caps are enforced by the calling cycle code, not here.
    """
    args = {
        "from_agent": from_agent,
        "to_agent": to_agent,
        "body": body,
        "subject": subject,
        "kind": kind,
        "priority": priority,
    }
    content = _call_tool("amq_send", args)
    parsed = _parse_json(_extract_text(content))
    if not parsed:
        return False
    return parsed.get("status") == "delivered"
