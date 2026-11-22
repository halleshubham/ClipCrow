#!/usr/bin/env bash
# Run (or update) the ClipCrow GPU worker on your own machine, from the published image.
#
#   ./home-gpu-worker.sh                                      # first run asks for the token (hidden) and can save it; later runs just start
#   SERVER=https://clipcrow.shackyapps.in NAME=home-gpu PARALLEL=2 CLIPCROW_WORKER_TOKEN=... ./home-gpu-worker.sh
#   ./home-gpu-worker.sh stop | logs | status
#
# Every setting is an environment variable with a default. The token is passed to docker by NAME (never written on the command line, so it does not show in `ps`).
set -euo pipefail

SERVER="${SERVER:-https://clipcrow.shackyapps.in}"      # your ClipCrow address
NAME="${NAME:-home-gpu}"                                 # the worker's name on the server (extra render slots are shown as NAME.r2, NAME.r3 ...)
IMAGE="${IMAGE:-ghcr.io/halleshubham/clipcrow:latest}"   # the same image the server runs, so the render code matches
MODEL="${MODEL:-large-v3}"                               # faster-whisper model (large-v3 needs about 4-5 GB of GPU memory)
POOL="${POOL:-home}"                                     # home = a machine you own; rented = an on-demand GPU
PARALLEL="${PARALLEL:-1}"                                # clips rendered at once (extra slots only render)
CACHE_GB="${CACHE_GB:-20}"                               # disk used to keep source videos between a job's clips
NO_RENDER="${NO_RENDER:-0}"                              # 1 = speech recognition only
MODELS_VOLUME="${MODELS_VOLUME:-clipcrow-models}"        # docker volume that keeps the downloaded model (about 3 GB)
CONTAINER="${CONTAINER:-clipcrow-worker}"
SHM_SIZE="${SHM_SIZE:-2g}"
PULL="${PULL:-1}"                                        # 0 = do not pull the image first

cmd="${1:-start}"
case "$cmd" in
  stop)   docker rm -f "$CONTAINER" >/dev/null 2>&1 && echo "stopped $CONTAINER" || echo "$CONTAINER was not running"; exit 0 ;;
  logs)   exec docker logs -f --tail 100 "$CONTAINER" ;;
  status) docker ps --filter "name=^${CONTAINER}$" --format 'table {{.Names}}\t{{.Status}}\t{{.Image}}'; exit 0 ;;
  start)  ;;
  *)      echo "usage: $0 [start|stop|logs|status]" >&2; exit 2 ;;
esac

# The token (the same value as CLIPCROW_WORKER_TOKEN in Coolify). Where it comes from, in this order: the environment, the private file below, a hidden prompt (then it offers to
# save it to the file, mode 600, so the next start does not ask). It is never put on a command line, in the script, or in a log.
TOKEN_FILE="${TOKEN_FILE:-$HOME/.config/clipcrow/worker.env}"
if [ -z "${CLIPCROW_WORKER_TOKEN:-}" ] && [ -f "$TOKEN_FILE" ]; then
  if [ "$(stat -c %a "$TOKEN_FILE" 2>/dev/null || stat -f %Lp "$TOKEN_FILE")" != "600" ]; then chmod 600 "$TOKEN_FILE"; fi
  CLIPCROW_WORKER_TOKEN="$(sed -n 's/^CLIPCROW_WORKER_TOKEN=//p' "$TOKEN_FILE" | head -n1)"
fi
if [ -z "${CLIPCROW_WORKER_TOKEN:-}" ]; then
  [ -t 0 ] || { echo "No token. Set CLIPCROW_WORKER_TOKEN, or run this once in a terminal to enter it." >&2; exit 1; }
  read -rsp "Worker token (same as CLIPCROW_WORKER_TOKEN in Coolify; typing is hidden): " CLIPCROW_WORKER_TOKEN; echo
  read -rp "Save it to $TOKEN_FILE (readable only by you) so you are not asked again? [Y/n] " save
  if [ "${save:-Y}" != "n" ] && [ "${save:-Y}" != "N" ]; then
    mkdir -p "$(dirname "$TOKEN_FILE")"; umask 077
    printf 'CLIPCROW_WORKER_TOKEN=%s\n' "$CLIPCROW_WORKER_TOKEN" > "$TOKEN_FILE"; chmod 600 "$TOKEN_FILE"
    echo "saved. To forget it: rm $TOKEN_FILE"
  fi
fi
export CLIPCROW_WORKER_TOKEN
if [ "${#CLIPCROW_WORKER_TOKEN}" -lt 16 ]; then echo "the token must be at least 16 characters" >&2; exit 1; fi
command -v docker >/dev/null || { echo "docker is not installed" >&2; exit 1; }
docker run --rm --gpus all nvidia/cuda:12.4.1-base-ubuntu22.04 nvidia-smi -L >/dev/null 2>&1 \
  || { echo "Docker cannot see the GPU. Install the NVIDIA driver (535+) and nvidia-container-toolkit." >&2; exit 1; }

[ "$PULL" = "1" ] && docker pull "$IMAGE"
docker rm -f "$CONTAINER" >/dev/null 2>&1 || true          # replace an older worker

args=(worker --server "$SERVER" --name "$NAME" --model "$MODEL" --pool "$POOL" --parallel "$PARALLEL" --cache-gb "$CACHE_GB")
[ "$NO_RENDER" = "1" ] && args+=(--no-render)

docker run -d --name "$CONTAINER" --restart unless-stopped --gpus all --shm-size="$SHM_SIZE" --no-healthcheck \
  -e CLIPCROW_WORKER_TOKEN -v "${MODELS_VOLUME}:/models" "$IMAGE" clipcrow "${args[@]}" >/dev/null

echo "started $CONTAINER ($NAME -> $SERVER). Follow it with:  $0 logs"
sleep 5
docker logs --tail 15 "$CONTAINER" 2>&1 | sed 's/^/  /'
docker run --rm "$IMAGE" python -c "from clipcrow.buildid import render_build; print('render build in this image:', render_build())" 2>/dev/null || true
