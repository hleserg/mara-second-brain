#!/usr/bin/env bash
# Этап 0 из ТЗ §12: волт, git, bare-зеркало, крон. Идемпотентно.
set -euo pipefail

VAULT="${VAULT:-/srv/vault}"
MIRROR="${MIRROR:-/srv/backup/vault.git}"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

say() { printf '\n== %s\n' "$*"; }

say "каталоги"
sudo mkdir -p "$VAULT" "$(dirname "$MIRROR")"
sudo chown "$(id -u):$(id -g)" "$VAULT" "$(dirname "$MIRROR")"

say "структура волта (ТЗ §3)"
cd "$VAULT"
mkdir -p raw/{claude-code,codex,claude-chat,hermes,untrusted} \
         kb/{decisions,howto,sessions,notes} \
         entities/{people,projects,tools,places,concepts} \
         daily timeline archive _system/{queue,prompts}
# git не хранит пустые каталоги
find raw kb entities daily timeline archive _system -type d -empty -exec touch {}/.gitkeep \;

cat > .gitignore <<'IGN'
.smart-env/
.basic-memory/
.obsidian/workspace*.json
# производный индекс плагина copilot, 85 МБ, восстановим (ТЗ §1)
.obsidian/copilot-index-*.json
*.tmp
.DS_Store
IGN

[ -f raw/README.md ] || cat > raw/README.md <<'RAW'
# raw/ — сырое зеркало

Append-only. Человек сюда не ходит, руками не правит.
Дистиллят живёт в `kb/`. Транскрипты сюда не копируются обратно (ТЗ §13.10).
RAW

say "git-репозиторий волта"
if [ ! -d .git ]; then
  git init -q -b main
  git config user.name  "vault-autocommit"
  git config user.email "vault@doctor.local"
fi
git add -A
git diff --cached --quiet || git commit -q -m "vault: структура из ТЗ §3"

say "bare-зеркало $MIRROR"
[ -d "$MIRROR" ] || git init -q --bare -b main "$MIRROR"
git -C "$MIRROR" symbolic-ref HEAD refs/heads/main
git push -q --mirror "$MIRROR"

say "парольная фраза для бэкапов"
# Бандлы шифруются симметрично; фраза живёт только тут. Генерим один раз и
# больше не трогаем: сменить её — значит потерять все прежние бандлы.
PASS="${PASS:-$HOME/.config/mara/backup-pass}"
if [ ! -s "$PASS" ]; then
  mkdir -p "$(dirname "$PASS")"
  (umask 077; head -c 32 /dev/urandom | base64 > "$PASS")
  echo "  создана $PASS — СКОПИРУЙТЕ ЕЁ В МЕНЕДЖЕР ПАРОЛЕЙ."
  echo "  Она лежит на том же doctor, что и бэкапы: сдохнет он — без копии"
  echo "  фразы бандлы не расшифровать."
else
  echo "  уже есть: $PASS"
fi
chmod 600 "$PASS"

say "крон: синк R2 /5 мин, коммит /15 мин, пуш /час, бэкап /неделю"
mkdir -p "$HOME/.local/state/mara"
tmp=$(mktemp)
# Вычищаем только свои строки, по именам скриптов. Раньше здесь стоял
# grep -v по тегу '# mara-second-brain' — и повторный запуск установщика
# сносил все остальные задачи с тем же тегом (ingest, реестр, очередь):
# идемпотентный по замыслу скрипт молча ломал этапы 2 и 4.
crontab -l 2>/dev/null \
  | grep -vE "scripts/(vault-r2-sync|vault-git|vault-backup|vault-restore-test)\.sh" > "$tmp" || true
cat >> "$tmp" <<CRON
*/5 * * * *  $REPO/scripts/vault-r2-sync.sh # mara-second-brain
*/15 * * * * $REPO/scripts/vault-git.sh commit # mara-second-brain
0 * * * *    $REPO/scripts/vault-git.sh push   # mara-second-brain
0 4 * * 1    $REPO/scripts/vault-backup.sh >> $HOME/.local/state/mara/backup.log 2>&1 # mara-second-brain
0 5 1 1,4,7,10 * $REPO/scripts/vault-restore-test.sh >> $HOME/.local/state/mara/backup.log 2>&1 # mara-second-brain
CRON
crontab "$tmp"; rm -f "$tmp"

say "готово"
git -C "$VAULT" log --oneline | head -3
crontab -l | grep -E "scripts/(vault-r2-sync|vault-git|vault-backup|vault-restore-test)\.sh"
