#!/usr/bin/env bash
# Run (or update) the ClipCrow GPU worker on your own machine, from the published image.
#
#   CLIPCROW_WORKER_TOKEN=... ./home-gpu-worker.sh            # token is the only thing you must give
#   SERVER=https://clipcrow.shackyapps.in NAME=home-gpu PARALLEL=2 CLIPCROW_WORKER_TOKEN=... ./home-gpu-worker.sh
#   ./home-gpu-worker.sh stop | logs | status
#
# Every setting is an environment variable with a default. The token is read from the environment and passed on by NAME (never written on the
# command line, so it does not show in `ps` or shell history when you export it first).
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

if [ -z "${CLIPCROW_WORKER_TOKEN:-}" ]; then
  echo "Set CLIPCROW_WORKER_TOKEN (the same value as on the server). Do not put it on the command line:" >&2
  echo "  read -rs CLIPCROW_WORKER_TOKEN && export CLIPCROW_WORKER_TOKEN" >&2
  exit 1
fi
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
