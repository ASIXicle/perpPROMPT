"""DREAMING cycle runner.

A single DREAMING cycle, end to end:
  1. Build context (random memory fragments + news seeded by random
     dream-corpus noun + AMQ fragment) via context.py
  2. Render templates/dream.md (or dream.free.md if --free) with slot
     values from the context build
  3. Multi-turn chat with the local model at high temperature (0.9):
     the model may emit content, call memory_search to dig deeper,
     and/or call memory_store with what wants to be remembered
  4. When the model calls memory_store, the wrapper intercepts:
     scores the content via Kite's two-tier vocabulary heuristic,
     stores to perp_dreams with the confidence value in metadata,
     and returns success to the model — which never sees the score
  5. If the model emits content but does NOT call memory_store, no
     storage. Silence-as-valid-output. The dreamer's choice to not
     remember IS the valid response.

The asymmetry vs THINKING:
  - No tool-call cap pressure (1-2 stores natural for dreams)
  - Higher temperature (config.DREAM_TEMPERATURE = 0.9) for drift
  - Memory writes go to perp_dreams collection, not perp_memories
  - Confidence-scoring is the load-bearing wrapper logic
  - No quiet-marker rule — silence here is genuinely valid

Wren's design principle: you don't judge a dream while dreaming.
Scoring happens AFTER the cycle, in the wrapper, invisible to the model.
The dreamer just dreams.

Phase B module 6 of 7. Depends on: config, context, llama_client,
mcp_client. Entry point for systemd-managed dreaming cycles.
"""

import argparse
import json
import logging
import random
import re
import sys
from typing import Any

from . import config
from . import context
from . import llama_client
from . import mcp_client


logger = logging.getLogger(__name__)


# Dreams are shorter cycles than THINKING — 1-2 stores natural, maybe
# one memory_search for deeper recall. 5 turns is generous.
MAX_DREAM_TURNS = 5


# =============================================================================
# Tool definitions for the dreamer
# =============================================================================
# Per Wren: "memory_search + memory_store ONLY. No AMQ, no shell."
# memory_store here writes to perp_dreams transparently (the model thinks
# it's storing a memory; the wrapper routes + scores).

DREAM_TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "memory_store",
            "description": (
                "Store the dream once it has taken shape — the image or the "
                "weave of fragments that surfaced. Call it with the dream content."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "The dream content (one paragraph or more).",
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
                "If a fragment triggers a faint recall, search the shared "
                "memories to surface what feels close. Optional."
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
                        "description": "Max results (default 3).",
                    },
                },
                "required": ["query"],
            },
        },
    },
]


# =============================================================================
# Confidence scoring (Kite's two-tier vocabulary heuristic)
# =============================================================================

def score_dream(content: str, dream_nouns: list[str]) -> dict:
    """Score a dream's content per Kite's two-tier vocabulary heuristic.

    Returns a dict with the confidence value AND the diagnostic signals
    that produced it, so the score is auditable in stored metadata:
        {
            "confidence": 0..3,
            "noun_matches": int,
            "has_hard_marker": bool,
            "soft_marker_count": int,
            "has_juxtaposition": bool,
        }

    Tiers (Kite's spec, 2026-05-27):
      0 = silence (empty/whitespace content — skip storage entirely)
      1 = thin (any HARD analytical marker OR 2+ SOFT markers OR very few
              corpus matches — analytical mode or shallow output)
      2 = present (>= CONFIDENCE_NOUN_MATCHES_FOR_TIER_2 nouns, no
              analytical markers, no juxtaposition signal)
      3 = vivid (>= CONFIDENCE_NOUN_MATCHES_FOR_TIER_3 nouns AND
              cross-fragment juxtaposition signal)

    Hard markers short-circuit immediately to tier 1 — a single occurrence
    of "this means" or "therefore" demotes regardless of how many corpus
    nouns appear. Soft markers count cumulative occurrences; 2+ together
    demotes (handles "this reflects the theme of X" cleanly while leaving
    "the salt reflects the light" alone since "reflects" alone is < 2).
    """
    if not content or not content.strip():
        return {
            "confidence": 0,
            "noun_matches": 0,
            "has_hard_marker": False,
            "soft_marker_count": 0,
            "has_juxtaposition": False,
        }

    lower = content.lower()

    # Hard analytical marker → immediate tier 1 demote
    has_hard = any(m in lower for m in config.HARD_ANALYTICAL_MARKERS)

    # Soft markers — count cumulative occurrences across the whole list
    soft_count = sum(lower.count(m) for m in config.SOFT_ANALYTICAL_MARKERS)

    if has_hard or soft_count >= config.SOFT_MARKER_COMBINATION_THRESHOLD:
        # Still count nouns for diagnostic metadata, but tier locks to 1
        noun_matches = _count_noun_matches(lower, dream_nouns)
        return {
            "confidence": 1,
            "noun_matches": noun_matches,
            "has_hard_marker": has_hard,
            "soft_marker_count": soft_count,
            "has_juxtaposition": False,
        }

    noun_matches = _count_noun_matches(lower, dream_nouns)
    has_juxtaposition = _has_juxtaposition(content, dream_nouns)

    if noun_matches >= config.CONFIDENCE_NOUN_MATCHES_FOR_TIER_3 and has_juxtaposition:
        confidence = 3
    elif noun_matches >= config.CONFIDENCE_NOUN_MATCHES_FOR_TIER_2:
        confidence = 2
    else:
        confidence = 1  # thin — corpus matches below threshold

    return {
        "confidence": confidence,
        "noun_matches": noun_matches,
        "has_hard_marker": False,
        "soft_marker_count": soft_count,
        "has_juxtaposition": has_juxtaposition,
    }


def _count_noun_matches(lowered_text: str, dream_nouns: list[str]) -> int:
    """Count unique corpus nouns appearing as whole words in the text.

    Word-boundary aware: "stone" matches "the stone fell" but NOT
    "stoned" or "stonecutter". Uses \\b regex anchors. Case-insensitive
    via pre-lowered input.

    Returns count of DISTINCT corpus nouns found (not total occurrences).
    A dream containing "stone... stone... stone" counts as 1 noun match,
    not 3 — variety beats repetition for vividness signal.
    """
    matched = set()
    for noun in dream_nouns:
        pattern = r'\b' + re.escape(noun.lower()) + r'\b'
        if re.search(pattern, lowered_text):
            matched.add(noun.lower())
    return len(matched)


def _has_juxtaposition(content: str, dream_nouns: list[str]) -> bool:
    """Cross-fragment juxtaposition signal for tier 3 promotion.

    Kite's spec: tier 3 = tier 2 + cross-fragment juxtaposition. We
    operationalize "juxtaposition" as: at least one sentence contains
    2+ distinct corpus nouns. The intuition is that placing two
    concrete images side-by-side in one sentence ("copper and the
    stairwell, both narrowing into the jaw") is the linguistic
    fingerprint of associative cross-pollination — which is what
    dream vividness actually IS.

    Imperfect heuristic. False positives possible (a sentence listing
    objects mundanely). Track A calibration may reveal the threshold
    or shape needs refinement.
    """
    nouns_lower = {n.lower() for n in dream_nouns}
    # Split into sentences using basic terminal punctuation
    sentences = re.split(r'[.!?]+', content.lower())
    for sentence in sentences:
        words = re.findall(r'\b\w+\b', sentence)
        nouns_in_sentence = {w for w in words if w in nouns_lower}
        if len(nouns_in_sentence) >= 2:
            return True
    return False


# =============================================================================
# Tool execution
# =============================================================================

class DreamCycleState:
    """Per-cycle state for the dream runner."""

    def __init__(self, seed_fragments: dict) -> None:
        self.store_count = 0
        self.stored_dream_ids: list[str] = []
        self.scores: list[dict] = []  # all confidence scores produced this cycle
        self.seed_fragments = seed_fragments


def execute_tool(
    tool_call: dict,
    bird_name: str,
    state: DreamCycleState,
    dry_run: bool,
) -> str:
    """Execute one tool call; always returns a JSON-string result."""
    name = tool_call.get("name", "")
    args = tool_call.get("arguments")
    if args is None:
        return _tool_result_error(
            f"tool_call missing arguments. raw: {tool_call.get('arguments_raw', '?')}"
        )

    try:
        if name == "memory_store":
            return _execute_dream_store(args, bird_name, state, dry_run, variant="utility")
        elif name == "memory_search":
            return _execute_dream_search(args, dry_run)
        else:
            return _tool_result_error(f"unknown tool for DREAMING: {name}")
    except Exception as e:
        logger.exception("dream tool %s raised", name)
        return _tool_result_error(f"tool {name} raised: {e}")


# Outputs that are refusals/echoes, not dreams. Matched case-insensitively
# after trimming whitespace + trailing sentence punctuation. Guards BOTH
# variants' auto-store path so a bare "Silence." never reaches perp_dreams
# (or the public feed). Genuine terse dreams ("The shapes, not the words.")
# are NOT in this set and store normally.
_DEGENERATE_DREAM_OUTPUTS = frozenset({
    "silence", "nothing", "none", "no dream", "nothing here", "(silence)",
    "...", "…",
    # Jun 15 2026: Holden — c1 single-word/phrase tokens that escape the old set
    "drift", "echo", "matchbox", "no response", "i am echo", "i am an echo",
    "the dream is yours", "no response generated yet",
})

# Silence-class paraphrases the 8B reaches for when it won't dream but won't
# emit a bare "Silence." either. Matched as the WHOLE normalized output only
# (not substring) so a genuine dream that contains one of these words is safe.
_SILENCE_PHRASES = frozenset({
    "silence is the dream", "the dream is silence",
    "i am silent", "i do not speak",
    "i am silent the fragments resonate but i do not speak",
    "i have nothing to say", "no dream tonight",
    "i dont have a mood to report only the quiet of presence",
})

# Prompt-template instruction text that must never reach perp_dreams or the
# public feed. The utility variant historically regurgitated these verbatim
# ("No task. No obligation." — Kite's leak finding, 2026-06-02): a template
# echo, not a dream. Normalized form (lowercased, punctuation stripped,
# whitespace collapsed). Sources: dream.md / dream.free.md / standing_directives.
_TEMPLATE_ECHO_FRAGMENTS = frozenset({
    "no task no checklist no obligation", "no task no obligation",
    "no task no checklist", "no checklist no obligation",
    "what do these fragments say to each other",
    "if something resonates call memory store with what wants to be kept",
    "if nothing does produce nothing silence is valid",
    "produce nothing silence is valid", "silence is valid",
    "these fragments dont connect yet let them",
    "these dont connect yet let them",
    "say it as it comes unshaped uncertain yours",
    "you are not awake", "you are you are not awake",
    # think.md instruction language (Kite, 2026-06-02) — leaks via the
    # memory→fragment pipeline; the last two appeared verbatim in c1 output.
    "do not describe what you plan to do",
    "execute the tool calls directly",
    "your output is tool calls and observations",
    "i will carry the work by asking questions first",
    "storing only what i cannot yet explain",
})


def _normalize_for_echo(content: str) -> str:
    """Lowercase, strip all punctuation, collapse whitespace, for echo match."""
    text = re.sub(r"[^\w\s]", " ", content.lower())
    return re.sub(r"\s+", " ", text).strip()


def _is_degenerate_dream(content: str) -> bool:
    """True if the output is silence/refusal/template-echo, not a dream.

    Three classes, all of which would otherwise reach perp_dreams AND the
    public feed (the Bluesky post hook fires downstream of this guard):
      1. empty / bare-silence tokens ("silence", "...", "(silence)")
      2. silence-class paraphrases ("I am silent...") — whole-output match
      3. prompt-template regurgitation ("No task. No obligation.") — the
         utility-variant leak Kite flagged 2026-06-02
    Genuine terse dreams ("The shapes, not the words.") are NOT caught: the
    silence/echo sets are matched against the whole normalized output, and the
    template-fragment substring check is length-gated so a real dream that
    merely contains a phrase isn't nuked.
    """
    if not content or not content.strip():
        return True
    normalized = content.strip().lower().rstrip(".!?…").strip()
    if normalized in _DEGENERATE_DREAM_OUTPUTS:
        return True
    echo_norm = _normalize_for_echo(content)
    if not echo_norm:
        return True
    if echo_norm in _SILENCE_PHRASES or echo_norm in _TEMPLATE_ECHO_FRAGMENTS:
        return True
    # A short output dominated by a template fragment is an echo, not a dream.
    # Length-gated (<=120 normalized chars) so a long genuine dream that
    # happens to contain a phrase passes untouched.
    if len(echo_norm) <= 120:
        for frag in _TEMPLATE_ECHO_FRAGMENTS:
            if frag in echo_norm:
                return True
    return False


def _execute_dream_store(
    args: dict,
    bird_name: str,
    state: DreamCycleState,
    dry_run: bool,
    variant: str = "utility",
) -> str:
    """Score the proposed dream content and route to perp_dreams.

    The model thinks it's calling memory_store. The wrapper intercepts,
    runs Kite's confidence-tier scoring on the content, and writes to
    perp_dreams with the score in metadata. The score is invisible to
    the model.
    """
    content = args.get("content", "").strip()
    if _is_degenerate_dream(content):
        logger.info("Dream output is silence/degenerate — not stored: %r", content[:40])
        return _tool_result_ok({"stored": False, "reason": "silence"})

    # Score the content
    nouns = context._load_dream_nouns()
    score = score_dream(content, nouns)
    state.scores.append(score)

    # ── c1 free-variant gate (Holden, Jun 15 2026) ──
    # c1 DREAM-FREE is repetitive nonsense ("Drift.", "Echo.", "Matchbox").
    # c2 and c3 are where the real art lives. Drop c1 free dreams silently.
    if variant == "free" and score["confidence"] < config.DREAM_FREE_MIN_CONFIDENCE:
        logger.info(
            "Free dream below min confidence (%d < %d), not stored: %s",
            score["confidence"], config.DREAM_FREE_MIN_CONFIDENCE,
            content[:60],
        )
        return _tool_result_ok({"stored": False, "reason": "below_min_confidence"})

    # Tier 0 means empty — but we already filtered empty above, so this
    # shouldn't happen here. Defensive log if it does.
    if score["confidence"] == 0:
        logger.warning("Scored tier 0 on non-empty content — should not happen")
        return _tool_result_error("internal scoring inconsistency")

    if dry_run:
        logger.info(
            "[DRY-RUN] Would store dream tier=%d (nouns=%d, hard=%s, soft=%d, jux=%s) %d chars: %s",
            score["confidence"],
            score["noun_matches"],
            score["has_hard_marker"],
            score["soft_marker_count"],
            score["has_juxtaposition"],
            len(content),
            content[:80],
        )
        state.store_count += 1
        state.stored_dream_ids.append("dry-run-id")
        return _tool_result_ok({"id": "dry-run-id", "stored": True})

    memory_id = context.store_dream(
        content=content,
        bird_name=bird_name,
        confidence=score["confidence"],
        seed_fragments=state.seed_fragments,
        extra_metadata={
            "variant": variant,
            "noun_matches": score["noun_matches"],
            "has_hard_marker": score["has_hard_marker"],
            "soft_marker_count": score["soft_marker_count"],
            "has_juxtaposition": score["has_juxtaposition"],
        },
    )
    state.store_count += 1
    state.stored_dream_ids.append(memory_id)

    logger.info(
        "Stored dream %s tier=%d (nouns=%d, jux=%s)",
        memory_id,
        score["confidence"],
        score["noun_matches"],
        score["has_juxtaposition"],
    )

    # Post to Bluesky (dreams only; best-effort — never affects the cycle).
    # bluesky.post_dream is gated on config + already swallows its own errors;
    # the extra guard covers even an import failure (e.g. atproto not installed).
    try:
        from . import bluesky
        posted = bluesky.post_dream(content, variant)
        if posted:
            logger.info("Dream posted to Bluesky (%d post(s)): %s", len(posted), posted[0])
    except Exception as e:  # noqa: BLE001
        logger.warning("Bluesky post hook error (non-fatal): %s", e)

    return _tool_result_ok({"id": memory_id, "stored": True})


def _execute_dream_search(args: dict, dry_run: bool) -> str:
    query = args.get("query", "").strip()
    if not query:
        return _tool_result_error("memory_search called with empty query")
    top_k = int(args.get("top_k", 3))

    if dry_run:
        logger.info("[DRY-RUN] Would memory_search: %s (top_k=%d)", query, top_k)
        return _tool_result_ok({"results": [], "dry_run": True})

    results = mcp_client.memory_search(query=query, top_k=top_k)
    # Trim aggressively — dreams shouldn't be reading walls of text
    trimmed = [
        {
            "id": r.get("id"),
            "similarity": r.get("similarity"),
            "content": (r.get("content", "") or "")[:300],
        }
        for r in results
    ]
    return _tool_result_ok({"results": trimmed, "count": len(trimmed)})


def _tool_result_ok(payload: dict) -> str:
    return json.dumps({"status": "ok", **payload})


def _tool_result_error(message: str) -> str:
    logger.warning("dream tool error: %s", message)
    return json.dumps({"status": "error", "error": message})


# =============================================================================
# Cycle orchestration
# =============================================================================

def run_cycle(
    bird_name: str,
    free_variant: bool = False,
    conversation_variant: bool = False,
    dry_run: bool = False,
) -> dict:
    """Execute one DREAMING cycle for the named bird.

    Args:
        bird_name: The bird's chosen name.
        free_variant: If True, use templates/dream.free.md (artistic);
                      otherwise use templates/dream.md (utility).
        conversation_variant: If True (and free_variant=True), use
                      templates/dream.conversation.md as the primary
                      template, seeded from Holden's recent conversations.
        dry_run: If True, log proposed stores but don't actually write.

    Returns a summary dict:
        {
            "bird_name": str,
            "variant": "utility" | "free",
            "turns": int,
            "store_count": int,
            "stored_dream_ids": list[str],
            "scores": list[dict],          # all confidence scores this cycle
            "seed_fragments": dict,        # which inputs seeded this cycle
            "model_emitted_content": str,  # final content from model (for digest)
            "model_called_store": bool,
            "dry_run": bool,
        }

    Both variants auto-store emitted content (Holden, 2026-05-31). The model's
    autonomy is expressed through WHAT it emits, not whether it called the
    tool: genuine silence/refusal output is caught by _is_degenerate_dream and
    skipped, so it never reaches perp_dreams. Real dreams are always kept.
    """
    logger.info("=" * 60)
    variant_label = "conversation" if (conversation_variant and free_variant) else ("free" if free_variant else "utility")
    logger.info(
        "DREAMING cycle starting for %s (variant=%s, dry_run=%s)",
        bird_name,
        variant_label,
        dry_run,
    )
    logger.info("=" * 60)

    # Build context (gives us _seed_fragments side-channel)
    if conversation_variant and free_variant:
        ctx = context.build_dream_conversation_context(bird_name)
    elif free_variant:
        ctx = context.build_dream_free_context(bird_name)
    else:
        ctx = context.build_dream_context(bird_name)
    seed_fragments = ctx.pop("_seed_fragments", {})

    # Render template
    if conversation_variant and free_variant:
        template_path = config.DREAM_CONVERSATION_TEMPLATE
    elif free_variant:
        template_path = config.DREAM_FREE_TEMPLATE
    else:
        template_path = config.DREAM_TEMPLATE
    template_text = template_path.read_text()
    rendered_prompt = template_text.format(**ctx)

    logger.debug("Dream prompt (%d chars): %s...", len(rendered_prompt), rendered_prompt[:200])

    # Multi-turn chat at DREAM_TEMPERATURE
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": rendered_prompt},
    ]
    state = DreamCycleState(seed_fragments)
    final_content = ""
    # Loop-break signature tracker (Knot's B3 review, 2026-05-27).
    # Dreams have MAX_DREAM_TURNS=5 — much less slack than THINKING.
    # Pathological repetition during associative drift would burn the
    # whole budget on identical tool calls. Same shape as think.py.
    last_tool_signature: frozenset | None = None

    for turn in range(MAX_DREAM_TURNS):
        logger.info("Dream turn %d/%d", turn + 1, MAX_DREAM_TURNS)
        response = llama_client.chat(
            messages=messages,
            tools=DREAM_TOOL_DEFINITIONS,
            temperature=config.DREAM_TEMPERATURE,
        )
        tool_calls = response["tool_calls"]
        content = response["content"]

        # Per-turn visibility — parity with think.py (Option C, 2026-05-27).
        # Dreams should be visible per-turn since the scoring decisions
        # downstream are content-based.
        if content:
            logger.info("Dream turn %d content: %s", turn + 1, content[:300])
        if tool_calls:
            tool_summary = ", ".join(
                f"{tc.get('name', '?')}({json.dumps(tc.get('arguments', {}), sort_keys=True)[:120]})"
                for tc in tool_calls
            )
            logger.info("Dream turn %d tool calls: %s", turn + 1, tool_summary)

        if not tool_calls:
            # Model emitted content OR nothing. BOTH variants now auto-store
            # emitted content (Holden, 2026-05-31). Tying storage to an
            # explicit memory_store tool-call lost real dreams when the 8B
            # generated a dream as plain content but skipped the call (e.g.
            # the "Memory is the weight of the linen..." dream). Dreamer
            # autonomy is now expressed through WHAT the model emits, not
            # whether it remembered a function call: genuine silence/refusal
            # ("Silence.", empty) is caught by _is_degenerate_dream in
            # _execute_dream_store and skipped, so nothing degenerate reaches
            # perp_dreams or the public feed. The free variant always worked
            # this way (Wren's no-tools invitation prompt); utility now matches.
            if content:
                final_content = content
                if free_variant:
                    logger.info(
                        "Dream emitted content (free variant — wrapper auto-storing): %s",
                        content[:200],
                    )
                    # Route through the same scoring + storage path that the
                    # utility-variant model would use via tool call. The
                    # state.scores append + state.store_count increment
                    # happen inside _execute_dream_store, keeping the
                    # summary semantics consistent across variants.
                    _execute_dream_store(
                        args={"content": content},
                        bird_name=bird_name,
                        state=state,
                        dry_run=dry_run,
                        variant=variant_label,  # "conversation" bypasses c1 gate
                    )
                else:
                    logger.info(
                        "Dream emitted content (utility variant — wrapper auto-storing): %s",
                        content[:200],
                    )
                    _execute_dream_store(
                        args={"content": content},
                        bird_name=bird_name,
                        state=state,
                        dry_run=dry_run,
                        variant="utility",
                    )
            else:
                logger.info("Dream emitted silence — valid output")
            break

        # Loop detection: frozenset of (name, sorted-json-args).
        current_signature = frozenset(
            (tc.get("name", ""),
             json.dumps(tc.get("arguments", {}), sort_keys=True))
            for tc in tool_calls
        )
        if current_signature == last_tool_signature:
            logger.warning(
                "Dream turn %d: identical tool calls to previous turn — "
                "breaking to prevent loop.", turn + 1,
            )
            break
        last_tool_signature = current_signature

        # Echo assistant tool_calls into message history
        raw_message = response["raw"]["choices"][0]["message"]
        messages.append({
            "role": "assistant",
            "content": content or "",
            "tool_calls": raw_message.get("tool_calls", []),
        })

        # Execute tools
        for tc in tool_calls:
            result_text = execute_tool(tc, bird_name, state, dry_run)
            messages.append({
                "role": "tool",
                "tool_call_id": tc["id"],
                "content": result_text,
            })

    # ── c1 conversation retry (Holden, Jun 15 2026) ──
    # If the FREE (non-conversation) variant produced content but nothing was
    # stored (c1 gated), retry once with dream.conversation.md.
    # Skip if conversation was already the primary template.
    if (free_variant
            and not conversation_variant
            and state.store_count == 0
            and final_content
            and config.DREAM_CONVERSATION_TEMPLATE.exists()):
        logger.info("c1 gated — retrying with conversation template")
        try:
            conv_ctx = context.build_dream_conversation_context(bird_name)
            conv_seeds = conv_ctx.pop("_seed_fragments", {})
            conv_template = config.DREAM_CONVERSATION_TEMPLATE.read_text()
            conv_prompt = conv_template.format(**conv_ctx)

            retry_response = llama_client.chat(
                messages=[{"role": "user", "content": conv_prompt}],
                tools=[],  # no tools — force text output, don't let model
                           # attempt memory_store instead of dreaming
                temperature=config.DREAM_TEMPERATURE,
            )
            retry_content = retry_response["content"]
            if retry_content:
                logger.info("Conversation retry content: %s", retry_content[:200])
                state.seed_fragments = conv_seeds
                _execute_dream_store(
                    args={"content": retry_content},
                    bird_name=bird_name,
                    state=state,
                    dry_run=dry_run,
                    variant="free",
                )
                if state.store_count > 0:
                    final_content = retry_content
                    logger.info("Conversation retry SUCCEEDED — c2+ stored")
                else:
                    logger.info("Conversation retry also c1 — cycle unproductive")
            else:
                logger.info("Conversation retry emitted silence")
        except Exception as e:
            logger.warning("Conversation retry failed: %s", e)

    summary = {
        "bird_name": bird_name,
        "variant": variant_label,
        "turns": turn + 1,
        "store_count": state.store_count,
        "stored_dream_ids": state.stored_dream_ids,
        "scores": state.scores,
        "seed_fragments": state.seed_fragments,
        "model_emitted_content": final_content,
        "model_called_store": state.store_count > 0,
        "dry_run": dry_run,
    }

    logger.info("=" * 60)
    logger.info("DREAMING cycle complete: stored=%d, scores=%s",
                state.store_count,
                [s["confidence"] for s in state.scores])
    logger.info("=" * 60)
    return summary


# =============================================================================
# CLI entry point
# =============================================================================

def main():
    parser = argparse.ArgumentParser(description="Run one DREAMING cycle.")
    parser.add_argument("--bird-name", required=True, help="The bird's chosen name.")
    parser.add_argument(
        "--free",
        action="store_true",
        help="Force the artistic variant (templates/dream.free.md) this run.",
    )
    parser.add_argument(
        "--free-weight",
        type=float,
        default=None,
        metavar="P",
        help="Probability [0.0-1.0] of choosing the free variant when --free "
             "is not set. Overrides config.DREAM_FREE_WEIGHT (env DREAM_FREE_WEIGHT) "
             "for this run. Used by the systemd timer to weight-alternate variants.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Log proposed stores, don't actually write to perp_dreams.",
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

    # Resolve which variant to run. --free forces FREE. Otherwise roll against
    # the weight: per-run --free-weight if given, else config.DREAM_FREE_WEIGHT
    # (env DREAM_FREE_WEIGHT, default 0.0 = always utility). This is the
    # weight-alternation the systemd dreaming timer relies on.
    weight = args.free_weight if args.free_weight is not None else config.DREAM_FREE_WEIGHT
    weight = max(0.0, min(1.0, weight))
    if args.free:
        free_variant = True
        logger.info("Variant: FREE (forced via --free)")
    else:
        roll = random.random()
        free_variant = roll < weight
        logger.info(
            "Variant: %s (weight=%.2f, roll=%.3f)",
            "FREE" if free_variant else "UTILITY", weight, roll,
        )

    # Within free-type cycles, roll for conversation vs pure free-association.
    # Conversation variant seeds dreams from Holden's recent conversations.
    conversation_variant = False
    if free_variant:
        conv_weight = config.DREAM_CONVERSATION_VARIANT_WEIGHT
        conv_roll = random.random()
        conversation_variant = conv_roll < conv_weight
        logger.info(
            "Sub-variant: %s (conv_weight=%.2f, roll=%.3f)",
            "CONVERSATION" if conversation_variant else "FREE",
            conv_weight, conv_roll,
        )

    summary = run_cycle(
        bird_name=args.bird_name,
        free_variant=free_variant,
        conversation_variant=conversation_variant,
        dry_run=args.dry_run,
    )

    # Print summary as JSON for systemd journal / digest consumption
    print(json.dumps(summary, default=str))


if __name__ == "__main__":
    main()
