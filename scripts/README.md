# Setup helpers

Two bash scripts that make getting Wideband-SDR running easier, especially for
first-time users. They're intended for Linux, macOS, and WSL.

## `setup.sh` — interactive setup wizard

```bash
./scripts/setup.sh
```

Walks through everything needed to get running:

1. **Docker** — verifies it's installed and the daemon is running; offers to install
   it (via the official `get.docker.com` script) if missing.
2. **Ollama** — verifies it's installed; offers to install it (via the official
   `ollama.com/install.sh` script) if missing.
3. **SDR connection** — prompts for the `rtl_tcp` host and port (default
   `127.0.0.1:1234`).
4. **Ollama connection** — prompts for the Ollama URL (default
   `http://localhost:11434`) and model (default `gemma4:latest`).
5. **`.env`** — creates it from `.env.example` and writes the values above.
6. **Model** — pulls the chosen Ollama model.
7. **Start** — runs `docker compose up -d --build`.

When it finishes, open <http://localhost:8000>.

## `check.sh` — environment diagnostics

```bash
./scripts/check.sh
```

Reports whether each piece is reachable, with a `✓` / `!` / `✗` status:

- **Docker** — installed and is the daemon running?
- **rtl_tcp** — is `${SDR_HOST}:${SDR_PORT}` accepting connections?
- **Ollama** — is `${OLLAMA_URL}` reachable, and which models are installed?
- **App** — is <http://localhost:8000> responding?

It reads `SDR_HOST`, `SDR_PORT`, and `OLLAMA_URL` from `.env` if present (otherwise the
defaults), so it reflects your actual configuration. Run it any time something isn't
working.

## Notes

- Both scripts are plain `bash` and only need `bash`, `curl`, and (for the rtl_tcp
  check) `python3`.
- The Docker setup uses `network_mode: host`, which works on Linux (and WSL). On macOS
  and Windows, Docker Desktop doesn't support host networking — use the "Run without
  Docker (development)" steps in the main README instead.
- Prefer to configure by hand? See the main [README](../README.md) — the scripts just
  automate those steps.
