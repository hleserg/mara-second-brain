#!/usr/bin/env bash
# Сторож cron-тикера Мары (ТЗ §7.4). Тикер — фоновый тред внутри gateway, он
# умирает молча, а `hermes cron status` при этом продолжает рапортовать, что
# всё живо. Единственный честный признак — файл-маркер: тикер кладёт в
# ~/.hermes/cron/ticker_last_success unix-время каждого удачного прохода.
#
# Сторож снаружи и на doctor, а не на маке: launchd-таймер в GUI-домене по ssh
# не поставить, а сторож внутри той же машины, что и подопечный, — не сторож.
#
# `systemd_watchdog_seconds` из ТЗ не ставим: это sd_notify, на launchd пусто.
set -euo pipefail

MAC="${MAC:-serg@192.168.1.80}"
STALE="${STALE:-600}"          # 10 минут: тикер ходит раз в минуту, но мак
                               # успевает и подтормозить, и уснуть на пару.

last=$(ssh -o BatchMode=yes -o ConnectTimeout=10 "$MAC" \
        'cat ~/.hermes/cron/ticker_last_success 2>/dev/null' 2>/dev/null) || {
  # Мак недоступен — рестартовать нечего. Молчим, но в лог пишем: подряд
  # идущие такие строки и есть «мак опять спит».
  echo "$(date -Is) mara-watchdog: мак недоступен"; exit 0; }

age=$(( $(date +%s) - ${last%%.*} ))
[ "$age" -lt "$STALE" ] && exit 0

echo "$(date -Is) mara-watchdog: тикер молчит $age с, рестарт gateway"
ssh -o BatchMode=yes -o ConnectTimeout=10 "$MAC" \
  'PATH=$HOME/.local/bin:$PATH hermes gateway restart' 2>&1 | tail -3
