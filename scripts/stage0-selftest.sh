#!/usr/bin/env bash
# Приёмка этапа 0 (ТЗ §12): файл со стороннего устройства доезжает до doctor
# и попадает в автокоммит; служебное в R2 не уезжает.
# Имитирует телефон, кладя объект прямо в бакет — Obsidian делает ровно это.
set -euo pipefail

VAULT="${VAULT:-/srv/vault}"
REMOTE="${REMOTE:-r2:obsidian-vault}"
RCLONE="${RCLONE:-/opt/rclone/rclone}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NAME="_selftest-$(date +%s).md"
fail=0

echo "1. кладу $NAME в R2 (как будто с телефона)"
printf '# selftest\n%s\n' "$(date -Iseconds)" | "$RCLONE" rcat "$REMOTE/$NAME"

echo "2. синк"
"$HERE/vault-r2-sync.sh" >/dev/null

echo -n "3. файл на doctor: "
if [ -f "$VAULT/$NAME" ]; then echo ок; else echo "НЕТ"; fail=1; fi

echo "4. автокоммит"
"$HERE/vault-git.sh" commit

echo -n "5. файл в git: "
if git -C "$VAULT" log --oneline -1 -- "$NAME" | grep -q .; then echo ок; else echo "НЕТ"; fail=1; fi

echo -n "6. служебное не в R2: "
leak=$("$RCLONE" lsf -R "$REMOTE" | grep -E '^\.git/|^\.smart-env/|^\.basic-memory/' || true)
if [ -z "$leak" ]; then echo ок; else echo "УТЕЧКА:"; echo "$leak"; fail=1; fi

echo "7. уборка"
rm -f "$VAULT/$NAME"
"$RCLONE" deletefile "$REMOTE/$NAME"
"$HERE/vault-r2-sync.sh" >/dev/null
"$HERE/vault-git.sh" commit

[ "$fail" -eq 0 ] && echo "ЭТАП 0: приёмка пройдена" || { echo "ЭТАП 0: ПРОВАЛ" >&2; exit 1; }
