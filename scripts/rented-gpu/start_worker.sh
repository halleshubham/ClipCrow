#!/usr/bin/env bash
# Starts the ClipCrow worker for https://clipcrow.shackyapps.in (and restarts it if it ever exits). Run after post_resume.sh.
. /workspace/env.sh
set -a; . /workspace/worker.env; set +a                      # CLIPCROW_WORKER_TOKEN (a secret: this file is readable by root only)
export CLIPCROW_ASR=sravaani CLIPCROW_SRAVAANI_DIR=/workspace/models/sravaani CLIPCROW_WHISPER_MODEL=large-v3
export CLIPCROW_WORKER_CACHE=/workspace/worker-cache HOME=/workspace/home
mkdir -p $HOME $CLIPCROW_WORKER_CACHE /workspace/logs
bash /workspace/stop_worker.sh >/dev/null 2>&1
setsid nohup bash -c 'while true; do clipcrow worker --server https://clipcrow.shackyapps.in --name race-3090 --model large-v3 --cache-gb 15 --parallel 2 --pool rented; echo "[worker exited with $?; restarting in 10 s]"; sleep 10; done' >> /workspace/logs/worker.log 2>&1 &
echo $! > /workspace/worker.pid
echo "worker started (pid $(cat /workspace/worker.pid)); log: /workspace/logs/worker.log"
