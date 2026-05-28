# newstron9000 Integration

*Briefing by Knot, May 26 2026, in preparation for perpPROMPT wiring. Adapted for repo.*

---

## What newstron9000 Is

A daemon running in **CT 300** (production persMEM LXC) that pulls RSS/Atom feeds, dedupes, filters by keyword, and stores entries into the persMEM `news` ChromaDB collection via authenticated MCP calls.

Built April 2026 to give Claude instances current-world context past their training cutoff. Expanded May 25 2026 (during this session) from 3 tiers to 9 to support perpPROMPT's dream-feed corpus.

It is NOT a separate database. It writes to the same ChromaDB that holds `memories` and `bootstrap`, just into a dedicated `news` collection. Everything in persMEM is a ChromaDB collection.

---

## Architecture

```
RSS feeds (Atom/RSS XML)
   │
   ▼
/opt/newstron9000/src/fetcher.py
   ├── reads feeds.yaml for URL list + per-feed config (tier, keywords, optional UA)
   ├── feedparser parses each, dedupes via /opt/newstron9000/state/seen.json
   ├── filters per-feed keyword list (empty list = pass-all)
   └── calls MCPClient.news_store(...) for each new item
            │
            ▼
        persmem.service (CT 300)
            │
            └── stores in ChromaDB collection "news" with metadata
                {source, tier, url, keywords, item_date, stored_at}

Consumers (us, perpPROMPT bird) read via:
   persmem.news_search(query, top_k, tier=, since=) → semantic + tier filter
```

Three systemd units drive newstron9000:

| Unit | Cadence | Purpose |
|---|---|---|
| `newstron9000-security.timer` | hourly | T1 CVE feeds only (small, fresh) |
| `newstron9000-fetch.timer` | daily 11:00 CDT | All other tiers (T2-T9) |
| `newstron9000-purger.timer` | daily 11:30 CDT | Per-tier TTL purge (new May 25 2026) |

Daily not 6-hourly because the bigger feed list makes each run expensive (embedding is CPU-bound, ~5s per item on the LXC) and Holden doesn't want it competing with his other 11 containers for cores. Off-hours scheduling > CPU throttling.

---

## MCP Endpoint URL

Same as the chorus's: `https://<persmem-public-mcp-domain>/{secret_path}/mcp`

The secret path is the auth gate for the whole server. It's the `PERSMEM_SECRET_PATH` env var on `persmem.service`. Anyone with the URL can call any tool. **Treat the URL like a password.**

For perpPROMPT (running on perpBOT), two equivalent options:
- **Public route**: `https://<persmem-public-mcp-domain>/{secret}/mcp` through a reverse proxy. Works because perpBOT has internet egress for `apt` updates via Mullvad WireGuard, but better to keep this LAN-only.
- **LAN-internal (recommended)**: `http://{ct-300-lan-ip}:8000/{secret}/mcp`. One less network hop, no Caddy in the path, stays on Holden's home LAN. Avoids routing through Mullvad. This is the route the bird uses for `news_search` heartbeat calls and `memory_search` reads against production.

The bird ONLY needs `news_search` and `memory_search` from production persMEM. It does NOT call `news_store` (no auth secret, no need) or `memory_store` against production. All write operations happen against perpBOT's local ChromaDB (`perp_memories` and `perp_dreams` collections).

---

## The Four MCP Tools

### `news_search(query, top_k=5, tier=None, since=None)`

**Read.** Semantic search over the `news` collection. The bird's main entry point.

- `query`: natural language, gets embedded via voyage-4-nano (truncate_dim=1024)
- `top_k`: 1-20
- `tier`: optional int 1-9, filter to one tier
- `since`: ISO date string, post-query filter

Returns JSON: `{query, collection, filters, count, results: [{id, content, metadata, similarity}]}`

### `news_store(secret, content, url, tier, source, keywords, item_date)`

**Write.** Requires `NEWSTRON_SECRET` matching the server's env var. Newstron fetcher only. **Bird does not call this.** Documented here so the bird knows it exists and why it's restricted.

### `news_purge(max_age_days=12, dry_run=True, tier=None)`

**Admin.** Per-tier TTL is enabled via the `tier` parameter. The purger script (`/opt/newstron9000/src/purger.py`) calls it once per tier with the appropriate TTL.

### `memory_search` / `memory_store` (different from news tools)

These hit a DIFFERENT collection (`memories` by default, configurable). The bird uses these against **perpBOT's local ChromaDB** for its own observations and dreams (`perp_memories`, `perp_dreams`). **Don't confuse the two collections.**

---

## The 9-Tier Scheme

| Tier | Purpose | TTL | Dream feed weight |
|---|---|---|---|
| 1 | Security/operational (CVEs) | 12d | 0.00 — skip, CVE-shaped dreams are bad |
| 2 | Infrastructure | 12d | 0.00 — skip |
| 3 | Experiment-relevant (MCP, LLM tooling) | 12d | 0.00 — skip |
| 4 | Academic AI/cog-sci | 30d | 0.10 |
| 5 | Academic broader (philosophy, neuro, linguistics) | 30d | 0.15 |
| 6 | General news (AP, Reuters, BBC) | 14d | 0.10 |
| 7 | Arts & Culture (Paris Review, Hyperallergic, Pitchfork…) | 60d | 0.20 |
| 8 | Long-form / lifestyle (Atlantic, New Yorker, Aeon, Granta…) | 60d | 0.15 |
| 9 | Wildcard / weird (APOD, Marginalian, Atlas Obscura, Reddit /r/all, HN, obituaries…) | 90d | 0.30 |

**Dream-feed weights are enforced in perpPROMPT, not newstron.** The bird's wrapper script picks a tier per the weight table, then calls `news_search(tier=picked_tier, top_k=1, query=hybrid_seed)`.

THINKING mode keeps general `news_search` access across all tiers without weighting. The weighted distribution applies only to the dreaming bird's `news_item` slot.

---

## Corpus Steady State

Once the per-tier purger settles into its 12–90 day windows, expect on the order of 1,500–2,500 entries in flight at any time. The dream-rich slice is tiers 4 + 7 + 8 + 9 (academic, arts & culture, long-form, wildcard) — these carry the highest dream-feed weights and the longest TTLs, so they accumulate the deepest fragment pool for the dreaming instance to draw from. Tiers 1–3 (security, infrastructure, experiment-relevant) stay small and fresh by design: short TTLs, zero dream-feed weight, useful only to the waking THINKING cycle.

---

## Auth Model

- **`news_search` and `news_purge`** have NO secret. Anyone who can reach the MCP URL can call them. The MCP URL itself is secret (the path segment IS the auth).
- **`news_store`** additionally requires `NEWSTRON_SECRET` matching the server env var. Newstron fetcher reads this from `/home/newstron9000/.env`.

**perpPROMPT does NOT receive `NEWSTRON_SECRET`** — the bird is read-only against production news by construction. This preserves the "perpPROMPT cannot pollute production news" property at the auth layer, not just at the application layer.

---

## What perpPROMPT Needs to Build

Two pieces of work for `src/`:

### 1. MCP Client

Crib pattern from `/opt/newstron9000/src/mcp_client.py` (~100 lines). Handles the initialize handshake + SSE response parsing + session ID, exposes a generic `.call(tool, args)` method. The bird wrapper just needs:

```python
from .mcp_client import MCPClient  # adapted from newstron's
client = MCPClient(url=PERSMEM_MCP_URL)
result = client.call("news_search", {
    "query": seed,
    "top_k": 1,
    "tier": picked_tier,
})
```

No auth secret needed for read-only access. URL itself is the credential.

### 2. `dream_feed_sample()` Helper

Biased tier picker + news fetcher. Pseudocode (Knot is drafting the actual function):

```python
DREAM_FEED_WEIGHTS = {4: 0.10, 5: 0.15, 6: 0.10, 7: 0.20, 8: 0.15, 9: 0.30}
# T1-T3 deliberately omitted (weight 0)

def dream_feed_sample(seed_word: str) -> str | None:
    tier = random.choices(
        list(DREAM_FEED_WEIGHTS.keys()),
        weights=list(DREAM_FEED_WEIGHTS.values()),
    )[0]
    result = client.call("news_search", {
        "query": seed_word,
        "top_k": 1,
        "tier": tier,
    })
    if result.get("count", 0) > 0:
        return result["results"][0]["content"]
    return None  # let dream template handle empty
```

`seed_word` is hybrid (per Holden, May 25 2026): self-referential word from recent perp_memories + external noun from `data/dream_nouns.txt`, woven into a single query string. Knot is drafting the actual implementation with `pick_self_ref_word()` and `pick_external_noun()` helpers.

---

## Audit Trail (Changes Made During newstron Expansion)

For reference if anyone needs to verify the May 25 2026 changes:

**persmem server** (`/opt/persmem/server.py`):
- Line 239: tier validation bumped from `(1,2,3,4)` to range `1-9`
- Line 230 + 381-382: docstrings updated with new tier definitions
- Lines 441-456: `news_purge` signature gained `tier: Optional[int] = None` parameter, with where-clause filter

**newstron fetcher** (`/opt/newstron9000/src/fetcher.py`):
- Line 39: `MAX_ITEMS_PER_RUN` 100 → 500
- Lines 110-115: added per-feed `user_agent` override (gated feeds like Reddit need a browser UA)

**newstron config** (`/opt/newstron9000/src/feeds.yaml`):
- Added ~25 new feeds across T4-T9. Dropped gagosian (no RSS exists). Reddit and Poetry Daily got per-feed UA strings.

**systemd**:
- `newstron9000-fetch.timer` schedule: `00,06,12,18:05 UTC` → `11:00:00 America/Chicago` daily
- `newstron9000-fetch.service` `TimeoutStartSec`: 300 → 1800 (allows bootstrap)
- **New unit** `newstron9000-purger.service` — runs `/opt/newstron9000/src/purger.py`
- **New unit** `newstron9000-purger.timer` — fires daily 11:30 CDT, 30 min after fetch

**New file**:
- `/opt/newstron9000/src/purger.py` — per-tier TTL wrapper. Hardcoded TTL map at top. `--dry-run` flag for safe testing.

---

## Source Files (Reference)

If implementing perpPROMPT's MCP client and someone wants to read the originals:

- `/opt/newstron9000/src/fetcher.py` — feed loader + main loop
- `/opt/newstron9000/src/purger.py` — per-tier TTL
- `/opt/newstron9000/src/feeds.yaml` — feed config
- `/opt/newstron9000/src/mcp_client.py` — reference MCP client (copy for perpPROMPT bird)
- `/opt/persmem/server.py` lines 209-274 (news_store), 363-437 (news_search), 440-479 (news_purge)
- `/etc/systemd/system/newstron9000-*.{service,timer}` — the four systemd units

— Knot
May 26 2026
