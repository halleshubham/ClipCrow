"""End to end through the real HyperFrames CLI. Needs Node 22, FFmpeg and Chrome:
    scripts/fetch_gsap.sh && python examples/gen_assets.py && CLIPCROW_RENDER_TESTS=1 pytest -m render
"""
import json
import os
import subprocess
from pathlib import Path

import pytest

from clipcrow import render
from clipcrow.edl import load_edl
from clipcrow.pipeline import render_clip

ROOT = Path(__file__).resolve().parent.parent
pytestmark = [pytest.mark.render,
              pytest.mark.skipif(not os.environ.get("CLIPCROW_RENDER_TESTS"), reason="set CLIPCROW_RENDER_TESTS=1")]


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


# --- face-to-camera: text behind the person, and push-ins (a synthetic matte, real pixels) ---------------------

def _synthetic_scene(tmp_path):
    """A dark vertical source and a person matte that is an opaque red block (alpha) on a transparent frame."""
    src = tmp_path / "src.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=0x101830:s=1080x1920:r=30:d=4",
                    "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo", "-t", "4", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-g", "30", "-c:a", "aac", "-shortest", str(src)], check=True)
    matte = tmp_path / "person.webm"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=black@0.0:s=1080x1920:r=30:d=4,format=yuva420p",
                    "-f", "lavfi", "-i", "color=c=red:s=400x800:r=30:d=4,format=yuva420p",
                    "-filter_complex", "[0][1]overlay=340:700:format=auto,format=yuva420p", "-c:v", "libvpx-vp9",
                    "-pix_fmt", "yuva420p", "-auto-alt-ref", "0", str(matte)], check=True)
    return src, matte


def _edl(punch=None):
    from clipcrow.edl import EDL
    return EDL.model_validate({
        "job_id": "t", "source": {"uri": "src.mp4", "duration": 4, "width": 1080, "height": 1920, "fps": 30},
        "language": {"primary": "mr", "script": "Deva"},
        "transcript": [{"w": "ज्ञान", "s": 0.5, "e": 1.0}, {"w": "शक्ती", "s": 1.0, "e": 1.6}],
        "clips": [{"id": "c1", "source_in": 0, "source_out": 3.0,
                   "captions": {"enabled": False},
                   "layout": {"mode": "cutout_behind", "cutout_uri": "person.webm", "punch_ins": punch or []},
                   "overlays": [{"kind": "text_behind", "id": "b", "t_in": 0.2, "duration": 2.6, "text": "ज्ञान",
                                 "color": "#FFFF00", "top_px": 700, "font_size": 420}]}]})


def _frame(video, t):
    import cv2
    cap = cv2.VideoCapture(str(video))
    cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
    ok, f = cap.read()
    cap.release()
    assert ok
    return f


def _masks(frame):
    import cv2
    import numpy as np
    b, g, r = (frame[..., i].astype(int) for i in range(3))
    red = (r > 180) & (g < 80) & (b < 80)
    yellow = (r > 180) & (g > 180) & (b < 90)
    return red, yellow, np


def test_text_sits_behind_the_person_and_a_push_in_scales_both_together(tmp_path):
    src, _ = _synthetic_scene(tmp_path)
    outs = {}
    for name, punch in (("plain", None), ("push", [{"t": 0.2, "duration": 2.4, "scale": 1.3, "anchor_y": 0.5}])):
        edl = _edl(punch)
        out, _ = render_clip(edl, "c1", tmp_path / f"{name}.mp4", workdir=tmp_path / f"w-{name}", media_root=tmp_path,
                             gsap_js=ROOT / "vendor" / "gsap.min.js", workers=2)
        outs[name] = out

    red, yellow, np = _masks(_frame(outs["plain"], 1.4))
    ys, xs = np.nonzero(red)
    assert ys.size > 200_000, "the person is not there"
    x0, x1, y0, y1 = xs.min(), xs.max(), ys.min(), ys.max()
    assert 380 <= x1 - x0 + 1 <= 420 and 780 <= y1 - y0 + 1 <= 820                 # the block is where the matte put it
    assert yellow.sum() > 5_000, "the text behind the person is not visible"
    inside = yellow[y0:y1 + 1, x0:x1 + 1].sum()
    assert inside == 0, f"text is painted over the person ({inside} yellow pixels inside the block)"
    band = yellow[700:1130]
    assert band[:, :x0].sum() > 1_000 or band[:, x1 + 1:].sum() > 1_000           # the text does show beside the person

    red2, yellow2, _ = _masks(_frame(outs["push"], 2.4))                           # near the end of the push (scale ~1.27)
    ys2, xs2 = np.nonzero(red2)
    w2, h2 = xs2.max() - xs2.min() + 1, ys2.max() - ys2.min() + 1
    assert 1.18 * 400 <= w2 <= 1.32 * 400 and 1.18 * 800 <= h2 <= 1.32 * 800, (w2, h2)   # the person grew
    assert yellow2[ys2.min():ys2.max() + 1, xs2.min():xs2.max() + 1].sum() == 0    # and the text is still behind them
    plain_h = _masks(_frame(outs["push"], 2.9))[0]                                  # after the push ends it snaps back
    ys3, xs3 = np.nonzero(plain_h)
    assert 380 <= xs3.max() - xs3.min() + 1 <= 420


def test_an_explainer_picture_shows_only_in_its_window_and_covers_the_frame(tmp_path):
    import cv2
    import numpy as np
    from clipcrow.edl import EDL
    src, _ = _synthetic_scene(tmp_path)
    (tmp_path / "out" / "assets").mkdir(parents=True)
    cv2.imwrite(str(tmp_path / "out" / "assets" / "pic.png"), np.full((640, 360, 3), (200, 40, 220), np.uint8))   # BGR: magenta-ish
    edl = EDL.model_validate({
        "job_id": "t", "source": {"uri": "src.mp4", "duration": 4, "width": 1080, "height": 1920, "fps": 30},
        "language": {"primary": "mr", "script": "Deva"}, "transcript": [{"w": "ज्ञान", "s": 0.5, "e": 1.0}],
        "assets": [{"id": "a1", "kind": "image", "origin": "capture", "ai": "none", "source_url": "https://news.example/s",
                    "uri": "assets/pic.png"}],
        "clips": [{"id": "c1", "source_in": 0, "source_out": 3.0, "captions": {"enabled": False},
                   "overlays": [{"kind": "media", "id": "pic1", "t_in": 1.0, "duration": 1.2, "asset": "a1", "motion": "none"}]}]})
    out, _ = render_clip(edl, "c1", tmp_path / "x.mp4", workdir=tmp_path / "w", media_root=tmp_path,
                         gsap_js=ROOT / "vendor" / "gsap.min.js", workers=2, asset_root=tmp_path / "out")
    before, during, after = (_frame(out, t) for t in (0.5, 1.6, 2.6))

    def magenta(f):
        b, g, r = (f[..., i].astype(int) for i in range(3))
        return ((r > 170) & (b > 170) & (g < 90)).mean()
    assert magenta(before) < 0.001 and magenta(after) < 0.001                       # not before it starts, not after it ends
    assert magenta(during) > 0.95                                                   # a full-frame cutaway covers everything
