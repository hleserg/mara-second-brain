#!/usr/bin/env bash
# Расписание Мары из репозитория в crontab (ТЗ §5, P0-5 аудита).
#
# До этого скрипта расписание существовало только на живой машине: два десятка
# строк, поставленных руками за несколько месяцев. Переустановка doctor
# означала бы восстановление по памяти, а расхождение репозитория с живым
# кроном не заметил бы никто.
#
#   install-cron.sh            показать расхождение, ничего не менять
#   install-cron.sh --apply    поставить, сделав копию текущего crontab
#
# Чужие строки (не из этого репозитория) не трогаются: на doctor живут кроны
# соседних проектов.
set -euo pipefail

REPO="${REPO:-$(cd "$(dirname "$0")/.." && pwd)}"
STATE="${STATE:-$HOME/.local/state/mara}"
VENV="${VENV:-$HOME/.local/share/mara/venv/bin/python}"
TPL="${TPL:-$REPO/install/mara.cron}"
BEGIN="# >>> mara-second-brain: install/mara.cron, правки руками затрёт >>>"
END="# <<< mara-second-brain <<<"

рендер() {
  printf '%s\n' "$BEGIN"
  sed -e "s|@REPO@|$REPO|g" -e "s|@STATE@|$STATE|g" -e "s|@VENV@|$VENV|g" "$TPL"
  printf '%s\n' "$END"
}

живой() { crontab -l 2>/dev/null || true; }

# Блок между маркерами. Нет маркеров — пусто, значит ставим впервые.
блок() { живой | awk -v b="$BEGIN" -v e="$END" '$0==b{f=1} f{print} $0==e{f=0}'; }

# Строки Мары мимо блока: те самые, поставленные руками. Их установщик
# заменяет своими, поэтому показать их до применения обязательно.
#
# Ищем два написания одного пути: полное и через тильду. Половина строк на
# doctor записана как ~/mara-second-brain/..., и по полному пути они не
# находятся — а значит, пережили бы установку и работали бы вторым экземпляром.
наши() { grep -F "$@" -e "$REPO/" -e "~/${REPO#$HOME/}/"; }

мимо() {
  живой | awk -v b="$BEGIN" -v e="$END" '$0==b{f=1} !f{print} $0==e{f=0}' \
        | наши || true
}

mode="${1:---check}"
case "$mode" in
  --check)
    drift=0
    if ! diff -u <(блок) <(рендер) > /tmp/mara-cron-diff.$$; then
      echo "== crontab расходится с репозиторием:"; cat /tmp/mara-cron-diff.$$
      drift=1
    else
      echo "== блок в crontab совпадает с install/mara.cron"
    fi
    rm -f /tmp/mara-cron-diff.$$
    if [ -n "$(мимо)" ]; then
      echo "== строки Мары мимо блока (их заменит --apply):"; мимо | sed 's/^/   /'
      drift=1
    fi
    exit $drift
    ;;
  --apply) ;;
  *) echo "использование: $0 [--check|--apply]" >&2; exit 2 ;;
esac

mkdir -p "$STATE"
backup="$STATE/crontab-$(date +%Y%m%dT%H%M%S).bak"
живой > "$backup"
echo "== копия текущего crontab: $backup"

# Собираем: чужие строки как есть, затем наш блок. Свои старые строки
# (и блок, и поставленные руками) выкидываем — их заменяет шаблон.
# Сначала целиком в файл и только потом в crontab: читать и писать одну и ту
# же таблицу в одном конвейере — способ однажды остаться без расписания.
newtab=$(mktemp)
trap 'rm -f "$newtab"' EXIT
{
  живой | awk -v b="$BEGIN" -v e="$END" '$0==b{f=1} !f{print} $0==e{f=0}' \
        | { наши -v || true; }
  рендер
} > "$newtab"
crontab "$newtab"

# Проверяем не то, что отправили, а то, что crontab принял.
if diff -u <(блок) <(рендер) > /tmp/mara-cron-verify.$$; then
  echo "== поставлено, строк Мары: $(рендер | grep -cE '^[0-9*]')"
else
  echo "== crontab принял не то, что отправляли:" >&2
  cat /tmp/mara-cron-verify.$$ >&2
  echo "== возврат из $backup" >&2
  crontab "$backup"
  rm -f /tmp/mara-cron-verify.$$
  exit 1
fi
rm -f /tmp/mara-cron-verify.$$
