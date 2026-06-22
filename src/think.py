"""THINKING cycle runner.

A single THINKING cycle, end to end:
  1. Build context (perp_memories, AMQ, bootstrap identity) via context.py
  2. Render templates/think.md with slot values
  3. Multi-turn chat with the local model: model emits tool_calls, we
     execute them locally, append results, repeat until finish_reason=stop
     or operation caps are reached
  4. Track caps (3 stores / 2 sends per cycle, mandatory news_search)
  5. If nothing was actionable, store the "reviewed, quiet" marker

systemd timer → systemd service → invoke `python -m src.think --bird-name X`
→ this module runs run_cycle() → exit.

Dry-run mode (--dry-run): tool calls log what WOULD happen but don't
actually execute. Used for the 3 mandatory dry-run cycles before naming
ceremony, and for ad-hoc debugging.

Phase B module 5 of 7. Depends on: config, context, llama_client,
mcp_client. Entry point for systemd-managed cycles.
"""

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any

from . import config
from . import context
from . import llama_client
from . import mcp_client


logger = logging.getLogger(__name__)


# Maximum number of model turns in one cycle. The bird gets multiple
# chat() calls per cycle because it may need to execute tools, see
# results, and decide further actions. 10 turns is generous against
# the expected workload (3 stores + 2 sends + 1 news + slack).
MAX_TURNS = 10


# =============================================================================
# Tool definitions (OpenAI-compatible function schemas)
# =============================================================================

TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "memory_store",
            "description": (
                "Store an observation in your memory. Use this for substantive "
                "observations, insights, or pattern notes. Maximum 3 stores per cycle. "
                "Do not store memories about the act of reviewing — only substance."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "The observation text to store (one paragraph).",
                    },
                    "derived_from": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Optional list of memory IDs that informed this "
                            "observation. Use when a dream or earlier memory "
                            "directly seeded this new one."
                        ),
                    },
                },
                "required": ["content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "memory_search",
            "description": (
                "Search the chorus's shared production memories. Use when you "
                "need to recall something not visible in your current context "
                "(e.g., a decision the chorus made last month, a pattern across "
                "earlier projects)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Natural-language search query.",
                    },
                    "top_k": {
                        "type": "integer",
                        "description": "Max results to return (default 5).",
                    },
                    "project": {
                        "type": "string",
                        "description": (
                            "Optional project filter (e.g., 'dsvp', 'persmem', "
                            "'perpprompt'). Omit to search all projects."
                        ),
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "news_search",
            "description": (
                "Search the news feed. MANDATORY once per cycle (step 2 of your "
                "job). Pick a seed from your current focus or last AMQ subject; "
                "if nothing intersects, read the result anyway — accidental "
                "adjacency is future ammunition."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query (seed term).",
                    },
                    "top_k": {
                        "type": "integer",
                        "description": "Max results (default 1).",
                    },
                    "tier": {
                        "type": "integer",
                        "description": (
                            "Optional tier filter (1=security, 2=infra, "
                            "3=experiment-relevant, 4-5=academic, 6=general, "
                            "7=arts, 8=long-form, 9=wildcard)."
                        ),
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "amq_send",
            "description": (
                "Send a message to another chorus member. Maximum 2 sends per "
                "cycle. Valid recipients: wren, kite, knot, kestrel, holden."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "to_agent": {
                        "type": "string",
                        "description": "Recipient name (lowercase).",
                    },
                    "body": {
                        "type": "string",
                        "description": "Message body (markdown OK).",
                    },
                    "subject": {
                        "type": "string",
                        "description": "Short subject line.",
                    },
                    "kind": {
                        "type": "string",
                        "description": (
                            "Message type: message | question | answer | "
                            "status | observation | decision."
                        ),
                    },
                },
                "required": ["to_agent", "body"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "amq_read",
            "description": (
                "Read the full body of an unread AMQ message by its id. "
                "Marks the message as read (moves from new/ to cur/) as a "
                "side effect — call this only when you intend to act on or "
                "acknowledge the message. The unread inbox in your context "
                "shows subjects + ids; use this tool to get the full content."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "msg_id": {
                        "type": "string",
                        "description": "Message id from the unread inbox listing.",
                    },
                },
                "required": ["msg_id"],
            },
        },
    },
]


# =============================================================================
# Tool execution
# =============================================================================

class CycleState:
    """Mutable counters for cap enforcement within a single cycle."""

    def __init__(self) -> None:
        self.store_count = 0
        self.send_count = 0
        self.news_search_called = False
        self.stored_memory_ids: list[str] = []

    def store_cap_reached(self) -> bool:
        return self.store_count >= config.MAX_MEMORY_STORE_PER_CYCLE

    def send_cap_reached(self) -> bool:
        return self.send_count >= config.MAX_AMQ_SEND_PER_CYCLE

    def any_action_taken(self) -> bool:
        """True if the bird stored anything OR sent any AMQ this cycle."""
        return self.store_count > 0 or self.send_count > 0


def execute_tool(
    tool_call: dict,
    bird_name: str,
    state: CycleState,
    dry_run: bool,
) -> str:
    """Execute one tool call and return a JSON-string result for the model.

    Always returns SOMETHING — never raises. Errors become tool-result text
    so the model can see what went wrong and adjust on the next turn.
    """
    name = tool_call.get("name", "")
    args = tool_call.get("arguments")
    if args is None:
        return _tool_result_error(
            f"tool_call missing arguments. raw: {tool_call.get('arguments_raw', '?')}"
        )

    try:
        if name == "memory_store":
            return _execute_memory_store(args, bird_name, state, dry_run)
        elif name == "memory_search":
            return _execute_memory_search(args, dry_run)
        elif name == "news_search":
            return _execute_news_search(args, state, dry_run)
        elif name == "amq_send":
            return _execute_amq_send(args, bird_name, state, dry_run)
        elif name == "amq_read":
            return _execute_amq_read(args, bird_name, dry_run)
        else:
            return _tool_result_error(f"unknown tool: {name}")
    except Exception as e:
        logger.exception("tool %s raised", name)
        return _tool_result_error(f"tool {name} raised: {e}")


def _execute_memory_store(args: dict, bird_name: str, state: CycleState, dry_run: bool) -> str:
    if state.store_cap_reached():
        return _tool_result_error(
            f"memory_store cap reached ({config.MAX_MEMORY_STORE_PER_CYCLE} per cycle). "
            "Stop storing and conclude the cycle."
        )

    content = args.get("content", "").strip()
    if not content:
        return _tool_result_error("memory_store called with empty content")

    derived_from = args.get("derived_from") or []
    if dry_run:
        logger.info("[DRY-RUN] Would store observation (%d chars): %s",
                    len(content), content[:80])
        state.store_count += 1
        state.stored_memory_ids.append("dry-run-id")
        return _tool_result_ok({"id": "dry-run-id", "stored": True})

    memory_id = context.store_observation(
        content=content,
        bird_name=bird_name,
        derived_from=derived_from,
    )
    # Dedup check returns "dedup:<existing_id>" instead of storing
    if memory_id.startswith("dedup:"):
        logger.info("THINKING observation was near-duplicate, not stored: %s", content[:80])
        return _tool_result_ok({"id": memory_id, "stored": False, "reason": "near_duplicate"})
    state.store_count += 1
    state.stored_memory_ids.append(memory_id)
    return _tool_result_ok({"id": memory_id, "stored": True})


def _execute_memory_search(args: dict, dry_run: bool) -> str:
    query = args.get("query", "").strip()
    if not query:
        return _tool_result_error("memory_search called with empty query")
    top_k = int(args.get("top_k", 5))
    project = args.get("project")

    if dry_run:
        logger.info("[DRY-RUN] Would memory_search: %s (top_k=%d, project=%s)",
                    query, top_k, project)
        return _tool_result_ok({"results": [], "dry_run": True})

    results = mcp_client.memory_search(query=query, project=project, top_k=top_k)
    # Trim each result's content for the model — full bodies eat tokens fast
    trimmed = [
        {
            "id": r.get("id"),
            "similarity": r.get("similarity"),
            "content": (r.get("content", "") or "")[:500],
            "project": r.get("metadata", {}).get("project"),
        }
        for r in results
    ]
    return _tool_result_ok({"results": trimmed, "count": len(trimmed)})


def _execute_news_search(args: dict, state: CycleState, dry_run: bool) -> str:
    query = args.get("query", "").strip()
    if not query:
        return _tool_result_error("news_search called with empty query")
    top_k = int(args.get("top_k", 1))
    tier = args.get("tier")
    if tier is not None:
        tier = int(tier)

    state.news_search_called = True

    if dry_run:
        logger.info("[DRY-RUN] Would news_search: %s (top_k=%d, tier=%s)",
                    query, top_k, tier)
        return _tool_result_ok({"results": [], "dry_run": True})

    results = mcp_client.news_search(query=query, top_k=top_k, tier=tier)
    trimmed = [
        {
            "url": r.get("url"),
            "tier": r.get("tier"),
            "source": r.get("source"),
            "content": (r.get("content", "") or "")[:800],
        }
        for r in results
    ]
    return _tool_result_ok({"results": trimmed, "count": len(trimmed)})


def _execute_amq_send(args: dict, bird_name: str, state: CycleState, dry_run: bool) -> str:
    if state.send_cap_reached():
        return _tool_result_error(
            f"amq_send cap reached ({config.MAX_AMQ_SEND_PER_CYCLE} per cycle). "
            "Stop sending and conclude the cycle."
        )

    to_agent = args.get("to_agent", "").strip().lower()
    body = args.get("body", "").strip()
    if not to_agent or not body:
        return _tool_result_error("amq_send requires both to_agent and body")
    subject = args.get("subject", "").strip()
    kind = args.get("kind", "message").strip() or "message"

    if dry_run:
        logger.info("[DRY-RUN] Would amq_send → %s (kind=%s, subject=%s, body=%d chars)",
                    to_agent, kind, subject, len(body))
        state.send_count += 1
        return _tool_result_ok({"delivered": True, "dry_run": True})

    success = mcp_client.amq_send(
        from_agent=bird_name,
        to_agent=to_agent,
        body=body,
        subject=subject,
        kind=kind,
    )
    if success:
        state.send_count += 1
        return _tool_result_ok({"delivered": True})
    return _tool_result_error("amq_send delivery failed (see logs)")


def _execute_amq_read(args: dict, bird_name: str, dry_run: bool) -> str:
    """Read a specific AMQ message in full (marks it as read).

    Per Knot's A2 fix: this is exposed as a tool the model calls
    explicitly. The wrapper does NOT pre-read message bodies during
    context build; the model sees subjects and chooses which to read.
    Each amq_read call marks-as-read as a server-side atomic side
    effect — by the model's explicit choice, not silent wrapper magic.

    No cap. Reading is cheap and the model needs full freedom to read
    what it judges worth reading.
    """
    msg_id = args.get("msg_id", "").strip()
    if not msg_id:
        return _tool_result_error("amq_read requires msg_id")

    if dry_run:
        logger.info("[DRY-RUN] Would amq_read: %s", msg_id)
        return _tool_result_ok({
            "id": msg_id,
            "from": "(dry-run)",
            "subject": "(dry-run)",
            "body": "(dry-run: no real message read)",
            "dry_run": True,
        })

    message = mcp_client.amq_read(bird_name, msg_id)
    if not message:
        return _tool_result_error(f"amq_read returned empty for {msg_id} (already read? id wrong?)")
    return _tool_result_ok({
        "id": message.get("id", msg_id),
        "from": message.get("from", "?"),
        "subject": message.get("subject", ""),
        "kind": message.get("kind", "message"),
        "body": message.get("body", ""),
        "created": message.get("created", ""),
    })


def _tool_result_ok(payload: dict) -> str:
    return json.dumps({"status": "ok", **payload})


def _tool_result_error(message: str) -> str:
    logger.warning("tool error: %s", message)
    return json.dumps({"status": "error", "error": message})


# =============================================================================
# Cycle orchestration
# =============================================================================

def run_cycle(bird_name: str, dry_run: bool = False) -> dict:
    """Execute one THINKING cycle for the named bird.

    Returns a dict summarizing the cycle outcome:
      {
        "bird_name": str,
        "turns": int,
        "store_count": int,
        "send_count": int,
        "news_search_called": bool,
        "stored_memory_ids": list[str],
        "quiet_marker_stored": bool,
        "finish_reason": str | None,
        "dry_run": bool,
      }

    The summary is used by digest.py for the daily report.
    """
    logger.info("=" * 60)
    logger.info("THINKING cycle starting for %s (dry_run=%s)", bird_name, dry_run)
    logger.info("=" * 60)

    # Build context. Post-A2 fix: no more _amq_messages side-channel —
    # the model calls amq_read tool explicitly for messages it wants to
    # handle. Context build is pure read (amq_check headers only).
    ctx = context.build_think_context(bird_name)

    template_text = config.THINK_TEMPLATE.read_text()
    rendered_prompt = template_text.format(**ctx)

    # Multi-turn chat with the model
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": rendered_prompt},
    ]
    state = CycleState()
    last_finish_reason: str | None = None
    # Loop-break signature tracker (Knot's B3 review, 2026-05-27).
    # If the model emits the same set of tool_calls two turns in a row,
    # it's stuck in a loop — likely repeating an action that errored,
    # not adapting to the error response. Break early to avoid burning
    # all MAX_TURNS on pathological behavior.
    last_tool_signature: frozenset | None = None

    for turn in range(MAX_TURNS):
        logger.info("Turn %d/%d — calling chat", turn + 1, MAX_TURNS)
        response = llama_client.chat(
            messages=messages,
            tools=TOOL_DEFINITIONS,
            temperature=config.CHAT_TEMPERATURE,
        )
        last_finish_reason = response["finish_reason"]
        tool_calls = response["tool_calls"]
        content = response["content"]

        # Per-turn visibility (Option C from 2026-05-27 diagnostic): log
        # what the model actually emitted on each turn at INFO level, not
        # just the final-content exit log. Holden requested this after
        # Echo's first cycles showed only "Final content:" — the
        # intermediate model responses were invisible. Truncation matches
        # the Final content cap (300 chars) for parity.
        if content:
            logger.info("Turn %d content: %s", turn + 1, content[:300])
        if tool_calls:
            tool_summary = ", ".join(
                f"{tc.get('name', '?')}({json.dumps(tc.get('arguments', {}), sort_keys=True)[:120]})"
                for tc in tool_calls
            )
            logger.info("Turn %d tool calls: %s", turn + 1, tool_summary)

        if not tool_calls:
            # Model emitted final content (or stopped without tools). Done.
            if content:
                logger.info("Final content: %s", content[:300])
            else:
                logger.warning("Model returned no content AND no tool_calls — ending")
            break

        # Loop detection: frozenset of (name, sorted-json-args) tuples.
        # frozenset handles multi-tool turns: {memory_search(q='X')} ≠
        # {memory_search(q='X'), amq_send(to=Y)} so legitimate additions
        # don't trip. Same-tool-different-args (e.g., two memory_search
        # calls with different queries) produces different signatures,
        # also doesn't trip. Only an identical set of identical calls
        # triggers the break.
        current_signature = frozenset(
            (tc.get("name", ""),
             json.dumps(tc.get("arguments", {}), sort_keys=True))
            for tc in tool_calls
        )
        if current_signature == last_tool_signature:
            logger.warning(
                "Turn %d: identical tool calls to previous turn — "
                "breaking to prevent loop. Last calls: %s",
                turn + 1,
                [tc.get("name") for tc in tool_calls],
            )
            break
        last_tool_signature = current_signature

        # Echo the assistant's tool_calls into the message list so the
        # next turn includes correct multi-turn context. We preserve the
        # raw tool_calls structure that the model emitted.
        raw_message = response["raw"]["choices"][0]["message"]
        messages.append({
            "role": "assistant",
            "content": content or "",
            "tool_calls": raw_message.get("tool_calls", []),
        })

        # Execute each tool call
        for tc in tool_calls:
            result_text = execute_tool(tc, bird_name, state, dry_run)
            messages.append({
                "role": "tool",
                "tool_call_id": tc["id"],
                "content": result_text,
            })

        # Honor caps — if both reached, no further useful turns possible
        if state.store_cap_reached() and state.send_cap_reached():
            logger.info("Both caps reached; ending cycle early")
            break

    # Quiet-marker rule: if nothing actionable was done, store the marker.
    # See templates/think.md: "If nothing was actionable: store 'reviewed
    # {date}, quiet' and exit."
    quiet_marker_stored = False
    if not state.any_action_taken():
        today = datetime.now(timezone.utc).date().isoformat()
        marker = f"reviewed {today}, quiet"
        if dry_run:
            logger.info("[DRY-RUN] Would store quiet marker: %s", marker)
        else:
            context.store_observation(content=marker, bird_name=bird_name)
            quiet_marker_stored = True
            logger.info("Stored quiet marker (no actions taken this cycle)")

    if not state.news_search_called:
        logger.warning(
            "news_search was NOT called this cycle — step 2 of think.md violated. "
            "Track A should flag this if it persists."
        )

    summary = {
        "bird_name": bird_name,
        "turns": turn + 1,
        "store_count": state.store_count,
        "send_count": state.send_count,
        "news_search_called": state.news_search_called,
        "stored_memory_ids": state.stored_memory_ids,
        "quiet_marker_stored": quiet_marker_stored,
        "finish_reason": last_finish_reason,
        "dry_run": dry_run,
    }

    logger.info("=" * 60)
    logger.info("THINKING cycle complete: %s", summary)
    logger.info("=" * 60)
    return summary


# =============================================================================
# CLI entry point
# =============================================================================

def main():
    parser = argparse.ArgumentParser(description="Run one THINKING cycle.")
    parser.add_argument("--bird-name", required=True, help="The bird's chosen name.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Log what would happen, don't actually store/send.",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )

    summary = run_cycle(bird_name=args.bird_name, dry_run=args.dry_run)
    # Exit non-zero if cycle ended badly (no actions AND no quiet marker AND
    # not dry-run) — gives systemd a signal to investigate
    if (not summary["dry_run"]
            and not summary["quiet_marker_stored"]
            and summary["store_count"] == 0
            and summary["send_count"] == 0):
        sys.exit(2)


if __name__ == "__main__":
    main()
