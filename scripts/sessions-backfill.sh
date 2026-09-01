#!/usr/bin/env bash
# Подбор карточек по сырью на doctor (ТЗ §6.1, §6.2). Закрывает три дыры:
#   - у Codex хуков нет вообще, карточки делаются только здесь;
#   - хук Claude Code мог не сработать: краш, сессия с мака, длинный оффлайн;
#   - карточка потерялась при чистке волта.
# Идемпотентно: --skip-existing не трогает готовое.
#
# Только свежее сырьё: разовый прогон всей истории — это bootstrap этапа 4 на
# GTR, с настоящей дистилляцией. Вываливать сюда 500 механических карточек ни
# к чему, они всё равно ждут очереди.
set -euo pipefail
VAULT="${VAULT:-/srv/vault}"
DAYS="${DAYS:-3}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
before=$(ls "$VAULT/kb/sessions" | wc -l)

for src in claude-code codex; do
  d="$VAULT/raw/$src"
  [ -d "$d" ] || continue
  while IFS= read -r -d '' f; do
    rel="${f#$VAULT/}"
    python3 "$HERE/session-note.py" "$f" --vault "$VAULT" \
      --raw-rel "$rel" --skip-existing --skip-empty || true
  done < <(find "$d" -name '*.jsonl' -mtime "-$DAYS" -print0)
done

echo "sessions-backfill: сырьё за $DAYS сут, карточек стало $(ls "$VAULT/kb/sessions" | wc -l) (было $before)"
