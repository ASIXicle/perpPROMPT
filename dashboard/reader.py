#!/usr/bin/env python3
"""
perpprompt-reader -- read-only LAN HTTP tap over Echo's perpBOT ChromaDB.

Exposes perp_memories (THINKING) and perp_dreams (DREAMING / DREAM-FREE) as JSON
for the persMEM dashboard's Echo tab.

ZERO third-party dependencies beyond chromadb (already in the venv): uses the
stdlib http.server, so there's nothing to pip/uv install. Keeps this directory
self-contained and liftable into the persMEM repo later.

Read-only BY CONSTRUCTION: only .get() and .count() are ever called -- never
add/update/delete, and never get_or_create, so it cannot mutate state even by
accident.

Config comes from the environment, so this file carries no LAN topology and is
safe to publish. The real bind IP belongs in the systemd drop-in (local-only),
the same pattern the cycle services use:

    PERPBOT_READER_HOST    bind address   (default 0.0.0.0 -- pin to your LAN IP
                                            via the drop-in; see note below)
    PERPBOT_READER_PORT    bind port      (default 8090)
    PERPBOT_CHROMADB_PATH  chromadb dir   (default /opt/perpbot/chromadb)

NOTE on bind host: default 0.0.0.0 keeps any specific LAN IP out of source. For
a no-auth service, prefer pinning PERPBOT_READER_HOST to your specific LAN
interface in the systemd drop-in so it never answers on any other interface
that might appear later.
"""

import os

# chromadb -> pydantic_settings stats `.env` in the process CWD at import time.
# If launched from a directory the service user can't stat (e.g. a locked-down
# home directory), the import raises
# PermissionError before any of our code runs. Anchor CWD to this script's own
# directory -- which the service user owns -- before importing chromadb, so the
# launch location never matters. systemd sets WorkingDirectory too; this is the
# belt-and-suspenders half.
os.chdir(os.path.dirname(os.path.abspath(__file__)))

import json
import time
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import chromadb

HOST = os.environ.get("PERPBOT_READER_HOST", "0.0.0.0")
PORT = int(os.environ.get("PERPBOT_READER_PORT", "8090"))
CHROMADB_PATH = os.environ.get("PERPBOT_CHROMADB_PATH", "/opt/perpbot/chromadb")

MEMORIES = "perp_memories"
DREAMS = "perp_dreams"

# Legacy (pre-patch) dreams have no `variant` key. Bucket them as utility --
# the reader-side backward-compat decision locked in the build plan 2026-05-28.
DEFAULT_VARIANT = "utility"

# Brief retry to ride out SQLite lock contention while a cycle writes. WAL mode
# allows concurrent readers + one writer, but a write mid-query can still throw
# a transient "database is locked"; a few short retries absorb it.
RETRY_ATTEMPTS = 5
RETRY_DELAY = 0.15  # seconds

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s reader: %(message)s",
)
logger = logging.getLogger("perpprompt-reader")

# One client per process is the chromadb convention. The cycle services hold
# their own clients to the same path in their own processes; this process only
# ever reads.
_client = chromadb.PersistentClient(path=CHROMADB_PATH)


def _with_retry(fn):
    """Run a chromadb call, retrying briefly on transient lock contention."""
    last = None
    for _ in range(RETRY_ATTEMPTS):
        try:
            return fn()
        except Exception as e:  # chromadb wraps the sqlite error in varying types
            last = e
            msg = str(e).lower()
            if "lock" in msg or "busy" in msg:
                time.sleep(RETRY_DELAY)
                continue
            raise
    raise last


def _get_collection(name):
    return _with_retry(lambda: _client.get_collection(name))


def _fetch(name):
    """Return [{id, content, metadata}, ...] for a collection, newest first."""
    col = _get_collection(name)
    res = _with_retry(lambda: col.get(include=["documents", "metadatas"]))
    ids = res.get("ids") or []
    docs = res.get("documents") or []
    metas = res.get("metadatas") or []
    rows = []
    for i, _id in enumerate(ids):
        rows.append({
            "id": _id,
            "content": docs[i] if i < len(docs) else "",
            "metadata": (metas[i] if i < len(metas) else {}) or {},
        })
    # stored_at is ISO-8601 with a consistent +00:00 offset across this corpus,
    # so lexicographic sort orders correctly. Revisit if mixed offsets appear.
    rows.sort(key=lambda r: r["metadata"].get("stored_at", ""), reverse=True)
    return rows


def _apply_filters(rows, since=None, limit=None):
    if since:
        rows = [r for r in rows if r["metadata"].get("stored_at", "") >= since]
    if limit is not None:
        rows = rows[:limit]
    return rows


def _qs_int(qs, name, default):
    vals = qs.get(name)
    if not vals:
        return default
    try:
        return int(vals[0])
    except (TypeError, ValueError):
        return default


def _qs_str(qs, name, default=None):
    vals = qs.get(name)
    return vals[0] if vals else default


def health_payload():
    return {
        "status": "ok",
        "collections": {
            MEMORIES: _with_retry(lambda: _get_collection(MEMORIES).count()),
            DREAMS: _with_retry(lambda: _get_collection(DREAMS).count()),
        },
    }


def memories_payload(qs):
    rows = _apply_filters(
        _fetch(MEMORIES),
        since=_qs_str(qs, "since"),
        limit=_qs_int(qs, "limit", 50),
    )
    return {"count": len(rows), "items": rows}


def dreams_payload(qs):
    variant = (_qs_str(qs, "variant", "all") or "all").lower()
    rows = _fetch(DREAMS)
    if variant in ("utility", "free"):
        rows = [
            r for r in rows
            if r["metadata"].get("variant", DEFAULT_VARIANT) == variant
        ]
    rows = _apply_filters(
        rows,
        since=_qs_str(qs, "since"),
        limit=_qs_int(qs, "limit", 50),
    )
    return {"count": len(rows), "variant": variant, "items": rows}


ROUTES = {
    "/health": lambda qs: health_payload(),
    "/perp_memories": memories_payload,
    "/perp_dreams": dreams_payload,
}


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        handler = ROUTES.get(parsed.path)
        if handler is None:
            self._send(404, {"status": "error", "error": "not found"})
            return
        try:
            self._send(200, handler(parse_qs(parsed.query)))
        except Exception as e:
            logger.exception("request failed: %s", parsed.path)
            self._send(503, {"status": "error", "error": str(e)})

    def log_message(self, fmt, *args):
        logger.info("%s - %s", self.address_string(), fmt % args)


if __name__ == "__main__":
    logger.info(
        "perpprompt-reader starting on %s:%s (chromadb=%s)",
        HOST, PORT, CHROMADB_PATH,
    )
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
