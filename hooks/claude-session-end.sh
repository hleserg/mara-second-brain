#!/usr/bin/env bash
# SessionEnd-хук Claude Code (ТЗ §6.1). Ставится на машину, где живёт Claude
# Code (beta-pi, мак), а волт — на doctor. Отсюда спул:
#
#   карточка + сырьё → ~/.local/state/mara/spool/ (форма волта) → rsync → волт
#
# Спул он же очередь ретраев: --remove-source-files стирает только доехавшее,
# следующий хук доносит остаток. Поэтому «сети нет» не теряет сессию (§этап 2).
#
# Хук обязан не мешать выходу из сессии: любая ошибка — тихий exit 0.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SPOOL="${MARA_SPOOL:-$HOME/.local/state/mara/spool}"
DEST="${MARA_VAULT:-${MARA_VAULT_SSH:-doctor:/srv/vault}}"

payload="$(cat)"
{ read -r sid; read -r tp; } < <(printf '%s' "$payload" | python3 -c '
import json,sys
d=json.load(sys.stdin)
print(d.get("session_id",""))
print(d.get("transcript_path",""))' 2>/dev/null)

if [ -n "${tp:-}" ] && [ -r "$tp" ]; then
  python3 "$HERE/../scripts/session-note.py" "$tp" ${sid:+--session-id "$sid"} --vault "$SPOOL"
  # Сырьё кладём рядом с карточкой: ночной rsync с beta-pi приедет только в
  # 04:30, а карточка ссылается на файл сразу.
  raw="$SPOOL/raw/claude-code/$(basename "$(dirname "$tp")")"
  mkdir -p "$raw" && cp -f "$tp" "$raw/${sid:-$(basename "$tp" .jsonl)}.jsonl"
fi

# rsync сам пишет во временный файл и переименовывает — гонки с автокоммитом
# (*/15) и bisync (*/5) на полузаписанной карточке не будет.
[ -d "$SPOOL" ] || exit 0
rsync -a --remove-source-files "$SPOOL/" "$DEST/" >/dev/null 2>&1 \
  && find "$SPOOL" -mindepth 1 -type d -empty -delete 2>/dev/null
exit 0
