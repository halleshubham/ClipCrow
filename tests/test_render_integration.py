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
    inside = yellow[y0 + 10:y1 + 1 - 10, x0 + 10:x1 + 1 - 10].sum()                 # the interior; edge pixels blend a little
    assert inside == 0, f"text is painted over the person ({inside} yellow pixels inside the block)"
    band = yellow[700:1130]
    assert band[:, :x0].sum() > 1_000 or band[:, x1 + 1:].sum() > 1_000           # the text does show beside the person

    red2, yellow2, _ = _masks(_frame(outs["push"], 2.4))                           # near the end of the push (scale ~1.27)
    ys2, xs2 = np.nonzero(red2)
    w2, h2 = xs2.max() - xs2.min() + 1, ys2.max() - ys2.min() + 1
    assert 1.18 * 400 <= w2 <= 1.32 * 400 and 1.18 * 800 <= h2 <= 1.32 * 800, (w2, h2)   # the person grew
    m = 10                                                                          # edges blend a little (chroma subsampling, scaler)
    assert yellow2[ys2.min() + m:ys2.max() + 1 - m, xs2.min() + m:xs2.max() + 1 - m].sum() == 0   # the text is still behind them
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


def test_cover_logo_and_outro_in_a_real_render(tmp_path):
    """Frame 0 carries the title; the logo shows after the cover, in its corner; the ending video follows the clip."""
    import cv2
    import numpy as np
    from clipcrow import branding
    from clipcrow.edl import EDL
    src, _ = _synthetic_scene(tmp_path)
    out_dir = tmp_path / "out"
    (out_dir / "brand").mkdir(parents=True)
    cv2.imwrite(str(out_dir / "brand" / "logo.png"), np.full((200, 400, 3), (0, 220, 0), np.uint8))          # a green block
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=0xFF00FF:s=640x360:r=30:d=3",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(out_dir / "brand" / "outro.mp4")], check=True)
    edl = EDL.model_validate({
        "job_id": "t", "source": {"uri": "src.mp4", "duration": 4, "width": 1080, "height": 1920, "fps": 30},
        "language": {"primary": "mr", "script": "Deva"}, "transcript": [{"w": "ज्ञान", "s": 0.5, "e": 1.0}],
        "assets": [{"id": "brand-logo", "kind": "image", "origin": "upload", "uri": "brand/logo.png"},
                   {"id": "brand-outro", "kind": "video", "origin": "upload", "uri": "brand/outro.mp4"}],
        "clips": [{"id": "c1", "source_in": 0, "source_out": 3.0, "captions": {"enabled": False},
                   "cover": {"text": "ज्ञान शक्ती आहे", "duration_ms": 400, "background": "color", "bg_color": "#FF0000",
                             "color": "#FFFFFF", "accent": "#FFFFFF"},
                   "logo": {"asset": "brand-logo", "position": "tl", "size_pct": 20, "opacity": 1.0},
                   "outro": {"asset": "brand-outro", "max_seconds": 2.0}}]})
    out, compiled = render_clip(edl, "c1", tmp_path / "x.mp4", workdir=tmp_path / "w", media_root=tmp_path,
                                gsap_js=ROOT / "vendor" / "gsap.min.js", workers=2, asset_root=out_dir)
    assert not [w for w in compiled.warnings if "skipped" in w or "not saved" in w], compiled.warnings
    info = branding.probe_media(out)
    assert abs(info["duration"] - 5.0) < 0.15 and info["has_audio"] and (info["width"], info["height"]) == (1080, 1920)

    def share(f, rgb, tol=40):
        r, g, b = (f[..., i].astype(int) for i in (2, 1, 0))
        return ((abs(r - rgb[0]) < tol) & (abs(g - rgb[1]) < tol) & (abs(b - rgb[2]) < tol)).mean()
    first, during, after, end = (_frame(out, t) for t in (0.0, 0.2, 1.0, 4.3))
    assert share(first, (255, 0, 0)) > 0.5 and share(during, (255, 0, 0)) > 0.5       # solid cover colour from frame 0
    assert share(after, (255, 0, 0)) < 0.01                                            # gone after 400 ms
    corner = after[250 + 60:250 + 60 + 100, 60:60 + 216]
    assert share(corner, (0, 220, 0)) > 0.8, "the logo is not in the top-left corner"
    assert share(after[1500:], (0, 220, 0)) < 0.001                                    # and only there
    assert share(end, (255, 0, 255)) > 0.2                                             # the ending video follows the clip
    poster = cv2.imread(str(out.with_name("x.cover.png")))
    assert share(poster, (255, 0, 0)) > 0.5                                            # the saved cover image is the title card


def test_a_style_s_caption_look_reaches_the_rendered_pixels(tmp_path):
    """The caption colour, size and position a style carries must show up in the frame, not just in the EDL."""
    from PIL import Image
    base = json.loads((ROOT / "examples" / "demo.edl.json").read_text("utf-8"))
    shots = {}
    for name, caps in (("plain", {}),
                       ("styled", {"top_px": 300, "size_scale": 1.6, "color": "#00FF00", "highlight_color": "#FF00FF"})):
        d = json.loads(json.dumps(base))
        clip = next(c for c in d["clips"] if c["id"] == "c2")
        clip["captions"] = {**clip.get("captions", {}), "enabled": True, **caps}
        p = tmp_path / f"{name}.edl.json"
        p.write_text(json.dumps(d), "utf-8")
        out, _ = render_clip(load_edl(p), "c2", tmp_path / f"{name}.mp4", workdir=tmp_path / f"w-{name}",
                             media_root=ROOT / "examples" / "assets", gsap_js=ROOT / "vendor" / "gsap.min.js", workers=2)
        png = tmp_path / f"{name}.png"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", "1.2", "-i", str(out), "-frames:v", "1", str(png)], check=True)
        shots[name] = Image.open(png).convert("RGB")

    def greenest(img):
        """The brightest pure-green pixel's row, and how much green there is: the styled captions are #00FF00."""
        px = img.load()
        rows = [(y, x) for y in range(0, img.height, 4) for x in range(0, img.width, 4)
                if px[x, y][1] > 180 and px[x, y][0] < 110 and px[x, y][2] < 110]
        return rows

    assert not greenest(shots["plain"]), "the unstyled render already has green captions: the test proves nothing"
    green = greenest(shots["styled"])
    assert green, "the style's caption colour never reached the pixels"
    top = min(y for y, _ in green)
    assert top < shots["plain"].height * 0.45, f"styled captions should sit high in the frame, found first green at y={top}"
