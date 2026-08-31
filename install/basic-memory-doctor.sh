#!/usr/bin/env bash
# Basic Memory + MCP на doctor (ТЗ §12, этап 0). Идемпотентно.
#
# Порт 8787, а не 8765: на doctor 8765 занят docker-proxy контейнера
# caddy-letheclaw. Он же светит 0.0.0.0 — не перепутать с нашим сокетом.
# Версия пинится (ТЗ §11): `--upgrade` утаскивает fastmcp 4.0.0b1.
set -euo pipefail

VAULT="${VAULT:-/srv/vault}"
PORT="${PORT:-8787}"
BM_VERSION="${BM_VERSION:-0.22.1}"
BIN="$HOME/.local/bin"

export PATH="$BIN:$PATH"
command -v uv >/dev/null 2>&1 || curl -LsSf https://astral.sh/uv/install.sh | sh

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
ExecStart=$BIN/basic-memory mcp --transport streamable-http --host 127.0.0.1 --port $PORT --project vault
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT

sudo systemctl daemon-reload
sudo systemctl reenable -q basic-memory-mcp.service
sudo systemctl restart basic-memory-mcp.service
sleep 10

# Проверка ТЗ §11: наш сокет обязан быть на loopback. Смотрим по PID сервиса,
# а не по порту — иначе увидим чужой контейнер и решим, что всё плохо (или хорошо).
systemctl is-active --quiet basic-memory-mcp.service || {
  journalctl -u basic-memory-mcp -n 20 --no-pager; exit 1; }
pid=$(systemctl show -p MainPID --value basic-memory-mcp.service)
sock=$(sudo ss -ltnp | grep "pid=$pid," || true)
[ -n "$sock" ] || { echo "ОШИБКА: сервис жив, но не слушает" >&2; exit 1; }
grep -qE '(0\.0\.0\.0|\[::\]):' <<<"$sock" && {
  echo "ОШИБКА: MCP слушает наружу — нарушение ТЗ §11" >&2; echo "$sock" >&2; exit 1; }
echo "$sock"
echo "ок: MCP только на loopback, порт $PORT"
