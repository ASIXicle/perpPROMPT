# perpBOT Server — Deployment Record

This document captures the actual deployment of the perpBOT host: hardware, OS, services, file layout, and commands. Source of truth for operational details, kept in sync with the live system.

For the *why* behind these choices, see `design.md`. This file is the *how*.

---

## 1. Hardware

| Component | Spec |
|---|---|
| Form factor | Supermicro 1U server |
| Motherboard | Supermicro X9DRD-iF /LF (or similar X9-series) |
| CPU | 2× Intel Xeon E5-2660 v1 (Sandy Bridge-EP, 8C/16T each, 16C/32T total, 2.2 GHz base) |
| Memory | 64 GB DDR3-1333 ECC, 16 slots populated (quad-channel per socket, ~29 GB/s bandwidth each) |
| Storage | Samsung 850 EVO 500GB SATA SSD, AHCI mode |
| Network | Onboard dedicated LAN ports + dedicated IPMI port |
| Power draw | ~120W idle, ~250-300W under inference load. ~98% idle duty cycle expected → ~$10-15/mo at TN electric rates |

**CPU instruction set constraint**: Sandy Bridge has AVX1, SSE/SSE2/SSE3/SSSE3/SSE4.1/SSE4.2, AES, POPCNT, PCLMULQDQ. **No AVX2, no FMA, no F16C, no BMI1/BMI2.** This dictates llama.cpp build flags and quant compatibility.

---

## 2. Operating System

- **Distribution**: Debian 13 (Trixie)
- **Kernel**: 6.12 (PREEMPT_DYNAMIC)
- **Hostname**: `perpBOT`
- **LAN IP**: 192.168.1.x (static, Holden's home LAN — actual octet redacted from public copies of this doc)

---

## 3. Users and access

| Username | UID range | Purpose | Shell | Sudo |
|---|---|---|---|---|
| `<admin-user>` | regular (1000+) | Holden's admin user | bash | passwordless via `/etc/sudoers.d/<admin-user>` |
| `perpbot` | system (988) | llama-server service user, future bird-cycle services | `/usr/sbin/nologin` | none |

`<admin-user>` is added to the `perpbot` group for read access into `/opt/perpbot/`.

SSH is **key-only** (no password auth). Hardening drop-in at `/etc/ssh/sshd_config.d/99-hardening.conf` enforces this.

---

## 4. Filesystem layout

```
/opt/perpbot/                                       (perpbot:perpbot 755)
├── bin/                                            llama-server binary + shared libs + helper scripts
│   ├── llama-server                                HTTPS-enabled rebuild (rebuilt 2026-05-27)
│   ├── lib*.so*                                    accompanying shared libraries
│   └── smoke_test_chromadb.py                      ChromaDB + Jina end-to-end smoke test
├── models/
│   ├── ministral-3-8b-reasoning-abliterated-Q8_0.gguf    ~8.5 GB (chat inference, socket 0)
│   └── jina-v5-nano-retrieval-F16.gguf                   ~480 MB (embeddings, socket 1)
├── venv/                                           Python 3.11.15 venv (uv-managed)
│   └── bin/python                                  invoke with `sudo -u perpbot /opt/perpbot/venv/bin/python ...`
├── chromadb/                                       local ChromaDB persistence (PersistentClient mode)
├── amq/                                            bird's AMQ maildir (planned, Phase C)
├── logs/                                           application logs
└── config/                                         configuration files (MCP secrets, etc.)
```

Permissions: `750` on subdirectories (perpbot user + perpbot group can read; "other" denied). The `<admin-user>` membership in `perpbot` group enables `ls`/`cat` for admin inspection without `sudo`.

---

## 5. Network

| Direction | Policy |
|---|---|
| Outbound general | Routed through Mullvad WireGuard (`wg-quick@mullvad.service`) for apt updates and incidental egress |
| Outbound LAN | Direct (192.168.1.0/24 stays on LAN interface, not tunneled) |
| Inbound | UFW default deny; explicit allow from `192.168.1.0/24` to ports 22 (SSH) and 8080 (llama-server) |
| Tailscale | **Not installed.** perpBOT is intentionally LAN-only — no remote access from outside Holden's home network |

Mullvad config at `/etc/wireguard/mullvad.conf` (chmod 600). `resolvconf` package required for wg-quick DNS handling.

---

## 6. llama.cpp build

Built from source at `/home/<admin-user>/llama.cpp/build/` (dev workspace; binaries copied to `/opt/perpbot/bin/` for the services to use).

Build flags critical to Sandy Bridge compatibility, plus HTTPS support:

```bash
sudo apt install -y libssl-dev    # required for LLAMA_OPENSSL=ON

cmake -B build \
  -DGGML_AVX=ON \
  -DGGML_AVX2=OFF \
  -DGGML_FMA=OFF \
  -DGGML_F16C=OFF \
  -DGGML_BMI2=OFF \
  -DGGML_NATIVE=OFF \
  -DLLAMA_OPENSSL=ON
cmake --build build --config Release -j16
```

`GGML_BMI2=OFF` is non-obvious — modern llama.cpp defaults to BMI2=ON (Haswell+ assumption). Building without disabling it produces an executable that crashes with `Illegal instruction` on Sandy Bridge.

`LLAMA_OPENSSL=ON` enables HTTPS support, which is required for the `-hf <repo>:<variant>` flag to auto-download models from HuggingFace. Without it, llama-server prints "HTTPS is not supported. Please rebuild..." when given an `-hf` argument. Local model files (`-m /path/to/file.gguf`) work regardless of this flag.

Recommended quant: **Q8_0** (older scalar-friendly format that doesn't require AVX2-only paths). IQ-family quants and some K-family quants may degrade or fail on AVX1-only. Embedding model uses **F16** (small enough that quantization isn't worth it; ~480 MB).

---

## 7. llama-server systemd service

Unit file at `/etc/systemd/system/llama-server.service`:

```ini
[Unit]
Description=llama.cpp server hosting abliterated Ministral 3 8B for perpBOT
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=perpbot
Group=perpbot
WorkingDirectory=/opt/perpbot

# Memory locking (allows --mlock to work)
LimitMEMLOCK=infinity
LimitNOFILE=65536

# Library search path for binaries copied from llama.cpp build
Environment="LD_LIBRARY_PATH=/opt/perpbot/bin"

ExecStart=/usr/bin/numactl --cpunodebind=0 --membind=0 -- \
  /opt/perpbot/bin/llama-server \
    -m /opt/perpbot/models/ministral-3-8b-reasoning-abliterated-Q8_0.gguf \
    --host 0.0.0.0 \
    --port 8080 \
    -t 8 \
    --ctx-size 8192 \
    --jinja \
    --reasoning off \
    -ngl 0 \
    --temp 0.4 \
    --mlock

Restart=on-failure
RestartSec=10

NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths=/opt/perpbot

StandardOutput=journal
StandardError=journal
SyslogIdentifier=llama-server

[Install]
WantedBy=multi-user.target
```

Notes:

- `numactl --cpunodebind=0 --membind=0` pins inference to socket 0 (NUMA node 0). Socket 1 is reserved for the future embedding service + ChromaDB.
- `-t 8` matches one socket's worth of physical cores. `-t 16` was tested-equivalent in industry literature for Sandy Bridge and not worth the QPI overhead.
- `--jinja` enables the GGUF's embedded chat template, which supports tool-calling for Ministral.
- `--reasoning off` controls llama-server's parsing/formatting of `<think>` tags. **It does NOT suppress the model's emission of reasoning content** — that's enforced by the system prompt the bird's Python wrapper injects on every cycle. See `design.md §7` for the empirical finding.
- `ProtectHome=read-only` was tried initially and broke execution from `/home/<admin-user>/llama.cpp/build/bin/`. Resolved by copying the binary to `/opt/perpbot/bin/` and removing the `ProtectHome` constraint.

### Service management

```bash
# Status
sudo systemctl status llama-server

# Start/stop/restart
sudo systemctl start llama-server
sudo systemctl stop llama-server
sudo systemctl restart llama-server

# Enable/disable on boot
sudo systemctl enable llama-server
sudo systemctl disable llama-server

# Tail logs
sudo journalctl -u llama-server -f
sudo journalctl -u llama-server -n 100 --no-pager
```

### Smoke test (LAN)

```bash
curl -s http://<perpbot-ip>:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "ministral-abliterated",
    "messages": [
      {"role": "system", "content": "Respond directly with the appropriate tool call. Do not show reasoning."},
      {"role": "user", "content": "What is the weather in San Francisco today?"}
    ],
    "tools": [{"type": "function", "function": {"name": "get_weather", "description": "Get current weather for a city", "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}}}],
    "tool_choice": "auto"
  }' | python3 -m json.tool
```

Expected: clean `tool_calls` array with `name: "get_weather"`, `arguments: "{\"city\": \"San Francisco\"}"`, `finish_reason: "tool_calls"`, no `reasoning_content` field. ~13 completion tokens, ~4 t/s generation rate.

---

## 8. Active services and substrate

### Embedding server (Jina v5 nano retrieval)

Second `llama-server` instance pinned to socket 1, port 8081. Same llama.cpp binary as chat-inference, different model + flags. **Operational as of 2026-05-27.**

Unit file at `/etc/systemd/system/llama-server-embedding.service`:

```ini
[Unit]
Description=llama.cpp embedding server (Jina v5 nano retrieval) for perpBOT ChromaDB
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=perpbot
Group=perpbot
WorkingDirectory=/opt/perpbot

LimitMEMLOCK=infinity
LimitNOFILE=65536

Environment="LD_LIBRARY_PATH=/opt/perpbot/bin"

ExecStart=/usr/bin/numactl --cpunodebind=1 --membind=1 -- \
  /opt/perpbot/bin/llama-server \
    -m /opt/perpbot/models/jina-v5-nano-retrieval-F16.gguf \
    --embedding \
    --pooling last \
    --host 0.0.0.0 \
    --port 8081 \
    -t 8 \
    --ctx-size 8192 \
    --mlock

Restart=on-failure
RestartSec=10

NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths=/opt/perpbot

StandardOutput=journal
StandardError=journal
SyslogIdentifier=llama-server-embedding

[Install]
WantedBy=multi-user.target
```

Notes:

- `--cpunodebind=1 --membind=1` pins to socket 1 (NUMA node 1). Chat-inference owns socket 0.
- `--embedding --pooling last` switches llama-server from chat to embedding mode with last-token pooling (Jina v5 spec).
- `--ctx-size 8192` is generous for embedding work (memories will rarely exceed 1K tokens).
- Model file at `/opt/perpbot/models/jina-v5-nano-retrieval-F16.gguf` (~480 MB F16, copied from HF cache after `-hf` test run).
- Resident memory ~84 MB after `--mlock` (small model).
- No `--reasoning off` — embedding models don't reason.

UFW rule for port 8081 (LAN-restricted):
```bash
sudo ufw allow from 192.168.1.0/24 to any port 8081
```

### Smoke test (embedding)

```bash
curl -s http://<perpbot-ip>:8081/v1/embeddings \
  -H "Content-Type: application/json" \
  -d '{"input": "test string", "model": "jina"}' | \
  python3 -c "import json,sys; r=json.load(sys.stdin); e=r['data'][0]['embedding']; print(f'dims={len(e)}, norm={sum(x*x for x in e)**0.5:.4f}')"
```

Expected: `dims=768, norm=1.0000`. The unit-normalized output means cosine similarity equals dot product, which matches ChromaDB's default distance metric.

### Python environment (uv-managed)

The bird's Python wrapper code (Phase B) runs in a uv-managed virtual environment.

**uv** is the modern Python package/venv manager (Astral, replaces pip/pyenv/virtualenv for most cases). Installed via Astral's standalone script. Binary at `/usr/local/bin/uv` for system-wide access:

```bash
# One-time install (as <admin-user>)
curl -LsSf https://astral.sh/uv/install.sh | sh
source ~/.local/bin/env

# Move to system path so perpbot can execute (/home/<admin-user> is mode 700)
sudo cp /home/<admin-user>/.local/bin/uv /usr/local/bin/uv
sudo cp /home/<admin-user>/.local/bin/uvx /usr/local/bin/uvx 2>/dev/null || true
sudo chmod 755 /usr/local/bin/uv /usr/local/bin/uvx
```

Venv at `/opt/perpbot/venv/` (Python 3.11.15, downloaded by uv on first request):

```bash
# Must cd to /opt/perpbot first — uv walks up from cwd looking for uv.toml,
# and /home/<admin-user> (mode 700) blocks perpbot from stat'ing it
cd /opt/perpbot
sudo -u perpbot /usr/local/bin/uv venv /opt/perpbot/venv --python 3.11
sudo -u perpbot /usr/local/bin/uv pip install \
  --python /opt/perpbot/venv/bin/python \
  chromadb requests
```

Resulting environment: ChromaDB 1.5.9, requests 2.34.2, plus 77 transitive dependencies. Total install time via uv: ~6 seconds.

To run Python scripts in the venv:
```bash
sudo -u perpbot /opt/perpbot/venv/bin/python /path/to/script.py
```

### ChromaDB

Local instance, PersistentClient mode (in-process — no separate service). Storage path `/opt/perpbot/chromadb/` (created on first use, owned by perpbot:perpbot). Configured to call the local embedding endpoint at `http://127.0.0.1:8081/v1/embeddings` via a custom embedding function.

Reference implementation in `src/smoke_test_chromadb.py` (see repo). Key pattern:

```python
import chromadb
import requests
from chromadb import EmbeddingFunction, Documents, Embeddings

class JinaLocalEmbeddingFunction(EmbeddingFunction):
    def __call__(self, input: Documents) -> Embeddings:
        response = requests.post(
            "http://127.0.0.1:8081/v1/embeddings",
            json={"input": input, "model": "jina"},
            timeout=60,
        )
        response.raise_for_status()
        return [item["embedding"] for item in response.json()["data"]]

client = chromadb.PersistentClient(path="/opt/perpbot/chromadb")
collection = client.create_collection(
    name="perp_memories",
    embedding_function=JinaLocalEmbeddingFunction(),
)
```

Collections to be created during Phase B / naming ceremony:
- `perp_memories` — read-write for THINKING, read-only for DREAMING
- `perp_dreams` — write-only by DREAMING, read-only by THINKING

### CRITICAL Phase B requirement: query/document prefix pattern

The smoke test on 2026-05-27 verified end-to-end functionality but surfaced an important finding: **Jina v5 retrieval is contrastive-trained and expects task-instruction prefixes** to properly separate queries from documents in vector space.

Naive embedding (no prefixes) produces semantically correct top-1 retrieval but with poorly-calibrated absolute distances (all top-1 matches landed in cosine-distance range 1.5-1.9, meaning negative cosine similarity — queries and documents collapsing into nearly the same subspace).

Phase B's `src/context.py` MUST embed:
- Queries with `"Represent this query for retrieving relevant passages: <text>"`
- Documents with `"Represent this passage: <text>"` (or pass as-is per Jina's exact spec)

Without these prefixes, distance thresholds (e.g., dream-to-thinking promotion at similarity > 0.6) won't be reliable. Top-1 retrieval will still work but ranking confidence will be miscalibrated.

### Bird-cycle services (still future)

`perpprompt-thinking.timer`, `perpprompt-dreaming.timer`, `perpprompt-digest.timer` — Phase C work. Will invoke Python wrappers in `src/` on systemd-managed cadence (4-6h THINKING, 8-12h DREAMING, daily digest at Holden's wake time).

---

## 9. Sanitization for public publication

This document references specific local IP addresses and may contain other infrastructure detail that should be redacted before the repo is made public. When `perpPROMPT` is forked or branched for public release:

- Replace any `192.168.1.x` with `<perpbot-lan-ip>` or similar placeholder
- Replace `<ct-300-lan-ip>` with placeholder
- Redact specific Mullvad config details (server names, public keys)
- Confirm no SSH host keys, no auth secrets, no NEWSTRON_SECRET values
- Verify `.gitignore` covers `*.conf`, `*.key`, `*.pem`, `secrets.yaml`, `.env`

Holden retains his real name on the public repo (his GitHub is `ASIXicle`; he hosts his art website there too). The sanitization concerns local-network detail, not author identity.

---

## 10. Build / setup history

- **2026-05-26**: Hardware bring-up, Debian 13 install, SSH hardened, UFW configured, Mullvad WireGuard active, llama.cpp built (after BMI2 footgun), abliterated Ministral 3 8B Q8_0 transferred and verified at 3.7 t/s
- **2026-05-26 (evening)**: Tool-use gate passed in single-shot test, reasoning suppression mechanism identified (system prompt, not server flag)
- **2026-05-26 (late evening)**: Architecture pivoted from CT 301 LXC clone to perpBOT-as-dedicated-server (commit `42da877`)
- **2026-05-27**: perpbot service user created, /opt/perpbot layout established, llama-server systemd unit deployed and validated across reboot
- **2026-05-27**: Embedding model decision locked (Jina v5 nano retrieval), dual-socket workload split planned
- **2026-05-27 (late, Phase A close)**: llama.cpp rebuilt with `-DLLAMA_OPENSSL=ON` for HTTPS support (enables `-hf` model auto-download). Mainline llama.cpp confirmed to support EuroBERT (PR #19826 landed) — Jina's patched fork not needed. Jina v5 nano retrieval F16 GGUF downloaded via `-hf` flag through Mullvad tunnel, ~480 MB, copied to `/opt/perpbot/models/`. Production binary at `/opt/perpbot/bin/` updated to HTTPS-enabled rebuild (with `systemctl stop` before `cp` to avoid "Text file busy"). `llama-server-embedding.service` deployed on socket 1, port 8081, validated with smoke test (dims=768, norm=1.0000).
- **2026-05-27 (Phase A close)**: uv installed as modern Python package manager (binary copied to `/usr/local/bin/` so perpbot user can execute despite `/home/<admin-user>` being mode 700). Python 3.11.15 venv created at `/opt/perpbot/venv/`. ChromaDB 1.5.9 + requests installed (79 packages total in ~6s via uv). End-to-end smoke test passed: ChromaDB PersistentClient + custom JinaLocalEmbeddingFunction → write 6 docs → semantic search → correct top-1 retrieval for all queries. Phase A memory substrate operational. Finding flagged for Phase B: Jina retrieval needs query/document task-instruction prefixes for properly-calibrated distances (raw embeddings produce correct top-1 ordering but absolute distances cluster in 1.5-1.9 range due to query/doc subspace collapse).
