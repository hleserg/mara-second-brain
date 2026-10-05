#!/bin/bash
# SessionStart-хук облачной сессии Claude Code. Ставить нечего: код на stdlib
# Python, фреймворков нет принципиально (scripts/run-tests.sh). Хук делает три
# вещи: даёт гейту те же переменные, что и CI (.github/workflows/tests.yml),
# заводит каталог под песочницу тестов (Г1 плана: mktemp не создаёт
# отсутствующий TMPDIR) и честно говорит, что в этом контейнере не работает.
set -euo pipefail

# Только облако: на doctor и BetaPi переменные берутся из живого окружения.
if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

tmp="${HOME}/.mara-tmp"
mkdir -p "$tmp/mara-blobs" "$tmp/vault"

if [ -n "${CLAUDE_ENV_FILE:-}" ]; then
  {
    # Гейт не должен читать ~/.config/mara/env и /etc/mara/contextd.env:
    # run-tests.sh уводит их в несуществующие файлы, здесь — то же самое,
    # чтобы прямой `python3 -m unittest tests.test_X` вёл себя как гейт.
    echo 'export MARA_ENV_FILE=/nonexistent/mara-env'
    echo 'export MARA_CONTEXTD_ENV=/nonexistent/mara-contextd-env'
    # Дефолты смотрят в /srv, которого тут нет (см. tests.yml).
    echo "export MARA_BLOBS=$tmp/mara-blobs"
    echo "export VAULT=$tmp/vault"
    echo "export MARA_VAULT=$tmp/vault"
    # Kotlin-работа гейта тут не собирается (ниже), пропускаем явно.
    echo 'export SKIP_ANDROID=1'
  } >> "$CLAUDE_ENV_FILE"
fi

echo "python: $(python3 --version 2>&1)"
if command -v java >/dev/null 2>&1; then
  echo "java: есть ($(java -version 2>&1 | grep -m1 version))"
else
  echo "java: нет — работа kotlin только в CI"
fi
# Android Gradle Plugin живёт на dl.google.com; сетевая политика окружения
# по умолчанию его не пускает, и `./gradlew test` падает на разрешении
# плагина. Проверяем за секунду, чтобы не узнавать об этом через минуту
# ожидания gradle.
if curl -sS -m 5 -o /dev/null https://dl.google.com/dl/android/maven2/ 2>/dev/null; then
  echo "dl.google.com: доступен — gradle-тесты android/ можно гонять тут"
else
  echo "dl.google.com: закрыт политикой сети — гейт Г2 (Kotlin) только через job kotlin в CI"
fi
echo "гейт: SKIP_ANDROID=1 bash scripts/run-tests.sh (≈1,5 мин); один модуль: python3 -m unittest tests.test_X -v"
