#!/usr/bin/env bash
# Двусторонний синк волта с R2 (ТЗ §2.1). Remotely Save — GUI-плагин Obsidian,
# на headless doctor его нет; эквивалент — rclone bisync к тому же бакету.
#
# Системный rclone на doctor 1.60 (2022), его bisync разваливается на первом
# конфликте. Берём свой бинарь из /opt, чужие бэкапы на общем rclone не трогаем.
set -euo pipefail

VAULT="${VAULT:-/srv/vault}"
REMOTE="${REMOTE:-r2:obsidian-vault}"
RCLONE="${RCLONE:-/opt/rclone/rclone}"
LOCK="${LOCK:-$VAULT/.git/vault-git.lock}"   # общий с vault-git.sh: §13.8
FILTERS="$(dirname "${BASH_SOURCE[0]}")/../config/r2-filters.txt"
STATE="$HOME/.cache/rclone/bisync"

exec 9>"$LOCK"
flock -w 300 9 || { echo "vault-r2-sync: занято, пропускаю" >&2; exit 0; }

args=(bisync "$VAULT" "$REMOTE"
  --filters-file "$FILTERS"
  --conflict-resolve newer
  --max-delete 25
  --resilient --recover)

# Первый прогон обязан быть --resync. path1 (волт на doctor) — надмножество R2,
# так что ничего из бакета не теряется.
if [ ! -d "$STATE" ] || ! ls "$STATE"/*.lst >/dev/null 2>&1; then
  args+=(--resync --resync-mode path1)
fi

"$RCLONE" "${args[@]}"
