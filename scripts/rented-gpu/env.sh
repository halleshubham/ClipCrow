# ClipCrow worker environment. Everything real lives under /workspace (the only disk that survives a pause).
export W=/workspace
SP=$W/venv/lib/python3.12/site-packages
export PATH=$W/bin:$W/node/bin:$W/npm/bin:$W/venv/bin:$PATH
export LD_LIBRARY_PATH=$W/chromelibs/usr/lib/x86_64-linux-gnu:$W/chromelibs/lib/x86_64-linux-gnu:$SP/nvidia/cublas/lib:$SP/nvidia/cudnn/lib:${LD_LIBRARY_PATH:-}
export HF_HOME=$W/hf-cache XDG_CACHE_HOME=$W/cache PIP_CACHE_DIR=$W/cache/pip NPM_CONFIG_CACHE=$W/cache/npm NPM_CONFIG_PREFIX=$W/npm
export HYPERFRAMES_BROWSER_PATH="$(cat $W/hf-home/browser-path 2>/dev/null)" HYPERFRAMES_NO_TELEMETRY=1 HYPERFRAMES_NO_UPDATE_CHECK=1 PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1
export CLIPCROW_HF_BIN=hyperframes CLIPCROW_FONT_DIR=$W/fonts CLIPCROW_GSAP=$W/clipcrow-vendor/gsap.min.js
