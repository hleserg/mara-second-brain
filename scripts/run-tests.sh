#!/usr/bin/env bash
# Весь прогон: юнит-тесты и все --self-check. Фреймворков нет принципиально —
# unittest из стандартной библиотеки умеет ровно то, что нужно, и не требует
# венва на doctor.
set -u
cd "$(dirname "$0")/.."
fail=0
echo "== unittest =="
python3 -m unittest discover -s tests -v || fail=1
echo
echo "== self-check =="
for f in scripts/*.py; do
  grep -q -- "--self-check" "$f" || continue
  if out=$(python3 "$f" --self-check 2>&1); then
    echo "ok   $f"
  else
    echo "FAIL $f"; echo "$out" | tail -3 | sed 's/^/     /'; fail=1
  fi
done
exit $fail
