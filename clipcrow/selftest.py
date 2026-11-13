"""``clipcrow selftest``: can this machine render? Checks each dependency, then renders a 2.5 s Devanagari clip end to end
(compile, HyperFrames check, headless Chrome, FFmpeg) and looks at the result. Used after installing and by the image's
smoke test; exits non-zero with the first thing that is wrong."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Callable

from . import fonts, render
from .edl import EDL


def _run(cmd: list[str], timeout: int = 60) -> str:
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip().splitlines()[-1] if (r.stderr or r.stdout).strip() else f"{cmd[0]} failed")
    return (r.stdout or r.stderr).strip()


def checks() -> list[tuple[str, Callable[[], str]]]:
    def tool(name):
        def f():
            if not shutil.which(name):
                raise RuntimeError(f"{name} is not installed")
            return _run([name, "-version"]).splitlines()[0]
        return f

    def hyperframes():
        return _run([*render._hf_cmd(), "--version"], timeout=120).splitlines()[-1]

    def chrome():
        path = os.environ.get("HYPERFRAMES_BROWSER_PATH") or os.environ.get("CLIPCROW_BROWSER") or \
               _run([*render._hf_cmd(), "browser", "path"], timeout=120).splitlines()[-1]
        if not path or not Path(path).is_file():
            raise RuntimeError("no Chrome found for rendering (run: hyperframes browser ensure)")
        return path

    def font_files():
        got = [fonts.resolve_font(f, w).name for f in ("Noto Serif Devanagari", "Noto Serif") for w in (400, 700)]
        return ", ".join(got)

    def gsap():
        p = Path(os.environ.get("CLIPCROW_GSAP") or "vendor/gsap.min.js")
        if not p.is_file():
            raise RuntimeError(f"{p} is missing (scripts/fetch_gsap.sh)")
        return str(p)
    def speech():
        """Really decode audio the way a transcription does (faster-whisper's decoder, i.e. PyAV): an import check is not enough,
        a PyAV that is too new imports fine and then fails on every file."""
        try:
            from faster_whisper.audio import decode_audio
            import importlib.metadata as md
        except ImportError:
            raise RuntimeError("faster-whisper is not installed (pip install 'clipcrow[asr]')") from None
        with tempfile.TemporaryDirectory() as d:
            wav = Path(d) / "tone.wav"
            subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "sine=f=440:d=2", str(wav)], check=True, timeout=30)
            samples = len(decode_audio(str(wav), sampling_rate=16000))
        if samples < 16000:
            raise RuntimeError(f"decoded only {samples} samples from a 2 s tone")
        return f"faster-whisper {md.version('faster-whisper')}, av {md.version('av')}: decoded {samples / 16000:.0f} s of audio"

    def gpu():
        from .asr import _cuda_devices
        n = _cuda_devices()
        return (f"{n} CUDA device(s) visible to CTranslate2: Whisper will use the GPU" if n
                else "no CUDA device visible to CTranslate2: Whisper will use the CPU (fine for testing, slow for long videos)")
    return [("ffmpeg", tool("ffmpeg")), ("ffprobe", tool("ffprobe")), ("hyperframes", hyperframes), ("chrome", chrome),
            ("fonts", font_files), ("gsap", gsap), ("speech", speech), ("gpu", gpu)]


def render_check() -> str:
    from .pipeline import render_clip
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=1280x720:r=30:d=3",
                        "-f", "lavfi", "-i", "sine=f=220:d=3", "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-g", "30",
                        "-c:a", "aac", str(tmp / "src.mp4")], check=True, timeout=60)
        edl = EDL.model_validate({
            "job_id": "selftest", "source": {"uri": "src.mp4", "duration": 3, "width": 1280, "height": 720, "fps": 30},
            "language": {"primary": "mr", "script": "Deva"},
            "transcript": [{"w": "नमस्कार", "s": 0.2, "e": 0.8}, {"w": "ClipCrow", "s": 0.8, "e": 1.4}, {"w": "तयार.", "s": 1.4, "e": 2.2}],
            "clips": [{"id": "c1", "source_in": 0, "source_out": 2.5, "overlays": [
                {"kind": "hook", "id": "hook", "t_in": 0.2, "duration": 2.0, "text": "चाचणी: सर्व काही चालते"}]}]})
        out, _ = render_clip(edl, "c1", tmp / "out.mp4", workdir=tmp / "w", media_root=tmp, gsap_js=os.environ.get("CLIPCROW_GSAP") or "vendor/gsap.min.js")
        info = json.loads(_run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,width,height,duration", "-of", "json", str(out)]))
        streams = info["streams"]
        v = next((s for s in streams if s["codec_type"] == "video"), None)
        if not v or (v["width"], v["height"]) != (1080, 1920):
            raise RuntimeError(f"the test clip came out the wrong size: {v}")
        if not any(s["codec_type"] == "audio" for s in streams):
            raise RuntimeError("the test clip has no audio")
        if abs(float(v["duration"]) - 2.5) > 0.3:
            raise RuntimeError(f"the test clip is {float(v['duration']):.1f} s long, not 2.5")
        return f"{v['width']}x{v['height']}, {float(v['duration']):.1f} s, {out.stat().st_size // 1024} KiB"


def run(say: Callable[[str], None] = print, *, skip_render: bool = False) -> bool:
    """Prints one line per check. Returns True when everything works."""
    ok = True
    for name, fn in checks() + ([] if skip_render else [("render", render_check)]):
        try:
            say(f"ok    {name}: {fn()}")
        except Exception as e:                                # report, then carry on so one run shows everything wrong
            ok = False
            say(f"FAIL  {name}: {str(e)[:300]}")
    return ok
