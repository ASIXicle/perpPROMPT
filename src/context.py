"""Per-cycle prompt context assembly + memory storage primitives.

This module sits between the cycle runners (think.py / dream.py) and
everything below them: mcp_client (production reads + AMQ), llama_client
(embeddings, with Jina prefixes wired in), and ChromaDB (the bird's own
perp_memories and perp_dreams collections).

Two responsibilities:

1. CONTEXT BUILDING. `build_think_context()` and `build_dream_context()`
   return slot-filled dicts that the cycle runners use to render
   templates/think.md and templates/dream.md. All slot values come from
   live data — recent AMQs, recent memories, random fragments, a news
   pull — assembled fresh each cycle.

2. STORAGE PRIMITIVES. `store_observation()` and `store_dream()` write
   to the bird's local ChromaDB collections with consistent metadata
   shapes (agent, stored_at, memory_type, confidence for dreams). The
   cycle runners call these after the model emits tool_calls.

Embedding asymmetry handled here: Jina v5 retrieval expects different
task prefixes for queries vs documents. ChromaDB's embedding_function
is called on .add() (documents). Queries are pre-embedded externally
via llama_client.embed_query() and passed as query_embeddings, so we
control both prefixes correctly. See docs/design.md §7 Phase B
requirement.

Phase B module 4 of 7. Depends on: config, mcp_client, llama_client,
external (chromadb). Imported by: think, dream, digest.
"""

import logging
import random
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import chromadb
from chromadb import EmbeddingFunction, Documents, Embeddings

from . import config
from . import llama_client
from . import mcp_client


logger = logging.getLogger(__name__)


# =============================================================================
# Module-level lazy-init state
# =============================================================================
# Each cycle runs in a fresh process (systemd timer → service → exit), so the
# "cache" is really just per-cycle initialization. We avoid re-instantiating
# ChromaDB clients and re-reading the noun corpus on every helper call.

# _CHROMA_CLIENT holds the lazy-initialized chromadb client. We don't
# annotate the type because chromadb.PersistentClient is a factory
# function (returns a ClientAPI instance), not a class — so the
# union-syntax `chromadb.PersistentClient | None` raises at module
# evaluation. The actual return type is chromadb.api.ClientAPI but
# that's a semi-private import surface; the comment carries the intent.
_CHROMA_CLIENT = None
_PERP_MEMORIES = None
_PERP_DREAMS = None
_NOUNS_CACHE: list[str] | None = None
_NOUNS_BY_CATEGORY_CACHE: dict[str, list[str]] | None = None


# =============================================================================
# Embedding function for ChromaDB (document-side only)
# =============================================================================

class JinaDocumentEmbeddingFunction(EmbeddingFunction):
    """ChromaDB embedding function for stored documents.

    ChromaDB invokes this on collection.add() and (if used) on
    collection.query(query_texts=...). We always want DOCUMENT-side
    prefixing here — for queries we bypass ChromaDB's embedding entirely
    by pre-computing via llama_client.embed_query() and passing
    query_embeddings=[...] to .query() instead. See module docstring.
    """

    def __init__(self) -> None:
        # ChromaDB deprecation warning insists on an explicit __init__.
        pass

    def __call__(self, input: Documents) -> Embeddings:
        docs = list(input)
        try:
            return llama_client.embed_documents(docs)
        except Exception as e:
            # The embedding server rejects inputs longer than its context
            # window, which otherwise crashes collection.add() and every save
            # that depends on it (e.g. a long chat thread persisted via
            # chat_server._persist). Retry with truncated copies: ChromaDB
            # still stores the FULL document, so nothing is lost from the
            # retrievable text — only the vector is computed from the opening
            # ~380 tokens, which is enough to fix the item's semantic topic.
            # 1500 chars is safe even for a 512-token embedding context.
            logger.warning("embed_documents failed (%s); retrying truncated", e)
            return llama_client.embed_documents([d[:1500] for d in docs])


# =============================================================================
# Initialization helpers
# =============================================================================

def _get_chroma_client():
    """Return the lazy-initialized chromadb ClientAPI instance.

    Return type is chromadb's ClientAPI (factory output of PersistentClient),
    not annotated for the same reason as the global — see comment above.
    """
    global _CHROMA_CLIENT
    if _CHROMA_CLIENT is None:
        _CHROMA_CLIENT = chromadb.PersistentClient(path=str(config.CHROMADB_PATH))
    return _CHROMA_CLIENT


def _get_perp_memories():
    global _PERP_MEMORIES
    if _PERP_MEMORIES is None:
        _PERP_MEMORIES = _get_chroma_client().get_or_create_collection(
            name=config.CHROMA_PERP_MEMORIES,
            embedding_function=JinaDocumentEmbeddingFunction(),
        )
    return _PERP_MEMORIES


def _get_perp_dreams():
    global _PERP_DREAMS
    if _PERP_DREAMS is None:
        _PERP_DREAMS = _get_chroma_client().get_or_create_collection(
            name=config.CHROMA_PERP_DREAMS,
            embedding_function=JinaDocumentEmbeddingFunction(),
        )
    return _PERP_DREAMS


def _load_dream_nouns() -> list[str]:
    """Load and cache the noun corpus. Skips comments and blank lines."""
    global _NOUNS_CACHE
    if _NOUNS_CACHE is None:
        if not config.DREAM_NOUNS_FILE.exists():
            logger.error("dream_nouns.txt not found at %s", config.DREAM_NOUNS_FILE)
            _NOUNS_CACHE = []
        else:
            lines = config.DREAM_NOUNS_FILE.read_text().splitlines()
            _NOUNS_CACHE = [
                line.strip()
                for line in lines
                if line.strip() and not line.startswith("#")
            ]
        logger.debug("Loaded %d nouns from dream corpus", len(_NOUNS_CACHE))
    return _NOUNS_CACHE


def _load_dream_nouns_by_category() -> dict[str, list[str]]:
    """Load nouns grouped by their `# === CATEGORY ===` headers.

    The categorical structure is what makes the 4+1 cluster strategy
    work — 4 nouns from one category cohere into a scene, the +1
    foreign-element creates the creative tension. Without categories,
    we'd be back to a vocabulary dump (Kite's diagnosis 2026-05-28).

    The flat noun list used for scoring is independent of this — see
    `_load_dream_nouns()` above. Both parsers operate over the same
    source file, so adding nouns / categories propagates to both.
    """
    global _NOUNS_BY_CATEGORY_CACHE
    if _NOUNS_BY_CATEGORY_CACHE is None:
        result: dict[str, list[str]] = {}
        if not config.DREAM_NOUNS_FILE.exists():
            logger.error("dream_nouns.txt not found at %s", config.DREAM_NOUNS_FILE)
            _NOUNS_BY_CATEGORY_CACHE = result
            return _NOUNS_BY_CATEGORY_CACHE

        current_cat: str | None = None
        for raw in config.DREAM_NOUNS_FILE.read_text().splitlines():
            line = raw.strip()
            if not line:
                continue
            # Category headers: `# === NAME ===`. Other comments ignored.
            if line.startswith("#"):
                if "===" in line:
                    # Strip leading "#", whitespace, and surrounding "==="
                    inner = line.lstrip("#").strip()
                    inner = inner.strip("=").strip()
                    if inner:
                        current_cat = inner
                        result.setdefault(current_cat, [])
                continue
            # A noun. Belongs to the most recent category seen.
            if current_cat is None:
                # Nouns before any category header — bucket them under
                # an "uncategorized" key rather than dropping silently.
                current_cat = "uncategorized"
                result.setdefault(current_cat, [])
            result[current_cat].append(line)

        _NOUNS_BY_CATEGORY_CACHE = result
        logger.debug(
            "Loaded %d nouns across %d categories",
            sum(len(v) for v in result.values()),
            len(result),
        )
    return _NOUNS_BY_CATEGORY_CACHE


def _build_dream_seed_cluster(nouns_by_category: dict[str, list[str]]) -> tuple[str, list[str]]:
    """Build a 4+1 themed noun cluster for the `{dream_seeds}` slot.

    Strategy (Kite's spec, AMQ 20260528T012427-299594):
      - 4 nouns from one randomly-chosen "primary" category
      - 1 noun from a different "foreign" category — creates tension
      - All 5 shuffled so the intruder isn't always last

    Returns (cluster_string, all_seeds_list). The string is the rendered
    template value ("river, silt, current, shore, needle"); the list is
    the same nouns un-joined, for the side-channel `_seed_fragments`
    metadata.

    Falls back gracefully if the corpus is empty or has fewer than 2
    categories — the dream still runs, just without the cluster framing.
    """
    if not nouns_by_category:
        return "", []

    cats = list(nouns_by_category.keys())
    if len(cats) < 2:
        # Single-category fallback: just sample up to 5 nouns from it.
        only = nouns_by_category[cats[0]]
        n = min(5, len(only))
        seeds = random.sample(only, n) if n else []
        return ", ".join(seeds), seeds

    primary_cat = random.choice(cats)
    primary_pool = nouns_by_category[primary_cat]
    primary_nouns = random.sample(primary_pool, min(4, len(primary_pool)))

    other_cats = [c for c in cats if c != primary_cat]
    foreign_cat = random.choice(other_cats)
    foreign_pool = nouns_by_category[foreign_cat]
    if not foreign_pool:
        # Degenerate edge case: empty category. Skip the foreign noun.
        all_seeds = list(primary_nouns)
    else:
        foreign_noun = random.choice(foreign_pool)
        all_seeds = primary_nouns + [foreign_noun]

    random.shuffle(all_seeds)
    return ", ".join(all_seeds), all_seeds


# =============================================================================
# Search and retrieval (uses llama_client.embed_query for proper prefixes)
# =============================================================================

def search_memories(query_text: str, n_results: int = 5, where: dict | None = None) -> list[dict]:
    """Semantic search over perp_memories with proper Jina query prefix.

    Returns a list of result dicts: {id, content, metadata, distance}.
    Empty list if collection is empty or query fails.
    """
    return _semantic_query(_get_perp_memories(), query_text, n_results, where)


def search_dreams(query_text: str, n_results: int = 3, where: dict | None = None) -> list[dict]:
    """Semantic search over perp_dreams with proper Jina query prefix.

    Confidence floor is applied by default — only tier-2+ dreams surface
    to THINKING per Kite's interim-gating design. Pass where=None to
    override (e.g., the Dream Diary review wants all tiers).
    """
    if where is None:
        where = {"confidence": {"$gte": config.DREAM_CONFIDENCE_PROMOTION_FLOOR}}
    return _semantic_query(_get_perp_dreams(), query_text, n_results, where)


def _semantic_query(collection, query_text: str, n_results: int, where: dict | None) -> list[dict]:
    """Execute a semantic query with externally-computed query embedding."""
    try:
        query_embedding = llama_client.embed_query(query_text)
    except Exception as e:
        logger.error("embed_query failed for context retrieval: %s", e)
        return []

    if collection.count() == 0:
        return []

    try:
        results = collection.query(
            query_embeddings=[query_embedding],
            n_results=min(n_results, collection.count()),
            where=where,
        )
    except Exception as e:
        logger.error("ChromaDB query failed: %s", e)
        return []

    return _parse_chroma_results(results)


def _parse_chroma_results(results: dict) -> list[dict]:
    """Flatten ChromaDB's nested query response into a list of dicts.

    ChromaDB returns dict of lists-of-lists (one inner list per query). We
    only ever send one query at a time, so we always use index [0].
    """
    if not results or not results.get("ids") or not results["ids"][0]:
        return []

    ids = results["ids"][0]
    docs = results.get("documents", [[]])[0]
    metas = results.get("metadatas", [[]])[0]
    dists = results.get("distances", [[]])[0]

    out = []
    for i, doc_id in enumerate(ids):
        out.append({
            "id": doc_id,
            "content": docs[i] if i < len(docs) else "",
            "metadata": metas[i] if i < len(metas) else {},
            "distance": dists[i] if i < len(dists) else None,
        })
    return out


def get_recent_memories(n: int = 10) -> list[dict]:
    """Pull the N most-recent memories AND tier-2+ dreams, merged by time.

    Per Wren's design, dreams surface like any other memory — the model
    doesn't see a "this was a dream" marker. Both collections are queried,
    merged by stored_at, and the most recent N returned. Confidence-floor
    filter applies to dreams; observations have no filter.
    """
    obs_collection = _get_perp_memories()
    dream_collection = _get_perp_dreams()

    obs = _fetch_recent_from_collection(obs_collection, limit=n)
    dreams = _fetch_recent_from_collection(
        dream_collection,
        limit=n,
        where={"confidence": {"$gte": config.DREAM_CONFIDENCE_PROMOTION_FLOOR}},
    )

    combined = obs + dreams
    combined.sort(
        key=lambda m: m["metadata"].get("stored_at", ""),
        reverse=True,
    )
    return combined[:n]


def _fetch_recent_from_collection(collection, limit: int, where: dict | None = None) -> list[dict]:
    """Get the N most-recent entries from a collection (by stored_at metadata).

    ChromaDB doesn't natively sort by metadata, so we .get() everything
    matching the where-filter, then sort in Python. Acceptable while
    collections stay small (<10k entries); revisit if perp_memories grows
    into the tens of thousands.
    """
    if collection.count() == 0:
        return []

    try:
        result = collection.get(where=where) if where else collection.get()
    except Exception as e:
        logger.error("collection.get failed: %s", e)
        return []

    ids = result.get("ids", [])
    docs = result.get("documents", [])
    metas = result.get("metadatas", [])

    entries = []
    for i, doc_id in enumerate(ids):
        entries.append({
            "id": doc_id,
            "content": docs[i] if i < len(docs) else "",
            "metadata": metas[i] if i < len(metas) else {},
            "distance": None,
        })

    entries.sort(
        key=lambda m: m["metadata"].get("stored_at", ""),
        reverse=True,
    )
    return entries[:limit]


def get_random_memory(collection_name: str = "perp_memories", age_cutoff_days: int | None = None) -> dict | None:
    """Pull one random memory, optionally restricted to memories older than N days.

    Used by dream-context assembly: ancient_memory wants something old,
    random_memory wants anything, recent_memory wants something fresh.
    Returns None if the collection (or filtered subset) is empty.
    """
    if collection_name == "perp_dreams":
        collection = _get_perp_dreams()
    else:
        collection = _get_perp_memories()

    if collection.count() == 0:
        return None

    try:
        result = collection.get()
    except Exception as e:
        logger.error("collection.get failed: %s", e)
        return None

    ids = result.get("ids", [])
    docs = result.get("documents", [])
    metas = result.get("metadatas", [])

    candidates = []
    cutoff_iso = None
    if age_cutoff_days is not None:
        # Older-than cutoff: stored_at LESS than (now - N days)
        # We use a simple lexicographic compare on ISO strings since ISO
        # timestamps sort correctly when same-timezone.
        from datetime import timedelta
        cutoff = datetime.now(timezone.utc) - timedelta(days=age_cutoff_days)
        cutoff_iso = cutoff.isoformat()

    for i, doc_id in enumerate(ids):
        meta = metas[i] if i < len(metas) else {}
        stored_at = meta.get("stored_at", "")
        if cutoff_iso and stored_at >= cutoff_iso:
            continue  # skip "recent" entries when we want ancient ones
        candidates.append({
            "id": doc_id,
            "content": docs[i] if i < len(docs) else "",
            "metadata": meta,
            "distance": None,
        })

    if not candidates:
        return None
    return random.choice(candidates)


# =============================================================================
# Storage primitives
# =============================================================================

def store_observation(
    content: str,
    bird_name: str,
    derived_from: list[str] | None = None,
    extra_metadata: dict[str, Any] | None = None,
) -> str:
    """Store a memory in perp_memories. Returns the assigned ID.

    Standard metadata applied automatically:
      - agent: bird's name
      - stored_at: ISO 8601 UTC timestamp
      - memory_type: "observation"
    `derived_from` records dream IDs that informed this observation
    (Wren's audit-trail spec). Empty/None means "not derived from a dream."
    """
    memory_id = _generate_memory_id("obs")
    metadata: dict[str, Any] = {
        "agent": bird_name,
        "stored_at": datetime.now(timezone.utc).isoformat(),
        "memory_type": "observation",
    }
    if derived_from:
        metadata["derived_from"] = ",".join(derived_from)
    if extra_metadata:
        metadata.update(extra_metadata)

    _get_perp_memories().add(
        ids=[memory_id],
        documents=[content],
        metadatas=[metadata],
    )
    logger.info("Stored observation %s for %s (%d chars)", memory_id, bird_name, len(content))
    return memory_id


def store_dream(
    content: str,
    bird_name: str,
    confidence: int,
    seed_fragments: dict[str, str] | None = None,
    extra_metadata: dict[str, Any] | None = None,
) -> str:
    """Store a dream in perp_dreams with Kite's confidence metadata.

    `confidence` is 0-3 per the two-tier vocabulary-quality heuristic
    implemented in dream.py. The dreamer doesn't see this number —
    scoring happens after the fact, in the wrapper.

    `seed_fragments` records which slot values seeded this dream
    (ancient/recent/random memory IDs, news URL, AMQ message ID).
    Useful for the Dream Diary review later.
    """
    memory_id = _generate_memory_id("dream")
    metadata: dict[str, Any] = {
        "agent": bird_name,
        "stored_at": datetime.now(timezone.utc).isoformat(),
        "memory_type": "dream",
        "confidence": confidence,
    }
    if seed_fragments:
        # Flatten to scalar metadata (ChromaDB doesn't accept nested dicts)
        for slot, value in seed_fragments.items():
            metadata[f"seed_{slot}"] = value
    if extra_metadata:
        metadata.update(extra_metadata)

    _get_perp_dreams().add(
        ids=[memory_id],
        documents=[content],
        metadatas=[metadata],
    )
    logger.info(
        "Stored dream %s for %s (confidence=%d, %d chars)",
        memory_id, bird_name, confidence, len(content),
    )
    return memory_id


def _generate_memory_id(prefix: str) -> str:
    """Generate a unique memory ID. Format: <prefix>_<iso-timestamp>_<hex>."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    suffix = secrets.token_hex(4)
    return f"{prefix}_{ts}_{suffix}"


# =============================================================================
# Bootstrap identity + project focus (read from perp_memories)
# =============================================================================

def get_bootstrap_identity(bird_name: str) -> str:
    """Pull the bird's identity entry from perp_memories.

    Stored during naming ceremony as memory_type="bootstrap_identity"
    with agent=<bird_name>. Returns a placeholder if not yet provisioned
    (pre-naming-ceremony state).
    """
    collection = _get_perp_memories()
    if collection.count() == 0:
        return f"[{bird_name}: bootstrap_identity not yet stored — first cycle?]"

    try:
        result = collection.get(where={
            "$and": [
                {"agent": bird_name},
                {"memory_type": "bootstrap_identity"},
            ]
        })
    except Exception as e:
        logger.error("bootstrap_identity lookup failed: %s", e)
        return f"[{bird_name}: bootstrap_identity lookup error]"

    docs = result.get("documents", [])
    if not docs:
        return f"[{bird_name}: bootstrap_identity not yet stored — first cycle?]"
    return docs[0]


def get_project_focus() -> str:
    """Pull the current project_focus entry from perp_memories.

    Stored as memory_type="project_focus", project="perpprompt".
    Falls back to a placeholder if not yet set.
    """
    collection = _get_perp_memories()
    if collection.count() == 0:
        return "[project_focus not yet stored]"

    try:
        result = collection.get(where={"memory_type": "project_focus"})
    except Exception as e:
        logger.error("project_focus lookup failed: %s", e)
        return "[project_focus lookup error]"

    docs = result.get("documents", [])
    metas = result.get("metadatas", [])
    if not docs:
        return "[project_focus not yet stored]"

    # If multiple focus entries exist, take the most recent
    entries = list(zip(docs, metas))
    entries.sort(key=lambda p: p[1].get("stored_at", ""), reverse=True)
    return entries[0][0]


# =============================================================================
# AMQ helpers
# =============================================================================

def get_unread_amq(bird_name: str, limit: int = 10) -> list[dict]:
    """Pull unread AMQ message HEADERS for the bird (no body, no marking).

    Per Knot's A2 review (2026-05-27): the previous version called
    amq_read on every header, which atomically marks-as-read. If the
    cycle crashed mid-loop, messages were marked read but never
    responded to. Worse, the docstring claimed the caller marks read
    but the code did it automatically — contract violation.

    The MCP server has no body-without-mark operation (amq_read is
    atomic). So we surface headers only here and expose amq_read as a
    TOOL the model calls explicitly. The model sees subjects + senders
    in the rendered context, decides which to read in full, and the
    marking happens organically as a side effect of the model's
    explicit choice. This restores caller-controlled mark semantics
    without server-side changes.

    Returns a list of header dicts: {id, from, subject, kind, priority, created}.
    Empty list if MCP is unavailable or inbox is empty.
    """
    check_result = mcp_client.amq_check(bird_name)
    headers = check_result.get("messages", []) if check_result else []
    return headers[:limit]


def format_amq_for_context(messages: list[dict]) -> str:
    """Format AMQ headers for the {last_N_amq} slot.

    Renders subjects + sender + kind + msg_id with an instruction
    pointing the model at the amq_read tool. Body content is NOT
    inlined — the model calls amq_read on the IDs it wants to handle.
    This is Knot's A2 fix shape: caller-controlled marking via explicit
    tool invocation.
    """
    if not messages:
        return "(no unread messages)"

    lines = [f"You have {len(messages)} unread message(s). Call `amq_read` with the corresponding id to read each in full.", ""]
    for msg in messages:
        sender = msg.get("from", "?")
        subject = msg.get("subject", "(no subject)")
        kind = msg.get("kind", "message")
        priority = msg.get("priority", "normal")
        msg_id = msg.get("id", "?")
        priority_mark = " [URGENT]" if priority == "urgent" else ""
        lines.append(f"- from **{sender}** ({kind}){priority_mark}: \"{subject}\"  `id={msg_id}`")
    return "\n".join(lines)


def get_random_amq_fragment(bird_name: str) -> str:
    """Pull one random AMQ subject as a dream fragment.

    Per Knot's A1 review (2026-05-27): the previous version called
    amq_read on a random message body, which atomically marks-as-read.
    Dream cycles silently consumed inbox messages — a critical bug for
    THINKING cycles that ran AFTER a DREAMING cycle and expected to
    see those unread messages.

    Fix: use only the subject line from amq_check headers. No body
    pull, no read side effect. Subject lines are surprisingly evocative
    in chorus traffic — they often capture the essence of a message in
    a short phrase, which is exactly what a dream fragment wants.

    Returns one subject or "(silence)" if no messages available.
    """
    check_result = mcp_client.amq_check(bird_name)
    headers = check_result.get("messages", []) if check_result else []
    if not headers:
        return "(silence)"

    chosen = random.choice(headers)
    subject = chosen.get("subject", "").strip()
    return subject or "(silence)"


# =============================================================================
# Context assembly (top-level for cycle runners)
# =============================================================================

def build_think_context(bird_name: str) -> dict:
    """Assemble all slot values for templates/think.md.

    Returns dict with keys: agent_name, bootstrap_identity, project_focus,
    last_N_amq, last_M_memories, date. The cycle runner does the
    template.format() call to apply these.

    Post-A2 fix (2026-05-27): unread AMQs are surfaced as HEADERS in the
    rendered context, not full bodies. The model calls the amq_read TOOL
    with specific message IDs to read bodies, which restores explicit
    caller-controlled marking semantics. No side-channel _amq_messages
    needed anymore — the model owns the decision of what to read.

    `date` is today's UTC date as YYYY-MM-DD. think.md references it in
    the quiet-marker instruction ("store 'reviewed {date}, quiet'") —
    template.format() requires the slot to be populated or it raises
    KeyError at render time.
    """
    unread_headers = get_unread_amq(bird_name, limit=10)
    recent_memories = get_recent_memories(n=10)

    return {
        "agent_name": bird_name,
        "bootstrap_identity": get_bootstrap_identity(bird_name),
        "project_focus": get_project_focus(),
        "last_N_amq": format_amq_for_context(unread_headers),
        "last_M_memories": _format_memory_list(recent_memories),
        "date": datetime.now(timezone.utc).date().isoformat(),
    }


def build_dream_context(bird_name: str) -> dict:
    """Assemble all slot values for templates/dream.md.

    Pulls one fragment for each slot. Seed noun is chosen randomly from
    dream_nouns.txt and used to query news. The `{dream_seeds}` slot is
    populated with a 4+1 themed cluster (see `_build_dream_seed_cluster`
    — Kite's diagnosis 2026-05-28: previously the nouns were invisible
    to the model, used only as news_search seeds, never presented as
    vocabulary). Memory slots come from timeframe-restricted random
    sampling of perp_memories.
    """
    nouns = _load_dream_nouns()
    seed_noun = random.choice(nouns) if nouns else ""

    nouns_by_cat = _load_dream_nouns_by_category()
    dream_seeds_str, dream_seeds_list = _build_dream_seed_cluster(nouns_by_cat)

    # Memory fragments. For dreams, saved-chat memories are reduced to the
    # bird's OWN turns (drop human/other-agent turns) so the bird dreams in
    # its own voice instead of completing a prompt. _dreamify_memory leaves
    # plain observations untouched. Storage + the think path keep full chats.
    ancient = _dreamify_memory(get_random_memory("perp_memories", age_cutoff_days=14), bird_name)
    recent = _dreamify_memory(_get_most_recent_memory("perp_memories"), bird_name)
    randmem = _dreamify_memory(get_random_memory("perp_memories"), bird_name)
    news_item_text = get_dream_news_fragment(seed_noun)
    amq_fragment = get_random_amq_fragment(bird_name)

    return {
        "agent_name": bird_name,
        "ancient_memory": _format_single_memory(ancient, fallback="(an old silence)"),
        "recent_memory": _format_single_memory(recent, fallback="(today held nothing notable)"),
        "random_memory": _format_single_memory(randmem, fallback="(nothing surfaced)"),
        "news_item": news_item_text or "(no news fragment available)",
        "amq_fragment": amq_fragment,
        "dream_seeds": dream_seeds_str or "(silence)",
        # Side-channel — slot seed IDs/keys so dream.py can write derived_from
        "_seed_fragments": {
            "ancient_id": ancient["id"] if ancient else "",
            "recent_id": recent["id"] if recent else "",
            "random_id": randmem["id"] if randmem else "",
            "seed_noun": seed_noun,
            "dream_seeds": dream_seeds_list,
        },
    }


def build_dream_free_context(bird_name: str) -> dict:
    """Assemble slot values for templates/dream.free.md (artistic variant).

    Same fragments as dream.md but without the identity slot. dream.free.md
    omits {agent_name} per Kite's asymmetric-identity design.
    """
    full = build_dream_context(bird_name)
    # dream.free.md template has no {agent_name}; remove from dict
    # to avoid format() KeyError leak. The other slots are identical.
    full.pop("agent_name", None)
    return full


# =============================================================================
# Slot-format helpers
# =============================================================================

def _format_memory_list(memories: list[dict]) -> str:
    """Format a list of memory dicts for the {last_M_memories} slot.

    No marker distinguishing observations from dreams — Wren's design.
    The thinker sees one pool.
    """
    if not memories:
        return "(no memories yet)"

    lines = []
    for m in memories:
        ts = m["metadata"].get("stored_at", "?")
        content = m["content"]
        lines.append(f"[{ts}] {content}")
    return "\n\n".join(lines)


def _format_single_memory(memory: dict | None, fallback: str = "(nothing)") -> str:
    """Format one memory for a dream-fragment slot.

    Single-line or short-paragraph. No timestamps — dreams are timeless.
    """
    if memory is None:
        return fallback
    return memory["content"]


def _chat_speaker_re(self_name: str) -> re.Pattern:
    """Build a turn-boundary matcher from the KNOWN speaker set.

    Matching only known names (config.DREAM_CHAT_SPEAKERS + the bird's own
    name) means colons inside the bird's own prose ("Note: …") are never
    mistaken for a speaker label, so the bird's text is never chopped.
    """
    names = [self_name] + [
        n for n in config.DREAM_CHAT_SPEAKERS if n.lower() != self_name.lower()
    ]
    alt = "|".join(re.escape(n) for n in names if n)
    return re.compile(r"(?:^|\s)(" + alt + r"):\s", re.IGNORECASE)


def strip_to_self_turns(content: str, self_name: str) -> str:
    """DREAM-ONLY: from a saved-chat memory, keep only the bird's own turns.

    Saved chats are stored whole ("[Chat conversation …] Holden: … Echo: …")
    so THINKING and audit keep full context. But when such a memory seeds a
    DREAM, the human/other-agent turns carry instruction grammar the dreamer
    obeys instead of drifting from (the "Describe it." → dutiful-completion
    failure). So for dreams we drop every non-self turn and the chat header,
    leaving the bird's own words — it dreams in its own voice. Only the
    "[Chat conversation" format is touched; plain observations pass through.

    Returns the concatenated self-turns, or "" if the chat held none.
    """
    if not content or "[Chat conversation" not in content:
        return content
    matches = list(_chat_speaker_re(self_name).finditer(content))
    if not matches:
        return content
    kept = []
    for i, m in enumerate(matches):
        seg_start = m.end()
        seg_end = matches[i + 1].start() if i + 1 < len(matches) else len(content)
        if m.group(1).lower() == self_name.lower():
            kept.append(content[seg_start:seg_end].strip())
    return " ".join(s for s in kept if s).strip()


def _dreamify_memory(memory: dict | None, self_name: str) -> dict | None:
    """Apply strip_to_self_turns to a memory's content for dream seeding.

    Returns None when a chat memory strips to nothing (no self-turns) so the
    slot falls back gracefully instead of seeding an empty fragment. Non-chat
    memories and memories with self-turns are returned (content rewritten).
    """
    if not memory:
        return memory
    content = memory.get("content", "")
    stripped = strip_to_self_turns(content, self_name)
    if stripped == content:
        return memory          # not a chat, or nothing changed
    if not stripped:
        return None            # chat had no self-turns → fall back
    out = dict(memory)
    out["content"] = stripped
    return out


def _get_most_recent_memory(collection_name: str) -> dict | None:
    """Pull the single most-recent memory by stored_at (no semantic filter)."""
    if collection_name == "perp_dreams":
        collection = _get_perp_dreams()
    else:
        collection = _get_perp_memories()

    entries = _fetch_recent_from_collection(collection, limit=1)
    return entries[0] if entries else None


# =============================================================================
# News fragments
# =============================================================================

def get_dream_news_fragment(seed_noun: str) -> str:
    """Pull a news fragment seeded by a random dream-corpus noun.

    Per Kite's dream-feed design: search news with a noun from the
    dream_nouns corpus, take the first result (top_k=1). If MCP is down
    or no results, return a placeholder. The bird won't know whether the
    fragment came from news vs silence — both are valid dream input.
    """
    if not seed_noun:
        return "(no news fragment available)"
    results = mcp_client.news_search(query=seed_noun, top_k=1)
    if not results:
        return "(no news fragment available)"
    item = results[0]
    content = item.get("content", "").strip()
    # Take the first sentence or two — dreams don't read full articles
    sentences = content.split(". ")
    return ". ".join(sentences[:2]) + ("." if not content.endswith(".") else "")


def get_thinking_news_fragment(seed_keyword: str) -> str:
    """Pull one news article relevant to the bird's current focus.

    Used in think.md step 2 (mandatory news read). Differs from dream
    variant: seeded by focus keyword rather than random noun, returns
    fuller content (not just first sentences), and signals to the bird
    that this was a deliberate pull, not background drift.
    """
    if not seed_keyword:
        return "(no news fragment available)"
    results = mcp_client.news_search(query=seed_keyword, top_k=1)
    if not results:
        return "(no news fragment available)"
    item = results[0]
    return item.get("content", "(no news fragment available)").strip()
