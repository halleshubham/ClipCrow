#!/usr/bin/env bash
# Smoke-test a built ClipCrow image: IMAGE [gpu|cpu]. Fails on the first thing that would break on a real server.
set -euo pipefail
IMG="${1:?usage: smoke_image.sh <image> [gpu|cpu]}"; VARIANT="${2:-cpu}"
say() { printf '\n== %s\n' "$*"; }

say "runs as an unprivileged user, not root"
[ "$(docker run --rm "$IMG" id -u)" = "10001" ]

say "the tools, and a real render of a Devanagari clip inside the image (Chrome, FFmpeg, fonts, GSAP, HyperFrames)"
docker run --rm --shm-size=1g "$IMG" clipcrow selftest

say "the same render with ROOT-OWNED volumes mounted (what a fresh Coolify volume or an old root-owned one looks like)"
vol="$(mktemp -d)"; mkdir -p "$vol/data" "$vol/models"; chmod 755 "$vol" "$vol/data" "$vol/models"   # owned by the runner's user, not uid 10001
docker run --rm --shm-size=1g -v "$vol/data:/data" -v "$vol/models:/models" "$IMG" clipcrow selftest
[ "$(stat -c %u "$vol/models/home")" = "10001" ]          # the entrypoint handed the cache directories to the app

say "libraries the server needs import"
docker run --rm "$IMG" python -c "import faster_whisper, cv2, boto3, fastapi, anthropic, playwright; print('imports ok')"
if [ "$VARIANT" = "gpu" ]; then
  docker run --rm "$IMG" python -c "import nvidia.cudnn, nvidia.cublas; print('CUDA libraries present')"
fi

say "refuses to listen publicly without any login"
if out="$(docker run --rm "$IMG" 2>&1)"; then echo "it started without a login"; exit 1; fi
grep -q "CLIPCROW_TOKEN" <<<"$out"          # (no pipes into grep -q: with pipefail its early exit breaks the writer)

say "starts, answers its health check, serves the UI, and stops cleanly on SIGTERM (shutdown handlers run)"
cid="$(docker run -d --shm-size=1g -e CLIPCROW_TOKEN=smoke-token -e ANTHROPIC_API_KEY=smoke -p 18000:8000 "$IMG")"
trap 'docker rm -f "$cid" >/dev/null 2>&1 || true' EXIT
for i in $(seq 1 90); do curl -fsS http://127.0.0.1:18000/api/config >/tmp/config.json 2>/dev/null && break || sleep 1; done
cat /tmp/config.json; echo
python3 - <<'PY'
import json
c = json.load(open("/tmp/config.json"))
assert c["needs_login"] is True and c["signed_in"] is False, c
assert c["ready"] is None, f"the server says it cannot run jobs: {c['ready']}"
PY
fail() { echo "SMOKE FAIL: $*"; docker logs "$cid" 2>&1 | tail -20; exit 1; }
page="$(curl -sS -w '\n%{http_code}' http://127.0.0.1:18000/ || true)"
echo "GET / -> HTTP $(tail -n1 <<<"$page"), $(wc -c <<<"$page") bytes"
grep -q "ClipCrow" <<<"$page" || fail "the UI page does not mention ClipCrow: $(head -c 300 <<<"$page")"
code="$(curl -sS -o /dev/null -w '%{http_code}' http://127.0.0.1:18000/api/jobs || true)"
echo "GET /api/jobs without a login -> HTTP $code"
[ "$code" = "401" ] || fail "the API answered $code without a login (wanted 401)"
docker exec "$cid" curl -fsS http://127.0.0.1:8000/api/config >/dev/null || fail "the container's own health check command failed"
docker stop -t 60 "$cid" >/dev/null
exit_code="$(docker inspect -f '{{.State.ExitCode}}' "$cid")"
echo "exit code after SIGTERM: $exit_code"
# uvicorn re-raises SIGTERM once it has shut down, so a normal "docker stop" ends in 143 (128 + 15); 0 is fine too.
# What matters is that the shutdown handlers ran (the app's drain is one of them) and that it exited on its own, not by SIGKILL (137).
case "$exit_code" in 0|143) ;; *) fail "it did not stop cleanly on SIGTERM (exit code $exit_code)";; esac
logs="$(docker logs "$cid" 2>&1)"
grep -q "Application shutdown complete" <<<"$logs" || fail "the shutdown handlers did not run (no 'Application shutdown complete')"
docker logs "$cid" 2>&1 | tail -5
echo; echo "image smoke test passed"
