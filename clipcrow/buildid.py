"""Which version of the render code is this? A GPU worker compiles and renders a clip itself, so a worker running older code than the server
(before a layout fix, or without a new field of the EDL) fails or renders something different. The server and the worker each hash the same
files; the server only sends renders to a worker whose hash matches."""
from __future__ import annotations

import hashlib
from pathlib import Path

# every module whose behaviour changes what a render looks like
FILES = ("compile.py", "edl.py", "branding.py", "pipeline.py", "render.py", "captions.py", "fonts.py", "fonts_catalog.json", "transitions.py",
         "cues.py", "loudness.py", "music.py", "platform_marks.py", "matte.py", "provenance.py")


def render_build() -> str:
    """12 hex digits over the render code (file names and contents); stable across machines for the same release."""
    h = hashlib.sha256()
    here = Path(__file__).resolve().parent
    for name in FILES:
        p = here / name
        h.update(name.encode() + b"\0")
        try:
            h.update(p.read_bytes().replace(b"\r\n", b"\n"))
        except OSError:
            h.update(b"missing")
    return h.hexdigest()[:12]
