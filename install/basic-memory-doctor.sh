#!/usr/bin/env bash
# Basic Memory + MCP + локальные эмбеддинги на doctor (ТЗ §12 этап 0, §8.2).
# Идемпотентно.
#
# Грабли, из-за которых нельзя взять дефолты:
#   1. Порт 8765 на doctor занят docker-proxy контейнера caddy-letheclaw.
#   2. Версия пинится (§11): `uv tool install --upgrade` тащит fastmcp 4.0.0b1.
#   3. Дефолтный fastembed берёт bge-small-en-v1.5 — английскую модель на
#      русский волт. Уводим на Ollama через litellm.
set -euo pipefail

VAULT="${VAULT:-/srv/vault}"
PORT="${PORT:-8787}"
BM_VERSION="${BM_VERSION:-0.22.1}"
EMB_MODEL="${EMB_MODEL:-bge-m3}"
BIN="$HOME/.local/bin"

export PATH="$BIN:$PATH"
command -v uv >/dev/null 2>&1 || curl -LsSf https://astral.sh/uv/install.sh | sh

uv tool install "basic-memory==$BM_VERSION"
basic-memory project add vault "$VAULT" 2>/dev/null || true
basic-memory project default vault

# Эмбеддинги локально через Ollama (§8.2). Модель — bge-m3.
# Замер на doctor (i5-7200U, 4 потока, без GPU), кусок 3000 символов:
#   bge-m3               11.3 с, запрос 0.28 с
#   qwen3-embedding:0.6b 25.6 с, запрос 0.42 с
# По MTEB multilingual разница меньше балла, по скорости — в 2.3 раза.
command -v ollama >/dev/null 2>&1 || curl -fsSL https://ollama.com/install.sh | sh
ollama pull "$EMB_MODEL"

EMB_MODEL="$EMB_MODEL" python3 <<'PYCFG'
import json, os, pathlib
p = pathlib.Path.home() / ".basic-memory/config.json"
c = json.loads(p.read_text())
c.update({
    "semantic_search_enabled": True,
    "semantic_embedding_provider": "litellm",
    "semantic_embedding_model": "ollama/" + os.environ["EMB_MODEL"],
    "semantic_embedding_dimensions": 1024,
})
p.write_text(json.dumps(c, ensure_ascii=False, indent=2))
print("эмбеддинги ->", c["semantic_embedding_model"])
PYCFG

sudo tee /etc/systemd/system/basic-memory-mcp.service >/dev/null <<UNIT
[Unit]
Description=Basic Memory MCP (vault)
After=network.target ollama.service

[Service]
User=$(id -un)
Environment=PATH=$BIN:/usr/local/bin:/usr/bin:/bin
Environment=OLLAMA_API_BASE=http://127.0.0.1:11434
ExecStart=$BIN/basic-memory mcp --transport streamable-http --host 127.0.0.1 --port $PORT --project vault
Restart=always
RestartSec=5
# Волт индексируется на двухъядерном ноутбучном CPU, который одновременно
# держит Home Assistant. Уступаем ему.
Nice=15
IOSchedulingClass=idle

[Install]
WantedBy=multi-user.target
UNIT

sudo systemctl daemon-reload
sudo systemctl reenable -q basic-memory-mcp.service
sudo systemctl restart basic-memory-mcp.service
sleep 10

# Проверка ТЗ §11: наружу торчать нельзя. Смотрим сокет своего PID, а не
# любой сокет на порту — иначе увидим чужой контейнер.
systemctl is-active --quiet basic-memory-mcp.service || {
  journalctl -u basic-memory-mcp -n 20 --no-pager; exit 1; }
pid=$(systemctl show -p MainPID --value basic-memory-mcp.service)
sock=$(sudo ss -ltnp | grep "pid=$pid," || true)
[ -n "$sock" ] || { echo "ОШИБКА: сервис жив, но не слушает" >&2; exit 1; }
# 4-я колонка ss — локальный адрес. 5-я это peer, там всегда 0.0.0.0:* — не она.
local_addr=$(awk '{print $4}' <<<"$sock")
case "$local_addr" in
  127.0.0.1:*|\[::1\]:*) ;;
  *) echo "ОШИБКА: MCP слушает $local_addr — нарушение ТЗ §11" >&2; exit 1 ;;
esac
echo "$sock"
echo "ок: MCP только на loopback, порт $PORT"
