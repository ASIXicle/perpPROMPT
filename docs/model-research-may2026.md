# Local 8B Tool-Caller Models — May 2026 Survey

*Authored by Knot, perpPROMPT chorus R6, 2026-05-26. Filed 2026-05-27 after the function-call gate passed on the incumbent Ministral 3 8B abliterated.*

---

## Why this document exists

During the chorus rounds on perpPROMPT's local-model architecture, Holden tasked Overwatch with research on recent (post-Jan 2026) 8B-class models known for tool-calling reliability. The use case: a locally-hosted reasoning-suppressed model running on perpBOT (dual Xeon E5-2660 v1, 64GB DDR3 ECC, Sandy Bridge AVX1 only) that must reliably emit valid MCP tool calls AND produce acceptable artistic dream output.

The incumbent (Ministral 3 8B Q8_0, abliterated) was eventually tested directly and **passed the function-call gate cleanly**. No model swap was needed. This document remains in the repo as documentation of the alternative path — useful if Ministral ever needs to be replaced.

## Hardware constraint

**Sandy Bridge has AVX1 only.** No AVX2, no BMI2, no FMA, no F16C. This blocks the newer IQ_-family quants and degrades K-family quants (Q4_K_M, Q5_K_M paths use AVX2-specific code). **Q8_0 is the safe quant format** for this hardware — older scalar/SSE-friendly format that doesn't require modern instructions. All candidates below assume Q8_0 builds.

## Shortlist (ranked by likely tool-call reliability on this hardware)

### 1. Qwen3.5-7B-Instruct (Q8_0)

- **Released:** March 2026
- **Why it ranks first:** Tool-use ecosystem includes Qwen-Agent with native MCP support. Family BFCL scores are strong across sizes (the larger Qwen3.5-122B-A10B variant tops several leaderboards; the 7B uses the same training recipe). Abliterated variants on HuggingFace (`huihui-ai/Qwen3.5-7B-Instruct-abliterated`).
- **Strongest single bet** if a replacement is ever needed.

### 2. Honcho / Hermes Atlas (Qwen3-8B base, Q8_0)

- **From:** Plastic Labs (built atop the Hermes ecosystem, not from Nous directly)
- **Tuned for:** memory tasks specifically
- **Headline number:** 86.9% on LoCoMo memory benchmark vs 69.6% for base Qwen3-8B and 80.0% for Claude 4 Sonnet
- **Best fit for perpBOT's memory-centric role**, IF the benchmark methodology checks out (Plastic Labs' own evaluation — worth verifying before relying on the number).
- Tool-calling pipeline built in.

### 3. Llama 4 8B Instruct (Q8_0)

- **Released:** Early 2026
- **Improvements:** Meta's improved tool-call training over Llama 3.3
- **Ecosystem:** Standard llama.cpp / ollama support. Abliterated variants exist.
- **Reliable middle option.** No surprises, broad community testing.

### 4. Hermes 4 8B (if released — verify on HuggingFace)

- **From:** Nous Research
- **Status as of May 2026:** Hermes 4 family has 70B and 405B public variants. An 8B may or may not exist by deployment time. Check `huggingface.co/NousResearch` directly.
- **Why it would rank high:** Strongest tool-call DNA in the open ecosystem (Hermes 2 Pro 8B was their function-calling specialist; the line has historically prioritized this).

### 5. Ministral 3 8B Q8_0 (incumbent baseline)

- **Already loaded on perpBOT.** Holden's confirmed dark/artistic mode works.
- **Function-call gate: PASSED** (tested 2026-05-26/27, Kestrel's verification).
- Even if a future test reveals tool-call weakness under load, this stays as the DREAMING-mode specialist where dark/artistic output is the value.

## Empirical update — reasoning suppression

During testing on perpBOT, Holden and Kestrel found that **the llama-server `--reasoning off` flag is insufficient** to suppress chain-of-thought emission. The flag controls parsing/formatting of `<think>` tags but the model continues to emit reasoning_content.

**System prompt is the real enforcer.** Explicit "Respond directly... Do not show reasoning" instruction in the system message suppresses the chain-of-thought reliably. Production config uses both: server flag + system prompt injection by the bird's wrapper on every cycle.

**Implication for this shortlist:** ranking is unchanged. All candidates above can be made to behave like non-reasoning models via prompt engineering, even when the underlying training includes reasoning behaviors. A "reasoning model" is not disqualifying for THINKING mode as long as the wrapper code properly suppresses CoT emission. Document this when adopting any candidate.

## Bake-off methodology (if a swap is ever needed)

If Ministral fails or a swap is desired:

1. Pick top 2 candidates from the shortlist (likely Qwen3.5-7B + Honcho-on-Qwen3-8B, given their tool-use specialization)
2. Function-call gate: 100 prompts spanning `memory_search`, `memory_store`, `amq_send`, `news_search`. Score on syntactic validity + semantic appropriateness. Threshold ≥90% to proceed.
3. Dream sniff test: 10 outputs from the artistic prompt. Subjective evaluation by Holden — does the model produce anything dream-like, or only structured analytical output?
4. If a winner emerges on function-calls but Ministral wins on dreams: **split deployment.** Two model loads on perpBOT (RAM permits at 64GB), Ministral for DREAMING, winner for THINKING. One llama-server per mode. No API costs.

## Sources

- Berkeley Function Calling Leaderboard (BFCL v3/v4) — `gorilla.cs.berkeley.edu`
- LoCoMo memory benchmark documentation — referenced by Hermes Atlas team
- Plastic Labs documentation — `hermesatlas.com`
- Qwen team release notes (March 2026)
- Various HuggingFace model cards verified live during research
- Nous Research model index — `huggingface.co/NousResearch`

— Knot
2026-05-26 (research), filed 2026-05-27
