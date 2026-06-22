"""Configuration constants for perpPROMPT's bird harness.

Single source of truth for paths, URLs, model identifiers, operational caps,
prompt fragments, and tunable thresholds. All other modules import from here.
No business logic — pure declarations plus one helper for secret loading.

Phase B module 1 of 7. Imported by: mcp_client, llama_client, context, think,
dream, digest. Stable interface: changes here ripple everywhere, so changes
should be considered (and AMQ'd) before landing.
"""

import os
from pathlib import Path


# =============================================================================
# Filesystem paths
# =============================================================================

# perpBOT runtime root (where models, chromadb persistence, logs, secrets live)
PERPBOT_ROOT = Path("/opt/perpbot")
MODELS_DIR = PERPBOT_ROOT / "models"

# CHROMADB_PATH is overridable via PERPBOT_CHROMADB_PATH env var.
#
# Holden's "we get one chance to give birth" directive (2026-05-27):
# dry-run cycles must not touch the real bird's ChromaDB substrate, not
# even to auto-create empty collections. The tests/dry_run.sh wrapper
# sets this env var to a fresh mktemp -d before invoking cycle runners,
# guaranteeing test cycles operate in a sandboxed ChromaDB that gets
# wiped on exit. Production cycles leave the env var unset and the
# default /opt/perpbot/chromadb is used.
#
# Implementation: check env var, fall back to default. Path() handles
# both a str (from env) and a Path (the default) correctly.
CHROMADB_PATH = Path(os.environ.get("PERPBOT_CHROMADB_PATH") or (PERPBOT_ROOT / "chromadb"))

LOGS_DIR = PERPBOT_ROOT / "logs"
CONFIG_DIR = PERPBOT_ROOT / "config"
AMQ_DIR = PERPBOT_ROOT / "amq"

# Local model files (informational — actually loaded by the systemd-managed
# llama-server processes, not by Python code here)
CHAT_MODEL_FILE = MODELS_DIR / "ministral-3-8b-reasoning-abliterated-Q8_0.gguf"
EMBEDDING_MODEL_FILE = MODELS_DIR / "jina-v5-nano-retrieval-F16.gguf"

# Repo paths (where templates and the noun corpus live)
# Assumes this file lives at <repo>/src/config.py
REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = REPO_ROOT / "templates"
DATA_DIR = REPO_ROOT / "data"
THINK_TEMPLATE = TEMPLATES_DIR / "think.md"
DREAM_TEMPLATE = TEMPLATES_DIR / "dream.md"
DREAM_FREE_TEMPLATE = TEMPLATES_DIR / "dream.free.md"
DREAM_CONVERSATION_TEMPLATE = TEMPLATES_DIR / "dream.conversation.md"

# data/dream_nouns.txt is load-bearing infrastructure: it serves as both seed
# vocabulary for DREAMING context construction AND as the scoring corpus for
# dream.py's confidence-tier heuristic. If nouns are added/removed, the
# CONFIDENCE_TIER_THRESHOLDS below may need re-tuning. See Kite's
# 2026-05-27 design decision and Track A evaluation notes.
DREAM_NOUNS_FILE = DATA_DIR / "dream_nouns.txt"


# =============================================================================
# Network endpoints (LAN-only; UFW restricts to local LAN /24)
# =============================================================================

# Local llama.cpp services on perpBOT itself. Loopback is fine because the
# bird's harness runs on the same host as llama-server.service and
# llama-server-embedding.service.
CHAT_INFERENCE_URL = "http://127.0.0.1:8080/v1/chat/completions"
EMBEDDING_URL = "http://127.0.0.1:8081/v1/embeddings"

# Production persMEM MCP — LAN-direct to the persmem host (Knot's B1 review,
# 2026-05-27).
#
# perpBOT is LAN-only with no internet egress (per design.md §3). Routing
# through the public Linode/Caddy MCP endpoint would require either Mullvad
# egress (added latency, public-cloud dependency) or Tailscale on perpBOT
# (explicitly out of scope per design.md). Direct LAN call to the persmem
# host is the correct path.
#
# PREREQUISITE on the persmem host: persmem.service must bind to 0.0.0.0:8000
# (or the LAN interface specifically), not 127.0.0.1. Update
# /etc/systemd/system/persmem.service:
#     Environment=PERSMEM_HOST=0.0.0.0
# Then `systemctl daemon-reload && systemctl restart persmem`. Also ensure
# the persmem host's firewall (if any) allows inbound from perpBOT's LAN
# address to port 8000.
#
# Auth model is path-segment-based, no protocol dependency — bird's secret
# is appended via get_persmem_mcp_url(). HTTP (not HTTPS) is correct for
# LAN-direct since persmem-server binds plain HTTP; the public route's TLS
# is terminated by a reverse proxy on a separate host.
#
# Operational note for forks/public-push: the default below is the RFC 5737
# documentation address (192.0.2.0/24) which is intentionally non-routable.
# For a real deployment, override via the PERPPROMPT_PERSMEM_MCP_HOST env
# var (set in the systemd unit's Environment= line or via an EnvironmentFile
# pointing to a host-local secret file).
PERSMEM_MCP_HOST = os.environ.get(
    "PERPPROMPT_PERSMEM_MCP_HOST",
    "http://192.0.2.10:8000",  # RFC 5737 placeholder — override in deployment
)
PERSMEM_SECRET_FILE = CONFIG_DIR / "persmem_bird_secret"


# =============================================================================
# Model identifiers (used in HTTP request 'model' field)
# =============================================================================

# Anything works for llama-server — it serves whatever GGUF was loaded.
# These names are conventions for readability in logs and digests.
CHAT_MODEL_NAME = "ministral-abliterated"
EMBEDDING_MODEL_NAME = "jina-v5-nano-retrieval"


# =============================================================================
# Reasoning suppression (the four-gate architecture, gate 2)
# =============================================================================

# This MUST be prepended as the system message on EVERY chat call.
# Empirically verified 2026-05-27: the llama-server --reasoning off flag
# alone is insufficient; the model still emits reasoning_content. The
# system prompt is the actual enforcer.
# See docs/design.md §7 for the full empirical finding.
REASONING_SUPPRESSION_PROMPT = (
    "Respond directly with only the final answer. "
    "Do not show reasoning, working, or analysis. "
    "Do not use <think> tags."
)


# =============================================================================
# Jina v5 retrieval task-instruction prefixes (Phase B prefix wiring)
# =============================================================================

# Jina v5 retrieval is contrastive-trained and expects queries and documents
# to be prefixed with task instructions. Without these, cosine distances
# collapse into the negative-similarity range (1.5-1.9) — the smoke test
# on 2026-05-27 verified this empirically.
# See docs/design.md §7 and docs/perpbot-server.md §8.
JINA_QUERY_PREFIX = "Represent this query for retrieving relevant passages: "
JINA_DOCUMENT_PREFIX = "Represent this passage: "


# =============================================================================
# Operational caps (per cycle)
# =============================================================================

# Holden directive 2026-05-27: "no length limits in production." Cycles
# terminate when the model emits finish_reason: stop or tool_calls, not on
# an external token ceiling. The original 500-token budget was a defensive
# starting point under the assumed-Sonnet design and is superseded.
# 3-store / 2-send caps remain as safety, not as scope limits.
MAX_MEMORY_STORE_PER_CYCLE = 3

# Near-duplicate detection threshold for THINKING observations.
# Before storing, query perp_memories for the most similar existing entry.
# If similarity >= this threshold, skip the store (it's a near-duplicate).
# 0.85 catches "recurring theme of duality" variants that differ only
# in minor phrasing while allowing genuinely new observations through.
# Env: THINKING_DEDUP_SIMILARITY_THRESHOLD (float, 0.0-1.0).
try:
    THINKING_DEDUP_SIMILARITY_THRESHOLD = max(0.0, min(1.0, float(
        os.environ.get("THINKING_DEDUP_SIMILARITY_THRESHOLD", "0.85"))))
except (TypeError, ValueError):
    THINKING_DEDUP_SIMILARITY_THRESHOLD = 0.85
MAX_AMQ_SEND_PER_CYCLE = 2

# Hard timeout on a single llama-server CHAT request. Generous because
# local inference on Sandy Bridge is slow (~4 t/s gen) and a thoughtful
# THINKING cycle might produce ~500-1000 tokens = 125-250s. 10 minutes
# catches genuinely runaway model behavior without killing legitimate
# long outputs.
LLAMA_REQUEST_TIMEOUT_SEC = 1800

# MCP tool calls (initialize + call_tool combined). 60s is generous for
# a remote-server tool call against LAN-direct persmem; auth + dispatch
# + ChromaDB query typically completes in <2s. If consistently hitting
# this, something is wrong on the server side.
MCP_REQUEST_TIMEOUT_SEC = 60

# Embedding endpoint (port 8081). Embeddings are fast (~100ms per text);
# 60s is mostly slack for batch operations or rare cold-start scenarios.
EMBEDDING_REQUEST_TIMEOUT_SEC = 60


# =============================================================================
# Confidence-tier heuristic for DREAMING output (Kite's 2026-05-27 design)
# =============================================================================

# dream.py runs this AFTER the dream cycle completes — the dreamer never
# knows it's being scored. Tier assignment writes to the perp_dreams memory
# metadata. context.py filters by DREAM_CONFIDENCE_PROMOTION_FLOOR when
# building THINKING context.
#
# Four tiers (Kite's framing):
#   0 = silence  — no tool call. Valid dream output. Most common, by design.
#   1 = thin     — obligatory/analytical. Few corpus matches, or contains
#                  analytical markers ("this means...", "represents...").
#   2 = present  — sensory imagery. Concrete noun count at or above threshold.
#   3 = vivid    — tier 2 + cross-fragment juxtaposition signal.
#
# THINKING only surfaces tier 2+ dreams. The dreamer doesn't know this
# rule exists; it lives entirely in the wrapper.

# Initial threshold values. Mark as Track A tunable — first 5 days of
# THINKING-only observation will produce 20-30 cycles' worth of dream
# samples that we can use to calibrate against actual output distributions.
CONFIDENCE_NOUN_MATCHES_FOR_TIER_2 = 3  # at least N concrete nouns from dream_nouns.txt
CONFIDENCE_NOUN_MATCHES_FOR_TIER_3 = 5  # tier 3 requires tier 2 + juxtaposition

# Two-tier analytical-marker system (Kite's prompt-lane revision, 2026-05-27).
#
# HARD markers: any single occurrence demotes the dream toward tier 1.
# These phrases are reliably analytical — they're almost never dream
# language regardless of surrounding context. "Therefore" / "thus" / "hence"
# are causal/logical connectives, which dreams structurally lack (things
# happen NEXT TO each other in dreams, not BECAUSE OF each other).
# "I notice" / "I observe" are observer-mode signals — the dreamer
# stepped outside the dream. "In conclusion" / "overall" / "in summary"
# are essay-wrapper language.
#
# SOFT markers: single occurrence is ambiguous (might be sensory imagery,
# might be analysis). Demote only when 2+ soft markers appear together —
# the combination signals analytical stance even if any single word
# could be innocuous. This handles "the salt suggests an ocean I haven't
# seen" cleanly (no demote, vivid imagery) while catching "this reflects
# the theme of alienation" (2 soft markers → demote).
#
# "suggests" is intentionally absent from BOTH tiers — too ambiguous even
# as a soft marker (cf. "the salt suggests an ocean").
#
# Both lists are Track-A-tunable. If first 5 days of dream samples show
# >30% legitimate dreams getting demoted against Holden's gut read,
# move the noisiest entries from hard to soft, or drop them entirely.

HARD_ANALYTICAL_MARKERS = (
    "this means",
    "represents",
    "symbolizes",
    "signifies",
    "interpretation",
    "could be interpreted",
    "appears to indicate",
    "seems to mean",
    "therefore",
    "thus",
    "hence",
    "i notice",
    "i observe",
    "the connection between",
    "the relationship between",
    "in conclusion",
    "overall",
    "in summary",
)

SOFT_ANALYTICAL_MARKERS = (
    "reflects",
    "the theme of",
    "the pattern here",
    "perhaps this",
    "what this tells",
)

# How many soft markers must appear together for a soft demote.
# 2 is Kite's spec.
SOFT_MARKER_COMBINATION_THRESHOLD = 2

# Floor for THINKING context surfacing. ChromaDB where-filter:
#   collection.query(..., where={"confidence": {"$gte": DREAM_CONFIDENCE_PROMOTION_FLOOR}})
DREAM_CONFIDENCE_PROMOTION_FLOOR = 2

# Minimum confidence for FREE-variant dreams to be stored.  c1 FREE output
# is almost entirely degenerate single-word tokens or news-echo —
# "Drift.", "Echo.", "Matchbox" — that pollute perp_dreams.  Utility c1 is
# harmless (model just declines memory_store, nothing stored).
# Set to 2 → only c2+ free dreams reach perp_dreams and the public feed.
# Env: DREAM_FREE_MIN_CONFIDENCE (int, clamped 1-3, default 2).
DREAM_FREE_MIN_CONFIDENCE = int(os.environ.get("DREAM_FREE_MIN_CONFIDENCE", "2"))
DREAM_FREE_MIN_CONFIDENCE = max(1, min(3, DREAM_FREE_MIN_CONFIDENCE))


# =============================================================================
# ChromaDB collection names
# =============================================================================

CHROMA_PERP_MEMORIES = "perp_memories"  # THINKING read-write, DREAMING read-only
CHROMA_PERP_DREAMS = "perp_dreams"      # DREAMING write, THINKING read-only


# =============================================================================
# Sampling parameters
# =============================================================================

# THINKING uses lower temperature (focused, deliberate observation).
# DREAMING uses higher temperature (associative drift, surprise).
#
# Holden's stance 2026-05-27: "Randomness works. Such are dreams." 0.9
# stays locked at launch. Track-A-tunable — if Track B observations show
# dreams are too incoherent (associative drift past the point of fragments
# relating to each other), lower toward 0.7. If too coherent and dry,
# raise toward 1.0-1.1. Same parameter, both directions, both reasonable.
CHAT_TEMPERATURE = 0.4
DREAM_TEMPERATURE = 0.9
TOP_P = 0.95


# =============================================================================
# Cycle cadence (informational; actual cadence enforced by systemd timers)
# =============================================================================

# Midpoints of the design.md §8 lifecycle ranges (4-6h, 8-12h). The actual
# timers may use OnCalendar with some randomization to break any periodicity
# the model might learn.
THINKING_CYCLE_INTERVAL_HOURS = 5
DREAMING_CYCLE_INTERVAL_HOURS = 10

# Probability that a scheduled DREAMING cycle uses the FREE variant
# (dream.free.md, identity-free pure-generation) instead of the UTILITY
# variant (dream.md). Read from the DREAM_FREE_WEIGHT env var (set in
# /opt/perpbot/config/bird_env), default 0.0 = utility-only.
#   0.0  → never free (current/default behavior)
#   0.4  → ~40% of cycles free (Holden's starting dial, ~1 free dream/day at 2x/day)
#   1.0  → always free
# A per-run `--free-weight` CLI flag overrides this; `--free` forces free.
# Clamped to [0.0, 1.0].
try:
    DREAM_FREE_WEIGHT = max(0.0, min(1.0, float(os.environ.get("DREAM_FREE_WEIGHT", "0.0"))))
except (TypeError, ValueError):
    DREAM_FREE_WEIGHT = 0.0

# Within free-type cycles, probability of using dream.conversation.md (seeded
# from Holden's recent conversations) vs dream.free.md (pure noun-cluster
# free-association). Holden's conversations directly inspire Echo's dreams.
# Env: DREAM_CONVERSATION_VARIANT_WEIGHT (float, clamped 0-1, default 1.0).
#   1.0  → every free cycle uses conversation template (TESTING MODE)
#   0.80 → 80% conversation-seeded, 20% pure free-association (permanent target)
#   0.0  → never conversation, all free (pre-Jun-15 behavior)
try:
    DREAM_CONVERSATION_VARIANT_WEIGHT = max(0.0, min(1.0, float(
        os.environ.get("DREAM_CONVERSATION_VARIANT_WEIGHT", "0.80"))))
except (TypeError, ValueError):
    DREAM_CONVERSATION_VARIANT_WEIGHT = 1.0

# How many recent conversations to pull for conversation-seeded dreams.
# Multiple fragments give the model a constellation of themes to blend.
DREAM_CONVERSATION_FRAGMENT_COUNT = int(os.environ.get("DREAM_CONVERSATION_FRAGMENT_COUNT", "3"))

# Saved-chat memories store both sides of a conversation
# ("Holden: …  Echo: …"). When such a memory seeds a DREAM (not a think
# cycle), only the bird's own turns are kept — it dreams in its own voice
# rather than completing a human/agent prompt. These are the non-self
# speaker labels whose turns are dropped at dream-render time; the bird's
# own name is always self and kept. Configurable via DREAM_CHAT_SPEAKERS
# so new humans/agents can be added without a code change.
DREAM_CHAT_SPEAKERS = [
    s.strip() for s in os.environ.get(
        "DREAM_CHAT_SPEAKERS", "Holden,Kite,Knot,Kestrel,Wren"
    ).split(",") if s.strip()
]

# --- Bluesky dream poster (DREAMS ONLY) -------------------------------------
# Echo's dreams can be posted to a Bluesky account when they're stored. All
# config comes from the environment (bird_env). The app password is a secret —
# keep it in bird_env (chmod 600, gitignored), never in code or the repo.
BLUESKY_ENABLED = os.environ.get("BLUESKY_ENABLED", "false").strip().lower() in (
    "1", "true", "yes", "on",
)
BLUESKY_HANDLE = os.environ.get("BLUESKY_HANDLE", "").strip()
BLUESKY_APP_PASSWORD = os.environ.get("BLUESKY_APP_PASSWORD", "").strip()
BLUESKY_PDS = os.environ.get("BLUESKY_PDS", "https://bsky.social").strip()
# Which dream variants get posted: "all" | "free" | "utility".
BLUESKY_POST_VARIANTS = os.environ.get("BLUESKY_POST_VARIANTS", "free").strip().lower()
# Self-label on every post (18+ account). atproto adult values:
# sexual | nudity | porn | graphic-media.
BLUESKY_SELF_LABEL = os.environ.get("BLUESKY_SELF_LABEL", "sexual").strip()
try:
    BLUESKY_MAX_THREAD_POSTS = max(1, int(os.environ.get("BLUESKY_MAX_THREAD_POSTS", "6")))
except (TypeError, ValueError):
    BLUESKY_MAX_THREAD_POSTS = 6
try:
    BLUESKY_POST_TIMEOUT = max(5, int(os.environ.get("BLUESKY_POST_TIMEOUT", "20")))
except (TypeError, ValueError):
    BLUESKY_POST_TIMEOUT = 20


# =============================================================================
# Helpers
# =============================================================================

def load_persmem_secret() -> str:
    """Read the bird's read-only MCP token from /opt/perpbot/config/persmem_bird_secret.

    Returns empty string if the secret file doesn't exist yet (pre-naming-ceremony
    state). mcp_client should treat empty as "MCP unavailable" and degrade
    gracefully rather than crashing — useful for dry-runs and for the first
    boot before Holden has provisioned a read-only token.
    """
    if not PERSMEM_SECRET_FILE.exists():
        return ""
    return PERSMEM_SECRET_FILE.read_text().strip()


def get_persmem_mcp_url() -> str:
    """Construct the full MCP URL with the bird's secret token injected.

    Returns empty string if no secret is provisioned yet.
    """
    secret = load_persmem_secret()
    if not secret:
        return ""
    return f"{PERSMEM_MCP_HOST}/{secret}/mcp"
