#!/usr/bin/env bash
# Basic Memory + MCP на doctor (ТЗ §12, этап 0). Идемпотентно.
#
# Две грабли, из-за которых нельзя просто взять дефолты:
#   1. `basic-memory mcp --host 127.0.0.1` НЕ работает: fastmcp читает FASTMCP_HOST
#      и молча слушает 0.0.0.0. ТЗ §11 это запрещает. Лечится только env-переменной.
#   2. Версия пиниться обязана (§11): `uv tool install --upgrade` утащил fastmcp
#      4.0.0b1, бету, вместе с ней и смену дефолта.
set -euo pipefail

VAULT="${VAULT:-/srv/vault}"
PORT="${PORT:-8765}"
BM_VERSION="${BM_VERSION:-0.22.1}"
BIN="$HOME/.local/bin"

command -v uv >/dev/null 2>&1 || curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$BIN:$PATH"

uv tool install "basic-memory==$BM_VERSION"
basic-memory project add vault "$VAULT" 2>/dev/null || true
basic-memory project default vault

sudo tee /etc/systemd/system/basic-memory-mcp.service >/dev/null <<UNIT
[Unit]
Description=Basic Memory MCP (vault)
After=network.target

[Service]
User=$(id -un)
Environment=PATH=$BIN:/usr/local/bin:/usr/bin:/bin
Environment=FASTMCP_HOST=127.0.0.1
ExecStart=$BIN/basic-memory mcp --transport streamable-http --host 127.0.0.1 --port $PORT --project vault
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT

sudo systemctl daemon-reload
sudo systemctl reenable -q basic-memory-mcp.service
sudo systemctl restart basic-memory-mcp.service
sleep 8

# Проверка §11: наружу торчать нельзя. Падаем громко, а не тихо светим порт.
systemctl is-active --quiet basic-memory-mcp.service || { journalctl -u basic-memory-mcp -n 20 --no-pager; exit 1; }
if ss -ltn | grep -qE "(0\.0\.0\.0|\[::\]):$PORT"; then
  echo "ОШИБКА: MCP слушает наружу на порту $PORT — нарушение ТЗ §11" >&2
  ss -ltn | grep ":$PORT" >&2
  exit 1
fi
ss -ltn | grep ":$PORT"
echo "ок: MCP только на loopback"
