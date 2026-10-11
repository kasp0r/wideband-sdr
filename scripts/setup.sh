#!/usr/bin/env bash
# Wideband-SDR setup wizard for non-experts.
#
# Walks through Docker, Ollama, the SDR (rtl_tcp) connection, writes a .env,
# pulls the Ollama model, and starts the app with Docker.
#
# Usage:
#   ./scripts/setup.sh
#
# Requires bash and (for the rtl_tcp reachability check) python3.

set -euo pipefail

GREEN=$'\033[32m'; YELLOW=$'\033[33m'; RED=$'\033[31m'; BOLD=$'\033[1m'; NC=$'\033[0m'
say()  { printf "${BOLD}%s${NC}\n" "$1"; }
ok()   { printf "  ${GREEN}\u2713${NC} %s\n" "$1"; }
warn() { printf "  ${YELLOW}!\033[0m %s\n" "$1"; }
fail() { printf "  ${RED}\u2717${NC} %s\n" "$1"; }

ask_yn() {
  # ask_yn "prompt" -> returns 0 for yes, 1 for no
  local prompt="$1" ans
  read -r -p "  ${prompt} [y/N] " ans
  [[ "$ans" =~ ^[Yy]$ ]]
}

say "Wideband-SDR setup"
echo

# --- 1. Docker ---
say "Checking Docker"
if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
  ok "Docker is installed and running"
elif command -v docker >/dev/null 2>&1; then
  fail "Docker is installed but the daemon is not running. Start Docker Desktop / dockerd and re-run this script."
  exit 1
else
  warn "Docker is not installed."
  if ask_yn "Install Docker now? (downloads the official installer, needs sudo)"; then
    curl -fsSL https://get.docker.com | sh
    ok "Docker installed — you may need to log out/in or start dockerd, then re-run this script."
  else
    fail "Docker is required. Install it from https://docs.docker.com/engine/install/ and re-run."
    exit 1
  fi
fi
echo

# --- 2. Ollama ---
say "Checking Ollama"
OLLAMA_INSTALLED=0
if command -v ollama >/dev/null 2>&1; then
  OLLAMA_INSTALLED=1
  ok "Ollama is installed"
else
  warn "Ollama is not installed (it powers the AI assistant)."
  if ask_yn "Install Ollama now? (downloads the official installer)"; then
    curl -fsSL https://ollama.com/install.sh | sh
    OLLAMA_INSTALLED=1
    ok "Ollama installed"
  else
    warn "Skipping Ollama — the AI assistant will be unavailable until it is installed."
  fi
fi
echo

# --- 3. SDR (rtl_tcp) connection ---
say "SDR (rtl_tcp) connection"
read -r -p "  SDR host [127.0.0.1]: " SDR_HOST
SDR_HOST="${SDR_HOST:-127.0.0.1}"
read -r -p "  SDR port [1234]: " SDR_PORT
SDR_PORT="${SDR_PORT:-1234}"
echo

# --- 4. Ollama connection ---
say "Ollama connection"
read -r -p "  Ollama URL [http://localhost:11434]: " OLLAMA_URL
OLLAMA_URL="${OLLAMA_URL:-http://localhost:11434}"
read -r -p "  Model [gemma4:latest]: " OLLAMA_MODEL
OLLAMA_MODEL="${OLLAMA_MODEL:-gemma4:latest}"
echo

# --- 5. Write .env ---
say "Writing .env"
[[ -f .env ]] || cp .env.example .env

set_env() {
  local key="$1" val="$2"
  if grep -q "^${key}=" .env; then
    awk -v k="$key" -v v="$val" 'index($0, k"=")==1 { print k"="v; next } { print }' .env > .env.tmp && mv .env.tmp .env
  else
    printf '%s=%s\n' "$key" "$val" >> .env
  fi
}

set_env SDR_HOST "$SDR_HOST"
set_env SDR_PORT "$SDR_PORT"
set_env OLLAMA_URL "$OLLAMA_URL"
set_env OLLAMA_MODEL "$OLLAMA_MODEL"
ok ".env written"
echo

# --- 6. Pull the model ---
if [[ "$OLLAMA_INSTALLED" == "1" ]]; then
  say "Pulling Ollama model (${OLLAMA_MODEL})"
  if ollama pull "$OLLAMA_MODEL"; then
    ok "model pulled"
  else
    warn "Model pull failed — retry later with: ollama pull ${OLLAMA_MODEL}"
  fi
  echo
fi

# --- 7. Start the app ---
say "Starting Wideband-SDR"
docker compose up -d --build
echo
ok "Done! Open http://localhost:8000 in your browser."
say "Tip: run ./scripts/check.sh any time to diagnose connectivity."
