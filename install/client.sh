#!/usr/bin/env bash
# Клиентская часть этапа 2: машина, где живут Claude Code и Codex.
# Ставит хук SessionEnd, туннель к MCP на doctor, зеркалку Codex. Идемпотентно.
#
#   ./install/client.sh            # Linux (systemd --user) или macOS (launchd)
#
# Переменные: DOCTOR (ssh-хост волта), MARA_VAULT_SSH (куда доставлять).
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DOCTOR="${DOCTOR:-doctor}"
DEST="${MARA_VAULT_SSH:-$DOCTOR:/srv/vault}"
say() { printf '\n== %s\n' "$*"; }

say "проверка ssh до $DOCTOR"
ssh -o BatchMode=yes -o ConnectTimeout=10 "$DOCTOR" true \
  || { echo "нет беспарольного ssh до $DOCTOR — сначала ssh-copy-id" >&2; exit 1; }

say "хук SessionEnd в ~/.claude/settings.json"
python3 - "$REPO" <<'PY'
import json, os, sys
p = os.path.expanduser("~/.claude/settings.json")
s = json.load(open(p)) if os.path.exists(p) else {}
cmd = os.path.join(sys.argv[1], "hooks/claude-session-end.sh")
e = s.setdefault("hooks", {}).setdefault("SessionEnd", [])
if any(cmd in h.get("command", "") for g in e for h in g.get("hooks", [])):
    print("уже стоит"); raise SystemExit
e.append({"hooks": [{"type": "command", "command": cmd, "timeout": 120}]})
os.makedirs(os.path.dirname(p), exist_ok=True)
json.dump(s, open(p, "w"), ensure_ascii=False, indent=2)
print("добавлен", cmd)
PY

say "туннель к Basic Memory MCP (§11: наружу порт не открываем)"
TUN="/usr/bin/ssh -N -T -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 \
-o ServerAliveCountMax=3 -L 127.0.0.1:8787:127.0.0.1:8787 $DOCTOR"
if [ "$(uname)" = "Darwin" ]; then
  P="$HOME/Library/LaunchAgents/ru.mara.mcp-tunnel.plist"
  mkdir -p "$(dirname "$P")"
  { echo '<?xml version="1.0" encoding="UTF-8"?>'
    echo '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">'
    echo '<plist version="1.0"><dict>'
    echo '<key>Label</key><string>ru.mara.mcp-tunnel</string>'
    echo '<key>ProgramArguments</key><array>'
    for a in /usr/bin/ssh -N -T -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 \
             -o ServerAliveCountMax=3 -L 127.0.0.1:8787:127.0.0.1:8787 "$DOCTOR"; do
      echo "  <string>$a</string>"; done
    echo '</array>'
    echo '<key>RunAtLoad</key><true/><key>KeepAlive</key><true/>'
    echo '</dict></plist>'; } > "$P"
  launchctl bootout "gui/$(id -u)/ru.mara.mcp-tunnel" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$P"
else
  mkdir -p ~/.config/systemd/user
  cat > ~/.config/systemd/user/mara-mcp-tunnel.service <<UNIT
[Unit]
Description=Туннель к Basic Memory MCP на doctor (ТЗ §11: только loopback)
After=network-online.target
[Service]
ExecStart=$TUN
Restart=always
RestartSec=10
[Install]
WantedBy=default.target
UNIT
  systemctl --user daemon-reload
  systemctl --user enable --now mara-mcp-tunnel.service
  loginctl enable-linger "$USER" 2>/dev/null || true
fi

say "MCP в клиентах"
claude mcp add --transport http --scope user basic-memory http://127.0.0.1:8787/mcp 2>&1 | tail -1 || true
command -v codex >/dev/null && { codex mcp add basic-memory --url http://127.0.0.1:8787/mcp 2>&1 | tail -1 || true; }

say "read-only для Basic Memory (ADR-0009 решение 3)"
# Сервер прав не различает, отказ живёт в конфиге клиента — и у Claude Code
# с Codex он разный. Правит только эти два файла, чужие правила не трогает.
python3 "$REPO/scripts/mcp-readonly.py"

say "зеркалка Codex по расписанию (у Codex хуков нет, §6.2)"
LINE="10 * * * * MARA_VAULT_SSH=$DEST $REPO/scripts/codex-mirror.sh # mara-second-brain"
( crontab -l 2>/dev/null | grep -v 'codex-mirror.sh'; echo "$LINE" ) | crontab -

say "готово"
echo "проверить: systemctl --user status mara-mcp-tunnel  (или launchctl list | grep mara)"
echo "           claude mcp list | grep basic-memory"
echo "           python3 scripts/mcp-readonly.py --check"
