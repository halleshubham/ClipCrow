"""Demo pipeline: the whole service without Whisper, an API key or a model.

It uses a built-in (synthetic) Marathi transcript and the model answers recorded for it, so any uploaded video of
3.5 minutes or more produces two real clips: real framing, real HyperFrames render. The uploaded options are
ignored (the recorded answers match one fixed set of options). For trying the UI and for demos, not for real work.
Needs a source checkout: the data lives in examples/ and tests/data/.
"""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from ..make import Hooks, MakeResult, make
from ..planner import PlannerOptions, ReplayLLM, load_transcript
from .jobs import Job
from .pipelines import render_one

ROOT = Path(__file__).resolve().parents[2]
TRANSCRIPT = ROOT / "examples" / "synthetic_talk_mr.whisper.json"
ANSWERS = ROOT / "tests" / "data" / "replay_synthetic_talk"
MIN_SECONDS = 203          # the built-in transcript is 202 s long


class DemoPipeline:
    def check_ready(self) -> str | None:
        if not TRANSCRIPT.is_file() or not ANSWERS.is_dir():
            return "Demo data not found: demo mode needs a source checkout of ClipCrow."
        return None

    def render_clip(self, job: Job, edl, clip_id: str, out: Path, workdir: Path, media_root: Path) -> list[str]:
        return render_one(job, edl, clip_id, out, workdir, media_root, ROOT / "vendor" / "gsap.min.js")

    def run(self, job: Job, video: Path, out: Path, hooks: Hooks, say) -> MakeResult:
        if job.source.get("duration", 0) < MIN_SECONDS:
            raise ValueError("Demo mode needs a video of at least 3.5 minutes: it uses a built-in 3.4-minute transcript.")
        opts = PlannerOptions(job_id=job.id, target_clips=2, min_clip=20, max_clip=60, speaker_name="शुभम",
                              speaker_title="संस्थापक", workers=1)
        with tempfile.TemporaryDirectory() as tmp:                       # ReplayLLM writes into its directory
            shutil.copytree(ANSWERS, tmp, dirs_exist_ok=True)
            return make(video, out, llm=ReplayLLM(tmp), opts=opts, hooks=hooks, say=say,
                        transcribe_fn=lambda media, language=None, progress=None: load_transcript(TRANSCRIPT),
                        gsap_js=ROOT / "vendor" / "gsap.min.js")
