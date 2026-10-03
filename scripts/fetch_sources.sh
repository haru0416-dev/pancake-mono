#!/usr/bin/env bash
# 合成の元になるフォントを sources/ に取得する。取得済みのものは飛ばす。
set -euo pipefail
cd "$(dirname "$0")/../sources" 2>/dev/null || { mkdir -p "$(dirname "$0")/../sources" && cd "$(dirname "$0")/../sources"; }

MAPLE_VERSION=v7.9
MAPLE_URL=https://github.com/subframe7536/maple-font/releases/download/$MAPLE_VERSION
KIWI_URL=https://raw.githubusercontent.com/google/fonts/main/ofl/kiwimaru

fetch_zip() {  # fetch_zip <zip 名> <展開先>
  if [ -d "$2" ]; then return; fi
  echo "取得: $1"
  curl -fsSL -o "$1" "$MAPLE_URL/$1"
  mkdir -p "$2" && unzip -oq "$1" -d "$2" && rm "$1"
}

# Maple Mono はヒンティング無しの配布物を使う(v7.9 のヒンティング済み TTF と NF は無限矢印リガチャが効かない)
fetch_zip MapleMono-TTF.zip maple
fetch_zip MapleMono-NF-unhinted.zip maple-nf-unhinted

for w in Light Regular Medium; do
  [ -f "KiwiMaru-$w.ttf" ] || { echo "取得: KiwiMaru-$w.ttf"; curl -fsSL -o "KiwiMaru-$w.ttf" "$KIWI_URL/KiwiMaru-$w.ttf"; }
done
[ -f OFL-Kiwi.txt ] || curl -fsSL -o OFL-Kiwi.txt "$KIWI_URL/OFL.txt"
echo "sources/ の準備ができました"
