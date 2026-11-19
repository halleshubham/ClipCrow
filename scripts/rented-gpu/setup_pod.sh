#!/usr/bin/env bash
# Installs the ClipCrow worker stack under /workspace (nothing outside it survives a pause).
set -u
W=/workspace; mkdir -p $W/logs $W/bin $W/src
t0=$(date +%s); step() { echo "[$(( $(date +%s)-t0 ))s] $*"; }
cd $W/src && tar xzf ../clipcrow-src.tar.gz && step "source unpacked"
if [ ! -x $W/node/bin/node ]; then
  F=$(curl -s https://nodejs.org/dist/latest-v22.x/SHASUMS256.txt | grep -o 'node-v22[0-9.]*-linux-x64.tar.xz' | head -1)
  curl -sL -o /tmp/node.tar.xz https://nodejs.org/dist/latest-v22.x/$F && mkdir -p $W/node && tar xf /tmp/node.tar.xz -C $W/node --strip-components=1
fi
export PATH=$W/bin:$W/node/bin:$W/npm/bin:$PATH
step "node $(node --version 2>&1)"
if [ ! -x $W/bin/ffprobe ]; then
  curl -sL -o /tmp/ff.tar.xz https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-linux64-gpl.tar.xz
  tar xf /tmp/ff.tar.xz -C /tmp && cp /tmp/ffmpeg-master-latest-linux64-gpl/bin/ffmpeg /tmp/ffmpeg-master-latest-linux64-gpl/bin/ffprobe $W/bin/
fi
step "ffmpeg $(ffmpeg -version 2>&1 | head -1 | cut -c1-40)"
mkdir -p $W/hf-home
npm install -g --prefix $W/npm hyperframes@0.9.29 >$W/logs/npm.log 2>&1; step "hyperframes npm rc=$?"
HOME=$W/hf-home HYPERFRAMES_NO_TELEMETRY=1 hyperframes browser ensure >$W/logs/chrome.log 2>&1; step "chrome ensure rc=$?"
HOME=$W/hf-home hyperframes browser path 2>/dev/null | tail -1 > $W/hf-home/browser-path; step "browser: $(cat $W/hf-home/browser-path)"
B=$(cat $W/hf-home/browser-path); ldd "$B" 2>&1 | grep "not found" | sort -u | tee $W/logs/chrome-missing.txt; step "chrome missing libs: $(wc -l < $W/logs/chrome-missing.txt)"
python3 $W/src/scripts/install_fonts.py $W/fonts >$W/logs/fonts.log 2>&1; step "fonts rc=$? $(tail -1 $W/logs/fonts.log)"
bash $W/src/scripts/fetch_gsap.sh $W/clipcrow-vendor >$W/logs/gsap.log 2>&1; step "gsap rc=$?"
python3 -m venv --system-site-packages $W/venv && step "venv"
$W/venv/bin/pip install -q "$W/src[llm,vision,service,storage,asr,assets]" soundfile sentencepiece "nvidia-cublas-cu12" "nvidia-cudnn-cu12==9.*" >$W/logs/pip.log 2>&1; step "pip rc=$?"
$W/venv/bin/pip install -q "transformers==5.23.0" >>$W/logs/pip.log 2>&1; step "transformers pin rc=$?"      # the newest tokenizers does not match the transformers pip picks
$W/venv/bin/python -c "import clipcrow, torch, transformers; print('python ok', torch.__version__, torch.cuda.is_available(), transformers.__version__)" 2>&1 | tail -2
step "DONE"
