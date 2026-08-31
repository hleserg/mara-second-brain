#!/usr/bin/env bash
# Basic Memory + MCP на doctor (ТЗ §12, этап 0). Идемпотентно.
# MCP слушает ТОЛЬКО loopback — дефолт basic-memory это 0.0.0.0, что ТЗ §11 запрещает.
set -euo pipefail

VAULT="${VAULT:-/srv/vault}"
PORT="${PORT:-8765}"
BIN="$HOME/.local/bin"

command -v uv >/dev/null 2>&1 || curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$BIN:$PATH"

uv tool install --upgrade basic-memory
basic-memory project add vault "$VAULT" 2>/dev/null || true
basic-memory project default vault

sudo tee /etc/systemd/system/basic-memory-mcp.service >/dev/null <<UNIT
[Unit]
Description=Basic Memory MCP (vault)
After=network.target

[Service]
User=$(id -un)
Environment=PATH=$BIN:/usr/local/bin:/usr/bin:/bin
ExecStart=$BIN/basic-memory mcp --transport streamable-http --host 127.0.0.1 --port $PORT --project vault
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT

sudo systemctl daemon-reload
sudo systemctl enable --now basic-memory-mcp.service
sleep 3
systemctl is-active basic-memory-mcp.service
ss -ltnp 2>/dev/null | grep ":$PORT" || true
