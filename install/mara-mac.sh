#!/usr/bin/env bash
# Этап 3: связать Мару (Hermes на маке) с волтом (ТЗ §7.4).
#
# Мара пишет в волт через Basic Memory MCP, который живёт на doctor. Значит
# нужен туннель. Инициирует его doctor, а не мак, и вот почему: на маке
# фоновая служба — это launchd в GUI-домене, а туда по ssh не достучаться
# (launchctl bootstrap из ssh-сессии отлетает). doctor же всегда включён и
# systemd там свой. Обратный форвард (-R) сажает порт на loopback мака —
# §11 соблюдён, наружу 8787 не торчит.
#
# Ставится на doctor:  install/mara-mac.sh
set -euo pipefail

# Адрес мака — в ~/.config/mara/env (MARA_MAC), репозиторий публичный.
[ -r "${HOME:-}/.config/mara/env" ] && . "${HOME:-}/.config/mara/env"
MAC="${MAC:-${MARA_MAC:?не задан MARA_MAC в ~/.config/mara/env}}"
PORT="${PORT:-8787}"

command -v systemctl >/dev/null || { echo "не doctor: нет systemd" >&2; exit 1; }
ssh -o BatchMode=yes -o ConnectTimeout=5 "$MAC" true \
  || { echo "нет ssh по ключу на $MAC — сначала положить туда ~/.ssh/id_ed25519.pub" >&2; exit 1; }

sudo tee /etc/systemd/system/mara-mac-tunnel.service >/dev/null <<UNIT
[Unit]
Description=Basic Memory MCP -> loopback Mac mini (Мара, ТЗ §7.4)
After=network-online.target basic-memory-mcp.service
Wants=network-online.target

[Service]
User=$USER
# ExitOnForwardFailure: без него ssh висит живой с непроброшенным портом, и
# systemd считает, что всё хорошо, а Мара не видит инструментов.
# ServerAlive*: мак засыпает, оборванный форвард надо заметить, а не ждать TCP.
ExecStart=/usr/bin/ssh -N -o BatchMode=yes -o ExitOnForwardFailure=yes \\
  -o ServerAliveInterval=30 -o ServerAliveCountMax=3 \\
  -R 127.0.0.1:$PORT:127.0.0.1:$PORT $MAC
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
UNIT

sudo systemctl daemon-reload
sudo systemctl reenable -q mara-mac-tunnel.service
sudo systemctl restart mara-mac-tunnel.service
sleep 3

systemctl is-active --quiet mara-mac-tunnel.service || {
  echo "туннель не поднялся:" >&2; systemctl status --no-pager -n 20 mara-mac-tunnel.service >&2; exit 1; }
ssh -o BatchMode=yes "$MAC" "nc -z -w2 127.0.0.1 $PORT" \
  || { echo "порт $PORT на маке не отвечает" >&2; exit 1; }
echo "туннель поднят: mac:127.0.0.1:$PORT -> doctor basic-memory"
