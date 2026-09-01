#!/usr/bin/env bash
# Зеркало сессий Codex → raw/codex/ (ТЗ §6.2). Хуков у Codex нет, поэтому
# просто периодический rsync; карточки делает sessions-backfill.sh на doctor.
#
# ⚠️ ГВОЗДЬ §6.2: в WSL и в нативном Windows у Codex ДВА РАЗНЫХ набора сессий.
# Под WSL /mnt/c/Users/*/.codex — это windows-набор, его тоже забираем, иначе
# теряется половина. Каждый набор кладём под своей меткой, чтобы было видно,
# откуда сессия.
set -euo pipefail
DEST="${MARA_VAULT:-${MARA_VAULT_SSH:-doctor:/srv/vault}}"
TAG="${MARA_TAG:-$(hostname -s)}"

mirror() {  # <каталог sessions> <метка>
  [ -d "$1" ] || return 0
  rsync -a --include='*/' --include='*.jsonl' --exclude='*' \
    "$1/" "$DEST/raw/codex/$2/"
}

mirror "$HOME/.codex/sessions" "$TAG"
for w in /mnt/c/Users/*/.codex/sessions; do
  mirror "$w" "$TAG-win"
done
