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
                          spread=p.spread, cutout=p.cutout, speaker_name=p.speaker_name or None,
                          speaker_title=p.speaker_title)


class RealPipeline:
    """Speech recognition with faster-whisper, planning with Claude, rendering with HyperFrames."""

    def check_ready(self) -> str | None:
        if importlib.util.find_spec("faster_whisper") is None:
            return "Speech recognition is not installed on the server (pip install 'clipcrow[asr]')."
        if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")
                or os.environ.get("CLIPCROW_SKIP_CREDENTIAL_CHECK")):
            return "No Claude credentials on the server: set ANTHROPIC_API_KEY."
        return None

    def run(self, job: Job, video: Path, out: Path, hooks: Hooks, say) -> MakeResult:
        from ..planner import AnthropicLLM
        return make(video, out, language=job.params.language, llm=AnthropicLLM(), opts=planner_options(job),
                    hooks=hooks, say=say, multi=job.params.multi,
                    gsap_js=os.environ.get("CLIPCROW_GSAP", "vendor/gsap.min.js"))
