#!/usr/bin/env bash
# Run after every resume (a resume starts a NEW container: only /workspace is kept). Safe to run again. It only makes the links and settings
# the worker stack expects outside /workspace; every real file stays inside it.
set -u
W=/workspace
mkdir -p /usr/local/bin /opt/clipcrow $W/cache $W/hf-cache $W/logs
for b in node npm npx; do ln -sf $W/node/bin/$b /usr/local/bin/$b; done
for b in ffmpeg ffprobe; do ln -sf $W/bin/$b /usr/local/bin/$b; done
ln -sf $W/npm/bin/hyperframes /usr/local/bin/hyperframes
for b in clipcrow; do [ -x $W/venv/bin/$b ] && ln -sf $W/venv/bin/$b /usr/local/bin/$b; done
ln -sfn $W/clipcrow-vendor /opt/clipcrow/vendor
ln -sfn $W/fonts /opt/fonts
printf '%s\n' $W/chromelibs/usr/lib/x86_64-linux-gnu $W/chromelibs/lib/x86_64-linux-gnu > /etc/ld.so.conf.d/clipcrow.conf && ldconfig
printf '%s\n' '. /workspace/env.sh' > /etc/profile.d/clipcrow.sh
grep -qs 'workspace/env.sh' /root/.bashrc || echo '. /workspace/env.sh' >> /root/.bashrc
# a Python environment that reuses the template's CUDA PyTorch (kept in /opt/venv inside the template image, so it is there again after a resume)
SPV=$W/venv/lib/python3.12/site-packages
echo /opt/venv/lib/python3.12/site-packages > $SPV/opt_venv.pth
. $W/env.sh
echo "post_resume ok: node $(node --version 2>&1), $(ffmpeg -version 2>&1 | head -1 | cut -c1-30), hyperframes $(hyperframes --version 2>&1 | tail -1)"
