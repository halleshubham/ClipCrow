"""Make the pictures an explainer plan asked for, before rendering.

The planner records what it wants as assets with no file yet (``uri == ""``): a page to capture
(``origin="capture"``, ``source_url``) or a picture to draw (``origin="ai_image"``, ``prompt``). ``fetch_assets``
produces the files, fills in ``uri``/``model``/``tool``, and for anything that fails it drops the asset and the media
overlays that used it and leaves a note: an explainer without one picture is still a video, a failed job is not.
Messages kept in notes are fixed sentences, never raw exceptions or addresses.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

from ..edl import EDL
from .images import ImageError, ImageProvider, NotConfigured
from .screenshot import CaptureError, capture_page


def fetch_assets(edl: EDL, out_dir: str | Path, *, images: Optional[ImageProvider] = None,
                 capture_fn: Callable[..., Path] = capture_page,
                 say: Callable[[str], None] = lambda _m: None) -> tuple[EDL, list[str]]:
    out_dir = Path(out_dir).resolve()          # asset uris are relative to this folder (compile's ``asset_root``)
    (out_dir / "assets").mkdir(parents=True, exist_ok=True)
    failed: dict[str, str] = {}
    assets = []
    for a in edl.assets:
        if a.uri or a.origin not in ("capture", "ai_image"):
            assets.append(a)
            continue
        dest = out_dir / "assets" / f"{a.id}.png"
        try:
            if a.origin == "capture":
                say(f"capturing a page for {a.id}")
                real = capture_fn(a.source_url, dest)
                assets.append(a.model_copy(update={"uri": _rel(real, out_dir), "tool": "page-screenshot"}))
            else:
                if images is None:
                    raise NotConfigured("image generation is not configured")
                say(f"generating an image for {a.id}")
                real = images.generate(a.prompt, dest)
                assets.append(a.model_copy(update={"uri": _rel(real, out_dir), "tool": images.name, "model": images.model}))
        except (CaptureError, ImageError) as e:
            failed[a.id] = str(e)[:160]
        except Exception:                                   # never let one picture fail the whole job
            failed[a.id] = "the picture could not be made"
    if not failed:
        return edl.model_copy(update={"assets": assets}), []
    warnings = [f"a picture was skipped: {why}" for why in dict.fromkeys(failed.values())]
    clips = []
    for c in edl.clips:
        keep = [o for o in c.overlays if not (o.kind == "media" and o.asset in failed)]
        if len(keep) != len(c.overlays):
            ai = c.ai.model_copy(update={"notes": [*c.ai.notes, *warnings]}) if c.ai else c.ai
            c = c.model_copy(update={"overlays": keep, "ai": ai})
        clips.append(c)
    return edl.model_copy(update={"assets": assets, "clips": clips}), warnings


def _rel(path: Path, root: Path) -> str:
    return str(Path(path).resolve().relative_to(root))
