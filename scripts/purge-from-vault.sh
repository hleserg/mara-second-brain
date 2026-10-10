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
REMOTE="${REMOTE:-r2:mara-vault}"
RCLONE="${RCLONE:-/opt/rclone/rclone}"
FR="${FR:-$HOME/.local/bin/git-filter-repo}"
# Носители бандлов — один список на снос старых и на сборку нового
# (`vault-backup.sh` читает TARGETS): два имени для одного списка на doctor
# дали бы снос в одном месте и запись в другом. BUNDLES оставлен как синоним.
TARGETS="${TARGETS:-${BUNDLES:-/mnt/backup/mara /mnt/win-backups/mara}}"
export TARGETS

[ $# -ge 1 ] || { echo "usage: $0 <путь-в-волте> [...]" >&2; exit 2; }
case $REMOTE in
  *:?*) ;;
  *) echo "REMOTE=$REMOTE: нужен бакет (remote:bucket)" >&2; exit 2 ;;
esac

# Пути — только относительные, к файлам, без `..` и `./`: git-filter-repo
# сравнивает строки как есть, и `./x` для него не `x` — история осталась бы,
# а R2 на `./x` ответил бы «нет». Каталог тоже не берём: `rm -f` на нём
# молчит, а R2 отвечает содержимым, в котором точного имени нет.
paths=()      # имя латиницей: массив по-русски bash не заведёт
for f in "$@"; do
  while [ "${f#./}" != "$f" ]; do f=${f#./}; done
  case $f in
    ""|/*|..|../*|*/..|*/../*|*/)
      echo "$f: путь относительно волта, без ведущего /, без .. и без хвостового /" >&2
      exit 2 ;;
  esac
  [ -d "$VAULT/$f" ] && { echo "$f — каталог, скрипт чистит файлы" >&2; exit 2; }
  paths+=("$f")
done
set -- "${paths[@]}"

# R2 — единственная копия, до которой скрипт дотягивается по сети, и отказ
# сети здесь нельзя читать как «файла нет»: при недоступном R2 файл в бакете
# остаётся, а скрипт отчитался бы нулём. Поэтому ответ R2 разбирается явно,
# по кодам rclone: 3 — каталога нет, 4 — файла нет; всё остальное — отказ.
ERR=$(mktemp); trap 'rm -f "$ERR"' EXIT
в_r2() {                 # 0 — файл в R2 есть, 1 — нет, 2 — R2 не ответил
  local out rc=0
  out=$("$RCLONE" lsf --files-only "$REMOTE/$1" 2>"$ERR") || rc=$?
  case $rc in
    0) grep -qxF -- "$(basename -- "$1")" <<<"$out" ;;
    3|4) return 1 ;;
    *) echo "  R2 не ответил ($1, код $rc): $(tr '\n' ' ' <"$ERR")" >&2; return 2 ;;
  esac
}

echo "== проверяю доступ к R2"
if ! "$RCLONE" lsf --retries=1 "$REMOTE" >/dev/null 2>"$ERR"; then
  echo "R2 недоступен ($REMOTE): $(tr '\n' ' ' <"$ERR")" >&2
  echo "вычистка не начата, повторите при живом доступе" >&2
  exit 1
fi

exec 9>"$VAULT/.git/vault-git.lock"
flock -w 300 9 || { echo "волт занят" >&2; exit 1; }

cd "$VAULT"
git add -A
git diff --cached --quiet || git commit -q -m "auto: перед вычисткой"

echo "== удаляю из рабочей копии и из R2"
for f in "$@"; do
  [ -e "$f" ] && rm -f -- "$f" && echo "  локально: $f"
  # Останавливаемся до переписывания истории: повторный прогон при живом R2
  # доделает всё с этого же места, а история, переписанная при файле в
  # бакете, создала бы ложное «чисто».
  rc=0; в_r2 "$f" || rc=$?
  case $rc in
    0)
      if ! "$RCLONE" deletefile "$REMOTE/$f" 2>"$ERR"; then
        echo "  R2: не удалось удалить $f: $(tr '\n' ' ' <"$ERR")" >&2
        echo "вычистка остановлена до переписывания истории" >&2; exit 1
      fi
      echo "  R2:       $f" ;;
    1) echo "  R2:       $f (уже нет)" ;;
    *) echo "вычистка остановлена до переписывания истории" >&2; exit 1 ;;
  esac
done
git add -A
git diff --cached --quiet || git commit -q -m "удалены файлы, подлежащие вычистке"

echo "== переписываю историю"
# Маркер прошлого прогона старше суток заставляет filter-repo спрашивать «Y/N»
# из stdin, которого у крона и ssh без tty нет: EOFError и вычистка встаёт.
# С --force маркер не нужен: проверки «свежий клон» и так отключены.
rm -f "$VAULT/.git/filter-repo/already_ran"
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
for t in $TARGETS; do
  [ -d "$t" ] || continue
  rm -f "$t"/vault-*.bundle.gpg && echo "  $t"
done
# Старые бандлы уже снесены: без нового копии волта вне doctor нет, и
# молчать об этом нулевым кодом нельзя — отказ сборки идёт в итоговый код.
fail=0
"$HERE/vault-backup.sh" || {
  echo "  НОВЫЙ БАНДЛ НЕ СОБРАЛСЯ: старые снесены, соберите vault-backup.sh руками" >&2
  fail=1; }

echo "== проверка"
# Не `git log … | grep -q .`: под pipefail grep закрывает трубу на первой
# строке, git log на длинной истории ловит SIGPIPE (141) — и это читалось
# бы как «чисто». Один хеш через подстановку — без трубы.
for f in "$@"; do
  if [ -n "$(git log --all --full-history --format=%H -n1 -- "$f")" ]; then
    echo "  ОСТАЛОСЬ В ИСТОРИИ: $f" >&2; fail=1
  else
    echo "  чисто в git: $f"
  fi
  rc=0; в_r2 "$f" || rc=$?
  case $rc in
    0) echo "  ОСТАЛОСЬ В R2: $f" >&2; fail=1 ;;
    1) echo "  чисто в R2:  $f" ;;
    *) echo "  R2 НЕ ОТВЕТИЛ: $f — проверьте в R2 руками" >&2; fail=1 ;;
  esac
done
exit $fail
