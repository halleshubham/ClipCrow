#!/usr/bin/env bash
# CI only: ffmpeg, ffprobe and the Noto fonts WITHOUT apt. The runners' Ubuntu archive mirrors are not always
# reachable (apt stalls for minutes), while GitHub and npm are. Fonts go to $CLIPCROW_FONT_DIR under the file
# names clipcrow.fonts.KNOWN expects; ffmpeg/ffprobe go to ~/.local/bin.
set -euo pipefail
BIN="$HOME/.local/bin"; FONTS="$HOME/clipcrow-fonts"; TMP="$(mktemp -d)"
mkdir -p "$BIN" "$FONTS"

echo "== ffmpeg"
if command -v ffmpeg >/dev/null && command -v ffprobe >/dev/null; then
  echo "already installed: $(ffmpeg -version | head -1)"
else
  REL="https://github.com/BtbN/FFmpeg-Builds/releases/download/latest"
  # Find the build's current file name (it has changed before), then try the long-standing name, then give up.
  NAME="$(curl -fsSL --retry 2 --connect-timeout 20 https://api.github.com/repos/BtbN/FFmpeg-Builds/releases/tags/latest 2>/dev/null \
          | grep -oE 'ffmpeg-master-latest-linux64-gpl\.tar\.xz' | head -1 || true)"
  ok=0
  for n in "$NAME" "ffmpeg-master-latest-linux64-gpl.tar.xz"; do
    [ -n "$n" ] || continue
    if curl -fsSL --retry 3 --connect-timeout 20 --max-time 400 "$REL/$n" -o "$TMP/ffmpeg.tar.xz"; then ok=1; break; fi
  done
  if [ "$ok" = 1 ]; then
    tar -xJf "$TMP/ffmpeg.tar.xz" -C "$TMP"
    cp "$TMP"/ffmpeg-*/bin/ffmpeg "$TMP"/ffmpeg-*/bin/ffprobe "$BIN/"
  else
    echo "::warning::could not download a current ffmpeg build; using the npm static binaries (a 2018 ffmpeg): renders may differ slightly"
    (cd "$TMP" && npm init -y >/dev/null && npm i --silent --no-audit --no-fund \
       @ffmpeg-installer/linux-x64 @ffprobe-installer/linux-x64)
    cp "$TMP/node_modules/@ffmpeg-installer/linux-x64/ffmpeg" "$TMP/node_modules/@ffprobe-installer/linux-x64/ffprobe" "$BIN/"
  fi
fi
export PATH="$BIN:$PATH"
ffmpeg -version | head -1; ffprobe -version | head -1

echo "== fonts"
(cd "$TMP" && npm init -y >/dev/null && npm i --silent --no-audit --no-fund \
   @expo-google-fonts/noto-serif-devanagari @expo-google-fonts/noto-serif \
   @expo-google-fonts/noto-sans-devanagari @expo-google-fonts/noto-sans)
for pair in noto-serif-devanagari:NotoSerifDevanagari noto-serif:NotoSerif noto-sans-devanagari:NotoSansDevanagari noto-sans:NotoSans; do
  pkg="${pair%%:*}"; stem="${pair##*:}"
  cp "$TMP/node_modules/@expo-google-fonts/$pkg/400Regular/${stem}_400Regular.ttf" "$FONTS/${stem}-Regular.ttf"
  cp "$TMP/node_modules/@expo-google-fonts/$pkg/700Bold/${stem}_700Bold.ttf" "$FONTS/${stem}-Bold.ttf"
done
ls -1 "$FONTS"

if [ -n "${GITHUB_ENV:-}" ]; then
  echo "CLIPCROW_FONT_DIR=$FONTS" >> "$GITHUB_ENV"
  echo "$BIN" >> "$GITHUB_PATH"
fi
rm -rf "$TMP"
