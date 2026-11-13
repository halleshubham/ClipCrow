#!/usr/bin/env bash
# Install the Noto faces ClipCrow ships (Serif/Sans, Latin and Devanagari) into DEST under the file names
# clipcrow.fonts.KNOWN expects, from npm (@expo-google-fonts: SIL OFL). Used by the Dockerfile and by CI.
set -euo pipefail
DEST="${1:?usage: install_fonts.sh <dest dir>}"; TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
mkdir -p "$DEST"
(cd "$TMP" && npm init -y >/dev/null && npm i --silent --no-audit --no-fund \
   @expo-google-fonts/noto-serif-devanagari @expo-google-fonts/noto-serif \
   @expo-google-fonts/noto-sans-devanagari @expo-google-fonts/noto-sans)
for pair in noto-serif-devanagari:NotoSerifDevanagari noto-serif:NotoSerif noto-sans-devanagari:NotoSansDevanagari noto-sans:NotoSans; do
  pkg="${pair%%:*}"; stem="${pair##*:}"
  cp "$TMP/node_modules/@expo-google-fonts/$pkg/400Regular/${stem}_400Regular.ttf" "$DEST/${stem}-Regular.ttf"
  cp "$TMP/node_modules/@expo-google-fonts/$pkg/700Bold/${stem}_700Bold.ttf" "$DEST/${stem}-Bold.ttf"
done
ls -1 "$DEST"
