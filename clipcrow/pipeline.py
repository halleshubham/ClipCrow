"""EDL clip -> MP4: compile, check, render."""
from __future__ import annotations

import os
from pathlib import Path

from . import matte, render
from .provenance import write_sidecar
from .compile import CompiledClip, compile_clip
from .edl import EDL, EDLError
from .render import RenderError

MATTE_FALLBACK = "text behind the speaker was skipped: the person matte could not be made ({why})"


def _without_matte(edl: EDL, clip, note: str):
    layout = clip.layout.model_copy(update={"mode": "passthrough"})
    ai = clip.ai.model_copy(update={"notes": [*clip.ai.notes, note]}) if clip.ai else None
    new = clip.model_copy(update={"layout": layout, "ai": ai,
                                  "overlays": [o for o in clip.overlays if o.kind != "text_behind"]})
    clips = [new if c.id == clip.id else c for c in edl.clips]
    assets = edl.assets
    if not any(c.layout.mode == "cutout_behind" for c in clips):      # the matte was never applied: do not claim it
        assets = [a for a in assets if a.origin != "matte"]
    return edl.model_copy(update={"clips": clips, "assets": assets}), new


def render_clip(edl: EDL, clip_id: str, output: str | Path, *, workdir: str | Path,
                media_root: str | Path, gsap_js: str | Path | None = None,
                workers: int | None = None, matte_device: str = "auto",
                run_check: bool = True, asset_root: str | Path | None = None) -> tuple[Path, CompiledClip]:
    clip = next((c for c in edl.clips if c.id == clip_id), None)
    if clip is None:
        raise EDLError(f"no clip {clip_id!r}; have {[c.id for c in edl.clips]}")
    gsap_js = gsap_js or os.environ.get("CLIPCROW_GSAP")
    project = Path(workdir) / f"{edl.job_id}-{clip.id}"

    cache = Path(workdir) / "matte-cache"
    matte_fn = lambda src, dest: matte.remove_background(src, dest, device=matte_device)   # noqa: E731
    try:
        compiled = compile_clip(edl, clip, project, media_root=media_root, gsap_js=gsap_js,
                                matte_fn=matte_fn, matte_cache=cache, asset_root=asset_root)
    except RenderError as e:
        if clip.layout.mode != "cutout_behind" or clip.layout.cutout_uri:
            raise
        # No matte (model missing, offline, out of memory): a reel without text behind the speaker is still
        # a reel. Say so, rather than failing the whole clip.
        lines = [x.strip() for x in str(e).splitlines() if x.strip()]
        why = lines[-1][:120] if lines else "unknown error"
        note = MATTE_FALLBACK.format(why=why)
        edl, clip = _without_matte(edl, clip, note)
        compiled = compile_clip(edl, clip, project, media_root=media_root, gsap_js=gsap_js, matte_cache=cache, asset_root=asset_root)
        compiled.warnings.append(note)

    if run_check:
        problems = render.check_problems(render.check(project))
        if problems:
            raise EDLError("the compiled project failed HyperFrames check:\n  " + "\n  ".join(problems))
    out = render.render(project, output, quality=edl.render.quality, workers=workers,
                        browser_path=os.environ.get("CLIPCROW_BROWSER"))
    write_sidecar(edl, clip, out)
    return out, compiled
