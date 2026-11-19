"""video -> clips: transcribe, plan, frame, render.

Each stage can be skipped by handing in its output (a transcript, or a whole EDL), which is how a
partly-done job resumes and how the pipeline runs without an API key or a Whisper model.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from .branding import BrandingChoice, BrandKit, apply_brand
from .edl import EDL, load_edl
from .framing import apply_framing
from .planner import PlannerOptions, load_transcript, plan_edl
from .textfix import REFINE_LANGS, refine
from .planner.llm import LLM
from .planner.source import probe_source


class Cancelled(BaseException):
    """The caller asked to stop. A BaseException on purpose: the many "a failure here is fine, carry on" handlers (``except Exception``) in the recogniser,
    the cleanup and the text correction must not swallow a person's cancel and quietly carry on with the next step."""


STAGES = ("transcribe", "plan", "frame", "render")


@dataclass
class Hooks:
    """Progress callbacks for a caller that shows or records progress (the web service)."""
    stage: Callable[[str, str, str], None] = lambda name, state, detail="": None   # state: running|done|skipped
    clip_ready: Callable[[str, Path], None] = lambda clip_id, path: None
    planned: Callable[[EDL, dict], None] = lambda edl, report: None          # clips chosen and framed
    clip_notes: Callable[[str, list], None] = lambda clip_id, notes: None    # warnings for a person to read
    cancelled: Callable[[], bool] = lambda: False
    brand: Optional[BrandKit] = None                  # the account's logo / ending video / styles
    choice: Optional[BrandingChoice] = None           # which of cover, logo and outro this job asked for

    def check(self) -> None:
        if self.cancelled():
            raise Cancelled()


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
         say: Callable[[str], None] = lambda _m: None, hooks: Hooks | None = None,
         multi: str = "switch", graphics: bool = False, images=None, capture_fn=None,
         brand: BrandKit | None = None, choice: BrandingChoice | None = None, refine_text: bool = True,
         clean: bool = False, clean_pace: str = "natural", clean_take: str = "last", clean_cuts: list | None = None, clean_reading: dict | None = None,
         speakers_fn: Callable | None = None, framing_fn: Callable | None = None) -> MakeResult:
    """``clean``: cut hesitation sounds, stutters, retakes and long dead air out of the raw recording first (``cleancut``). The cleaned video
    (``out/clean.mp4``) becomes the source for everything after: planning, framing, rendering and later re-renders."""
    video, out = Path(video), Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    report: dict = {}
    hk = hooks or Hooks()
    brand, choice = brand or hk.brand, choice or hk.choice
    hk.check()

    def cleaned(tr):
        """(video, transcript, note) after the cleanup, or the inputs unchanged when it is off or leaves the recording alone."""
        nonlocal llm
        if not clean:
            return video, tr, ""
        from .cleancut import clean_video
        if clean_cuts is not None:
            (out / "clean.mp4").unlink(missing_ok=True)           # a re-cut: the old cut video must not outlive a choice that no longer cuts anything
        if llm is None:
            from .planner import AnthropicLLM
            llm = AnthropicLLM()
        say("cutting fumbles and dead air out of the recording")
        try:
            res = clean_video(video, tr, out, pace=clean_pace, llm=llm, say=say, check=hk.check, take=clean_take, cuts=clean_cuts, reading=clean_reading)
        except (ValueError, OSError) as e:                       # an unreadable file, a failed cut: the original still works
            say(f"the cleanup was skipped: {e}")
            return video, tr, ""
        if res is None:
            return video, tr, ""
        return res.video, res.transcript, ", " + res.summary

    if edl_path:
        edl = load_edl(edl_path)
        say(f"using the EDL {edl_path}")
        hk.stage("transcribe", "skipped", "EDL provided")
        hk.stage("plan", "skipped", "EDL provided")
    else:
        if transcript_path:
            tr = load_transcript(transcript_path, language)
            say(f"using the transcript {transcript_path}")
            before = video
            video, tr_cut, note = cleaned(tr)
            if clean_cuts is not None:                            # a re-cut from the saved words: write the files a first run would have written
                if video != before:
                    (out / "transcript.original.json").write_text(json.dumps(
                        {"language": tr.language, "words": [w.model_dump(exclude_none=True) for w in tr.words]}, ensure_ascii=False, indent=1), encoding="utf-8")
                (out / "transcript.json").write_text(json.dumps(
                    {"language": tr_cut.language, "words": [w.model_dump(exclude_none=True) for w in tr_cut.words]}, ensure_ascii=False, indent=1), encoding="utf-8")
            tr = tr_cut
            hk.stage("transcribe", "skipped", "transcript provided" + note)
        else:
            hk.stage("transcribe", "running", "")
            if transcribe_fn is None:
                from .asr import release_models, transcribe as transcribe_fn   # noqa: PLW0127
            else:
                release_models = lambda: None  # noqa: E731
            say("transcribing")
            import inspect
            takes_check = "check" in inspect.signature(transcribe_fn).parameters      # a double in a test may not
            tr = transcribe_fn(str(video), language, progress=say, **({"check": hk.check} if takes_check else {}))
            release_models()
            if not language and tr.language in ("hi", "mr"):                       # a worker or an older recogniser may have labelled Marathi as Hindi (or the reverse): the words decide
                from .indic import language_from_text
                said = language_from_text(" ".join(w.w for w in tr.words))
                if said and said != tr.language:
                    say(f"the words are {'Marathi' if said == 'mr' else 'Hindi'}, not {'Marathi' if tr.language == 'mr' else 'Hindi'}: the titles and the text correction use {'Marathi' if said == 'mr' else 'Hindi'}")
                    tr.language = said
            (out / "transcript.json").write_text(json.dumps(
                {"language": tr.language, "words": [w.model_dump(exclude_none=True) for w in tr.words]},
                ensure_ascii=False, indent=1), encoding="utf-8")
            detail, fixed = f"{len(tr.words)} words, language {tr.language}", ""
            if refine_text and tr.language in REFINE_LANGS:        # first: the cuts below are decided on the CORRECTED text, not on Whisper's Hindi-flavoured spelling
                if llm is None:
                    from .planner import AnthropicLLM
                    llm = AnthropicLLM()
                say("correcting the Marathi/Hindi text")
                raw_tr = tr
                tr, n = refine(tr, llm, say=say)
                if n:
                    (out / "transcript.raw.json").write_text(json.dumps(
                        {"language": raw_tr.language, "words": [w.model_dump(exclude_none=True) for w in raw_tr.words]},
                        ensure_ascii=False, indent=1), encoding="utf-8")
                    fixed = f", {n} sentence(s) corrected"
                    detail += fixed
                    say(f"{n} sentence(s) corrected; the original is in transcript.raw.json")
            before = video
            video, tr_cut, note = cleaned(tr)
            if video != before:                                   # the timeline changed: keep the one the recogniser heard
                (out / "transcript.original.json").write_text(json.dumps(
                    {"language": tr.language, "words": [w.model_dump(exclude_none=True) for w in tr.words]}, ensure_ascii=False, indent=1), encoding="utf-8")
                tr = tr_cut
                detail = f"{len(tr.words)} words kept, language {tr.language}{fixed}{note}"
            (out / "transcript.json").write_text(json.dumps(
                {"language": tr.language, "words": [w.model_dump(exclude_none=True) for w in tr.words]},
                ensure_ascii=False, indent=1), encoding="utf-8")
            hk.stage("transcribe", "done", detail)
        hk.check()
        hk.stage("plan", "running", "")
        if llm is None:
            from .planner import AnthropicLLM
            llm = AnthropicLLM()
        opts = opts or PlannerOptions()
        opts.on_progress = opts.on_progress or say
        plan = plan_edl(tr, probe_source(video, uri=video.name), llm, opts, score_llm=score_llm)
        edl, report = plan.edl, plan.report
        from .transitions import seams_from_cleanup, with_seams
        edl = with_seams(edl, seams_from_cleanup(out / "cleanup.json"))       # the cleanup's jump cuts, for transitions and cues
        say(f"planned {len(edl.clips)} clip(s)")
        hk.stage("plan", "done", f"{len(edl.clips)} clip(s)")

    hk.check()
    pending = [a for a in edl.assets if not a.uri and (a.origin in ("capture", "ai_image") or (a.origin == "upload" and a.ref))]
    if pending:
        from .assets.fetch import fetch_assets
        hk.stage("assets", "running", f"{len(pending)} picture(s)")
        kw = {"capture_fn": capture_fn} if capture_fn else {}
        edl, warns = fetch_assets(edl, out, images=images, say=say, pictures={pid: Path(p) for pid, p in (opts.pictures if opts else ())}, **kw)
        for w in warns:
            say(w)
        hk.stage("assets", "done", f"{sum(1 for a in edl.assets if a.uri)} of {len(pending)} picture(s) made")
    else:
        hk.stage("assets", "skipped", "no pictures requested")
    hk.check()
    hk.stage("frame", "running", "")
    speakers = None
    if multi != "off" and not graphics:                                  # who is on screen, who is heard, who is whose: once per video, kept in out/speakers.json
        try:
            if speakers_fn is None:
                from .seats import build_speaker_map as speakers_fn
            speakers = speakers_fn(video, out, detector=detector, say=say, check=hk.check)
        except Exception as e:                                               # a model that will not load must never fail the job (a cancel is not an Exception): the picture alone decides
            say(f"voices could not be matched to people ({type(e).__name__}); the picture alone decides who is shown")
    edl = apply_framing(edl, str(video), detector=detector, say=say, multi=multi, graphics=graphics, speakers=speakers, framing_fn=framing_fn)
    hk.stage("frame", "done", ", ".join(f"{c.id}: {c.layout.framing}" for c in edl.clips))
    if choice is not None and choice.any():
        edl = apply_brand(edl, brand or BrandKit(), choice, out)
    hk.planned(edl, report)
    (out / "edl.json").write_text(edl.model_dump_json(indent=2, exclude_none=True), encoding="utf-8")
    if report:
        (out / "plan-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    res = MakeResult(edl, out / "edl.json", report=report)
    if render:
        if render_fn is None:
            from .pipeline import render_clip as render_fn
        hk.stage("render", "running", f"0 of {len(edl.clips)}")
        for i, c in enumerate(edl.clips):
            hk.check()
            say(f"rendering {c.id}")
            mp4, compiled = render_fn(edl, c.id, out / f"{c.id}.mp4", workdir=out / "work", media_root=video.parent,
                                      gsap_js=gsap_js, workers=workers, asset_root=out)
            for w in getattr(compiled, "warnings", None) or []:
                say(f"{c.id}: {w}")
            hk.clip_notes(c.id, list(getattr(compiled, "warnings", None) or []))
            res.videos.append(Path(mp4))
            hk.clip_ready(c.id, Path(mp4))
            hk.stage("render", "running", f"{i + 1} of {len(edl.clips)}")
        hk.stage("render", "done", f"{len(edl.clips)} clip(s)")
    else:
        hk.stage("render", "skipped", "not requested")
    return res
