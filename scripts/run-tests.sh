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
# Presidio живёт в венве на doctor, а не в системном питоне: гоняем self-check
# им, если он есть, иначе системным.
PY=python3
[ -x "$HOME/.local/share/mara/venv/bin/python" ] && PY="$HOME/.local/share/mara/venv/bin/python"
for f in scripts/*.py; do
  grep -q -- "--self-check" "$f" || continue
  if out=$("$PY" "$f" --self-check 2>&1); then
    echo "ok   $f"
  elif [ "$f" = "scripts/redact.py" ] && printf '%s' "$out" | grep -q "Presidio НЕ готов"; then
    # Не провал, а неготовое окружение: слой 1 (регэкспы) проверен, слой 2
    # физически отсутствует. Красный на каждой машине без венва — это гейт,
    # который перестают читать.
    echo "skip $f — нет Presidio в этом окружении"
  else
    echo "FAIL $f"; echo "$out" | tail -3 | sed 's/^/     /'; fail=1
  fi
done
exit $fail
