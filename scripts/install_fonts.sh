#!/usr/bin/env bash
# Install the font families ClipCrow can draw (every Devanagari family on Google Fonts and the best-known Latin ones; SIL Open Font License)
# into DEST under the file names clipcrow.fonts.KNOWN expects. The list is clipcrow/fonts_catalog.json. Used by the Dockerfile and by CI.
set -euo pipefail
DEST="${1:?usage: install_fonts.sh <dest dir>}"
exec python3 "$(dirname "$0")/install_fonts.py" "$DEST"
