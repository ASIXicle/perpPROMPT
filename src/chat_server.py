"""HTTP chat service for the perpPROMPT instance.

Exposes the instance's conversational brain over the LAN so the persMEM
dashboard can hold a real conversation with it. SEPARATE from the read-only
reader (:8090) on purpose — this service INVOKES the model and WRITES memory,
so it lives in its own process on its own port (:8091).

Design
------
* Reasoning suppression (defense-in-depth). Outgoing messages pass through
  llama_client._inject_suppression (gate 2). A last-mile suppression reminder
  is injected as an invisible system message right before generation. During
  streaming, ONLY delta.content is forwarded — delta.reasoning_content is
  dropped on the floor (structural guarantee that chain-of-thought never
  reaches the UI). A regex belt strips any stray inline <think>..</think> from
  the content stream. If reasoning_content ever appears, it's logged as a
  gate-2-weakening signal (mirrors llama_client's existing leak alarm).

* Refresh-resilience (the "page refresh kills the stream" problem). The
  conversation and the in-progress reply live server-side, keyed by a
  conversation_id. Generation runs in a background thread NOT tied to the HTTP
  connection, so a dashboard refresh mid-stream does not abort it. SSE clients
  tail the shared pending buffer: a reconnecting client replays the partial so
  far and continues seamlessly. No external pub/sub, no per-token database.

* --remember. A conversation (or its last N turns) can be persisted into the
  instance's perp_memories via context.store_observation, tagged origin=chat +
  saver so it surfaces in future cycles. Both Holden (UI button -> saver=holden)
  and the instance ([[remember]] marker -> saver=echo) can save; a tally records
  who saved how many times (per-conversation and global).

Run:  /opt/perpbot/venv/bin/python -m src.chat_server   (WorkingDirectory=/opt/perpbot)
Env:  PERPBOT_CHAT_HOST (default 0.0.0.0)
      PERPBOT_CHAT_PORT (default 8091)
      PERPBOT_CHAT_GROUNDING_K (default 4; 0 disables memory grounding)
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import requests

from . import config
from . import context
from . import llama_client
from . import chat as chat_module

logger = logging.getLogger("chat_server")

HOST = os.environ.get("PERPBOT_CHAT_HOST", "0.0.0.0")
PORT = int(os.environ.get("PERPBOT_CHAT_PORT", "8091"))
GROUNDING_K = int(os.environ.get("PERPBOT_CHAT_GROUNDING_K", "4"))

REMEMBER_MARKER = "[[remember]]"

# Last-mile suppression reminder — injected as an invisible system message right
# before generation (recency-weighted reinforcement of gate 2).
LAST_MILE_SUPPRESSION = (
    "Reminder: answer directly, in your own voice. Do not emit <think> tags, "
    "analysis blocks, or chain-of-thought. Give the observation, not the "
    "reasoning that produced it."
)

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


# =============================================================================
# Conversation store (in-memory; survives page refresh within service lifetime)
# =============================================================================

_conversations: dict[str, dict] = {}
_store_lock = threading.Lock()
_global_tally = {"holden": 0, "echo": 0}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _get_or_create(cid: str) -> dict:
    with _store_lock:
        conv = _conversations.get(cid)
        if conv is None:
            conv = {
                "id": cid,
                "messages": [],  # committed turns [{role, content}]
                "pending": {"content": "", "done": True, "error": None},
                "tally": {"holden": 0, "echo": 0},
                "lock": threading.Lock(),
                "created": _now(),
                "updated": _now(),
            }
            _conversations[cid] = conv
        return conv


def _public_view(conv: dict) -> dict:
    return {
        "id": conv["id"],
        "messages": conv["messages"],
        "pending": dict(conv["pending"]),
        "tally": conv["tally"],
        "global_tally": _global_tally,
        "created": conv["created"],
        "updated": conv["updated"],
    }


# =============================================================================
# Identity + context assembly
# =============================================================================

def _bird_name() -> str:
    name_file = config.CONFIG_DIR / "bird_name"
    if name_file.exists():
        txt = name_file.read_text().strip()
        if txt:
            return txt
    return "the instance"


def _build_system_message(bird_name: str, user_text: str) -> str:
    """Identity + focus (reused from chat.py) + optional retrieval grounding
    from the instance's own perp_memories + the [[remember]] marker note."""
    base = chat_module._build_chat_system_message(bird_name)

    grounding = ""
    if GROUNDING_K > 0 and user_text:
        try:
            hits = context.search_memories(user_text, n_results=GROUNDING_K)
        except Exception as e:
            logger.warning("grounding search failed (continuing without): %s", e)
            hits = []
        lines = []
        for hit in hits:
            doc = (hit.get("document") or hit.get("content") or "").strip()
            if doc:
                lines.append("- " + doc.replace("\n", " ")[:400])
        if lines:
            grounding = (
                "\n\n## RELEVANT FRAGMENTS FROM YOUR OWN MEMORY\n"
                "Things you have previously observed or dreamt that may bear on "
                "this. Draw on them if useful; ignore them if not.\n"
                + "\n".join(lines)
            )

    marker_note = (
        "\n\n## SAVING\n"
        f"If you judge this exchange worth keeping in your own memory — something "
        f"that should inform your future cycles — end your reply with {REMEMBER_MARKER} "
        f"on its own line. It will be stored to your memory and stripped from what "
        f"Holden sees. Use it sparingly; most exchanges don't need saving."
    )
    voice_note = (
        "\n\n## VOICE\n"
        "Let your replies breathe. Follow an idea where it leads and give it room "
        "to unfold rather than collapsing to a single tidy line. This is about the "
        "fullness of your answer — not about showing working. Still no reasoning "
        "scaffolding and no <think> tags."
    )
    return base + voice_note + grounding + marker_note


def _clean_content(raw: str) -> str:
    """Strip <think>..</think> spans, including an unclosed trailing one
    (in-progress reasoning during streaming)."""
    s = _THINK_RE.sub("", raw)
    low = s.lower()
    idx = low.rfind("<think>")
    if idx != -1 and "</think>" not in low[idx:]:
        s = s[:idx]
    return s


# =============================================================================
# Generation (background thread; streams into conv['pending'])
# =============================================================================

def _generate(conv: dict, bird_name: str) -> None:
    pending = conv["pending"]
    with conv["lock"]:
        history = list(conv["messages"])
    user_text = ""
    if history and history[-1]["role"] == "user":
        user_text = history[-1]["content"]

    system_msg = _build_system_message(bird_name, user_text)
    # Fold the suppression reminder into the single front system message. A
    # trailing system message AFTER the user turn breaks the Mistral/Ministral
    # chat template (llama-server returns 500), so we keep one system message
    # up front and let the user turn be last.
    system_msg = system_msg + "\n\n" + LAST_MILE_SUPPRESSION
    msgs = [{"role": "system", "content": system_msg}] + history
    final = llama_client._inject_suppression(msgs)  # gate 2

    payload = {
        "model": config.CHAT_MODEL_NAME,
        "messages": final,
        "temperature": config.CHAT_TEMPERATURE,
        "top_p": config.TOP_P,
        "stream": True,
        # DRY sampler (llama.cpp): penalize repeated *sequences*, not just
        # individual tokens — breaks the phrase/structure loops a small corpus
        # tends to produce, without flattening vocabulary. Chat path only.
        "dry_multiplier": 0.8,
        "dry_base": 1.75,
        "dry_allowed_length": 2,
    }

    raw_content = ""
    emitted = 0
    leaked_reasoning = False
    try:
        resp = requests.post(
            config.CHAT_INFERENCE_URL,
            headers={"Content-Type": "application/json"},
            json=payload,
            stream=True,
            timeout=config.LLAMA_REQUEST_TIMEOUT_SEC,
        )
        if resp.status_code >= 400:
            detail = ""
            try:
                detail = resp.text[:500]
            except Exception:
                pass
            raise RuntimeError(f"llama-server {resp.status_code}: {detail}")
        resp.encoding = "utf-8"  # SSE is text/*, which requests defaults to latin-1; force utf-8
        for line in resp.iter_lines(decode_unicode=True):
            if not line or not line.startswith("data:"):
                continue
            data = line[len("data:"):].strip()
            if data == "[DONE]":
                break
            try:
                obj = json.loads(data)
            except json.JSONDecodeError:
                continue
            choices = obj.get("choices") or []
            if not choices:
                continue
            delta = choices[0].get("delta") or {}
            if delta.get("reasoning_content"):
                leaked_reasoning = True  # dropped, never forwarded
            piece = delta.get("content")
            if piece:
                raw_content += piece
                cleaned = _clean_content(raw_content)
                if len(cleaned) > emitted:
                    emitted = len(cleaned)
                    with conv["lock"]:
                        pending["content"] = cleaned
                        conv["updated"] = _now()

        final_text = _clean_content(raw_content).strip()
        saver_echo = False
        if REMEMBER_MARKER in final_text:
            final_text = final_text.replace(REMEMBER_MARKER, "").strip()
            saver_echo = True

        with conv["lock"]:
            pending["content"] = final_text
            pending["done"] = True
            if final_text:
                conv["messages"].append({"role": "assistant", "content": final_text})
            conv["updated"] = _now()

        if leaked_reasoning:
            logger.warning(
                "reasoning_content appeared during chat (dropped from stream); "
                "gate-2 suppression may be weakening"
            )
        if saver_echo and final_text:
            try:
                _persist(conv, saver="echo")
            except Exception as e:
                logger.error("instance self-save failed: %s", e)

    except Exception as e:
        logger.error("generation failed: %s", e)
        with conv["lock"]:
            pending["content"] = ""
            pending["error"] = str(e)[:300]
            pending["done"] = True


# =============================================================================
# Persistence (--remember)
# =============================================================================

def _persist(conv: dict, saver: str, n: int | None = None) -> str:
    with conv["lock"]:
        msgs = list(conv["messages"])
    if n and n > 0:
        msgs = msgs[-n:]
    if not msgs:
        raise ValueError("nothing to save")

    bird = _bird_name()
    lines = [f"[Chat conversation, saved by {saver}, {len(msgs)} turns, {_now()}]", ""]
    for m in msgs:
        who = "Holden" if m["role"] == "user" else bird
        lines.append(f"{who}: {m['content']}")
        lines.append("")
    doc = "\n".join(lines).strip()

    mem_id = context.store_observation(
        content=doc,
        bird_name=bird,
        extra_metadata={
            "origin": "chat",
            "saver": saver,
            "conversation_id": conv["id"],
            "turns": len(msgs),
        },
    )
    with conv["lock"]:
        conv["tally"][saver] = conv["tally"].get(saver, 0) + 1
    with _store_lock:
        _global_tally[saver] = _global_tally.get(saver, 0) + 1
    logger.info("saved conversation %s as %s (saver=%s, %d turns)",
                conv["id"], mem_id, saver, len(msgs))
    return mem_id


# =============================================================================
# SSE
# =============================================================================

def _sse(data: str) -> bytes:
    return ("data: " + data + "\n\n").encode("utf-8")


def _sse_tail(handler: "ChatHandler", conv: dict) -> None:
    """Stream conv['pending'] as SSE: replay what's accumulated, then follow
    until done. Survives reconnect (a new caller replays from the top)."""
    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream")
    handler.send_header("Cache-Control", "no-cache")
    handler.send_header("X-Accel-Buffering", "no")
    handler.send_header("Connection", "close")
    handler.end_headers()

    sent = 0
    deadline = time.time() + config.LLAMA_REQUEST_TIMEOUT_SEC + 30
    try:
        while True:
            with conv["lock"]:
                content = conv["pending"]["content"]
                done = conv["pending"]["done"]
                error = conv["pending"]["error"]
            if len(content) > sent:
                handler.wfile.write(_sse(json.dumps({"delta": content[sent:]})))
                handler.wfile.flush()
                sent = len(content)
            if done and sent >= len(content):
                if error:
                    handler.wfile.write(_sse(json.dumps({"error": error})))
                handler.wfile.write(_sse(json.dumps({"done": True})))
                handler.wfile.flush()
                break
            if time.time() > deadline:
                handler.wfile.write(_sse(json.dumps({"error": "timeout"})))
                handler.wfile.flush()
                break
            time.sleep(0.05)
    except (BrokenPipeError, ConnectionResetError):
        logger.info("SSE client left conversation %s (generation continues)", conv["id"])


# =============================================================================
# HTTP handler
# =============================================================================

class ChatHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        logger.info("%s - %s", self.address_string(), fmt % args)

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        if parsed.path == "/health":
            self._json({
                "status": "ok",
                "bird": _bird_name(),
                "conversations": len(_conversations),
                "global_tally": _global_tally,
            })
            return
        if parsed.path == "/conversation":
            cid = (qs.get("id") or ["default"])[0]
            conv = _get_or_create(cid)
            with conv["lock"]:
                view = _public_view(conv)
            self._json(view)
            return
        if parsed.path == "/stream":
            cid = (qs.get("id") or ["default"])[0]
            conv = _get_or_create(cid)
            _sse_tail(self, conv)
            return
        self._json({"error": "not found"}, 404)

    def do_POST(self):
        parsed = urlparse(self.path)
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            self._json({"error": "invalid json"}, 400)
            return

        if parsed.path == "/message":
            cid = body.get("conversation_id") or "default"
            content = (body.get("content") or "").strip()
            if not content:
                self._json({"error": "empty content"}, 400)
                return
            conv = _get_or_create(cid)
            bird = _bird_name()
            start_gen = False
            with conv["lock"]:
                if conv["pending"]["done"]:
                    conv["messages"].append({"role": "user", "content": content})
                    conv["pending"] = {"content": "", "done": False, "error": None}
                    conv["updated"] = _now()
                    start_gen = True
                # else: generation already running for this conversation —
                # ignore the new content, just attach the SSE to the live stream.
            if start_gen:
                threading.Thread(target=_generate, args=(conv, bird), daemon=True).start()
            _sse_tail(self, conv)
            return

        if parsed.path == "/save":
            cid = body.get("conversation_id") or "default"
            saver = body.get("saver") or "holden"
            if saver not in ("holden", "echo"):
                saver = "holden"
            conv = _get_or_create(cid)
            try:
                mem_id = _persist(conv, saver=saver, n=body.get("n"))
            except Exception as e:
                self._json({"error": str(e)[:300]}, 400)
                return
            with conv["lock"]:
                tally = dict(conv["tally"])
            self._json({"saved": True, "memory_id": mem_id,
                        "tally": tally, "global_tally": _global_tally})
            return

        if parsed.path == "/clear":
            cid = body.get("conversation_id") or "default"
            conv = _get_or_create(cid)
            with conv["lock"]:
                if not conv["pending"]["done"]:
                    # A generation is in flight — refuse rather than drop the
                    # turn being produced. Client retries after it completes.
                    self._json({"error": "generation in progress"}, 409)
                    return
                conv["messages"] = []
                conv["pending"] = {"content": "", "done": True, "error": None}
                conv["updated"] = _now()
            self._json({"cleared": True})
            return

        self._json({"error": "not found"}, 404)


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logger.info("perpprompt-chat starting on %s:%d (grounding_k=%d, bird=%s)",
                HOST, PORT, GROUNDING_K, _bird_name())
    server = ThreadingHTTPServer((HOST, PORT), ChatHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
