from __future__ import annotations

import json
import subprocess
from pathlib import Path

from .plan import SourceInfo


def probe_source(path: str | Path, uri: str | None = None, *, remote: bool = False) -> SourceInfo:
    """Duration, size and fps of a media file via ffprobe. ``remote=True``: ``path`` is an http(s) URL that the SERVER made
    (a presigned link to its own bucket, never an address a person typed), read with range requests so a 4 GB upload is
    probed without downloading it."""
    args = ["ffprobe", "-v", "error"]
    if remote:
        args += ["-protocol_whitelist", "http,https,tcp,tls,crypto", "-rw_timeout", "30000000"]
    r = subprocess.run([*args, "-select_streams", "v:0", "-show_entries",
                        "stream=width,height,r_frame_rate:format=duration", "-of", "json", str(path)],
                       capture_output=True, text=True, timeout=120 if remote else None)
    if r.returncode != 0:
        raise ValueError(f"ffprobe could not read {'the upload' if remote else path}: {r.stderr.strip()[-200:]}")
    info = json.loads(r.stdout)
    st = info["streams"][0]
    num, _, den = st["r_frame_rate"].partition("/")
    fps = float(num) / float(den or 1) if float(den or 1) else 30.0
    return SourceInfo(uri=uri or (Path(str(path).split("?")[0]).name), duration=float(info["format"]["duration"]),
                      width=int(st["width"]), height=int(st["height"]), fps=round(fps, 3))
