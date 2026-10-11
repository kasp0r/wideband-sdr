#!/usr/bin/env bash
# Check that the Wideband-SDR prerequisites are in place.
#
# Usage:
#   ./scripts/check.sh
#
# Reads SDR_HOST/SDR_PORT/OLLAMA_URL from .env if present (environment wins).

set -uo pipefail

if [[ -f .env ]]; then
  set -a; source .env; set +a
fi

SDR_HOST="${SDR_HOST:-127.0.0.1}"
SDR_PORT="${SDR_PORT:-1234}"
OLLAMA_URL="${OLLAMA_URL:-http://localhost:11434}"

GREEN=$'\033[32m'; YELLOW=$'\033[33m'; RED=$'\033[31m'; NC=$'\033[0m'
ok()   { printf "  ${GREEN}\u2713${NC} %s\n" "$1"; }
warn() { printf "  ${YELLOW}!\033[0m %s\n" "$1"; }
fail() { printf "  ${RED}\u2717${NC} %s\n" "$1"; }

echo "Wideband-SDR environment check"
echo

# Docker
echo "Docker:"
if command -v docker >/dev/null 2>&1; then
  if docker info >/dev/null 2>&1; then
    ok "installed and running ($(docker --version | awk '{print $3}' | tr -d ','))"
  else
    fail "installed but the daemon is not running (start Docker Desktop / dockerd)"
  fi
else
  fail "not installed (https://docs.docker.com/engine/install/)"
fi

# rtl_tcp
echo
echo "rtl_tcp (${SDR_HOST}:${SDR_PORT}):"
if python3 -c "import socket; s=socket.create_connection(('${SDR_HOST}', ${SDR_PORT}), timeout=3); s.close()" 2>/dev/null; then
  ok "reachable"
else
  fail "not reachable — start rtl_tcp (see README: Running rtl_tcp)"
fi

# Ollama
echo
echo "Ollama (${OLLAMA_URL}):"
if curl -fsS "${OLLAMA_URL}/api/tags" >/tmp/wb_ollama_tags.json 2>/dev/null; then
  ok "reachable"
  models=$(python3 -c 'import json; print(", ".join(m["name"] for m in json.load(open("/tmp/wb_ollama_tags.json")).get("models", [])))' 2>/dev/null || echo "?")
  printf "  models: %s\n" "${models:-none}"
  rm -f /tmp/wb_ollama_tags.json
else
  fail "not reachable — install/start Ollama (https://ollama.com)"
fi

# App
echo
echo "App (http://localhost:8000):"
if curl -fsS http://localhost:8000/api/status >/dev/null 2>&1; then
  ok "running"
else
  warn "not running (expected until 'docker compose up')"
fi

echo
echo "All checks complete."
