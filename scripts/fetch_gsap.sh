#!/usr/bin/env bash
# Vendor GSAP locally: the render browser must not depend on a CDN.
# GSAP is NOT committed: it is under GreenSock's own licence (free for commercial use,
# but it forbids use in no-code visual animation tools that compete with Webflow),
# so it is fetched from npm at build time, never redistributed from this repo.
set -euo pipefail
VERSION="${GSAP_VERSION:-3.15.0}"
DEST="${1:-vendor}"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
mkdir -p "$DEST"
( cd "$TMP" && npm pack "gsap@${VERSION}" --silent && tar xzf "gsap-${VERSION}.tgz" )
cp "$TMP/package/dist/gsap.min.js" "$DEST/gsap.min.js"
echo "wrote $DEST/gsap.min.js (gsap@${VERSION})"
