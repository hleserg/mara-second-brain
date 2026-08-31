#!/usr/bin/env bash
# Историк волта. commit — раз в 15 минут, push — раз в час (см. ТЗ §2.1).
# Оба под одним flock: два процесса не лезут в один .git (§13.8).
set -euo pipefail

VAULT="${VAULT:-/srv/vault}"
MIRROR="${MIRROR:-/srv/backup/vault.git}"
LOCK="${LOCK:-/run/lock/vault-git.lock}"

usage() { echo "usage: $0 {commit|push}" >&2; exit 2; }
[ $# -eq 1 ] || usage

exec 9>"$LOCK"
flock -w 60 9 || { echo "vault-git: занято, пропускаю" >&2; exit 0; }

cd "$VAULT"

case "$1" in
  commit)
    git add -A
    git diff --cached --quiet && exit 0   # нечего коммитить — тихо выходим
    git commit -q -m "auto: $(date -Iseconds)"
    ;;
  push)
    git push -q --mirror "$MIRROR"
    ;;
  *) usage ;;
esac
