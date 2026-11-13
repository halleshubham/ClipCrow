#!/usr/bin/env bash
# Install ffmpeg and ffprobe (a current static GPL build from GitHub: libx264, libvpx with alpha) into DEST (default
# /usr/local/bin). Used by the Dockerfile and by CI, so the image runs the same ffmpeg the tests do. Falls back to the npm
# static binaries (a 2018 ffmpeg!) with a loud warning, because a render that works is better than none.
set -euo pipefail
DEST="${1:-/usr/local/bin}"; TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
mkdir -p "$DEST"
REL="https://github.com/BtbN/FFmpeg-Builds/releases/download/latest"
NAME="$(curl -fsSL --retry 2 --connect-timeout 20 https://api.github.com/repos/BtbN/FFmpeg-Builds/releases/tags/latest 2>/dev/null \
        | grep -oE 'ffmpeg-master-latest-linux64-gpl\.tar\.xz' | head -1 || true)"
ok=0
for n in "$NAME" "ffmpeg-master-latest-linux64-gpl.tar.xz"; do
  [ -n "$n" ] || continue
  if curl -fsSL --retry 3 --connect-timeout 20 --max-time 400 "$REL/$n" -o "$TMP/ffmpeg.tar.xz"; then ok=1; break; fi
done
if [ "$ok" = 1 ]; then
  tar -xJf "$TMP/ffmpeg.tar.xz" -C "$TMP"
  cp "$TMP"/ffmpeg-*/bin/ffmpeg "$TMP"/ffmpeg-*/bin/ffprobe "$DEST/"
else
  echo "::warning::could not download a current ffmpeg build; using the npm static binaries (a 2018 ffmpeg): renders may differ slightly"
  (cd "$TMP" && npm init -y >/dev/null && npm i --silent --no-audit --no-fund @ffmpeg-installer/linux-x64 @ffprobe-installer/linux-x64)
  cp "$TMP/node_modules/@ffmpeg-installer/linux-x64/ffmpeg" "$TMP/node_modules/@ffprobe-installer/linux-x64/ffprobe" "$DEST/"
fi
chmod +x "$DEST/ffmpeg" "$DEST/ffprobe"
"$DEST/ffmpeg" -version | head -1; "$DEST/ffprobe" -version | head -1
