"""HTTP client for local llama-server (chat-inference and embedding).

Two services on perpBOT, both speaking OpenAI-compatible HTTP:
  - chat (port 8080): Ministral 3 8B abliterated, socket 0
  - embedding (port 8081): Jina v5 nano retrieval F16, socket 1

Three concerns this module owns:

1. REASONING SUPPRESSION INJECTION. Every chat call gets
   `REASONING_SUPPRESSION_PROMPT` automatically prepended as a system
   message. This is gate 2 of the four-gate reasoning architecture
   (--reasoning off server flag alone is insufficient; system prompt
   is the actual enforcer — empirically verified 2026-05-27). Callers
   cannot forget. YES FOR THE LOVE OF NOODLES — H. Matarazzo, 2026-05-27.

2. TOOL-CALL RESPONSE PARSING. llama-server's tool_calls payload nests
   the arguments as a JSON STRING inside a function object. This module
   parses that string and returns clean Python dicts.

3. EMBEDDING WITH JINA PREFIXES. The Phase A smoke test confirmed end-to-end
   embedding works, but naive plain-text embedding produces collapsed
   query/document subspaces. This module applies JINA_QUERY_PREFIX or
   JINA_DOCUMENT_PREFIX before sending text to the embedding endpoint,
   so distances are properly calibrated.

Failure model is fail-LOUD here (opposite of mcp_client). Local llama-server
is core infrastructure; if it's down or returning malformed responses,
the cycle should crash and surface the failure to the operator. There's
no useful degraded mode for "model unreachable" — without the model,
the bird isn't a bird.

Phase B module 3 of 7. Depends on: config. Imported by: context, think,
dream, digest.
"""

import json
import logging
from typing import Any

import requests

from . import config


logger = logging.getLogger(__name__)


class LlamaError(Exception):
    """Raised on llama-server protocol or response-shape failures.

    Network/timeout errors propagate as requests exceptions (caller decides
    whether to retry or crash). This is for cases where the connection
    succeeded but the response was malformed or returned an error.
    """


# =============================================================================
# Chat completion
# =============================================================================

def chat(
    messages: list[dict],
    tools: list[dict] | None = None,
    temperature: float | None = None,
    top_p: float | None = None,
    inject_suppression: bool = True,
) -> dict:
    """Make a chat completion call to local llama-server.

    Args:
        messages: List of {"role": "system"|"user"|"assistant"|"tool", "content": ...}.
                  Caller does NOT need to include a system message for reasoning
                  suppression — this function prepends it automatically.
        tools: Optional list of OpenAI-style tool definitions. If provided,
               the model may return tool_calls in its response.
        temperature: Sampling temperature. Default config.CHAT_TEMPERATURE (0.4).
                     dream.py overrides with config.DREAM_TEMPERATURE (0.9).
        top_p: Top-p sampling. Default config.TOP_P (0.95).
        inject_suppression: If True (default), prepend REASONING_SUPPRESSION_PROMPT
                            as the first system message. The only case for
                            False is testing the model's raw behavior — never
                            in production cycle code.

    Returns:
        A parsed response dict:
            {
                "content": str | None,    # final assistant text, None if tool_calls
                "tool_calls": list[dict], # parsed tool calls, empty list if none
                "finish_reason": str,     # "stop" | "tool_calls" | "length"
                "usage": dict,            # tokens used, latency, etc.
                "raw": dict,              # full response for debugging/logging
            }

        Each tool_call dict has the shape:
            {"id": str, "name": str, "arguments": dict}
        where `arguments` is the JSON-decoded form of what the model emitted.
        If arguments fail to parse as JSON, they appear as
            {"id": str, "name": str, "arguments_raw": str, "arguments_error": str}
        so the cycle code can decide whether to retry or skip.

    Raises:
        LlamaError: on malformed response (missing choices, missing message, etc.).
        requests.exceptions.RequestException: on network/timeout failure.
    """
    # Build the message list with reasoning suppression in front.
    if inject_suppression:
        final_messages = _inject_suppression(messages)
    else:
        final_messages = list(messages)

    payload: dict[str, Any] = {
        "model": config.CHAT_MODEL_NAME,
        "messages": final_messages,
        "temperature": config.CHAT_TEMPERATURE if temperature is None else temperature,
        "top_p": config.TOP_P if top_p is None else top_p,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"

    response = requests.post(
        config.CHAT_INFERENCE_URL,
        headers={"Content-Type": "application/json"},
        json=payload,
        timeout=config.LLAMA_REQUEST_TIMEOUT_SEC,
    )
    response.raise_for_status()
    raw = response.json()

    return _parse_chat_response(raw)


def _inject_suppression(messages: list[dict]) -> list[dict]:
    """Prepend REASONING_SUPPRESSION_PROMPT as the first system message.

    If the caller already has a system message as the first entry, MERGE:
    the suppression text goes in front of their content, separated by a
    blank line. Caller's other-role messages are untouched.
    """
    if not messages:
        return [{"role": "system", "content": config.REASONING_SUPPRESSION_PROMPT}]

    first = messages[0]
    if first.get("role") == "system":
        # Merge in front of existing system content
        merged_content = config.REASONING_SUPPRESSION_PROMPT + "\n\n" + first["content"]
        return [{"role": "system", "content": merged_content}] + list(messages[1:])

    # No existing system message; insert one at the start
    return [{"role": "system", "content": config.REASONING_SUPPRESSION_PROMPT}] + list(messages)


def _parse_chat_response(raw: dict) -> dict:
    """Extract content, tool_calls, finish_reason, and usage from a raw response."""
    choices = raw.get("choices")
    if not choices:
        raise LlamaError(f"chat response has no 'choices': {raw}")

    choice = choices[0]
    message = choice.get("message")
    if message is None:
        raise LlamaError(f"chat response choice has no 'message': {choice}")

    # Reasoning leakage detection (Knot's B2 review, 2026-05-27). llama-server
    # populates `reasoning_content` on the message specifically when CoT
    # happens. Presence of this field is 1:1 with "model emitted reasoning"
    # — zero false positives because the check is on a structured field, not
    # free text. The system-prompt suppression should prevent this; if it
    # appears anyway, gate-2 enforcement is failing and Track B should see
    # the pattern in digest within 24h.
    reasoning_content = message.get("reasoning_content")
    if reasoning_content:
        logger.warning(
            "REASONING LEAKAGE: model emitted reasoning_content despite suppression "
            "(first 200 chars: %r). Gate-2 enforcement may be failing — check "
            "REASONING_SUPPRESSION_PROMPT injection path and consider raising "
            "to LlamaError if leakage persists.",
            reasoning_content[:200],
        )

    content = message.get("content")
    # llama-server returns empty string when tool_calls present; normalize to None
    if content == "":
        content = None

    tool_calls_raw = message.get("tool_calls") or []
    tool_calls = [_parse_tool_call(tc) for tc in tool_calls_raw]

    return {
        "content": content,
        "tool_calls": tool_calls,
        "finish_reason": choice.get("finish_reason"),
        "usage": raw.get("usage", {}),
        "raw": raw,
    }


def _parse_tool_call(tc: dict) -> dict:
    """Parse a single tool_call entry.

    llama-server emits:
        {"id": "...", "type": "function", "function": {"name": "...", "arguments": "JSON string"}}

    We flatten to:
        {"id": "...", "name": "...", "arguments": dict}
    or on argument-parse failure:
        {"id": "...", "name": "...", "arguments_raw": str, "arguments_error": str}
    """
    func = tc.get("function", {})
    name = func.get("name", "")
    args_str = func.get("arguments", "")
    tc_id = tc.get("id", "")

    if not args_str:
        return {"id": tc_id, "name": name, "arguments": {}}

    try:
        args = json.loads(args_str)
        return {"id": tc_id, "name": name, "arguments": args}
    except json.JSONDecodeError as e:
        logger.warning("tool_call %s arguments not valid JSON: %s", name, e)
        return {
            "id": tc_id,
            "name": name,
            "arguments_raw": args_str,
            "arguments_error": str(e),
        }


# =============================================================================
# Embedding (with Jina v5 task-instruction prefixes)
# =============================================================================

def embed_query(text: str) -> list[float]:
    """Generate an embedding for a search QUERY.

    Applies JINA_QUERY_PREFIX before sending to the embedding endpoint.
    This is required for proper distance calibration — see Phase B requirement
    in docs/design.md §7.

    Returns a 768-dim list of floats (L2-normalized; norm ≈ 1.0).
    """
    return _embed_single(config.JINA_QUERY_PREFIX + text)


def embed_document(text: str) -> list[float]:
    """Generate an embedding for a DOCUMENT being stored.

    Applies JINA_DOCUMENT_PREFIX. Used when writing to ChromaDB so that
    documents land in a different region of vector space than queries —
    which is what gives meaningful distance values for retrieval.
    """
    return _embed_single(config.JINA_DOCUMENT_PREFIX + text)


def embed_documents(texts: list[str]) -> list[list[float]]:
    """Batch version of embed_document.

    Single HTTP request, multiple embeddings returned. Use when adding
    many documents to ChromaDB at once.
    """
    if not texts:
        return []
    prefixed = [config.JINA_DOCUMENT_PREFIX + t for t in texts]
    return _embed_batch(prefixed)


def embed_queries(texts: list[str]) -> list[list[float]]:
    """Batch version of embed_query. Rare, but useful for multi-query search."""
    if not texts:
        return []
    prefixed = [config.JINA_QUERY_PREFIX + t for t in texts]
    return _embed_batch(prefixed)


def _embed_single(prefixed_text: str) -> list[float]:
    """One text in, one embedding out."""
    embeddings = _embed_batch([prefixed_text])
    if not embeddings:
        raise LlamaError(f"embedding endpoint returned no results for input")
    return embeddings[0]


def _embed_batch(prefixed_texts: list[str]) -> list[list[float]]:
    """Send N prefixed texts to the embedding endpoint, get N vectors back."""
    payload = {
        "input": prefixed_texts,
        "model": config.EMBEDDING_MODEL_NAME,
    }
    response = requests.post(
        config.EMBEDDING_URL,
        headers={"Content-Type": "application/json"},
        json=payload,
        timeout=config.EMBEDDING_REQUEST_TIMEOUT_SEC,
    )
    response.raise_for_status()
    raw = response.json()

    data = raw.get("data")
    if not data:
        raise LlamaError(f"embedding response has no 'data': {raw}")

    return [item["embedding"] for item in data]
