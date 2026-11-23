"""Continue a job from a step it already finished, instead of doing everything again.

A job runs transcribe -> plan -> (assets) -> frame -> render. Each step leaves something on disk, and ``make()`` already knows how to start from it:

* **plan**   needs ``out/transcript.json`` (the words, already cleaned and corrected) and the video the clips are cut from (``working_video``: the cleaned cut when the
  cleanup ran). Nothing is transcribed again, so the speech model is not asked and the cleanup is not repeated.
* **frame**  needs ``out/edl.json``: the plan is kept, so the AI planner is not called again (that is the token spend); pictures and framing are done again.
* **render** needs ``out/edl.json`` written AFTER framing, and keeps every clip that already rendered (``ready`` and its file on disk): only the missing clips are made.

A restart (a deploy, a crash) resumes by itself from the furthest step it can (``"auto"``); a person can pick an earlier one on the job page. Everything here only LOOKS at the
job and its folder; the files are the evidence that a step really finished (a stage that says "done" without its file is not trusted).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .jobs import Job

STAGE_ORDER = ("transcribe", "plan", "assets", "frame", "render")
STEPS = ("plan", "frame", "render")                      # the points a job can continue from ("start over" is the absence of one)
WORDS = {"plan": "planning", "frame": "framing", "render": "rendering"}
MAX_RESTARTS = 5                                         # automatic restarts of one job, in total: the loop guard (a restart with no progress since the last one still stops it)


def progress(job: Job) -> int:
    """How far a job got: finished stages plus ready clips. A restart that finds this larger than at the previous restart did not waste the last attempt."""
    return sum(1 for s in job.stages if s.state in ("done", "skipped")) + sum(1 for c in job.clips if c.ready)


def _done(job: Job, name: str) -> bool:
    return job.stage(name).state in ("done", "skipped")


def _has(path: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def rendered_clips(job: Job, out: Path) -> set[str]:
    """Clips whose video is on disk and which the job already counts as ready: kept when only the rest has to be rendered."""
    return {c.id for c in job.clips if c.ready and _has(out / f"{c.id}.mp4")}


def available(job: Job, out: Path) -> list[str]:
    """The steps this job can continue from, earliest first (empty: only starting over is possible)."""
    steps: list[str] = []
    if _done(job, "transcribe") and _has(out / "transcript.json"):
        steps.append("plan")
    if _done(job, "transcribe") and _done(job, "plan") and _has(out / "edl.json"):
        steps.append("frame")
        if _done(job, "frame"):
            steps.append("render")
    return steps


def resolve(job: Job, out: Path, requested: Optional[str]) -> Optional[str]:
    """The step to continue from for a request: ``"auto"`` is the furthest available one, a named step must be available (None: it cannot, or nothing was asked)."""
    if not requested:
        return None
    steps = available(job, out)
    if requested == "auto":
        return steps[-1] if steps else None
    return requested if requested in steps else None


def reset_from(job: Job, step: Optional[str]) -> None:
    """Mark the stages from ``step`` onward as waiting again and keep the ones before it as finished. ``None`` starts everything over."""
    first = {"plan": "plan", "frame": "assets", "render": "render"}.get(step or "", "transcribe")
    cut = STAGE_ORDER.index(first)
    for s in job.stages:
        if s.name in STAGE_ORDER and STAGE_ORDER.index(s.name) >= cut:
            s.state, s.detail, s.started, s.seconds = "pending", "", None, None
    if step in (None, "plan", "frame"):
        job.clips = [c.model_copy(update={"ready": False, "notes": []}) for c in job.clips] if step else []


@dataclass
class Resume:
    step: str
    video: Path
    transcript_path: Optional[Path] = None
    edl_path: Optional[Path] = None
    skip_frame: bool = False
    rendered: set[str] = field(default_factory=set)


def plan_for(job: Job, out: Path, working: Optional[Path]) -> Optional[Resume]:
    """What ``make()`` should be given to continue this job (None when it is not continuing, or the evidence is gone and it must start over)."""
    step = resolve(job, out, job.resume_from)
    if step is None or working is None:
        return None
    if step == "plan":
        return Resume("plan", working, transcript_path=out / "transcript.json")
    if step == "frame":
        return Resume("frame", working, edl_path=out / "edl.json")
    return Resume("render", working, edl_path=out / "edl.json", skip_frame=True, rendered=rendered_clips(job, out))
