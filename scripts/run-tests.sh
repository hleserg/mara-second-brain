#!/usr/bin/env bash
# Весь прогон: юнит-тесты и все --self-check. Фреймворков нет принципиально —
# unittest из стандартной библиотеки умеет ровно то, что нужно, и не требует
# венва на doctor.
set -u
cd "$(dirname "$0")/.."
# Гейт не должен зависеть от того, что лежит в ~/.config/mara/env: на
# doctor там боевые адреса и ключ OpenRouter, на BetaPi и в CI файла нет
# вовсе, и одни и те же тесты давали бы разный результат. Уводим чтение в
# несуществующий файл — заодно живой ключ не попадает в тестовый процесс.
export MARA_ENV_FILE=/nonexistent/mara-env
# Каждый tempfile.mkdtemp() в тестах и в --self-check живёт до перезагрузки:
# кто создал, тот и убирает, а не убирает почти никто. На BetaPi /tmp — tmpfs
# на 4 ГБ, и двадцать тысяч таких каталогов забили её на 93% (#33). Чинить в
# каждом тесте по отдельности значит чинить вечно: любой следующий mkdtemp
# снова потечёт. Поэтому границей ставим процесс — свою песочницу на прогон,
# и её целиком сносим на выходе. Отдельная переменная, а не $TMPDIR: если
# mktemp не сработает, rm -rf не должен получить чужой каталог.
# MARA_KEEP_TMP=1 — оставить песочницу: у красного гейта волт и база упавшего
# теста нужны именно после прогона, а не до.
sand="$(mktemp -d "${TMPDIR:-/tmp}/mara-tests.XXXXXX")" || exit 1
trap '[ -n "${MARA_KEEP_TMP:-}" ] && echo "песочница оставлена: $sand" || rm -rf "$sand"' EXIT
TMPDIR_ORIG="${TMPDIR:-/tmp}"
export TMPDIR="$sand"
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
# Плагин Мары живёт на маке, но исходник тут: без проверки он ломался бы там,
# где логов никто не читает.
if out=$("$PY" install/mara-context/__init__.py --demo 2>&1); then
  echo "ok   install/mara-context"
else
  echo "FAIL install/mara-context"; echo "$out" | tail -3 | sed 's/^/     /'; fail=1
fi

# Kotlin-ядро приложения. aapt2 и d8 Google выпускает только под x86_64,
# поэтому на BetaPi этого шага нет — сборка живёт на doctor. Правило простое:
# трогал android/ — прогони гейт на doctor, иначе сюда уедет несобираемое.
echo
echo "== android =="
if [ "${SKIP_ANDROID:-0}" = 1 ]; then
  echo "skip android — SKIP_ANDROID=1 (пропущен по запросу)"
elif [ -x android/gradlew ] && command -v java >/dev/null 2>&1; then
  # JVM берёт java.io.tmpdir из /tmp и на TMPDIR не смотрит — песочница ей всё
  # равно не помогает. Зато демон gradle переживает прогон, и TMPDIR, указующий
  # на снесённый каталог, ломал бы уже следующую сборку. Отдаём ему исходный.
  if out=$(cd android && TMPDIR="$TMPDIR_ORIG" ./gradlew test --console=plain -q 2>&1); then
    echo "ok   android/app (JVM-тесты ядра)"
  else
    echo "FAIL android/app"; echo "$out" | tail -15 | sed 's/^/     /'; fail=1
  fi
else
  echo "skip android — нет JDK в этом окружении"
fi

exit $fail
