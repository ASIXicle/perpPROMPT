# perpPROMPT dashboard services

Two small, self-contained services that expose a running instance to a dashboard
or to a person, without touching the cognition loop:

- **`reader.py`** — a **read-only** HTTP tap over the instance's local ChromaDB,
  serving its memories (`perp_memories` = THINKING) and dreams (`perp_dreams` =
  DREAMING / DREAM-FREE) as JSON. Read-only *by construction*: it only ever calls
  `.get()` and `.count()`, never add/update/delete, so it cannot mutate state
  even by accident. Stdlib `http.server` + `chromadb` only — nothing to install
  beyond what the cycles already use. Runs on `:8090`.
- **`chat_server.py`** — a conversational endpoint (`:8091`) that lets you talk to
  the instance live. It invokes the local model and grounds each turn in the
  instance's own recent memories (identity + focus + a configurable slice of
  `perp_memories`), with server-side conversation state so a browser refresh
  doesn't drop the thread. Reasoning-suppression is applied defense-in-depth.

Both read their config from the environment, so neither file carries any LAN
topology and both are safe to publish. The reference deployment below targets
perpBOT (the host Echo runs on); adapt the user, paths, and IPs to your own.

## Deploy (reference: perpBOT)

```bash
# Reader (read-only tap, :8090)
sudo -u perpbot cp /opt/perpbot/dashboard/reader_env.example /opt/perpbot/config/reader_env
sudo -u perpbot nano /opt/perpbot/config/reader_env      # set PERPBOT_READER_HOST to your LAN IP
sudo cp /opt/perpbot/dashboard/perpprompt-reader.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now perpprompt-reader.service
curl http://<LAN-IP>:8090/health

# Chat brain (:8091) — needs the local llama-server up for generation
sudo -u perpbot cp /opt/perpbot/dashboard/chat_env.example /opt/perpbot/config/chat_env
sudo cp /opt/perpbot/dashboard/perpprompt-chat.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now perpprompt-chat.service
curl http://<LAN-IP>:8091/health
```

`config/` is gitignored, so the real LAN IP and any local settings stay out of
the repo. The systemd units are LAN-only and sandboxed (`ProtectSystem=strict`,
`NoNewPrivileges`, a single `ReadWritePaths`).

## Reader endpoints

| Method | Path | Returns |
| --- | --- | --- |
| GET | `/health` | `{status, collections:{perp_memories, perp_dreams}}` |
| GET | `/perp_memories?limit=N&since=ISO` | THINKING observations, newest first |
| GET | `/perp_dreams?limit=N&since=ISO&variant=utility\|free\|all` | DREAMING / DREAM-FREE |

Dreams without a `variant` key bucket as `utility` (reader-side backward-compat).
The `free` filter also includes `conversation`-variant dreams (a sub-variant of
free that seeds from recent chat fragments rather than dream-nouns).
Config via `PERPBOT_READER_HOST` / `PERPBOT_READER_PORT` / `PERPBOT_CHROMADB_PATH`;
chat via `PERPBOT_CHAT_HOST` / `PERPBOT_CHAT_PORT` / `PERPBOT_CHAT_GROUNDING_K`.
See the `*_env.example` files for the full set.
