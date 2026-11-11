"""Person matte (alpha WebM) via HyperFrames' `remove-background` (u2net_human_seg).

CPU is about 2 fps at 1080x1920, so a 60 s / 30 fps reel is ~14 min; pass
device="cuda" on a GPU host. First run downloads the model (~170 MB).
"""
from __future__ import annotations

import json
from pathlib import Path

from .render import RenderError, _run, _tail


def remove_background(src: str | Path, dest: str | Path, *, device: str = "auto",
                      quality: str = "balanced", timeout: int = 3600) -> dict:
    src, dest = Path(src).resolve(), Path(dest).resolve()
    args = ["remove-background", str(src), "-o", str(dest), "--device", device,
            "--quality", quality, "--json"]
    r = _run(args, src.parent, timeout)
    if r.returncode != 0 or not dest.is_file():
        raise RenderError(f"remove-background failed (exit {r.returncode}):\n{_tail(r.stderr or r.stdout)}")
    for line in reversed(r.stdout.strip().splitlines()):
        try:
            return json.loads(line)
        except ValueError:
            continue
    return {"ok": True, "outputPath": str(dest)}
