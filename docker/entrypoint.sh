#!/bin/sh
# Container entrypoint: runs as root only long enough to fix volume ownership, then becomes the unprivileged user.
set -eu
UID_APP=10001

for d in /data /models /models/home /models/hf; do mkdir -p "$d"; done
# Volumes arrive owned by root: fix the top level quickly and only walk the tree when it is not already ours.
for d in /data /models; do
  [ "$(stat -c %u "$d")" = "$UID_APP" ] || chown -R "$UID_APP:$UID_APP" "$d"
done

export HOME=/models/home HF_HOME=/models/hf XDG_CACHE_HOME=/models/home/.cache
# The Chrome HyperFrames pins, baked into the image (no download at run time); Playwright screenshots reuse it.
export HYPERFRAMES_BROWSER_PATH="$(cat /opt/hf-home/browser-path)"
# CUDA 12 libraries from pip wheels (the CUDA variant of the image), found by faster-whisper's ctranslate2
SP="$(python -c 'import site; print(site.getsitepackages()[0])')"
for lib in nvidia/cublas/lib nvidia/cudnn/lib; do
  [ -d "$SP/$lib" ] && export LD_LIBRARY_PATH="$SP/$lib:${LD_LIBRARY_PATH:-}"
done

exec setpriv --reuid="$UID_APP" --regid="$UID_APP" --init-groups "$@"
