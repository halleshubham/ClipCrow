from __future__ import annotations

import json
import subprocess
from pathlib import Path

from .plan import SourceInfo


def probe_source(path: str | Path, uri: str | None = None) -> SourceInfo:
    """Duration, size and fps of a local media file via ffprobe."""
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                        "stream=width,height,r_frame_rate:format=duration", "-of", "json", str(path)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise ValueError(f"ffprobe could not read {path}: {r.stderr.strip()[-200:]}")
    info = json.loads(r.stdout)
    st = info["streams"][0]
    num, _, den = st["r_frame_rate"].partition("/")
    fps = float(num) / float(den or 1) if float(den or 1) else 30.0
    return SourceInfo(uri=uri or Path(path).name, duration=float(info["format"]["duration"]),
                      width=int(st["width"]), height=int(st["height"]), fps=round(fps, 3))
