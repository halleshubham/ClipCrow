#!/usr/bin/env bash
# Checks the worker stack from a clean shell: prints OK/MISSING per part.
. /workspace/env.sh
ok() { echo "OK       $*"; }; no() { echo "MISSING  $*"; }
command -v node >/dev/null && ok "node $(node --version)" || no node
command -v ffmpeg >/dev/null && ok "ffmpeg $(ffmpeg -hide_banner -encoders 2>/dev/null | grep -c nvenc) nvenc encoders" || no ffmpeg
command -v ffprobe >/dev/null && ok ffprobe || no ffprobe
command -v hyperframes >/dev/null && ok "hyperframes $(hyperframes --version 2>&1 | tail -1)" || no hyperframes
B="$HYPERFRAMES_BROWSER_PATH"; [ -x "$B" ] && ok "chrome $($B --version 2>&1 | head -1)" || no "chrome ($B)"
m=$(ldd "$B" 2>&1 | grep -c "not found"); [ "$m" = 0 ] && ok "chrome libraries (none missing)" || no "chrome libraries ($m missing)"
[ -f $CLIPCROW_GSAP ] && ok "gsap" || no gsap
[ "$(ls $CLIPCROW_FONT_DIR/*.ttf 2>/dev/null | wc -l)" -ge 200 ] && ok "fonts ($(ls $CLIPCROW_FONT_DIR/*.ttf | wc -l) files)" || no fonts
python - <<'PY' 2>&1 | tail -4
import torch, transformers, clipcrow
print("OK       python: torch", torch.__version__, "cuda", torch.cuda.is_available(), "| transformers", transformers.__version__, "| clipcrow", clipcrow.__file__.split("/workspace")[-1])
PY
python -c "import faster_whisper, ctranslate2; print('OK       faster-whisper, ctranslate2', ctranslate2.__version__, 'cuda devices', ctranslate2.get_cuda_device_count())" 2>&1 | tail -1
