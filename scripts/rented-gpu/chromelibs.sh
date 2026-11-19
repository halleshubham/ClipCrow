#!/usr/bin/env bash
set -u
W=/workspace; t0=$(date +%s); step() { echo "[$(( $(date +%s)-t0 ))s] $*"; }
mkdir -p $W/apt/lists/partial $W/apt/cache/archives/partial $W/chromelibs
cat > $W/apt/sources.list <<'S'
deb https://ftp.iitm.ac.in/ubuntu noble main universe
deb https://ftp.iitm.ac.in/ubuntu noble-updates main universe
deb https://ftp.iitm.ac.in/ubuntu noble-security main universe
S
OPT="-o Dir::Etc::sourcelist=$W/apt/sources.list -o Dir::Etc::sourceparts=/dev/null -o Dir::State::lists=$W/apt/lists -o Dir::Cache=$W/apt/cache -o APT::Get::List-Cleanup=0 -o Acquire::Retries=3 -o Acquire::http::Timeout=30 -o Acquire::https::Timeout=30"
export DEBIAN_FRONTEND=noninteractive
apt-get $OPT update >$W/logs/apt-update.log 2>&1; step "apt update rc=$? ($(tail -1 $W/logs/apt-update.log | cut -c1-80))"
apt-get $OPT install -y --download-only --no-install-recommends libnss3 libnspr4 libatk1.0-0t64 libatk-bridge2.0-0t64 libatspi2.0-0t64 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libgbm1 libasound2t64 libxkbcommon0 libdbus-1-3 >$W/logs/apt-dl.log 2>&1; step "apt download rc=$? ($(ls $W/apt/cache/archives/*.deb 2>/dev/null | wc -l) debs)"
for d in $W/apt/cache/archives/*.deb; do dpkg -x "$d" $W/chromelibs; done; step "unpacked"
cat > $W/env.sh <<'S'
export W=/workspace
export PATH=$W/bin:$W/node/bin:$W/npm/bin:$W/venv/bin:$PATH
export LD_LIBRARY_PATH=$W/chromelibs/usr/lib/x86_64-linux-gnu:$W/chromelibs/lib/x86_64-linux-gnu:${LD_LIBRARY_PATH:-}
export HOME_HF=$W/hf-home HYPERFRAMES_NO_TELEMETRY=1 HYPERFRAMES_NO_UPDATE_CHECK=1
export CLIPCROW_HF_BIN=$W/npm/bin/hyperframes CLIPCROW_FONT_DIR=$W/fonts CLIPCROW_GSAP=$W/clipcrow-vendor/gsap.min.js
export HF_HOME=$W/hf-cache
S
. $W/env.sh
B=$(cat $W/hf-home/browser-path); ldd "$B" 2>&1 | grep "not found" | sort -u | tee $W/logs/chrome-missing2.txt; step "chrome still missing: $(wc -l < $W/logs/chrome-missing2.txt)"
step DONE_LIBS
