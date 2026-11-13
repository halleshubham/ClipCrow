#!/usr/bin/env bash
# CI only: ffmpeg, ffprobe and the Noto fonts WITHOUT apt (the runners' Ubuntu archive mirrors are not always reachable
# and apt stalls for minutes, while GitHub and npm are). Same installers as the Dockerfile uses.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
BIN="$HOME/.local/bin"; FONTS="$HOME/clipcrow-fonts"
mkdir -p "$BIN"
echo "== ffmpeg"
if command -v ffmpeg >/dev/null && command -v ffprobe >/dev/null; then
  echo "already installed: $(ffmpeg -version | head -1)"
else
  "$HERE/install_ffmpeg.sh" "$BIN"
fi
echo "== fonts"
"$HERE/install_fonts.sh" "$FONTS"
if [ -n "${GITHUB_ENV:-}" ]; then
  echo "CLIPCROW_FONT_DIR=$FONTS" >> "$GITHUB_ENV"
  echo "$BIN" >> "$GITHUB_PATH"
fi
