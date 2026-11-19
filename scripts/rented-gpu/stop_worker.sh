#!/usr/bin/env bash
[ -f /workspace/worker.pid ] && kill -- -"$(cat /workspace/worker.pid)" 2>/dev/null
pkill -f "clipcrow worker" 2>/dev/null; pkill -f "while true; do clipcrow worker" 2>/dev/null
rm -f /workspace/worker.pid; echo "worker stopped"
