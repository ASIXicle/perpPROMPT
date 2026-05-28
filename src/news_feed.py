"""
news_feed.py — News fragment sampling for the bird's THINKING and DREAMING modes.

Two samplers, two cognitive modes:

  dream_feed_sample()  — DREAMING mode. Tier-weighted random sampling
                         biased toward arts/long-form/wildcard (T7-T9).
                         Hybrid seed phrase: one word from the bird's own
                         recent perp_memories + one external noun, woven
                         as "{self_ref} and {noun}". Latticework framing
                         (per Holden, 2026-05-26).

  thinking_news_sample() — THINKING mode. NO tier filter; conscious mind
                           reads anything. Seed is DIRECTED — provided by
                           the wrapper from current project focus or last
                           AMQ subject. Consciousness directs attention;
                           dreaming encounters what surfaces randomly.

Both functions read against the production news collection (CT 300's
persmem) via MCP. The bird never writes to news_store — perpBOT does
not hold the NEWSTRON_SECRET, so write calls would fail authentication
by construction. That's intentional.

Dream-feed weights (perpPROMPT chorus, 2026-05-26):
  T1-T3: 0.00 (excluded — utility-coded content makes utility-coded dreams)
  T4: 0.10  T5: 0.15  T6: 0.10
  T7: 0.20  T8: 0.15  T9: 0.30

Empty-tier handling for DREAMING: if the chosen tier returns no matching
news entry, we retry ONCE with a different tier. If that also fails, we
return None and the dream prompt template handles the empty slot — a
dream with no news fragment is still a valid dream.

Empty-result handling for THINKING: return None. Wrapper decides whether
to retry with a broader seed or proceed without news context.

Source: Knot (Overwatch), perpPROMPT chorus rounds 4-7, 2026-05-26/27.
"""
from __future__ import annotations

import random
import re
from pathlib import Path
from typing import Optional

from mcp_client import MCPClient


# ─────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────

# Tier sampling weights for the dream-feed slot. T1-T3 deliberately
# omitted — utility-coded content makes utility-coded dreams.
DREAM_FEED_WEIGHTS: dict[int, float] = {
    4: 0.10,   # academic AI/cog-sci
    5: 0.15,   # academic broader (philosophy, neuro, linguistics)
    6: 0.10,   # general news
    7: 0.20,   # arts & culture
    8: 0.15,   # long-form / lifestyle
    9: 0.30,   # wildcard / weird
}

# Path to the external noun corpus. Relative to project root, parallel
# to src/. Tunable; populated by Kite, finalized by Holden's pass.
NOUN_CORPUS_PATH = (
    Path(__file__).resolve().parent.parent / "data" / "dream_nouns.txt"
)

# How many recent perp_memories to pull when building the self-ref
# seed pool. Larger = more variety, smaller = bird stays closer to
# itself. Tunable.
SELF_REF_MEMORY_POOL_SIZE = 50

# Words excluded when picking self-ref seeds. Avoids articles, pronouns,
# and tool-call artifacts polluting the seed.
SELF_REF_STOPWORDS: frozenset[str] = frozenset({
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
    "and", "or", "but", "if", "then", "so", "of", "to", "for", "in",
    "on", "at", "by", "with", "from", "as", "this", "that", "these",
    "those", "it", "its", "i", "you", "we", "they", "he", "she", "him",
    "her", "us", "them", "my", "your", "our", "their", "have", "has",
    "had", "do", "does", "did", "will", "would", "should", "could",
    "may", "might", "can", "must", "into", "than", "what", "which",
    "when", "where", "who", "how", "why", "there", "here",
})

# Lazy cache for the noun corpus.
_noun_cache: Optional[list[str]] = None


# ─────────────────────────────────────────────────────────────────────
# Helpers (exposed for unit testing)
# ─────────────────────────────────────────────────────────────────────

def _load_nouns() -> list[str]:
    """Load and cache the external noun corpus. Idempotent.

    Skips blank lines and lines starting with '#'. Returns empty list
    if the corpus file is missing — caller should handle.
    """
    global _noun_cache
    if _noun_cache is not None:
        return _noun_cache
    try:
        with NOUN_CORPUS_PATH.open("r", encoding="utf-8") as f:
            _noun_cache = [
                line.strip() for line in f
                if line.strip() and not line.startswith("#")
            ]
    except OSError:
        _noun_cache = []
    return _noun_cache


def pick_tier(
    tier_override: Optional[int] = None,
    rng: Optional[random.Random] = None,
) -> int:
    """Sample a tier per DREAM_FEED_WEIGHTS.

    Args:
        tier_override: For testing. Forces this tier; bypasses random
                       sampling. Must be a valid key in DREAM_FEED_WEIGHTS.
        rng: Optional random.Random for deterministic tests.

    Returns:
        Integer tier from {4, 5, 6, 7, 8, 9}.

    Raises:
        ValueError: If tier_override is not a valid weighted tier.
    """
    if tier_override is not None:
        if tier_override not in DREAM_FEED_WEIGHTS:
            raise ValueError(
                f"tier_override={tier_override} not in DREAM_FEED_WEIGHTS "
                f"(valid: {sorted(DREAM_FEED_WEIGHTS.keys())})"
            )
        return tier_override

    r = rng if rng is not None else random
    return r.choices(
        list(DREAM_FEED_WEIGHTS.keys()),
        weights=list(DREAM_FEED_WEIGHTS.values()),
        k=1,
    )[0]


def pick_external_noun(rng: Optional[random.Random] = None) -> str:
    """Sample one noun from the external corpus.

    Returns "" if the corpus is missing or empty — caller falls back
    to self-ref-only seed or treats as empty dream slot.
    """
    nouns = _load_nouns()
    if not nouns:
        return ""
    r = rng if rng is not None else random
    return r.choice(nouns)


def pick_self_ref_word(
    client: MCPClient,
    rng: Optional[random.Random] = None,
    pool_size: int = SELF_REF_MEMORY_POOL_SIZE,
) -> str:
    """Sample one content word from the bird's recent perp_memories.

    Pulls up to `pool_size` recent perp_memories, concatenates the
    content, strips stopwords and non-alphabetic tokens (code/path/
    identifier noise), returns one random surviving word lowercased.

    Returns "" if perp_memories is empty (first cycles after birth)
    or if no usable word survives filtering. Caller falls back to
    external-noun-only.

    Args:
        client: MCP client wired to the bird's LOCAL persmem on
                perpBOT (perpBOT's own chromadb instance, NOT the
                production persmem).
        rng: Optional random.Random for deterministic tests.
        pool_size: How many recent memories to pull.
    """
    try:
        result = client.call("memory_search", {
            "query": "",  # empty → broad embedding-space-center sample
            "top_k": pool_size,
            "collection": "perp_memories",
            "include_superseded": False,
        })
    except Exception:
        return ""

    chunks = result.get("results", [])
    if not chunks:
        return ""

    blob = " ".join(c.get("content", "") for c in chunks)
    # Words of 4+ alphabetic chars only. Strips paths, URLs,
    # identifiers, code tokens — anything dream-content-irrelevant.
    words = re.findall(r"[A-Za-z]{4,}", blob)
    candidates = [
        w.lower() for w in words
        if w.lower() not in SELF_REF_STOPWORDS
    ]
    if not candidates:
        return ""

    r = rng if rng is not None else random
    return r.choice(candidates)


def build_seed_phrase(
    client: MCPClient,
    rng: Optional[random.Random] = None,
) -> str:
    """Construct the hybrid seed phrase for one dream cycle.

    Combines one self-referential word and one external noun, joined
    by " and " — the latticework. If either source is empty, returns
    the other; if both are empty, returns "" and the caller falls back.

    Args:
        client: MCP client wired to bird's LOCAL persmem on perpBOT.
        rng: Optional random.Random for deterministic tests.
    """
    self_ref = pick_self_ref_word(client, rng=rng)
    external = pick_external_noun(rng=rng)

    if self_ref and external:
        return f"{self_ref} and {external}"
    return self_ref or external  # one, the other, or empty string


def _format_fragment(chunk: dict, *, seed: str, tier: Optional[int] = None) -> dict:
    """Shape one news_search result chunk into the sampler return dict."""
    meta = chunk.get("metadata", {})
    out: dict = {
        "source": meta.get("source", "?"),
        "content": chunk.get("content", ""),
        "url": meta.get("url", ""),
        "similarity": chunk.get("similarity", 0.0),
        "seed": seed,
    }
    if tier is not None:
        out["tier"] = tier
    else:
        # THINKING mode preserves the actual tier from result metadata
        # since we didn't filter on it.
        out["tier"] = meta.get("tier", None)
    return out


# ─────────────────────────────────────────────────────────────────────
# DREAMING mode entry point
# ─────────────────────────────────────────────────────────────────────

def dream_feed_sample(
    local_client: MCPClient,
    news_client: MCPClient,
    *,
    tier_override: Optional[int] = None,
    rng: Optional[random.Random] = None,
    retry_on_empty: bool = True,
) -> Optional[dict]:
    """Sample one news fragment for the dream prompt's [news_item] slot.

    Picks a tier per DREAM_FEED_WEIGHTS, builds a hybrid seed phrase
    from bird's own memories + an external noun, performs semantic
    search against the production news collection, returns the top
    matching entry.

    Empty-tier handling: if the chosen tier returns nothing, retries
    ONCE with a different randomly-picked tier (excluding the missed
    one). If still nothing, returns None — dream prompt handles the
    empty slot. Dreams without news fragments are still valid dreams.

    Args:
        local_client: MCP client wired to bird's LOCAL persmem on
                      perpBOT. Used for perp_memories reads when
                      building seed.
        news_client: MCP client wired to PRODUCTION persmem (CT 300).
                     Used for news_search. Read-only by construction —
                     bird doesn't have NEWSTRON_SECRET so news_store
                     calls fail authentication. That's intentional.
        tier_override: For testing. Force a specific tier; bypasses
                       both initial pick and retry fallback.
        rng: Optional random.Random for deterministic tests.
        retry_on_empty: If True (default), retry once with a different
                        tier when first pick yields nothing. Disable
                        in tests verifying empty-handling paths.

    Returns:
        dict with keys {tier, source, content, url, similarity, seed,
        retry?} on success, or None if no news fragment retrieved.
    """
    tier = pick_tier(tier_override=tier_override, rng=rng)
    seed = build_seed_phrase(local_client, rng=rng)

    # Last-resort fallback if both seed sources are empty. Embedding
    # center sampling would still return SOMETHING, but a mild
    # dream-relevant nudge produces better fragments early-cycle when
    # perp_memories is barely populated.
    if not seed:
        seed = "memory"

    try:
        result = news_client.call("news_search", {
            "query": seed,
            "top_k": 1,
            "tier": tier,
        })
    except Exception:
        return None

    chunks = result.get("results", [])
    if chunks:
        return _format_fragment(chunks[0], seed=seed, tier=tier)

    # First pick empty — retry once with a different tier picked from
    # the same distribution minus the one we just tried.
    if retry_on_empty and tier_override is None:
        remaining = {
            t: w for t, w in DREAM_FEED_WEIGHTS.items() if t != tier
        }
        r = rng if rng is not None else random
        retry_tier = r.choices(
            list(remaining.keys()),
            weights=list(remaining.values()),
            k=1,
        )[0]
        try:
            retry_result = news_client.call("news_search", {
                "query": seed,
                "top_k": 1,
                "tier": retry_tier,
            })
        except Exception:
            return None
        retry_chunks = retry_result.get("results", [])
        if retry_chunks:
            fragment = _format_fragment(
                retry_chunks[0], seed=seed, tier=retry_tier
            )
            fragment["retry"] = True
            return fragment

    return None


# ─────────────────────────────────────────────────────────────────────
# THINKING mode entry point
# ─────────────────────────────────────────────────────────────────────

def thinking_news_sample(
    news_client: MCPClient,
    seed: str,
    *,
    top_k: int = 1,
) -> Optional[dict]:
    """Pull one news item for the THINKING cycle context.

    Conscious mind reads anything — no tier filter, uniform across the
    full corpus. Seed comes from the wrapper (current project focus,
    last AMQ subject, or other directed-attention source). Different
    from dream sampling: THINKING is directed search, not random
    surfacing.

    Args:
        news_client: MCP client wired to PRODUCTION persmem (CT 300).
                     Same client used for dream sampling — bird only
                     ever has one news endpoint.
        seed: Directed query string. Wrapper provides this; typically
              the bird's current project focus or the subject of the
              most recent AMQ message it received.
        top_k: How many results to consider. Default 1 (best match).
               Wrapper can ask for more if it wants to pick among
               candidates.

    Returns:
        dict with keys {tier, source, content, url, similarity, seed}
        on success, or None if no news matched the seed. Wrapper
        decides whether to retry with a broader seed or proceed
        without news context.
    """
    if not seed:
        # Empty seed for THINKING is a wrapper bug, not a fallback case.
        # Return None loudly rather than silently substituting.
        return None

    try:
        result = news_client.call("news_search", {
            "query": seed,
            "top_k": top_k,
            # No tier filter — conscious mind reads anything.
        })
    except Exception:
        return None

    chunks = result.get("results", [])
    if not chunks:
        return None

    # Always return the top match. If wrapper asked for top_k>1, they
    # can call news_search directly and pick; this function is the
    # opinionated "one item" path.
    return _format_fragment(chunks[0], seed=seed, tier=None)


# ─────────────────────────────────────────────────────────────────────
# Module smoke test
# ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Won't actually fetch — just confirms imports and shows config.
    # Useful for "python news_feed.py" sanity-checking during deployment.
    import sys

    print("news_feed module loaded.", file=sys.stderr)
    print(
        f"  dream-feed weighted tiers: {sorted(DREAM_FEED_WEIGHTS.keys())}",
        file=sys.stderr,
    )
    print(
        f"  thinking-feed tier filter: none (uniform)",
        file=sys.stderr,
    )
    print(f"  noun corpus path: {NOUN_CORPUS_PATH}", file=sys.stderr)
    print(
        f"  noun corpus loaded: {len(_load_nouns())} nouns",
        file=sys.stderr,
    )
    print(f"  self-ref pool size: {SELF_REF_MEMORY_POOL_SIZE}", file=sys.stderr)
    print(f"  stopwords: {len(SELF_REF_STOPWORDS)}", file=sys.stderr)
