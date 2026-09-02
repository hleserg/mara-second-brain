#!/usr/bin/env bash
# Тридцать секунд тона вместо звонка: настоящего голоса в репозитории не
# держим, а нарезку, склейку и путь файла до карточки это проверяет целиком.
set -eu
out="${1:-/tmp/sample-call.m4a}"
ffmpeg -v error -y -f lavfi -i "sine=frequency=440:duration=30" -ac 1 -ar 16000 "$out"
echo "$out"
