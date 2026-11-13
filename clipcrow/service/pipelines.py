"""The real pipeline (faster-whisper + Claude + HyperFrames) and a demo one that needs none of them."""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path

from ..make import Hooks, MakeResult, make
from ..planner import PlannerOptions
from .jobs import Job


def planner_options(job: Job) -> PlannerOptions:
    p = job.params
    return PlannerOptions(job_id=job.id, min_clip=p.min_clip, max_clip=p.max_clip, target_clips=p.clips,
                          spread=p.spread, cutout=p.cutout, immersive=p.immersive, explainer=p.explainer,
                          links=tuple(p.links), ai_images=p.ai_images, speaker_name=p.speaker_name or None,
                          speaker_title=p.speaker_title)


def render_one(job: Job, edl, clip_id: str, out: Path, workdir: Path, media_root: Path, gsap_js) -> list[str]:
    """Render one clip to ``out`` (via a temporary file, so a video being played is never half-written)."""
    from ..pipeline import render_clip
    tmp = out.with_name(out.stem + ".new.mp4")
    mp4, compiled = render_clip(edl, clip_id, tmp, workdir=workdir, media_root=media_root, gsap_js=gsap_js,
                                asset_root=out.parent)
    os.replace(mp4, out)
    cover_new, cover = tmp.with_name(tmp.stem + ".cover.png"), out.with_name(out.stem + ".cover.png")
    if cover_new.is_file():
        os.replace(cover_new, cover)
    else:
        cover.unlink(missing_ok=True)
    side = Path(str(mp4) + ".ai.json")
    if side.is_file():
        os.replace(side, Path(str(out) + ".ai.json"))
    return list(getattr(compiled, "warnings", None) or [])


class RealPipeline:
    """Speech recognition with faster-whisper, planning with Claude, rendering with HyperFrames."""

    @property
    def can_generate_images(self) -> bool:
        return bool(os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"))

    def check_ready(self) -> str | None:
        if importlib.util.find_spec("faster_whisper") is None:
            return "Speech recognition is not installed on the server (pip install 'clipcrow[asr]')."
        if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")
                or os.environ.get("CLIPCROW_SKIP_CREDENTIAL_CHECK")):
            return "No Claude credentials on the server: set ANTHROPIC_API_KEY."
        return None

    @staticmethod
    def _images():
        from ..assets.images import GeminiImages
        return GeminiImages()

    def render_clip(self, job: Job, edl, clip_id: str, out: Path, workdir: Path, media_root: Path) -> list[str]:
        return render_one(job, edl, clip_id, out, workdir, media_root, os.environ.get("CLIPCROW_GSAP", "vendor/gsap.min.js"))

    def run(self, job: Job, video: Path, out: Path, hooks: Hooks, say) -> MakeResult:
        from ..planner import AnthropicLLM
        return make(video, out, language=job.params.language, llm=AnthropicLLM(), opts=planner_options(job),
                    hooks=hooks, say=say, multi=job.params.multi,
                    images=self._images() if job.params.ai_images else None,
                    gsap_js=os.environ.get("CLIPCROW_GSAP", "vendor/gsap.min.js"))
