"""video -> clips: transcribe, plan, frame, render.

Each stage can be skipped by handing in its output (a transcript, or a whole EDL), which is how a
partly-done job resumes and how the pipeline runs without an API key or a Whisper model.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from .edl import EDL, load_edl
from .framing import apply_framing
from .planner import PlannerOptions, load_transcript, plan_edl
from .planner.llm import LLM
from .planner.source import probe_source


@dataclass
class MakeResult:
    edl: EDL
    edl_path: Path
    videos: list[Path] = field(default_factory=list)
    report: dict = field(default_factory=dict)


def make(video: str | Path, out_dir: str | Path, *, language: Optional[str] = None,
         transcript_path: str | Path | None = None, edl_path: str | Path | None = None,
         llm: LLM | None = None, score_llm: LLM | None = None, opts: PlannerOptions | None = None,
         detector=None, render: bool = True, gsap_js: str | Path | None = None, workers: int | None = None,
         transcribe_fn: Callable | None = None, render_fn: Callable | None = None,
         say: Callable[[str], None] = lambda _m: None) -> MakeResult:
    video, out = Path(video), Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    report: dict = {}

    if edl_path:
        edl = load_edl(edl_path)
        say(f"using the EDL {edl_path}")
    else:
        if transcript_path:
            tr = load_transcript(transcript_path, language)
            say(f"using the transcript {transcript_path}")
        else:
            if transcribe_fn is None:
                from .asr import release_models, transcribe as transcribe_fn   # noqa: PLW0127
            else:
                release_models = lambda: None  # noqa: E731
            say("transcribing")
            tr = transcribe_fn(str(video), language, progress=say)
            release_models()
            (out / "transcript.json").write_text(json.dumps(
                {"language": tr.language, "words": [w.model_dump(exclude_none=True) for w in tr.words]},
                ensure_ascii=False, indent=1), encoding="utf-8")
        if llm is None:
            from .planner import AnthropicLLM
            llm = AnthropicLLM()
        opts = opts or PlannerOptions()
        opts.on_progress = opts.on_progress or say
        plan = plan_edl(tr, probe_source(video, uri=video.name), llm, opts, score_llm=score_llm)
        edl, report = plan.edl, plan.report
        say(f"planned {len(edl.clips)} clip(s)")

    edl = apply_framing(edl, str(video), detector=detector, say=say)
    (out / "edl.json").write_text(edl.model_dump_json(indent=2, exclude_none=True), encoding="utf-8")
    if report:
        (out / "plan-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    res = MakeResult(edl, out / "edl.json", report=report)
    if render:
        if render_fn is None:
            from .pipeline import render_clip as render_fn
        for c in edl.clips:
            say(f"rendering {c.id}")
            mp4, _ = render_fn(edl, c.id, out / f"{c.id}.mp4", workdir=out / "work", media_root=video.parent,
                               gsap_js=gsap_js, workers=workers)
            res.videos.append(Path(mp4))
    return res
