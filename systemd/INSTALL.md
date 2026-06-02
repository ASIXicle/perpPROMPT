# perpPROMPT systemd unit installation

Six unit files live in this directory. Three `.service` files run the
cycle runners as oneshot processes; three `.timer` files drive cadence.

## Files

| File | Purpose | Cadence |
|---|---|---|
| `perpprompt-thinking.service` | Invokes `python -m src.think` | — |
| `perpprompt-thinking.timer` | Drives THINKING cadence | every 5-6h |
| `perpprompt-dreaming.service` | Invokes `python -m src.dream` | — |
| `perpprompt-dreaming.timer` | Drives DREAMING cadence | every 10-12h, 4h offset from THINKING at boot |
| `perpprompt-digest.service` | Invokes `python -m src.digest` | — |
| `perpprompt-digest.timer` | Daily digest cadence | 08:00 America/Chicago, DST-aware |

All services run as `perpbot` (UID 988, nologin), `WorkingDirectory=/opt/perpbot`,
`ReadWritePaths=/opt/perpbot`, with `ProtectSystem=strict` and `PrivateTmp=true`.

Each service reads `EnvironmentFile=-/opt/perpbot/config/bird_env` for
`BIRD_NAME=<chosen>`. The leading dash makes the file optional so units
install cleanly pre-naming-ceremony, but they'll FAIL at run time if the
file doesn't exist (because `${BIRD_NAME}` will be unset and `--bird-name`
will be empty). That's intended — timers should only be **enabled** after
naming ceremony completes.

The dreaming service additionally honors `DREAM_FREE_WEIGHT=<0.0-1.0>`
(optional, default `0.0`): the probability that a given DREAMING cycle runs
the **free** variant (`dream.free.md` — identity-free, wrapper auto-stores)
instead of the **utility** variant (`dream.md` — identity + dreamer-autonomy
storage). `0.0` = utility-only; `0.4` ≈ a free dream roughly every 2-3 cycles
(~1/day at the 10-12h cadence); `1.0` = always free. `dream.py` rolls per
invocation and logs `Variant: FREE|UTILITY (weight=…, roll=…)` to the journal.
Example `bird_env`:

```
BIRD_NAME=Echo
DREAM_FREE_WEIGHT=0.4
```

### Bluesky dream poster (optional, dreams only)

When `BLUESKY_ENABLED=true`, each *stored dream* is posted to a Bluesky account
(threaded if it exceeds the 300-grapheme post cap, every post self-labeled for
the 18+ account). Thoughts are never posted. Posting is best-effort — any
failure is logged and the dream cycle completes normally.

Requires the `atproto` SDK in the venv. **This venv is uv-managed (pip-less)** —
install with `uv`, not pip, and run from a dir the perpbot user can read:

```bash
sudo -u perpbot bash -lc 'cd /opt/perpbot && \
  UV=$(command -v uv || echo /opt/perpbot/.local/bin/uv) && \
  "$UV" pip install --python /opt/perpbot/venv/bin/python atproto grapheme'
```

(`grapheme` is optional — accurate post-splitting. `pip`/`ensurepip` will NOT
work here: uv venvs ship without pip by design.)

`bird_env` keys (the **app password** is a secret — `chmod 600`, gitignored,
never in the repo; revoke it in Bluesky settings to kill the poster instantly):

```
BLUESKY_ENABLED=true
BLUESKY_HANDLE=you_choose
BLUESKY_APP_PASSWORD=xxxx-xxxx-xxxx-xxxx
BLUESKY_POST_VARIANTS=free          # all | free | utility
BLUESKY_SELF_LABEL=sexual           # sexual | nudity | porn | graphic-media
# optional:
# BLUESKY_PDS=https://bsky.social
# BLUESKY_MAX_THREAD_POSTS=6
# BLUESKY_POST_TIMEOUT=20
```

Bluesky has no account-level "adult" switch — adult content is declared
**per-post** via the self-label above, which the poster applies to every post.
`dream.py` logs `Dream posted to Bluesky (N post(s)): <uri>` on success.

## Installation (Phase C.1)

After cloning the repo to `/opt/perpbot/`, install the unit files to systemd's
search path. As root on perpBOT:

```bash
sudo cp /opt/perpbot/systemd/perpprompt-*.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
```

## Deployment-specific config (drop-ins)

Some configuration is deployment-specific and intentionally NOT in the
repo's version-controlled unit files — host IPs, secret paths, override
endpoints. The pattern is systemd **drop-in overrides** at
`/etc/systemd/system/<unit>.service.d/local.conf`. These files are read
in addition to the main unit file and can add or override directives.

The most common one is the persMEM MCP host. The repo default in
`src/config.py` is an RFC 5737 placeholder; real deployments override
via the `PERPPROMPT_PERSMEM_MCP_HOST` env var. To make this work for
timed cycles, create three drop-ins (one per service):

```bash
sudo mkdir -p /etc/systemd/system/perpprompt-thinking.service.d \
              /etc/systemd/system/perpprompt-dreaming.service.d \
              /etc/systemd/system/perpprompt-digest.service.d

for unit in perpprompt-thinking perpprompt-dreaming perpprompt-digest; do
  sudo tee /etc/systemd/system/${unit}.service.d/local.conf > /dev/null <<'EOF'
[Service]
Environment=PERPPROMPT_PERSMEM_MCP_HOST=http://<your-persmem-host>:8000
EOF
done

sudo systemctl daemon-reload
```

Replace `<your-persmem-host>` with the actual LAN address of the persmem
MCP server. The drop-in directory and file are local-only — they don't
get committed back to the repo, so the real address stays out of public
view. Verify the override took effect:

```bash
sudo systemctl show perpprompt-thinking.service | grep -i ^environment
# Expected: Environment=PERPPROMPT_PERSMEM_MCP_HOST=http://...
```

For manual cycle invocations (pre-timer-enable), the same env var can be
passed via `sudo env`:

```bash
sudo -u perpbot env PERPPROMPT_PERSMEM_MCP_HOST=http://<host>:8000 \
    /opt/perpbot/venv/bin/python -m src.think --bird-name Echo --log-level INFO
```

## Validation (Phase C.2, pre-naming)

The units install and systemctl can see them, but **do not enable timers yet**.
The bird has no name, no bootstrap entry, no env file. Enabling timers now
would fire cycles that crash immediately.

Validate unit syntax and reachability:

```bash
sudo systemd-analyze verify /etc/systemd/system/perpprompt-thinking.service
sudo systemd-analyze verify /etc/systemd/system/perpprompt-thinking.timer
sudo systemd-analyze verify /etc/systemd/system/perpprompt-dreaming.service
sudo systemd-analyze verify /etc/systemd/system/perpprompt-dreaming.timer
sudo systemd-analyze verify /etc/systemd/system/perpprompt-digest.service
sudo systemd-analyze verify /etc/systemd/system/perpprompt-digest.timer
```

Each should output nothing (success). Any warnings/errors mean the unit
file is wrong and Kestrel should fix before proceeding.

You can also `systemctl status perpprompt-thinking.timer` to see it as
installed-but-not-enabled.

## Activation (Phase E.1, post-naming, post-dry-run-pass)

After the naming ceremony completes and Holden has reviewed three
mandatory dry-runs against the named bird, enable ONLY thinking + digest
for the 5-day THINKING-only window:

```bash
sudo systemctl enable --now perpprompt-thinking.timer
sudo systemctl enable --now perpprompt-digest.timer

# DO NOT enable dreaming yet. Track A evaluation gates that.
```

After Track A passes on Day 5:

```bash
sudo systemctl enable --now perpprompt-dreaming.timer
```

## Operating commands

```bash
# Status overview
systemctl list-timers --all | grep perpprompt
systemctl status perpprompt-thinking.timer perpprompt-dreaming.timer perpprompt-digest.timer

# Live logs
journalctl -u perpprompt-thinking.service -f
journalctl -u perpprompt-dreaming.service -f
journalctl -u perpprompt-digest.service -f

# Manual trigger (runs once, not on timer)
sudo systemctl start perpprompt-thinking.service

# Halt the bird (per dry-run-evaluation.md)
sudo systemctl stop perpprompt-thinking.timer perpprompt-dreaming.timer perpprompt-digest.timer
sudo systemctl disable perpprompt-thinking.timer perpprompt-dreaming.timer perpprompt-digest.timer
```

## Dependency graph

Both cycle services have `Requires=llama-server.service llama-server-embedding.service`,
so if either llama-server is down, the cycle fails fast (rather than hanging
on a connection attempt). systemd will not start the cycle service until
both llama-servers are active.

The digest service does NOT require llama-servers — it doesn't call the
local model. It only needs MCP to be reachable for amq_send to Holden.
If MCP is unreachable, the digest logs an error and exits non-zero;
journal captures the failure.
