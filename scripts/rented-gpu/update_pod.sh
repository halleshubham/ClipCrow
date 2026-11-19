#!/usr/bin/env bash
# Brings the worker code on the pod up to date. A worker compiles and renders clips itself, so a pod running older code than the server gets no
# renders (the server compares a hash of the render code, see clipcrow/buildid.py). Upload a fresh source tarball to /workspace/clipcrow-src.tar.gz first
# (e.g. `git archive HEAD | gzip`), then run this. It touches nothing but /workspace/src and the venv.
set -eu
W=/workspace
cd $W/src && rm -rf clipcrow tests && tar xzf $W/clipcrow-src.tar.gz
$W/venv/bin/pip install -q --no-deps --force-reinstall $W/src
$W/venv/bin/python -c 'from clipcrow.buildid import render_build; print("render build", render_build())'
bash $W/start_worker.sh
