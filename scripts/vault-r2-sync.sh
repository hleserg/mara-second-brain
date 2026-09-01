#!/usr/bin/env bash
# Двусторонний синк волта с R2 (ТЗ §2.1). Remotely Save — GUI-плагин Obsidian,
# на headless doctor его нет; эквивалент — rclone bisync к тому же бакету.
#
# Системный rclone на doctor 1.60 (2022), его bisync разваливается на первом
# конфликте. Берём свой бинарь из /opt, чужие бэкапы на общем rclone не трогаем.
set -euo pipefail

VAULT="${VAULT:-/srv/vault}"
REMOTE="${REMOTE:-r2:mara-vault}"
RCLONE="${RCLONE:-/opt/rclone/rclone}"
LOCK="${LOCK:-$VAULT/.git/vault-git.lock}"   # общий с vault-git.sh: §13.8
FILTERS="$(dirname "${BASH_SOURCE[0]}")/../config/r2-filters.txt"
STATE="$HOME/.cache/rclone/bisync"

exec 9>"$LOCK"
flock -w 300 9 || { echo "vault-r2-sync: занято, пропускаю" >&2; exit 0; }

args=(bisync "$VAULT" "$REMOTE"
  --filters-file "$FILTERS"
  --conflict-resolve newer
  # Потолок от «волт внезапно опустел». Разовая большая чистка проходит
  # надзорным прогоном: MAX_DELETE=100 scripts/vault-r2-sync.sh
  --max-delete "${MAX_DELETE:-25}"
  --resilient --recover
  # Свой lock-файл bisync ставит без срока годности (до 2226 года): один
  # прибитый прогон — и все следующие падают с "prior lock file found".
  # 15 минут живой прогон продлевает сам, зависший — отпускает.
  --max-lock 15m
  # R2 умеет висеть на соединении молча. Без этих двух прогон стоял 15 минут
  # с нулевым CPU, держа общий с vault-git.sh флок.
  --contimeout 20s --timeout 60s
  # R2 периодически отвечает 500 InternalError на PutObject. Без этого
  # bisync падает с одной попытки и требует --resync для восстановления.
  --retries 3 --low-level-retries 10)

# Первый прогон обязан быть --resync.
#
# ⚠️ --resync делает пути одинаковыми в ОБЕ стороны: файл, который есть только
# в R2, он копирует в волт. После перестройки структуры это воскрешает всё,
# что мы только что удалили или перенесли, — и каждый сорванный прогон
# воскрешает по новой порции. Поэтому перед resync проверяем, что в R2 нет
# ничего лишнего, и отказываемся работать, если есть.
# .lst-old остаётся от прерванного прогона: базовая линия жива, её поднимет
# --recover. Без этой проверки любой убитый bisync выглядел бы как «синка
# никогда не было» и тянул за собой лишний --resync.
shopt -s nullglob
have_state=("$STATE"/*.lst "$STATE"/*.lst-old)   # через ls нельзя: pipefail
shopt -u nullglob                                # ловит несовпавший глоб
if [ ${#have_state[@]} -eq 0 ]; then
  # LC_ALL=C нужен и comm тоже: он сверяет порядок своей локалью, и при
  # кириллице расходится с тем, чем сортировали, — сыплет "not in sorted order"
  # и врёт результатом.
  stale=$(LC_ALL=C; export LC_ALL
          comm -23 <("$RCLONE" lsf -R --files-only --filter-from "$FILTERS" "$REMOTE" | sort) \
                   <(cd "$VAULT" && find . -type f -not -path "./.git/*" -printf "%P\n" | sort))
  if [ -n "$stale" ]; then
    echo "resync отменён: в R2 есть файлы, которых нет в волте." >&2
    echo "resync затащил бы их обратно. Удалить их из бакета или вернуть в волт:" >&2
    echo "$stale" | sed "s/^/  /" >&2
    exit 1
  fi
  args+=(--resync --resync-mode path1)
fi

# Жёсткий потолок: флок общий с автокоммитом, зависший прогон не должен держать
# его вечно. Обычный прогон занимает секунды; десять минут — это на --resync.
exec timeout -k 10s 10m "$RCLONE" "${args[@]}"
