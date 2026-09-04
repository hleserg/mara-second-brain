#!/usr/bin/env bash
# Третья копия волта (ТЗ §12): зашифрованный git bundle на внешние носители.
#
# Копий формально три, но защищают они от разного. /srv/vault и bare-зеркало
# /srv/backup/vault.git лежат на одном физическом диске: он умрёт — умрут обе.
# R2 — синк, а не бэкап: удаление и порча уезжают туда за пять минут и там же
# затирают хорошее. Бандл этим не болеет — он лежит и не меняется.
#
# Шифруем, потому что в git лежит raw/ — сырьё разговоров с Марой и рабочие
# логи. На сетевую шару соседней машины оно в открытом виде не поедет.
#
# Парольная фраза лежит только на doctor. Умрёт doctor — бандлы без неё
# макулатура, поэтому копия фразы в менеджере паролей у владельца обязательна
# (issue #1). Проверить, что бандл вообще разворачивается, — vault-restore-test.sh.
set -euo pipefail

MIRROR="${MIRROR:-/srv/backup/vault.git}"
PASS="${PASS:-$HOME/.config/mara/backup-pass}"
WORK="${WORK:-/var/tmp/mara-backup}"
KEEP="${KEEP:-8}"                     # 8 недель по ~150 МБ на носитель
# Два носителя: внешний диск и шара на соседней машине. Второй может быть
# отключён — это не повод валить прогон.
TARGETS="${TARGETS:-/mnt/backup/mara /mnt/win-backups/mara}"
# Пробел разделяет список, поэтому пробел внутри пути не поддерживается: такой
# путь распадается на два несуществующих, оба тихо пропускаются по continue —
# и бэкапа нет, а скрипт доволен. Относительный кусок — верный признак этого.
for t in $TARGETS; do
  case "$t" in
    /*) ;;
    *) echo "vault-backup: «$t» — путь не абсолютный; носители разделяются" \
            "пробелами, пробел внутри пути не поддерживается" >&2; exit 1;;
  esac
done

[ -s "$PASS" ] || { echo "vault-backup: нет парольной фразы $PASS" >&2; exit 1; }
[ "$(stat -c %a "$PASS")" = 600 ] || { echo "vault-backup: $PASS должен быть 600" >&2; exit 1; }
[ -d "$MIRROR" ] || { echo "vault-backup: нет зеркала $MIRROR" >&2; exit 1; }

# Носитель обязан быть отдельным устройством. Причина и подробности — в
# `смонтирован()` в scripts/mara_ingest.py: у python-бэкапа ядра ровно та же
# дыра и то же лекарство. Коротко: mkdir -p ниже воссоздаст каталог на
# корневой ФС, если диск или шара отвалились, — и бандл ляжет на тот же диск,
# что и волт с зеркалом, а скрипт отрапортует успехом.
# MARA_BACKUP_ALLOW_SAME_DEV=1 снимает требование.
dev_of() {                            # устройство ближайшего существующего предка
  local p="$1"
  while [ ! -e "$p" ]; do p=$(dirname "$p"); done
  stat -c %d "$p"
}
mirror_dev=$(dev_of "$MIRROR")

name="vault-$(date +%F).bundle.gpg"
mkdir -p "$WORK"
bundle="$WORK/vault.bundle"
enc="$WORK/$name"
trap 'rm -f "$bundle" "$enc"' EXIT

# --all, а не одна ветка: бандл должен разворачиваться в полноценный репозиторий.
git -C "$MIRROR" bundle create -q "$bundle" --all
git -C "$MIRROR" bundle verify "$bundle" >/dev/null

# loopback обязателен: без него gpg под кроном садится ждать pinentry.
gpg --batch --yes --quiet --pinentry-mode loopback --passphrase-file "$PASS" \
    --symmetric --cipher-algo AES256 -o "$enc" "$bundle"

ok=0
for t in $TARGETS; do
  if [ -z "${MARA_BACKUP_ALLOW_SAME_DEV:-}" ] && [ "$(dev_of "$t")" = "$mirror_dev" ]; then
    echo "vault-backup: $t на одном устройстве с $MIRROR — носитель не смонтирован, пропускаю" >&2
    continue
  fi
  if ! mkdir -p "$t" 2>/dev/null; then
    echo "vault-backup: $t недоступен, пропускаю" >&2; continue
  fi
  # Временный файл на том же носителе: переименование между монтированиями
  # не атомарно, а на CIFS ещё и не всегда работает.
  if cp "$enc" "$t/.$name.tmp" && mv "$t/.$name.tmp" "$t/$name"; then
    ok=$((ok + 1))
    # Ротация по счёту: имена с датой сортируются как даты.
    ls -1 "$t"/vault-*.bundle.gpg 2>/dev/null | head -n "-$KEEP" | xargs -r rm -f
    echo "vault-backup: $t/$name ($(du -h "$t/$name" | cut -f1))"
  else
    rm -f "$t/.$name.tmp"
    echo "vault-backup: $t не записался" >&2
  fi
done

[ "$ok" -gt 0 ] || { echo "vault-backup: ни один носитель не записан" >&2; exit 1; }
