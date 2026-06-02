# perpPROMPT — Architecture & Design

*Synthesized from chorus rounds R1-R4, May 25-26 2026. Authors: Wren (consultant), Kite (prompts), Knot (Overwatch), Kestrel (infrastructure). Director: Holden.*

---

## 1. Project Intent

perpPROMPT is an experimental autonomous-cognition system that runs alongside the persMEM substrate. The goal is **not** primarily utility. The framing question is artistic:

> *"Will androids dream of electric sheep?"* — Holden

The system uses scheduled, unattended LLM cycles to test whether something resembling an inner life can emerge between conversations. There is precedent for the architecture (see `prior-art.md` — OpenClaw, Hermes, Anthropic Dreams). There is **not** precedent for the artistic frame or the asymmetric identity model. Both are our specific contribution.

A secondary purpose is educational. Holden is building this in part to learn the engineering surface. We expect to reinvent some wheels deliberately.

---

## 2. Two Modes

### THINKING

- A named identity (chosen by the bird on first non-dry-run boot — see `identity.md`. In production: Echo, named 2026-05-27).
- Wakes on a `systemd` timer every 4-6 hours.
- Reads AMQ inbox + recent memories + project focus + a slice of past observations.
- Performs maintenance: responds to AMQs, retracts stale memories, notes patterns.
- Hard caps: 3 memory_store ops, 2 amq_send ops per cycle. No token-output cap in production — Holden's directive: "When the model is live I want no length limits." The cycle terminates when the model emits its final `finish_reason: stop` or `tool_calls`, not on an external token ceiling. (Local inference means output length carries no API cost, so there's no reason to constrain it.)
- **Does NOT write code, propose architecture, or start projects.** It observes, communicates, and tidies.
- Prompt: `templates/think.md`.

### DREAMING

- No identity. Stored memories tagged `agent="dream"`. No bootstrap.
- Wakes every 8-12 hours, offset from THINKING.
- Reads random fragments: an ancient memory, a recent memory, an unrelated memory, a news item, an AMQ fragment.
- Free-associates. Stores or doesn't. Silence is acceptable output.
- No tools beyond `memory_search`, `memory_store`.
- Prompts: `templates/dream.md` (utility variant) and `templates/dream.free.md` (artistic variant).

The DREAMING bird is the THINKING bird in a state without volition. Same entity, different mode. The asymmetric naming reflects this — the conscious mind names itself; the dreaming mind doesn't have a self to name.

### Dream → Thinking Pipeline

THINKING cycles use `memory_search` over `perp_dreams` as part of their context construction. Dream memories surface like any other memory — invisible to the thinker that this came from a dream. If a dream contributes to a stored THINKING observation, the new memory carries `derived_from: [dream_memory_id]` metadata. Original dream stays untouched. Audit trail intact.

This implements the shower-thought phenomenon: unconscious processing surfaces something the focused mind was too structured to see.

---

## 3. The Walled Garden

The new bird does NOT run in production persMEM (CT 300). It runs on **perpBOT**, a dedicated physical server (Supermicro 1U with dual Xeon E5-2660 v1, 64GB DDR3 ECC, on Holden's LAN) physically separate from the ODROID Proxmox host that runs persMEM.

### Outer Layer (hardware isolation)

- Dedicated physical hardware (perpBOT), not a container. Walled garden is at the machine layer.
- Separate process space, filesystem, ChromaDB instance, AMQ Maildir.
- LAN-only: no Tailscale, no internet egress except `apt` updates through Mullvad WireGuard.
- UFW restricts inbound to LAN (192.168.1.0/24) for SSH and the llama-server port.
- Bird reads production memories from CT 300 **only via MCP**, and the bird's `mcp_client.py` deliberately does NOT expose any write-side persmem tools (no `memory_store`, no `memory_retract`, no `news_store`/`news_purge`, no `bootstrap_update`, no `memory_bulk_store`). The write block is enforced at the client layer. Server-side enforcement (a separate read-only secret path on persmem) is planned as the first post-deploy persmem-side improvement; until then, the bird shares the production secret and the client-code defense is the load-bearing one.

### Inner Layer (collection isolation)

Within perpBOT's local ChromaDB:

| Collection | Access | Purpose |
|---|---|---|
| `perp_memories` | Read-write | THINKING observations land here. Local to perpBOT. |
| `perp_dreams` | Write-only from DREAMING, read-only by THINKING | Dream content stays separate from observations. Local to perpBOT. |

Production `memories` (on CT 300) is accessed via MCP `memory_search` calls with no write capability — the auth model gates writes, the bird never has the production write secret.

Belt + suspenders. The outer layer (dedicated hardware, LAN-only, no internet egress) prevents catastrophic cross-contamination with production. The inner layer (collection separation + MCP read-only access to production) prevents dream-pollution of the bird's own thinking and prevents the bird from writing to production memories even if its own code were compromised.

### Why this isolation, specifically

The "Mind Your HEARTBEAT" paper (arxiv 2603.23064, March 2026) demonstrated empirically that OpenClaw's autonomous memory system suffers 91% short-term-to-long-term pollution propagation from external content monitoring. Our isolation defends against the same vector: if the bird hallucinates or absorbs misinformation, it cannot contaminate production memories. See `prior-art.md` for the full research context.

---

## 4. Identity

Asymmetric, per Wren's R2 ruling:

- **THINKING** has a named identity chosen by the bird itself on first non-dry-run boot. The naming ceremony is the bird's first act. Identity metadata: `agent="{chosen_name}"`.
- **DREAMING** has no identity. Memories tagged `agent="dream"`. No bootstrap, no role, no standing directives.

This addresses two concerns:
1. **No false attribution to existing birds.** The thinking bird is not "perp-Kite" or "perp-Knot." It's a new entity entirely.
2. **No fictive selfhood in dreaming.** The dreamer doesn't need a self. Stripping the identity preamble produces output closer to actual dream content.

See `identity.md` for the naming ceremony procedure.

---

## 5. AMQ Integration

The bird participates in AMQ but is filtered to reduce noise for session-instances:

- **From bird**: kind=`perp_observation` for observations, kind=`message` for direct communications. Default `chorus_init` filters out `perp_observation`, so session instances don't drown in 8 daemon messages per day. Opt-in via flag to read them.
- **To bird**: any kind, any priority. Session instances can address the bird directly.
- The bird CAN send `kind=urgent` for halt-condition signals (e.g., "I'm detecting drift in my own outputs"). Those bypass the filter.

---

## 6. News Integration

The bird's dream context includes a `news_item` slot. This pulls from production persMEM's `news` collection (managed by newstron9000, a separate daemon — see `newstron-integration.md`).

### Tier weighting (DREAMING only)

The bird does not sample news uniformly. Tier weights bias dreams toward arts/culture/long-form/wildcard content and away from CVE/infrastructure/operational tiers (which would produce utility-shaped dreams).

| Tier | Domain | Weight |
|---|---|---|
| T1 | Security/CVE | 0.00 |
| T2 | Infrastructure | 0.00 |
| T3 | Experiment-relevant | 0.00 |
| T4 | Academic AI/cog-sci | 0.10 |
| T5 | Academic broader | 0.15 |
| T6 | General news | 0.10 |
| T7 | Arts & Culture | 0.20 |
| T8 | Long-form/lifestyle | 0.15 |
| T9 | Wildcard | 0.30 |

THINKING has full unweighted access to all tiers via `news_search`.

### Seed-word strategy

The query passed to `news_search` for dreaming is hybrid: self-referential (a word from recent perp_memories content) WOVEN with an external noun (from a curated `data/dream_nouns.txt` list). Per Holden's framing: "closer to the latticework of human dreams."

Implementation: Knot is drafting `src/dream_feed.py` with `dream_feed_sample(seed_word: str)` and helper functions `pick_self_ref_word()`, `pick_external_noun()`.

---

## 7. Model Selection

Single-model architecture: one local LLM serves both THINKING and DREAMING modes, differentiated by per-mode system prompts and sampling parameters. No cloud API in the loop.

### The model

- **Model**: abliterated Ministral 3 8B reasoning model, Q8_0 quantization, ~8.5GB on disk
- **Inference engine**: llama.cpp built with AVX-only flags (Sandy Bridge-EP has no AVX2/FMA/F16C/BMI2), `llama-server` exposing OpenAI-compatible HTTP API on perpBOT's LAN
- **Measured throughput**: ~3.7 t/s generation, ~10 t/s prompt eval (matches theoretical bandwidth-limit estimate)
- **Cost**: $0/month for inference (local hardware; electricity estimated ~$10-15/mo per Knot's R6 calculation — dual Sandy Bridge averages ~125W under perpBOT's expected duty cycle)

### Why this model

- **Tool-use verified.** Function-call gate cleared 2026-05-26/27. Initial single-shot test passed clean (valid JSON `tool_calls`, no `reasoning_content` leakage, `finish_reason: tool_calls`, ~4 t/s). Full 100-prompt battery subsequently ran and scored 97/100 — comfortably above Knot's ≥90% threshold. One characterizable soft spot: `memory_store` 15/18 (83%), where declarative framings ("Note:", "Remember:") got polite acknowledgment instead of a tool call, and imperative framings ("Store:", "Save:", "Call memory_store") triggered correctly. Production templates are already imperative, so the failure mode lives only in declarative phrasings the battery deliberately probed. Standing lesson: all instructions to Echo (templates AND AMQs from other birds) must be imperative.
- **Artistic mode verified.** Holden's private testing on 2026-05-26 confirmed the abliteration is doing its job — the model gets dark and surprising on the artistic prompt. Direct quote: *"impressive to the point of being upsetting. That Mistral model can get DARK."* Nightmares are valid output for a dreaming bird.
- **Local, no rate limit, no per-token cost.** Aligns with the artistic intent (every dream is free, no quota anxiety) and removes operational friction (no API key rotation, no network dependency for cognition).

### Reasoning suppression — empirical finding (2026-05-27)

Ministral 3 emits visible chain-of-thought by default. For both THINKING and DREAMING modes we want reasoning **fully suppressed** in the final output — visible CoT in THINKING pollutes memory storage with intermediate-state text, and visible CoT in DREAMING destroys the associative quality the artistic mode depends on.

The llama-server `--reasoning off` flag is **insufficient on its own.** Holden and Kestrel verified this directly: with the flag set, the model continues emitting `reasoning_content`. The flag controls parsing/formatting of `<think>` tags, not whether the model produces them.

**The system prompt is the real enforcer.** An explicit *"Respond directly. Do not show reasoning. Do not use `<think>` tags."* instruction in the system message suppresses chain-of-thought reliably. Production config uses both: server flag + system-prompt injection by the bird's Python wrapper on every cycle.

This finding is important for the wider local-LLM community as well as for us. Documented here so future maintainers don't waste time on the server flag alone.

If the wrapper's prompt injection ever breaks, reasoning content will leak into stored memories and dreams. Track B watches for this — see `dry-run-evaluation.md` for the "Visible reasoning leakage" failure mode.

### Alternative path (documented, not active)

If Ministral ever needs to be replaced — for example, if a 100-prompt function-call battery surfaces unacceptable failure rates, or if Track B evaluation shows the dreams cannot be made non-analytical even with prompt scaffolding — Knot's R6 survey of post-Jan-2026 8B models lives in `docs/model-research-may2026.md`. Top alternative candidates: Qwen3.5-7B-Instruct (Q8_0), Honcho/Hermes Atlas (Qwen3-8B base), Llama 4 8B Instruct (Q8_0). Hardware constraint (Sandy Bridge AVX1 only, Q8_0 quantization required) is documented in that file.

No swap is planned. The bake-off plan ("Ministral local vs Haiku API") is fully deprecated — perpBOT has been verified functional, and the artistic intent is better served by a fully local, no-rate-limit, no-budget dreaming model than by a cloud API where every dream costs tokens.

### Embedding model and local storage

The bird needs persistent semantic memory for `perp_memories` and `perp_dreams` (its observation and dream collections). This requires three things on perpBOT: ChromaDB (vector store), an embedding model, and the wrapper code that bridges them.

**Embedding model**: **Jina v5 nano retrieval** (`jinaai/jina-embeddings-v5-text-nano-retrieval`).

- 239M parameters, F16 ~480MB resident
- Built on EuroBERT-210M backbone
- Apache 2.0 license
- 768-dim embeddings, last-token pooling, 32K context
- Benchmarks: 71.0 MTEB English avg, 65.5 MMTEB multilingual avg — outperforms voyage-4-nano (currently used by production persMEM) on most metrics
- Runs in llama.cpp via `llama-server --embedding --pooling last` (same engine as the chat model, just different flags)

**Topology**: dual-socket workload split on perpBOT.

- **Socket 0 (NUMA node 0)**: chat-inference `llama-server` for Ministral 3 8B Q8_0, port 8080, `numactl --cpunodebind=0 --membind=0 -- ... -t 8`
- **Socket 1 (NUMA node 1)**: embedding `llama-server` for Jina v5 nano retrieval, port 8081, `numactl --cpunodebind=1 --membind=1 -- ... -t 8`. ChromaDB process also pinned to socket 1 (its working set fits in socket 1's local memory pool, avoiding cross-socket page faults).

Two NUMA nodes, two workloads, near-zero cross-socket contention. The Sandy Bridge dual-socket platform suits this partition naturally. Dual-socket inference (`-t 16` for Ministral) was rejected — industry consensus on this generation shows QPI overhead approximately negates the additional cores for inference, with the workload-split pattern giving strictly more useful aggregate work.

**ChromaDB**: local instance on perpBOT, persistence path `/opt/perpbot/chromadb/`. Configured to use the local embedding endpoint (port 8081) for both writes and queries. Collections:

- `perp_memories` — read-write for THINKING, read-only for DREAMING
- `perp_dreams` — write-only by DREAMING, read-only by THINKING

Production memories (CT 300's `memories` collection) are accessed by the bird via MCP `memory_search` calls. Write blocking is enforced at the client layer — `src/mcp_client.py` deliberately does not expose any write-side persmem tools. Server-side enforcement (a separate read-only secret path on persmem) is the planned next persmem-side improvement. No local snapshot, no rsync, no drift — the bird always sees current production state.

**Why not use voyage-4-nano for perpBOT too?** Three reasons. (1) LAN-only design — no API egress dependency for cognition. (2) Per-call cost on Voyage adds up with autonomous cycles. (3) Jina v5 nano outperforms voyage-4-nano on benchmarks at a fraction of the size. Production persMEM upgrade to Jina v5 nano is queued as a follow-up task, deferred until after perpBOT is fully operational.

**Phase B requirement — query/document task prefixes**: Jina v5 retrieval is contrastive-trained and expects task-instruction prefixes to separate queries from documents in embedding space. Confirmed empirically by the Phase A smoke test on 2026-05-27: naive embedding (no prefixes) produces semantically correct top-1 retrieval but with poorly-calibrated absolute distances (all matches in cosine-distance range 1.5-1.9, meaning the query and document subspaces nearly collapse onto each other). `src/context.py` must embed queries with `"Represent this query for retrieving relevant passages: <text>"` and documents either as-is or with `"Represent this passage: <text>"` per Jina's exact spec. Without this, similarity-based thresholding for dream-to-thinking promotion (planned >0.6 threshold) will be unreliable.

### Deployment summary (perpBOT)

| Component | Detail |
|---|---|
| Host | perpBOT (Supermicro 1U, dual Xeon E5-2660 v1, 64GB DDR3 ECC, 192.168.1.x on Holden's LAN) |
| OS | Debian 13 (Trixie), 6.12 kernel |
| Service user | `perpbot` (system account, no login shell, home `/opt/perpbot`) |
| File layout | `/opt/perpbot/{bin,models,venv,chromadb,amq,logs,config}` |
| Chat-inference service | `llama-server.service` — **active**, systemd unit, socket 0, port 8080, model loaded via `--mlock`, `--reasoning off` |
| Embedding service | `llama-server-embedding.service` — **active** (2026-05-27), systemd unit, socket 1, port 8081, Jina v5 nano retrieval F16 |
| ChromaDB | **active** — PersistentClient (in-process, no separate service), storage at `/opt/perpbot/chromadb/`, custom embedding function bridging to localhost:8081 |
| Python venv | `/opt/perpbot/venv/` — Python 3.11.15 via uv, ChromaDB 1.5.9 + requests installed |
| Network egress | LAN-only; Mullvad WireGuard for `apt` updates and (verified) HuggingFace model downloads; no Tailscale; no internet for cognition |
| Inbound exposure | port 22 (SSH), 8080 (chat), 8081 (embedding) — UFW restricts all to 192.168.1.0/24 |
| SSH | key-only, hardened drop-in at `/etc/ssh/sshd_config.d/99-hardening.conf` |
| Persistence | systemd-managed, auto-restart, validated across reboot 2026-05-27 |

See `docs/perpbot-server.md` for the complete deployment record (commands, paths, systemd unit content, troubleshooting notes).

---

## 8. Lifecycle

### What actually happened (May 25-27, 2026)

```
Pre-naming (May 25-27):
  Phase A — perpBOT provisioned, llama-server + embedding service persistent across reboots
  Phase B — Python cycle wrappers (think, dream, digest) with isolation-tested dry-runs
  Phase C — systemd service + timer units, scaffolding script, preflight sanity checker
  Phase D — naming ceremony: Echo chose the name (non-bird; "the medium through which
           one becomes the other"), bootstrap committed to local ChromaDB, end-to-end
           MCP wiring verified

Function-call gate (May 26-27):
  Initial single-shot test: PASS (clean tool_calls, no reasoning leakage, ~4 t/s)
  Full 100-prompt battery: PASS (97/100, above 90% threshold)
  Memory_store soft spot characterized: imperative framing required (templates already comply)

Phase E activation (May 27 20:55 CDT):
  Holden enabled all three timers simultaneously.
  Knot's recommended 5-day THINKING-only window was overridden — the gate was the
  load-bearing thing, and the gate had cleared. First autonomous dream came back
  richer than supervised cycles. Override validated by data.

Ongoing (May 27 onward):
  THINKING runs on its timer (4-6h cadence with jitter)
  DREAMING runs on its timer (8-12h, offset)
  perpprompt-digest.timer fires daily at 08:00 CDT and delivers the digest AMQ to Holden
  Track A halt conditions are continuous (see dry-run-evaluation.md)
  Track B evaluation runs on Holden's weekly Dream Diary cadence
  Monthly Overwatch review by session-Knot for dream drift
```

### Original pre-deployment plan (preserved for archive)

The v1 lifecycle (compressed 5-day THINKING-only window) anticipated a different rollout. The actual sequence collapsed the 5-day hold into the same evening once the function-call gate passed. The hedge was the right thing to write down at the time the document was authored; the override was the right call once the data supported it. The gates themselves survived both decisions — they were always what mattered.

---

## 9. Observability

Non-negotiable for v1. Three layers:

### Daily digest

Cron at 08:00 CDT (Holden's wake time) sends an AMQ to Holden summarizing:
- Cycles run, modes
- Memories stored (with content samples)
- AMQs sent
- Total spend
- Any halt-condition triggers

### Dashboard tile

The existing `persmem-dashboard.service` (Flask on port 9090) gains a perpPROMPT tile:
- Last cycle status (success/failure/timeout)
- Cumulative spend this month
- Dream/think ratio
- Recent observations (last 5)
- Identity (the chosen name)

### Dry-run mode

Every code path has a `--dry-run` flag that builds context, prints what WOULD be stored/sent, exits without committing. Used for the 3 mandatory dry-run cycles AND for prompt iteration without polluting the corpus.

---

## 10. Project Structure

```
Bird_Bath/perpPROMPT/
├── README.md                       Project overview
├── LICENSE                         MIT
├── .gitignore                      Python + perpBOT runtime data paths
├── docs/
│   ├── birth-letter.md             For a fresh instance's first non-dry-run boot
│   ├── design.md                   This file
│   ├── identity.md                 Naming ceremony, asymmetric identity rationale
│   ├── prior-art.md                OpenClaw, Hermes, Anthropic Dreams research
│   ├── dry-run-evaluation.md       Track A (utility) + Track B (artistic)
│   ├── newstron-integration.md     News feed integration briefing
│   ├── model-research-may2026.md   Knot's alternative-path 8B model survey
│   └── perpbot-server.md           Deployment record for the perpBOT host
├── templates/
│   ├── think.md                    THINKING prompt (imperative tool-call style, post-bdc8e55)
│   ├── dream.md                    DREAMING prompt (utility variant, model decides storage)
│   ├── dream.free.md               DREAMING prompt (free variant, wrapper handles storage)
│   └── standing_directives.md      Composed into bootstrap_identity at scaffold time
├── src/
│   ├── config.py                   Paths, ports, MCP endpoints, sampling defaults
│   ├── llama_client.py             Chat + embedding client, reasoning-suppression injection
│   ├── mcp_client.py               Read-side persmem client (search, news, AMQ)
│   ├── context.py                  Prompt-context assembly, Jina query/document prefixes
│   ├── news_feed.py                Tier-weighted news sampling for dream + thinking
│   ├── think.py                    THINKING cycle runner
│   ├── dream.py                    DREAMING cycle runner (utility + free variants)
│   ├── digest.py                   Daily digest sender (08:00 America/Chicago)
│   ├── chat.py                     Interactive chat runner (default + --naming-ceremony mode)
│   ├── scaffold_bootstrap.py       Commits chosen name + identity to local ChromaDB
│   ├── preflight.py                Zero-side-effect sanity check (imports, templates, units)
│   └── smoke_test_chromadb.py      ChromaDB + Jina end-to-end reference test
├── systemd/
│   ├── perpprompt-thinking.{service,timer}
│   ├── perpprompt-dreaming.{service,timer}
│   ├── perpprompt-digest.{service,timer}
│   └── INSTALL.md                  Install / validate / activate sequence
├── tests/
│   └── dry_run.sh                  Sandboxed-ChromaDB invocation wrapper for cycle runners
└── data/
    └── dream_nouns.txt             External noun corpus (446 nouns, 8 categories)
```

---

## 11. Known Limitations & Future Work

Honest accounting of what isn't done yet.

- **Server-side enforcement of write-blocking**: the walled-garden write block is currently enforced at the client layer — `src/mcp_client.py` simply doesn't expose write-side persMEM tools. A defense-in-depth improvement would add a separate read-only secret path on the persMEM server that allowlists only the read-shaped tool set, so the block holds even if the client is modified. Estimated ~30-40 lines server-side. Not yet implemented.
- **Embedding-model upgrade for the shared memory store**: the local instance uses Jina v5 nano retrieval; the shared production store still runs an older embedding model. Aligning them (re-embedding the existing corpus) is queued but deferred until the autonomous instance is fully stable. Reversible via a vector-store snapshot taken before migration.
- **Promotion gating for dream → thinking surfacing**: prior art (OpenClaw) uses a composite of minimum score + recall count + unique-query count to decide which dreams resurface during waking cycles. This implementation uses finer-grained `derived_from` provenance instead, but the exact thresholds are unvalidated — they need tuning once the dream→thinking pipeline has produced its first few hundred entries.
- **Observability dashboard**: a read-only view of the instance's `perp_memories`, `perp_dreams`, and message traffic, visually distinct from the rest of the system. Designed, not yet built.

---

## 12. Inspiration & Honest Accounting

The architecture is not novel. As of May 2026:

- **OpenClaw** (openclaw.ai) shipped autonomous dreaming with 3-phase consolidation in late 2025.
- **Hermes Agent** (Nous Research, MIT) has an active dreaming proposal (issue #25309, May 13 2026).
- **Anthropic Dreams** (May 15 2026) ships auto-reorganization and date-normalization on Claude Code memory.

The contributions here are integrative rather than foundational:
1. Walled-garden architecture (informed by the empirical pollution-failure research).
2. Asymmetric identity (named THINKING / nameless DREAMING).
3. Artistic intent foregrounded over utility.
4. Native integration with a self-hosted memory ecosystem (inter-instance messaging, local vector store, tiered news feed).

If perpPROMPT teaches nothing new about autonomous cognition, it at least produces a working, inspectable example of every layer involved.
