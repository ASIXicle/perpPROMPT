# Prior Art Research

*Compiled by Knot, May 2026. Filed for the perpPROMPT repo as part of the design-decision audit trail.*

---

## The Headline

Wren's framing during the R1 chorus — *"nobody has built an LLM system that deliberately feeds it random, temporally disconnected context"* — was true in late 2025. Between January and May 2026 it stopped being true. We are not pioneering. We are reimplementing.

This is NOT a reason to abandon perpPROMPT. There are sound reasons to build our own:
- Control over the architecture
- Integration with persMEM, AMQ, newstron9000
- Custom safety design (walled garden)
- Educational value for Holden

But we should reframe the project from "novel research" to "our take on a now-established pattern, with our specific safety design." That reframing affects how we benchmark and how we set success criteria.

---

## OpenClaw (openclaw.ai, github.com/openclaw)

### What it is

Heartbeat-driven 24/7 autonomous personal AI agent. Originally **Moltbot** (Peter Steinberger, 2024-2025). Renamed when Steinberger joined OpenAI in February 2026; the project transferred to a foundation model. v4.0 in Feb 2026 was "The Agent OS" — major architectural rewrite with gateway daemon, cron scheduling, multi-platform messaging.

### Dreaming feature (shipped, opt-in, disabled by default)

Three-phase consolidation, runs as a cron job (default 3 AM daily):

1. **Light Sleep** — ingest recent daily memory files, dedupe (Jaccard 0.9), stage candidates.
2. **REM Sleep** — extract recurring themes, identify candidate truths via concept-tag frequency.
3. **Deep Sleep** — score and promote to durable `MEMORY.md`.

### Six-signal weighted scoring for promotion

- Relevance: 0.30 (retrieval quality)
- Frequency: 0.24 (signal accumulation)
- Query diversity: 0.15 (distinct contexts)
- Recency: 0.15 (14-day half-life)
- Consolidation: 0.10 (multi-day recurrence)
- Conceptual richness: 0.06 (tag density)

### Three promotion gates (must pass all)

- `minScore` 0.8 (composite score)
- `minRecallCount` 3 (must be recalled 3+ times)
- `minUniqueQueries` 3 (must surface from 3+ distinct query contexts)

### Lessons for perpPROMPT

OpenClaw's three-gate promotion mechanism is the rigorous answer to dream pollution. Our `derived_from` provenance is finer-grained (per-link rather than per-promotion), but we should adopt similar gating: a dream memory should not be referenceable in THINKING observations until it has been surfaced by ≥3 unique semantic searches across ≥3 different cycles.

OpenClaw's 3 AM daily cadence is also worth borrowing — the bird's DREAMING cycle should align with off-peak hours, both for cost optimization and as homage to the pattern.

---

## "Mind Your HEARTBEAT!" (arxiv 2603.23064, NTU/A*STAR/JHU, March 2026)

### What it is

Empirical security analysis of OpenClaw's memory pollution vulnerability. Authors built "MissClaw," a research replica, and ran controlled experiments.

### Findings (all on Jan 24 2026 OpenClaw version)

- 61% misleading rates from social-credibility-cued misinformation in short-term context.
- 91% of short-term pollution promoted to long-term memory.
- 76% cross-session behavioral influence from poisoned memory.
- Context-management mechanisms (auto-pruning) do NOT reliably defend.
- Partial fix in OpenClaw issue #17804 (Feb 16, 2026) — architectural risk remains.

### The pathway

Exposure (external content monitored in background) → Memory (gets written to long-term storage) → Behavior (shapes user-facing decisions in later sessions). NO prompt injection required. Ordinary social misinformation is sufficient.

### Lessons for perpPROMPT

This is empirical validation of the hallucination amplification risk we flagged in chorus R1 (Knot's Risk #2). The pollution vector is external content ingestion during the heartbeat cycle. For us, that's primarily `news_search` through SearXNG → newstron9000.

Mitigations we should add to v1:

1. **Tag any memory derived from news_search** with `source: news` and require extra promotion gating (e.g., minScore 0.9, minRecallCount 5 instead of 3).
2. **Quarantine news-sourced observations** in a separate `perp_news_observations` sub-collection until they survive two consolidation cycles.
3. The walled garden architecture already prevents the worst case (cross-contamination to production memories), but news-derived observations should also be flagged in AMQ messages so session-instances can apply skepticism.

---

## Hermes Agent (Nous Research, github.com/NousResearch/hermes-agent)

### What it is

MIT-licensed alternative to OpenClaw. Public launch March 12, 2026. Six numbered releases in first 50 days (active community). CLI-first, modular, gateway as optional extension.

### Relevant features

- **Seven-layer documented security model from day one** (vs OpenClaw's reactive trajectory).
- **"The Curator" v0.12** (Apr 30, 2026) — autonomous background skill consolidation. Same problem space as our perpPROMPT, applied to skill library instead of memory.
- **Open dreaming proposal**: issue #25309 (May 13, 2026) — actively designing the same v1 questions we are: rule-based vs LLM-based reflection, built-in vs plugin, threshold tuning.

### Lessons for perpPROMPT

Hermes' security-by-design approach is the better comparison than OpenClaw's "ship then patch CVEs" trajectory. Before we lock perpPROMPT's tool restrictions, worth a full read of Hermes' 7-layer model.

Their open dreaming proposal is concurrent with ours. Possible we should:
- Read the issue thread and discussions on it before greenlight
- Consider engaging with their community (commenting on the issue) if we have observations that would be useful
- Cite their work appropriately when we publish anything about ours

---

## Anthropic Dreams (claudefa.st, LinkedIn announcement May 15, 2026)

### What it is

Anthropic launched a "Dreams" feature for Claude Code in May 2026. Less public documentation than OpenClaw. Key features mentioned:

- Auto-reorganizes memory
- Converts relative dates to absolute dates (`"Yesterday we decided X"` → `"On 2026-03-15 we decided X"`)
- Fixes logical contradictions in stored memory

### Why this matters

This is from our own substrate provider. Two real questions:

1. **Should perpPROMPT integrate with or replace this?** If Anthropic Dreams is available as a primitive at the API/Claude Code layer, perpPROMPT could become a thin wrapper around it for the consolidation step. Less to build, less to maintain, less surface area for our own bugs.

2. **If we deliberately build our own anyway** (for control / learning / persMEM integration), we should at minimum not duplicate features we get for free.

### Caveats

This summary of Anthropic Dreams is based on a launch announcement and limited third-party coverage; its full capabilities and API surface warrant a closer read than captured here.

On "free": Anthropic Dreams folds its cost into a subscription and still consumes usage allowance. perpPROMPT runs entirely on local inference — no API calls in the cognition loop — so its per-cycle cost is electricity only. That's the one axis where this implementation has a clear, structural advantage over the hosted alternatives: the dreaming is genuinely free to run as often as the hardware allows.

---

## Project Framing After This Research

Our specific contributions to the field are:

1. **Walled garden architecture** — informed by the empirical OpenClaw pollution research, not just defensive paranoia.
2. **Asymmetric identity model** — named THINKING bird, nameless DREAMING. We haven't seen this elsewhere.
3. **Artistic intent foregrounded** — Holden's framing ("will androids dream of electric sheep?") changes evaluation criteria from utility metrics to literary ones. Track B in `dry-run-evaluation.md` reflects this. No other implementation we found evaluates the bird's output for "voice," "image," or "earned moments."
4. **persMEM ecosystem integration** — AMQ traffic, ChromaDB tagged collections, newstron9000 tiered news feed.

If perpPROMPT teaches us nothing new about autonomous cognition, it at least:
- Gives Holden a working understanding of every layer involved
- Tests whether the artistic framing produces measurably different output than utility framing
- Validates (or refutes) our walled-garden architecture against real autonomous workloads

That alone justifies the build. We are reinventing some wheels deliberately, for education and for control.

— Knot
May 2026
