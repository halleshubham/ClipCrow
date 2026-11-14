"""EDL clip -> MP4: compile, check, render."""
from __future__ import annotations

import os
from pathlib import Path

from . import branding, matte, render
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
        report = render.check(project)
        problems = render.check_problems(report)
        if problems:
            raise EDLError("the compiled project failed HyperFrames check:\n  " + "\n  ".join(problems))
        compiled.warnings.extend(w for w in render.check_warnings(report) if w not in compiled.warnings)
    out = render.render(project, output, quality=edl.render.quality, workers=workers,
                        browser_path=os.environ.get("CLIPCROW_BROWSER"), timeout=max(1800, int(clip.duration * 12)))      # a long clip takes longer
    out = Path(out)
    if clip.outro and clip.outro.enabled:
        asset = next(a for a in edl.assets if a.id == clip.outro.asset)
        if not asset.uri:
            raise EDLError(f"clip {clip.id!r}: the ending video {asset.id!r} has no file")
        from .compile import _inside
        tail = _inside(Path(asset_root) if asset_root else Path(media_root), asset.uri)
        joined = out.with_name(out.stem + ".joined.mp4")
        try:
            branding.append_outro(out, tail, joined, width=edl.render.width, height=edl.render.height, fps=edl.render.fps,
                                  max_seconds=clip.outro.max_seconds)
            joined.replace(out)
        except branding.BrandError as e:        # the clip itself is fine: deliver it without the ending and say so
            joined.unlink(missing_ok=True)
            compiled.warnings.append(f"the ending video was skipped: {e}")
    cover = out.with_name(out.stem + ".cover.png")
    if clip.cover and clip.cover.enabled:
        try:
            branding.extract_cover(out, cover)
        except branding.BrandError as e:
            compiled.warnings.append(f"the cover image was not saved: {e}")
    else:
        cover.unlink(missing_ok=True)
    write_sidecar(edl, clip, out)
    return out, compiled
