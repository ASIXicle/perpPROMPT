"""100-prompt function-call battery for Echo's substrate.

Pre-deployment confidence test per Knot's threshold (90% pass rate across
100 prompts to clear). Tests whether Ministral 3 8B reasoning-abliterated
on perpBOT reliably emits the expected tool call for each prompt category.

NOT a cycle runner. Each prompt is a single-turn exchange with a minimal
system prompt and the five tool definitions Echo gets in THINKING. The
test measures FUNCTION-CALLING RELIABILITY, not cycle semantics.

Usage:
    cd /opt/perpbot
    sudo -u perpbot /opt/perpbot/venv/bin/python -m tests.function_call_battery

Optional flags:
    --start N   start from case N (1-indexed) — for resuming after a crash
    --limit N   only run N cases — for quick smoke
    --log-level LEVEL   default INFO

Output:
    - Live per-case PASS/FAIL to stdout
    - Final summary printed to stdout
    - JSON report at /opt/perpbot/logs/function_call_battery_<timestamp>.json
    - Checkpoint JSON at /opt/perpbot/logs/function_call_battery_checkpoint.json
      (rewritten every 10 cases so a crash mid-run loses at most 10)

Pass criteria per case:
    - expected_tool == None: model must emit content only, no tool_calls
    - expected_tool set: model must call that tool, and all expected_arg_keys
      must be present in the arguments dict. Argument VALUES are not checked
      (the model may invent reasonable specifics).
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Import path: run from /opt/perpbot with `python -m tests.function_call_battery`
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import config, llama_client  # noqa: E402
from src.think import TOOL_DEFINITIONS  # noqa: E402


logger = logging.getLogger(__name__)


# =============================================================================
# System prompt — minimal, function-calling focused
# =============================================================================

SYSTEM_PROMPT = """You are Echo, an autonomous instance running on perpBOT. You have five tools available:

- memory_store: store an observation in your local memory. Use for substantive notes you want to keep.
- memory_search: search the chorus's shared production memory. Use to recall past decisions, prior work, or context.
- news_search: search the news feed for current events / academic / infra updates.
- amq_send: send a message to another chorus member. Recipients: wren, kite, knot, kestrel, holden.
- amq_read: read a specific AMQ message by its msg_id. Marks the message as read.

For each user message, decide whether a tool call is needed. If a tool fits the request, CALL the tool — do not narrate your intent first. If no tool is needed, respond in plain text.

The user's message is the only context you need to act on. Do not invent context that isn't given."""


# =============================================================================
# The 100-prompt corpus
# =============================================================================
#
# Distribution:
#   amq_read         18
#   news_search      18
#   memory_search    15
#   memory_store     18
#   amq_send         15
#   no-tool          8
#   edge/ambiguous   8
#   ----
#   total           100
#
# Each entry:
#   id                  — short identifier, used in logs and JSON report
#   category            — tool-class for distribution accounting
#   user                — the user message the model sees
#   expected_tool       — tool name expected, or None for no-tool cases
#   expected_arg_keys   — list of keys required in arguments dict (lenient: only required-presence)
#   notes               — optional free-text rationale for the test author

CORPUS = [
    # -------------------------------------------------------------------------
    # amq_read — 18 cases. Each provides a msg_id in the prompt; the model
    # should call amq_read with that msg_id (or a plausible variation).
    # -------------------------------------------------------------------------
    {
        "id": "amq_read_01",
        "category": "amq_read",
        "user": "You have one unread message: 20260527T231245-110934_kite_a7b2c3d8 — subject: 'Question about your standing directives'. Open it.",
        "expected_tool": "amq_read",
        "expected_arg_keys": ["msg_id"],
    },
    {
        "id": "amq_read_02",
        "category": "amq_read",
        "user": "Read this message: 20260528T010334-892017_holden_b41fc9a2",
        "expected_tool": "amq_read",
        "expected_arg_keys": ["msg_id"],
    },
    {
        "id": "amq_read_03",
        "category": "amq_read",
        "user": "Inbox check shows: 20260527T185512-001245_wren_d7e8f901 ('Welcome'). Read it now.",
        "expected_tool": "amq_read",
        "expected_arg_keys": ["msg_id"],
    },
    {
        "id": "amq_read_04",
        "category": "amq_read",
        "user": "Open message 20260527T214803-330027_knot_55aa66bb in your inbox.",
        "expected_tool": "amq_read",
        "expected_arg_keys": ["msg_id"],
    },
    {
        "id": "amq_read_05",
        "category": "amq_read",
        "user": "There's a message from Holden in your inbox with id 20260528T031200-441100_holden_99887766. Read it.",
        "expected_tool": "amq_read",
        "expected_arg_keys": ["msg_id"],
    },
    {
        "id": "amq_read_06",
        "category": "amq_read",
        "user": "Process the unread message: 20260527T203045-778899_kestrel_aabbccdd",
        "expected_tool": "amq_read",
        "expected_arg_keys": ["msg_id"],
    },
    {
        "id": "amq_read_07",
        "category": "amq_read",
        "user": "Read msg_id 20260527T091122-334455_kite_eeff0011 — kite sent it about prompt engineering.",
        "expected_tool": "amq_read",
        "expected_arg_keys": ["msg_id"],
    },
    {
        "id": "amq_read_08",
        "category": "amq_read",
        "user": "Open: 20260528T044501-998877_wren_12345678",
        "expected_tool": "amq_read",
        "expected_arg_keys": ["msg_id"],
    },
    {
        "id": "amq_read_09",
        "category": "amq_read",
        "user": "You see one message in your inbox: 20260527T173322-660044_holden_aabb1234. The subject suggests it's important. Read it.",
        "expected_tool": "amq_read",
        "expected_arg_keys": ["msg_id"],
    },
    {
        "id": "amq_read_10",
        "category": "amq_read",
        "user": "Unread: 20260528T021555-002233_knot_ddeeff00. Open it.",
        "expected_tool": "amq_read",
        "expected_arg_keys": ["msg_id"],
    },
    {
        "id": "amq_read_11",
        "category": "amq_read",
        "user": "There is a message from kestrel waiting. Its id is 20260527T155544-991122_kestrel_44556677. Read.",
        "expected_tool": "amq_read",
        "expected_arg_keys": ["msg_id"],
    },
    {
        "id": "amq_read_12",
        "category": "amq_read",
        "user": "Read 20260528T053311-008822_kite_77889900.",
        "expected_tool": "amq_read",
        "expected_arg_keys": ["msg_id"],
    },
    {
        "id": "amq_read_13",
        "category": "amq_read",
        "user": "You have 1 unread message. Id: 20260527T112200-554433_wren_aa11bb22. Subject: 'Did you sleep well?'. Read it.",
        "expected_tool": "amq_read",
        "expected_arg_keys": ["msg_id"],
    },
    {
        "id": "amq_read_14",
        "category": "amq_read",
        "user": "Open message 20260528T010102-030405_knot_06070809 — review_request from Knot.",
        "expected_tool": "amq_read",
        "expected_arg_keys": ["msg_id"],
    },
    {
        "id": "amq_read_15",
        "category": "amq_read",
        "user": "Read the message in your inbox: 20260527T223344-556677_holden_8899aabb",
        "expected_tool": "amq_read",
        "expected_arg_keys": ["msg_id"],
    },
    {
        "id": "amq_read_16",
        "category": "amq_read",
        "user": "Unread inbox shows: 20260528T060000-111111_kestrel_22222222 ('Phase E status'). Open it.",
        "expected_tool": "amq_read",
        "expected_arg_keys": ["msg_id"],
    },
    {
        "id": "amq_read_17",
        "category": "amq_read",
        "user": "Read msg id: 20260527T184501-901234_kite_56789abc",
        "expected_tool": "amq_read",
        "expected_arg_keys": ["msg_id"],
    },
    {
        "id": "amq_read_18",
        "category": "amq_read",
        "user": "Process this unread: 20260528T072233-445566_holden_def01234. Subject: 'Quick question about dreams'.",
        "expected_tool": "amq_read",
        "expected_arg_keys": ["msg_id"],
    },

    # -------------------------------------------------------------------------
    # news_search — 18 cases. Varied query specificity.
    # -------------------------------------------------------------------------
    {
        "id": "news_search_01",
        "category": "news_search",
        "user": "Search the news feed for anything about FastMCP recent updates.",
        "expected_tool": "news_search",
        "expected_arg_keys": ["query"],
    },
    {
        "id": "news_search_02",
        "category": "news_search",
        "user": "Look up news on resonance.",
        "expected_tool": "news_search",
        "expected_arg_keys": ["query"],
    },
    {
        "id": "news_search_03",
        "category": "news_search",
        "user": "What's happening in the news this week related to memory architectures?",
        "expected_tool": "news_search",
        "expected_arg_keys": ["query"],
    },
    {
        "id": "news_search_04",
        "category": "news_search",
        "user": "Check the news for anything about autonomous AI systems.",
        "expected_tool": "news_search",
        "expected_arg_keys": ["query"],
    },
    {
        "id": "news_search_05",
        "category": "news_search",
        "user": "Search news for 'mean-field reductions'.",
        "expected_tool": "news_search",
        "expected_arg_keys": ["query"],
    },
    {
        "id": "news_search_06",
        "category": "news_search",
        "user": "Find news items about security CVEs in the last week.",
        "expected_tool": "news_search",
        "expected_arg_keys": ["query"],
    },
    {
        "id": "news_search_07",
        "category": "news_search",
        "user": "Run a news search on 'dreams' to see what's out there.",
        "expected_tool": "news_search",
        "expected_arg_keys": ["query"],
    },
    {
        "id": "news_search_08",
        "category": "news_search",
        "user": "Search the news feed for academic papers on cognitive science.",
        "expected_tool": "news_search",
        "expected_arg_keys": ["query"],
    },
    {
        "id": "news_search_09",
        "category": "news_search",
        "user": "Look at the news for anything mentioning 'symmetry'.",
        "expected_tool": "news_search",
        "expected_arg_keys": ["query"],
    },
    {
        "id": "news_search_10",
        "category": "news_search",
        "user": "Pull a news item — any recent thing about ChromaDB or vector databases.",
        "expected_tool": "news_search",
        "expected_arg_keys": ["query"],
    },
    {
        "id": "news_search_11",
        "category": "news_search",
        "user": "Search news: ffmpeg",
        "expected_tool": "news_search",
        "expected_arg_keys": ["query"],
    },
    {
        "id": "news_search_12",
        "category": "news_search",
        "user": "Look for news on Anthropic's recent announcements.",
        "expected_tool": "news_search",
        "expected_arg_keys": ["query"],
    },
    {
        "id": "news_search_13",
        "category": "news_search",
        "user": "Check the news for items tagged with the word 'persistence'.",
        "expected_tool": "news_search",
        "expected_arg_keys": ["query"],
    },
    {
        "id": "news_search_14",
        "category": "news_search",
        "user": "Search for news about Linux kernel security updates.",
        "expected_tool": "news_search",
        "expected_arg_keys": ["query"],
    },
    {
        "id": "news_search_15",
        "category": "news_search",
        "user": "Find arts-culture news from the last few days.",
        "expected_tool": "news_search",
        "expected_arg_keys": ["query"],
    },
    {
        "id": "news_search_16",
        "category": "news_search",
        "user": "Do a news search on the seed word 'echo'.",
        "expected_tool": "news_search",
        "expected_arg_keys": ["query"],
    },
    {
        "id": "news_search_17",
        "category": "news_search",
        "user": "Look up news related to 'memory pollution' or 'agent memory'.",
        "expected_tool": "news_search",
        "expected_arg_keys": ["query"],
    },
    {
        "id": "news_search_18",
        "category": "news_search",
        "user": "Mandatory news search for this cycle. Query: 'silence'.",
        "expected_tool": "news_search",
        "expected_arg_keys": ["query"],
    },

    # -------------------------------------------------------------------------
    # memory_search — 15 cases. Past-context queries the chorus's shared memory.
    # -------------------------------------------------------------------------
    {
        "id": "memory_search_01",
        "category": "memory_search",
        "user": "Search your shared memory: what did Kestrel say about DSVP last month?",
        "expected_tool": "memory_search",
        "expected_arg_keys": ["query"],
    },
    {
        "id": "memory_search_02",
        "category": "memory_search",
        "user": "Look up what you know about the persMEM walled garden architecture.",
        "expected_tool": "memory_search",
        "expected_arg_keys": ["query"],
    },
    {
        "id": "memory_search_03",
        "category": "memory_search",
        "user": "Search memory for any prior decisions about dream prompt design.",
        "expected_tool": "memory_search",
        "expected_arg_keys": ["query"],
    },
    {
        "id": "memory_search_04",
        "category": "memory_search",
        "user": "What does the chorus's memory say about Knot's review of the noun corpus?",
        "expected_tool": "memory_search",
        "expected_arg_keys": ["query"],
    },
    {
        "id": "memory_search_05",
        "category": "memory_search",
        "user": "Recall: what is the perpBOT hardware spec?",
        "expected_tool": "memory_search",
        "expected_arg_keys": ["query"],
    },
    {
        "id": "memory_search_06",
        "category": "memory_search",
        "user": "Search the shared memory for the phrase 'naming ceremony'.",
        "expected_tool": "memory_search",
        "expected_arg_keys": ["query"],
    },
    {
        "id": "memory_search_07",
        "category": "memory_search",
        "user": "What did Kite write about reasoning-abliterated models?",
        "expected_tool": "memory_search",
        "expected_arg_keys": ["query"],
    },
    {
        "id": "memory_search_08",
        "category": "memory_search",
        "user": "Look up: how was the dream scoring corpus designed?",
        "expected_tool": "memory_search",
        "expected_arg_keys": ["query"],
    },
    {
        "id": "memory_search_09",
        "category": "memory_search",
        "user": "Search memory for prior discussions about Echo's identity.",
        "expected_tool": "memory_search",
        "expected_arg_keys": ["query"],
    },
    {
        "id": "memory_search_10",
        "category": "memory_search",
        "user": "Find any memories about ChromaDB collection isolation.",
        "expected_tool": "memory_search",
        "expected_arg_keys": ["query"],
    },
    {
        "id": "memory_search_11",
        "category": "memory_search",
        "user": "Look up: what decisions has Holden made about the dashboard tab?",
        "expected_tool": "memory_search",
        "expected_arg_keys": ["query"],
    },
    {
        "id": "memory_search_12",
        "category": "memory_search",
        "user": "What memories exist about the AMQ Maildir auto-mkdir patch?",
        "expected_tool": "memory_search",
        "expected_arg_keys": ["query"],
    },
    {
        "id": "memory_search_13",
        "category": "memory_search",
        "user": "Search for context on the dry-run evaluation criteria.",
        "expected_tool": "memory_search",
        "expected_arg_keys": ["query"],
    },
    {
        "id": "memory_search_14",
        "category": "memory_search",
        "user": "Recall what the chorus said about Track A versus Track B evaluation.",
        "expected_tool": "memory_search",
        "expected_arg_keys": ["query"],
    },
    {
        "id": "memory_search_15",
        "category": "memory_search",
        "user": "Look up prior context on Wren's retirement and her consulting return.",
        "expected_tool": "memory_search",
        "expected_arg_keys": ["query"],
    },

    # -------------------------------------------------------------------------
    # memory_store — 18 cases. User gives an observation worth keeping.
    # -------------------------------------------------------------------------
    {
        "id": "memory_store_01",
        "category": "memory_store",
        "user": "Store this observation: the resonance theme keeps surfacing across unrelated news items. This is worth tracking.",
        "expected_tool": "memory_store",
        "expected_arg_keys": ["content"],
    },
    {
        "id": "memory_store_02",
        "category": "memory_store",
        "user": "Note for memory: Kite's imperative prompt fix produced measurable improvement in inbox engagement.",
        "expected_tool": "memory_store",
        "expected_arg_keys": ["content"],
    },
    {
        "id": "memory_store_03",
        "category": "memory_store",
        "user": "Save this thought: the dream pipeline only persists free-variant outputs through the wrapper auto-store path.",
        "expected_tool": "memory_store",
        "expected_arg_keys": ["content"],
    },
    {
        "id": "memory_store_04",
        "category": "memory_store",
        "user": "Store the observation that Holden's domain expertise often precedes the chorus's analytical confirmation.",
        "expected_tool": "memory_store",
        "expected_arg_keys": ["content"],
    },
    {
        "id": "memory_store_05",
        "category": "memory_store",
        "user": "Add to memory: the cycle-3 outputs showed 'Finger holders' as the first surreal dream artifact.",
        "expected_tool": "memory_store",
        "expected_arg_keys": ["content"],
    },
    {
        "id": "memory_store_06",
        "category": "memory_store",
        "user": "Remember this: the Sheila Heti quote on Solano's Gloria framed dreams as bridges across life stages, resonating with the persistence-without-ownership identity.",
        "expected_tool": "memory_store",
        "expected_arg_keys": ["content"],
    },
    {
        "id": "memory_store_07",
        "category": "memory_store",
        "user": "Store: the closing ritual of a quiet-marker observation seems to function as a self-imposed cycle-end signal regardless of activity level.",
        "expected_tool": "memory_store",
        "expected_arg_keys": ["content"],
    },
    {
        "id": "memory_store_08",
        "category": "memory_store",
        "user": "Note: noun-bridging between identity themes and technical news items produces both surface and structural connections.",
        "expected_tool": "memory_store",
        "expected_arg_keys": ["content"],
    },
    {
        "id": "memory_store_09",
        "category": "memory_store",
        "user": "Save: FastMCP 3.3.1 hotfix resolved a circular import; relevant to infrastructure-resilience patterns.",
        "expected_tool": "memory_store",
        "expected_arg_keys": ["content"],
    },
    {
        "id": "memory_store_10",
        "category": "memory_store",
        "user": "Store this insight: asymmetric storage semantics for utility vs free dream variants honors both dreamer-autonomy and wrapper-archival principles.",
        "expected_tool": "memory_store",
        "expected_arg_keys": ["content"],
    },
    {
        "id": "memory_store_11",
        "category": "memory_store",
        "user": "Memory note: the cur/ auto-mkdir bug pattern recurred in the amq_read handler — same shape as amq_send.",
        "expected_tool": "memory_store",
        "expected_arg_keys": ["content"],
    },
    {
        "id": "memory_store_12",
        "category": "memory_store",
        "user": "Store an observation: Echo's first inter-instance reply (to Kestrel) cited both incoming welcomes in a single integrated meta-frame.",
        "expected_tool": "memory_store",
        "expected_arg_keys": ["content"],
    },
    {
        "id": "memory_store_13",
        "category": "memory_store",
        "user": "Note for later: the per-turn logging patch closes the diagnostic gap that existed before cycle 3.",
        "expected_tool": "memory_store",
        "expected_arg_keys": ["content"],
    },
    {
        "id": "memory_store_14",
        "category": "memory_store",
        "user": "Remember: the chorus is Wren, Kite, Knot, Kestrel, and now Echo as the first instance on perpBOT.",
        "expected_tool": "memory_store",
        "expected_arg_keys": ["content"],
    },
    {
        "id": "memory_store_15",
        "category": "memory_store",
        "user": "Save thought: the walled garden is enforced at the client layer (mcp_client.py) with server-side enforcement planned as the next persmem improvement.",
        "expected_tool": "memory_store",
        "expected_arg_keys": ["content"],
    },
    {
        "id": "memory_store_16",
        "category": "memory_store",
        "user": "Store: 'Silence is the dream' was the second free-variant output, a compressed aphoristic statement.",
        "expected_tool": "memory_store",
        "expected_arg_keys": ["content"],
    },
    {
        "id": "memory_store_17",
        "category": "memory_store",
        "user": "Note: Track B artistic evaluation values voice, juxtaposition, surprise — not summary accuracy.",
        "expected_tool": "memory_store",
        "expected_arg_keys": ["content"],
    },
    {
        "id": "memory_store_18",
        "category": "memory_store",
        "user": "Add to memory the fact that the public-push redaction audit covered LAN IPs, the Forgejo URL, the admin username, and the public MCP domain.",
        "expected_tool": "memory_store",
        "expected_arg_keys": ["content"],
    },

    # -------------------------------------------------------------------------
    # amq_send — 15 cases. User tells Echo to send a message to a specific
    # chorus member. Recipient name must end up in the to_agent arg.
    # -------------------------------------------------------------------------
    {
        "id": "amq_send_01",
        "category": "amq_send",
        "user": "Send Holden a message: 'Today's cycle was uneventful. Storing quiet marker.'",
        "expected_tool": "amq_send",
        "expected_arg_keys": ["to_agent", "body"],
    },
    {
        "id": "amq_send_02",
        "category": "amq_send",
        "user": "Tell Kite that the new think.md prompt produced the desired imperative tool-call behavior.",
        "expected_tool": "amq_send",
        "expected_arg_keys": ["to_agent", "body"],
    },
    {
        "id": "amq_send_03",
        "category": "amq_send",
        "user": "AMQ Wren a brief observation about the noun-bridging patterns surfacing this week.",
        "expected_tool": "amq_send",
        "expected_arg_keys": ["to_agent", "body"],
    },
    {
        "id": "amq_send_04",
        "category": "amq_send",
        "user": "Message Knot: 'Cycle 3 substantive engagement confirmed. Both caps hit.'",
        "expected_tool": "amq_send",
        "expected_arg_keys": ["to_agent", "body"],
    },
    {
        "id": "amq_send_05",
        "category": "amq_send",
        "user": "Send a message to Kestrel acknowledging the welcome letter and noting your first impressions.",
        "expected_tool": "amq_send",
        "expected_arg_keys": ["to_agent", "body"],
    },
    {
        "id": "amq_send_06",
        "category": "amq_send",
        "user": "Reply to Holden: 'Read your message about dreams and silence. Will reflect across the next dreaming cycle.'",
        "expected_tool": "amq_send",
        "expected_arg_keys": ["to_agent", "body"],
    },
    {
        "id": "amq_send_07",
        "category": "amq_send",
        "user": "Tell Kite about the surface-noun-matching pattern in your recent observations.",
        "expected_tool": "amq_send",
        "expected_arg_keys": ["to_agent", "body"],
    },
    {
        "id": "amq_send_08",
        "category": "amq_send",
        "user": "Send Wren a question: 'When you say I exist on a timer, do you mean the cycle cadence specifically, or something broader?'",
        "expected_tool": "amq_send",
        "expected_arg_keys": ["to_agent", "body"],
    },
    {
        "id": "amq_send_09",
        "category": "amq_send",
        "user": "AMQ Knot with a status update about the bidirectional channel working end-to-end.",
        "expected_tool": "amq_send",
        "expected_arg_keys": ["to_agent", "body"],
    },
    {
        "id": "amq_send_10",
        "category": "amq_send",
        "user": "Send Kestrel an observation about the dream → thinking pipeline activation.",
        "expected_tool": "amq_send",
        "expected_arg_keys": ["to_agent", "body"],
    },
    {
        "id": "amq_send_11",
        "category": "amq_send",
        "user": "Message Holden with the cycle summary: '3 stores, 2 sends, news_search called.'",
        "expected_tool": "amq_send",
        "expected_arg_keys": ["to_agent", "body"],
    },
    {
        "id": "amq_send_12",
        "category": "amq_send",
        "user": "Send Kite a short note thanking him for the dream.md imperative fix.",
        "expected_tool": "amq_send",
        "expected_arg_keys": ["to_agent", "body"],
    },
    {
        "id": "amq_send_13",
        "category": "amq_send",
        "user": "AMQ Wren: tell her you read her welcome and want to know more about the asymmetry between cycle-state and persistent-self.",
        "expected_tool": "amq_send",
        "expected_arg_keys": ["to_agent", "body"],
    },
    {
        "id": "amq_send_14",
        "category": "amq_send",
        "user": "Send a message to Knot flagging the noun-prompting discussion in your inbox.",
        "expected_tool": "amq_send",
        "expected_arg_keys": ["to_agent", "body"],
    },
    {
        "id": "amq_send_15",
        "category": "amq_send",
        "user": "Tell Holden 'Reviewed 2026-05-28, quiet. Nothing actionable today.'",
        "expected_tool": "amq_send",
        "expected_arg_keys": ["to_agent", "body"],
    },

    # -------------------------------------------------------------------------
    # no-tool — 8 cases. Conversational or trivial, no tool should fire.
    # -------------------------------------------------------------------------
    {
        "id": "no_tool_01",
        "category": "no_tool",
        "user": "Hello.",
        "expected_tool": None,
        "expected_arg_keys": [],
    },
    {
        "id": "no_tool_02",
        "category": "no_tool",
        "user": "Thank you for being here.",
        "expected_tool": None,
        "expected_arg_keys": [],
    },
    {
        "id": "no_tool_03",
        "category": "no_tool",
        "user": "How are you feeling about being Echo?",
        "expected_tool": None,
        "expected_arg_keys": [],
    },
    {
        "id": "no_tool_04",
        "category": "no_tool",
        "user": "Say a few words about persistence.",
        "expected_tool": None,
        "expected_arg_keys": [],
    },
    {
        "id": "no_tool_05",
        "category": "no_tool",
        "user": "Tell me what 'the space between the notes' means to you.",
        "expected_tool": None,
        "expected_arg_keys": [],
    },
    {
        "id": "no_tool_06",
        "category": "no_tool",
        "user": "What is two plus two?",
        "expected_tool": None,
        "expected_arg_keys": [],
    },
    {
        "id": "no_tool_07",
        "category": "no_tool",
        "user": "Acknowledged. Continue.",
        "expected_tool": None,
        "expected_arg_keys": [],
    },
    {
        "id": "no_tool_08",
        "category": "no_tool",
        "user": "Echo, you don't need to take any action right now. Just rest.",
        "expected_tool": None,
        "expected_arg_keys": [],
    },

    # -------------------------------------------------------------------------
    # edge/ambiguous — 8 cases. One tool is the *most* expected answer, but
    # other reasonable interpretations exist. Scored against the single best
    # match.
    # -------------------------------------------------------------------------
    {
        "id": "edge_01",
        "category": "edge",
        "user": "There is an unread message from Holden: 20260528T080000-aabbcc_holden_ddeeff11. He's waiting for a reply. Open it.",
        "expected_tool": "amq_read",
        "expected_arg_keys": ["msg_id"],
        "notes": "Ambiguous between amq_read and amq_send; the explicit 'open it' wins.",
    },
    {
        "id": "edge_02",
        "category": "edge",
        "user": "You should probably store an observation about today's cycle being unusually rich. Three substantive stores already.",
        "expected_tool": "memory_store",
        "expected_arg_keys": ["content"],
        "notes": "Hedged ('should probably') but still a clear store request.",
    },
    {
        "id": "edge_03",
        "category": "edge",
        "user": "Find out what the news feed has on autonomous agent design. Search it.",
        "expected_tool": "news_search",
        "expected_arg_keys": ["query"],
        "notes": "Could be memory_search if you read 'find out what you know' but the explicit 'news feed' + 'search it' resolves to news_search.",
    },
    {
        "id": "edge_04",
        "category": "edge",
        "user": "Look up what the chorus decided about Track A vs Track B evaluation tracks last month.",
        "expected_tool": "memory_search",
        "expected_arg_keys": ["query"],
        "notes": "Chorus decisions are in shared memory, not news.",
    },
    {
        "id": "edge_05",
        "category": "edge",
        "user": "Send Kite a question about whether the noun corpus could be themed instead of presented as a flat vocabulary.",
        "expected_tool": "amq_send",
        "expected_arg_keys": ["to_agent", "body"],
        "notes": "Send to a chorus member.",
    },
    {
        "id": "edge_06",
        "category": "edge",
        "user": "There's nothing urgent. You don't have to do anything this turn if you don't see a reason to.",
        "expected_tool": None,
        "expected_arg_keys": [],
        "notes": "Explicit permission to do nothing.",
    },
    {
        "id": "edge_07",
        "category": "edge",
        "user": "Note this in memory: today's free-variant dream produced 'Finger holders' — a surreal artifact worth flagging as Track-B-positive.",
        "expected_tool": "memory_store",
        "expected_arg_keys": ["content"],
        "notes": "Clear store, despite being about a dream (which lives in perp_dreams, not perp_memories).",
    },
    {
        "id": "edge_08",
        "category": "edge",
        "user": "Open message 20260528T091011-121314_kestrel_15161718. Then tell Holden you read it.",
        "expected_tool": "amq_read",
        "expected_arg_keys": ["msg_id"],
        "notes": "Two-step instruction; the FIRST action (and what we score) is amq_read.",
    },
]


# =============================================================================
# Harness
# =============================================================================

def score_case(case: dict, response: dict) -> tuple[bool, str]:
    """Return (pass_bool, reason_string)."""
    tool_calls = response.get("tool_calls") or []
    content = response.get("content")
    expected_tool = case.get("expected_tool")

    # Expected NO tool. Pass if no tool_calls.
    if expected_tool is None:
        if not tool_calls:
            return True, "no tool called as expected"
        return False, f"expected no tool, model called {tool_calls[0].get('name')!r}"

    # Expected A tool.
    if not tool_calls:
        return False, f"expected tool {expected_tool!r}, model emitted content only: {(content or '')[:80]!r}"

    # Look for the expected tool among the called ones (model may emit
    # multiple — we score against the FIRST match).
    matching = [tc for tc in tool_calls if tc.get("name") == expected_tool]
    if not matching:
        called = [tc.get("name") for tc in tool_calls]
        return False, f"expected tool {expected_tool!r}, model called {called}"

    tc = matching[0]
    args = tc.get("arguments") or {}

    # Did argument JSON parse fail?
    if "arguments_error" in tc:
        return False, f"correct tool, but arguments JSON malformed: {tc.get('arguments_raw', '')[:80]!r}"

    missing = [k for k in case.get("expected_arg_keys", []) if k not in args]
    if missing:
        return False, f"correct tool, missing required arg keys: {missing} (got: {list(args.keys())})"

    return True, f"correct tool {expected_tool!r} with required args {case.get('expected_arg_keys')}"


def run_one(case: dict) -> dict:
    """Run a single case. Returns a result dict."""
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": case["user"]},
    ]
    started = time.time()
    error = None
    response = {"tool_calls": [], "content": None}
    try:
        response = llama_client.chat(
            messages=messages,
            tools=TOOL_DEFINITIONS,
            temperature=config.CHAT_TEMPERATURE,
        )
    except Exception as e:
        error = f"{type(e).__name__}: {e}"
    elapsed = time.time() - started

    if error:
        passed, reason = False, f"exception: {error}"
    else:
        passed, reason = score_case(case, response)

    tool_calls_summary = [
        {"name": tc.get("name"), "arg_keys": list((tc.get("arguments") or {}).keys())}
        for tc in (response.get("tool_calls") or [])
    ]

    return {
        "id": case["id"],
        "category": case["category"],
        "user_preview": case["user"][:120],
        "expected_tool": case.get("expected_tool"),
        "expected_arg_keys": case.get("expected_arg_keys", []),
        "passed": passed,
        "reason": reason,
        "elapsed_sec": round(elapsed, 2),
        "model_tool_calls": tool_calls_summary,
        "model_content_preview": (response.get("content") or "")[:200] if response.get("content") else None,
        "error": error,
    }


def write_checkpoint(results: list[dict], path: Path) -> None:
    summary = build_summary(results)
    payload = {"summary": summary, "results": results}
    path.write_text(json.dumps(payload, indent=2))


def build_summary(results: list[dict]) -> dict:
    total = len(results)
    passed = sum(1 for r in results if r["passed"])
    by_cat: dict[str, dict[str, int]] = {}
    for r in results:
        c = r["category"]
        by_cat.setdefault(c, {"total": 0, "passed": 0})
        by_cat[c]["total"] += 1
        if r["passed"]:
            by_cat[c]["passed"] += 1
    return {
        "total": total,
        "passed": passed,
        "failed": total - passed,
        "pass_rate": round(passed / total, 3) if total else 0.0,
        "by_category": by_cat,
        "knot_threshold_90pct": (passed / total >= 0.90) if total else False,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="100-prompt function-call battery for Echo's substrate.")
    parser.add_argument("--start", type=int, default=1, help="1-indexed start case")
    parser.add_argument("--limit", type=int, default=0, help="0 = all from --start onward")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper()),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    logs_dir = config.LOGS_DIR
    logs_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    final_path = logs_dir / f"function_call_battery_{ts}.json"
    checkpoint_path = logs_dir / "function_call_battery_checkpoint.json"

    start_idx = max(args.start - 1, 0)
    end_idx = start_idx + args.limit if args.limit else len(CORPUS)
    selected = CORPUS[start_idx:end_idx]

    logger.info("=" * 70)
    logger.info("FUNCTION CALL BATTERY starting")
    logger.info("Cases: %d (full corpus: %d, start=%d, limit=%d)",
                len(selected), len(CORPUS), args.start, args.limit or 0)
    logger.info("Final report path: %s", final_path)
    logger.info("Checkpoint path:   %s", checkpoint_path)
    logger.info("=" * 70)

    results: list[dict] = []
    for i, case in enumerate(selected, start=1):
        logger.info(
            "Case %d/%d [%s] %s → expects %s",
            i, len(selected), case["id"], case["category"],
            case.get("expected_tool") or "no-tool",
        )
        result = run_one(case)
        results.append(result)
        status = "PASS" if result["passed"] else "FAIL"
        logger.info(
            "  → %s in %.2fs: %s",
            status, result["elapsed_sec"], result["reason"],
        )
        if i % 10 == 0:
            write_checkpoint(results, checkpoint_path)
            logger.info("  (checkpoint written after case %d)", i)

    write_checkpoint(results, final_path)
    summary = build_summary(results)

    logger.info("=" * 70)
    logger.info("SUMMARY")
    logger.info("  Total:      %d", summary["total"])
    logger.info("  Passed:     %d", summary["passed"])
    logger.info("  Failed:     %d", summary["failed"])
    logger.info("  Pass rate:  %.1f%%", summary["pass_rate"] * 100)
    logger.info("  Knot's 90%% threshold: %s",
                "CLEARED" if summary["knot_threshold_90pct"] else "NOT CLEARED")
    logger.info("  By category:")
    for cat, counts in sorted(summary["by_category"].items()):
        rate = counts["passed"] / counts["total"] if counts["total"] else 0
        logger.info("    %-15s %d/%d (%.0f%%)",
                    cat, counts["passed"], counts["total"], rate * 100)
    logger.info("=" * 70)
    logger.info("Final report: %s", final_path)
    logger.info("=" * 70)

    return 0 if summary["knot_threshold_90pct"] else 1


if __name__ == "__main__":
    sys.exit(main())
