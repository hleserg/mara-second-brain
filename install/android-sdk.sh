#!/usr/bin/env bash
# Android SDK на doctor: сборка APK `Mara Capture` (ТЗ §5).
#
# BetaPi — aarch64, а aapt2/d8 Google собирает только под x86_64, поэтому
# собираем на doctor. Скрипт идемпотентный: повторный запуск ничего не ломает.
#
#   ssh doctor 'bash ~/mara-second-brain/install/android-sdk.sh'
set -euo pipefail

SDK="${ANDROID_SDK_ROOT:-$HOME/Android/Sdk}"
CLT=13114758                       # commandlinetools-linux, sdkmanager сам обновится
PLATFORM="platforms;android-34"
BUILDTOOLS="build-tools;34.0.0"

if ! command -v java >/dev/null; then
  echo "== ставлю JDK 17 (AGP 8.x требует именно 17) =="
  sudo apt-get update -qq
  sudo apt-get install -y -qq openjdk-17-jdk-headless unzip
fi
java -version 2>&1 | head -1

if [ ! -x "$SDK/cmdline-tools/latest/bin/sdkmanager" ]; then
  echo "== ставлю cmdline-tools =="
  mkdir -p "$SDK/cmdline-tools"
  tmp=$(mktemp -d)
  curl -fsSL -o "$tmp/clt.zip" \
    "https://dl.google.com/android/repository/commandlinetools-linux-${CLT}_latest.zip"
  unzip -q "$tmp/clt.zip" -d "$tmp"
  rm -rf "$SDK/cmdline-tools/latest"
  mv "$tmp/cmdline-tools" "$SDK/cmdline-tools/latest"
  rm -rf "$tmp"
fi

export ANDROID_SDK_ROOT="$SDK" ANDROID_HOME="$SDK"
BIN="$SDK/cmdline-tools/latest/bin"
yes 2>/dev/null | "$BIN/sdkmanager" --licenses >/dev/null || true
"$BIN/sdkmanager" --install "platform-tools" "$PLATFORM" "$BUILDTOOLS" >/dev/null

# local.properties гитом не отслеживается: путь к SDK у каждой машины свой
repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [ -d "$repo/android" ]; then
  echo "sdk.dir=$SDK" > "$repo/android/local.properties"
fi

echo "== готово =="
echo "SDK:      $SDK"
echo "platform: $(ls "$SDK/platforms" 2>/dev/null | tr '\n' ' ')"
echo "build:    $(ls "$SDK/build-tools" 2>/dev/null | tr '\n' ' ')"
