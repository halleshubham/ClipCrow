"""End to end through the real HyperFrames CLI. Needs Node 22, FFmpeg and Chrome:
    scripts/fetch_gsap.sh && python examples/gen_assets.py && REELCORE_RENDER_TESTS=1 pytest -m render
"""
import json
import os
import subprocess
from pathlib import Path

import pytest

from reelcore import render
from reelcore.edl import load_edl
from reelcore.pipeline import render_clip

ROOT = Path(__file__).resolve().parent.parent
pytestmark = [pytest.mark.render,
              pytest.mark.skipif(not os.environ.get("REELCORE_RENDER_TESTS"), reason="set REELCORE_RENDER_TESTS=1")]


def probe(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,width,height,duration",
                          "-of", "json", str(path)], capture_output=True, text=True, check=True).stdout
    return json.loads(out)["streams"]


def test_passthrough_clip_renders_with_the_expected_shape(tmp_path):
    edl = load_edl(ROOT / "examples" / "demo.edl.json")
    out, compiled = render_clip(edl, "c2", tmp_path / "c2.mp4", workdir=tmp_path / "w",
                                media_root=ROOT / "examples" / "assets", gsap_js=ROOT / "vendor" / "gsap.min.js", workers=2)
    streams = probe(out)
    video = next(s for s in streams if s["codec_type"] == "video")
    assert (video["width"], video["height"]) == (1080, 1920)
    assert abs(float(video["duration"]) - 6.8) < 0.1
    assert any(s["codec_type"] == "audio" for s in streams)
    assert render.check_problems(render.check(compiled.project_dir)) == []
