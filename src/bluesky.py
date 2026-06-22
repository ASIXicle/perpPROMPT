"""Post Echo's dreams to Bluesky — DREAMS ONLY.

Called from dream.py's storage chokepoint after a dream is stored. Thoughts
never reach this path. Posting is **best-effort and fully isolated**: every
failure (network, auth, rate-limit, Mullvad down, atproto missing) is caught
and logged so a Bluesky problem can never delay or fail a dream cycle.

Long dreams are threaded (Bluesky caps posts at 300 graphemes). Every post is
self-labeled (config.BLUESKY_SELF_LABEL) because the account is declared 18+.

Config (all from bird_env via config.py):
  BLUESKY_ENABLED, BLUESKY_HANDLE, BLUESKY_APP_PASSWORD, BLUESKY_PDS,
  BLUESKY_POST_VARIANTS, BLUESKY_SELF_LABEL, BLUESKY_MAX_THREAD_POSTS,
  BLUESKY_POST_TIMEOUT

The app password lives only in bird_env (chmod 600, gitignored) — never here.
"""

import concurrent.futures
import logging

from . import config

logger = logging.getLogger(__name__)

# Bluesky's post text cap is 300 graphemes. Leave headroom for a " (n/m)"
# thread counter appended after chunking.
_POST_LIMIT = 290


def _chunk_text(text: str, limit: int = _POST_LIMIT) -> list[str]:
    """Split text into <=limit-grapheme chunks on whitespace boundaries.

    Grapheme-accurate when the `grapheme` package is installed; otherwise
    char-counts (close enough for mostly-Latin prose, and we keep a 10-unit
    margin under 300). A single over-long word is hard-split as a last resort.
    """
    text = " ".join(text.split())  # normalize whitespace + newlines
    try:
        from grapheme import length as _glen
    except ImportError:
        _glen = len

    if _glen(text) <= limit:
        return [text]

    chunks: list[str] = []
    buf = ""
    for word in text.split(" "):
        candidate = word if not buf else buf + " " + word
        if buf and _glen(candidate) > limit:
            chunks.append(buf)
            buf = word
        else:
            buf = candidate
        # Rare: a single word longer than the limit — hard-split it.
        while _glen(buf) > limit:
            chunks.append(buf[:limit])
            buf = buf[limit:]
    if buf:
        chunks.append(buf)
    return [c for c in chunks if c]


def post_dream(content: str, variant: str) -> list[str]:
    """Post a stored dream to Bluesky. Returns posted URIs ([] if skipped/failed).

    Never raises. Gated on BLUESKY_ENABLED and BLUESKY_POST_VARIANTS. Runs the
    network work under a hard timeout so a hang can't stall the dream cycle.
    """
    if not config.BLUESKY_ENABLED:
        return []
    if config.BLUESKY_POST_VARIANTS != "all":
        allowed = {v.strip() for v in config.BLUESKY_POST_VARIANTS.split(",")}
        # "conversation" is a sub-variant of "free" — post if either is allowed
        effective = variant if variant != "conversation" else "free"
        if effective not in allowed:
            return []
    if not content or not content.strip():
        return []
    if not config.BLUESKY_HANDLE or not config.BLUESKY_APP_PASSWORD:
        logger.warning("Bluesky enabled but handle/app-password unset — skipping post")
        return []

    ex = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        future = ex.submit(_do_post, content.strip())
        return future.result(timeout=config.BLUESKY_POST_TIMEOUT)
    except concurrent.futures.TimeoutError:
        logger.warning(
            "Bluesky post timed out after %ss (non-fatal)", config.BLUESKY_POST_TIMEOUT
        )
        return []
    except Exception as e:  # noqa: BLE001 — best-effort; the dream cycle is sacred
        logger.warning("Bluesky post failed (non-fatal): %s", e)
        return []
    finally:
        ex.shutdown(wait=False)  # don't block the cycle on a hung worker


def _do_post(content: str) -> list[str]:
    """Build + send the (threaded, self-labeled) post(s). Runs in a worker thread."""
    from atproto import Client, models

    chunks = _chunk_text(content)
    n = len(chunks)
    if n > config.BLUESKY_MAX_THREAD_POSTS:
        chunks = chunks[: config.BLUESKY_MAX_THREAD_POSTS]
        # mark truncation on the final kept chunk
        tail = chunks[-1][: _POST_LIMIT - 1].rstrip()
        chunks[-1] = tail + "\u2026"
        n = len(chunks)
    if n > 1:
        chunks = [f"{c} ({i}/{n})" for i, c in enumerate(chunks, start=1)]

    client = Client(config.BLUESKY_PDS)
    client.login(config.BLUESKY_HANDLE, config.BLUESKY_APP_PASSWORD)

    labels = models.ComAtprotoLabelDefs.SelfLabels(
        values=[models.ComAtprotoLabelDefs.SelfLabel(val=config.BLUESKY_SELF_LABEL)]
    )

    uris: list[str] = []
    root_ref = None
    parent_ref = None
    for chunk in chunks:
        reply = None
        if root_ref is not None:
            reply = models.AppBskyFeedPost.ReplyRef(parent=parent_ref, root=root_ref)
        record = models.AppBskyFeedPost.Record(
            created_at=client.get_current_time_iso(),
            text=chunk,
            langs=["en"],
            labels=labels,
            reply=reply,
        )
        resp = client.com.atproto.repo.create_record(
            models.ComAtprotoRepoCreateRecord.Data(
                repo=client.me.did,
                collection="app.bsky.feed.post",  # NSID literal; avoids the `ids` import (not exported in atproto 0.0.67)
                record=record,
            )
        )
        uris.append(resp.uri)
        ref = models.create_strong_ref(resp)
        if root_ref is None:
            root_ref = ref          # thread root stays the first post
        parent_ref = ref            # parent advances to the previous post

    logger.info(
        "Posted dream to Bluesky: %d post(s), root=%s",
        len(uris), uris[0] if uris else "(none)",
    )
    return uris
