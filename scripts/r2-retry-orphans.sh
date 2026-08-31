#!/usr/bin/env bash
# Повторная попытка удалить залипшие объекты R2 (config/r2-orphans.txt).
# На них Cloudflare отдаёт 500 InternalError и на PutObject, и на DeleteObject,
# хотя GET и LIST работают, а на свежесозданном ключе delete проходит.
# Что удалось — печатаем; соответствующие строки можно убрать из r2-filters.txt
# (правка фильтров требует нового --resync, см. vault-r2-sync.sh).
set -uo pipefail
RCLONE="${RCLONE:-/opt/rclone/rclone}"
REMOTE="${REMOTE:-r2:obsidian-vault}"
LIST="$(dirname "${BASH_SOURCE[0]}")/../config/r2-orphans.txt"
gone=0 stuck=0
while IFS= read -r f; do
  [ -n "$f" ] || continue
  if "$RCLONE" deletefile "$REMOTE/$f" --retries 1 --low-level-retries 2 >/dev/null 2>&1; then
    echo "удалён   $f"; gone=$((gone+1))
  else
    echo "залип    $f"; stuck=$((stuck+1))
  fi
done < "$LIST"
echo "удалено $gone, осталось $stuck"
[ "$stuck" -eq 0 ]
