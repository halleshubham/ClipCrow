"""EDL clip -> MP4: compile, check, render."""
from __future__ import annotations

import os
from pathlib import Path

from . import matte, render
from .compile import CompiledClip, compile_clip
from .edl import EDL, EDLError


def render_clip(edl: EDL, clip_id: str, output: str | Path, *, workdir: str | Path,
                media_root: str | Path, gsap_js: str | Path | None = None,
                workers: int | None = None, matte_device: str = "auto",
                run_check: bool = True) -> tuple[Path, CompiledClip]:
    clip = next((c for c in edl.clips if c.id == clip_id), None)
    if clip is None:
        raise EDLError(f"no clip {clip_id!r}; have {[c.id for c in edl.clips]}")
    gsap_js = gsap_js or os.environ.get("REELCORE_GSAP")
    project = Path(workdir) / f"{edl.job_id}-{clip.id}"

    compiled = compile_clip(
        edl, clip, project, media_root=media_root, gsap_js=gsap_js,
        matte_fn=lambda src, dest: matte.remove_background(src, dest, device=matte_device))

    if run_check:
        problems = render.check_problems(render.check(project))
        if problems:
            raise EDLError("the compiled project failed HyperFrames check:\n  " + "\n  ".join(problems))
    out = render.render(project, output, quality=edl.render.quality, workers=workers,
                        browser_path=os.environ.get("REELCORE_BROWSER"))
    return out, compiled
