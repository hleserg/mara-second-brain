#!/usr/bin/env bash
# ВРЕМЕННЫЙ. Односторонний забор из старого бакета obsidian-vault.
#
# Пока Remotely Save на телефоне, маке и винде ещё смотрит в старый бакет,
# заметки, созданные там, до doctor не доедут: боевой синк уже переехал на
# mara-vault. Этот скрипт затягивает их вниз и ничего не удаляет и не заливает
# наверх — старый бакет только источник.
#
# --update: копируем только если в бакете новее. Испорченные 14 объектов в нём
# заморожены и всегда старее волта, так что устаревшую копию они не навяжут.
#
# Удалить вместе со строкой в crontab и config/r2-legacy-pull.txt, когда все
# устройства перецелены на mara-vault.
set -euo pipefail

VAULT="${VAULT:-/srv/vault}"
LEGACY="${LEGACY:-r2:obsidian-vault}"
RCLONE="${RCLONE:-/opt/rclone/rclone}"
LOCK="${LOCK:-$VAULT/.git/vault-git.lock}"   # общий с vault-git.sh: §13.8
FILTERS="$(dirname "${BASH_SOURCE[0]}")/../config/r2-legacy-pull.txt"

exec 9>"$LOCK"
flock -w 120 9 || { echo "r2-legacy-pull: занято, пропускаю" >&2; exit 0; }

exec timeout -k 10s 5m "$RCLONE" copy "$LEGACY" "$VAULT" \
  --filter-from "$FILTERS" --update \
  --contimeout 20s --timeout 60s --retries 2 --low-level-retries 3
