#!/usr/bin/env bash
# Тест восстановления (ТЗ §12), раз в квартал: бэкап, который ни разу не
# разворачивали, — не бэкап, а надежда.
#
# Разворачиваем свежий бандл с носителя и проверяем, что получился волт, а не
# пустой каталог: расшифровка, целостность бандла, клон, родословная сходится с
# зеркалом, заметки на месте.
#
# Переиндексацию Basic Memory сюда не тащим: она идёт часами и требует своего
# окружения, а проверяет то же самое — что markdown на месте. Прогнали руками
# один раз при заводке (README, отступление про бэкапы).
set -euo pipefail

MIRROR="${MIRROR:-/srv/backup/vault.git}"
PASS="${PASS:-$HOME/.config/mara/backup-pass}"
FROM="${FROM:-/mnt/backup/mara}"
MIN_NOTES="${MIN_NOTES:-100}"     # столько .md в волте есть всегда
CHECK="${CHECK:-raw/README.md}"   # файл, который обязан быть непустым

# Тот же вопрос, что у vault-backup.sh: каталог на корневой ФС бэкап себе
# создаёт сам, и развернуть бандл из него значит доложить «копия жива» ровно
# про ту копию, которой нет. Разбор — в `смонтирован()` в scripts/mara_ingest.py.
dev_of() {                            # устройство ближайшего существующего предка
  local p="$1"
  while [ ! -e "$p" ]; do p=$(dirname "$p"); done
  stat -c %d "$p"
}
if [ -z "${MARA_BACKUP_ALLOW_SAME_DEV:-}" ] && [ "$(dev_of "$FROM")" = "$(dev_of "$MIRROR")" ]; then
  echo "restore-test: $FROM на одном устройстве с $MIRROR — носитель не смонтирован" >&2
  exit 1
fi

src=$(ls -1 "$FROM"/vault-*.bundle.gpg 2>/dev/null | tail -1)
[ -n "$src" ] || { echo "restore-test: в $FROM нет бандлов" >&2; exit 1; }

tmp=$(mktemp -d /var/tmp/mara-restore.XXXXXX)
trap 'rm -rf "$tmp"' EXIT
echo "== беру $src, ему $(( ( $(date +%s) - $(stat -c %Y "$src") ) / 86400 )) сут."

gpg --batch --quiet --pinentry-mode loopback --passphrase-file "$PASS" \
    -o "$tmp/vault.bundle" -d "$src"
git bundle verify "$tmp/vault.bundle" >/dev/null
echo "== бандл целый"

git clone -q "$tmp/vault.bundle" "$tmp/vault"
head=$(git -C "$tmp/vault" rev-parse HEAD)
# Тот ли это волт: коммит из бандла обязан быть в зеркале. Сверять HEAD с
# HEAD зеркала нельзя — бандл недельной давности отстаёт по определению.
git -C "$MIRROR" cat-file -e "$head^{commit}"
echo "== родословная сходится с зеркалом ($head)"

n=$(find "$tmp/vault" -name '*.md' -not -path '*/.git/*' | wc -l)
[ "$n" -ge "$MIN_NOTES" ] || { echo "restore-test: заметок всего $n" >&2; exit 1; }
[ -s "$tmp/vault/$CHECK" ] || { echo "restore-test: нет $CHECK" >&2; exit 1; }
echo "== развернулось: заметок $n, $CHECK на месте"
echo "restore-test: ок"
