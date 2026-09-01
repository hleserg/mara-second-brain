#!/usr/bin/env bash
# Вычистить файлы из волта насовсем: рабочая копия, история git, зеркало, R2.
# ТЗ §11: секрет, попавший в волт, удаляется из истории, а не просто из файла.
#
# Историю переписывает git-filter-repo — все хеши коммитов меняются, поэтому
# bare-зеркало пересоздаётся с нуля, а не пушится поверх.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
VAULT="${VAULT:-/srv/vault}"
MIRROR="${MIRROR:-/srv/backup/vault.git}"
REMOTE="${REMOTE:-r2:obsidian-vault}"
RCLONE="${RCLONE:-/opt/rclone/rclone}"
FR="${FR:-$HOME/.local/bin/git-filter-repo}"

[ $# -ge 1 ] || { echo "usage: $0 <путь-в-волте> [...]" >&2; exit 2; }

exec 9>"$VAULT/.git/vault-git.lock"
flock -w 300 9 || { echo "волт занят" >&2; exit 1; }

cd "$VAULT"
git add -A
git diff --cached --quiet || git commit -q -m "auto: перед вычисткой"

echo "== удаляю из рабочей копии и из R2"
for f in "$@"; do
  [ -e "$f" ] && rm -f -- "$f" && echo "  локально: $f"
  "$RCLONE" deletefile "$REMOTE/$f" 2>/dev/null && echo "  R2:       $f" || echo "  R2:       $f (уже нет)"
done
git add -A
git diff --cached --quiet || git commit -q -m "удалены файлы, подлежащие вычистке"

echo "== переписываю историю"
args=(); for f in "$@"; do args+=(--path "$f"); done
"$FR" --invert-paths "${args[@]}" --force
git reflog expire --expire=now --all
git gc --prune=now -q

echo "== пересоздаю зеркало"
rm -rf "$MIRROR"
git init -q --bare -b main "$MIRROR"
git push -q --mirror "$MIRROR"
git -C "$MIRROR" symbolic-ref HEAD refs/heads/main

# Бандл — снимок всей истории. Пока старые бандлы лежат на носителях, секрет
# из истории никуда не делся: он там, просто под шифром. Сносим и делаем новый.
echo "== сношу старые бандлы"
for t in ${BUNDLES:-/mnt/backup/mara /mnt/win-backups/mara}; do
  [ -d "$t" ] || continue
  rm -f "$t"/vault-*.bundle.gpg && echo "  $t"
done
"$HERE/vault-backup.sh" || echo "  новый бандл не собрался, соберите руками" >&2

echo "== проверка"
fail=0
for f in "$@"; do
  if git log --all --full-history --oneline -- "$f" | grep -q .; then
    echo "  ОСТАЛОСЬ В ИСТОРИИ: $f" >&2; fail=1
  else
    echo "  чисто в git: $f"
  fi
  if "$RCLONE" lsf "$REMOTE/$f" 2>/dev/null | grep -q .; then
    echo "  ОСТАЛОСЬ В R2: $f" >&2; fail=1
  else
    echo "  чисто в R2:  $f"
  fi
done
exit $fail
