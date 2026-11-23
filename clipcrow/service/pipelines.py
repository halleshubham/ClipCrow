"""The real pipeline (faster-whisper + Claude + HyperFrames) and a demo one that needs none of them."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

from ..make import Hooks, MakeResult, make
from ..planner import PlannerOptions
from .jobs import Job
from .workers import hub_view


def _worker_clip_limit() -> float:
    """Clips longer than this many seconds go only to a GPU worker that can send the MP4 to the bucket, else they render on the server (CLIPCROW_WORKER_MAX_CLIP_SECONDS, 0 = no limit)."""
    try:
        return max(0.0, float(os.environ.get("CLIPCROW_WORKER_MAX_CLIP_SECONDS", "60")))
    except ValueError:
        return 60.0


def planner_options(job: Job, pictures_dir: Path | None = None) -> PlannerOptions:
    p = job.params
    opts = PlannerOptions(job_id=job.id, min_clip=p.min_clip, max_clip=p.max_clip, target_clips=p.clips, whole=p.whole,
                          spread=p.spread, cutout=p.cutout, immersive=p.immersive, explainer=p.explainer, sounds=p.sounds,
                          transition=None if p.transition == "none" else p.transition,
                          links=(), ai_images=p.ai_images, speaker_name=p.speaker_name or None,
                          speaker_title=p.speaker_title)
    if pictures_dir is not None:
        opts.pictures = tuple((pid, pictures_dir / f"{pid}.png") for pid in p.pictures if (pictures_dir / f"{pid}.png").is_file())
    st = p.style
    if st is not None:                    # clip length was already settled when the job was made (see api.build_params)
        opts.caption_max_words = st.caption_max_words
        opts.caption_pause_break = st.caption_pause_break
        opts.immersive = opts.immersive or st.punch_ins
        opts.punch_min_gap = st.punch.min_gap
        opts.punch_max_per_clip = st.punch.max_per_clip
        if st.captions.from_reference:
            opts.caption_look = st.captions.model_dump()
    return opts


def render_one(job: Job, edl, clip_id: str, out: Path, workdir: Path, media_root: Path, gsap_js, render_fn=None) -> list[str]:
    """Render one clip to ``out`` (via a temporary file, so a video being played is never half-written).
    ``render_fn`` replaces ``pipeline.render_clip`` (a RemoteRenderer, which falls back to it)."""
    from ..pipeline import render_clip
    render_clip = render_fn or render_clip
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
        renderer = None
        if self.hub is not None:                              # a re-render after an edit can use the GPU worker too
            from .workers import RemoteRenderer
            renderer = RemoteRenderer(hub_view(self.hub, job), max_clip_seconds=_worker_clip_limit())
        return render_one(job, edl, clip_id, out, workdir, media_root, os.environ.get("CLIPCROW_GSAP", "vendor/gsap.min.js"), renderer)

    @staticmethod
    def reference_look(path: str, duration: float) -> dict | None:
        """What the reference's captions look like, read by one Claude vision call. None when there are no credentials."""
        import anthropic
        from ..planner import AnthropicLLM
        from ..styles_look import read_look
        llm = AnthropicLLM(client=anthropic.Anthropic(timeout=60.0, max_retries=1))      # a stuck call must not hold the queue
        return read_look(path, duration, llm).model_dump()

    hub = None                                                   # a WorkerHub, set by create_app when CLIPCROW_WORKER_TOKEN is on

    def run(self, job: Job, video: Path, out: Path, hooks: Hooks, say) -> MakeResult:
        from ..planner import AnthropicLLM
        from ..planner.llm import Interruptible
        transcribe_fn = None
        hub = hub_view(self.hub, job)
        if hub is not None and hub.online():           # a GPU worker is out there: let it listen, fall back if it cannot
            from .workers import RemoteTranscriber
            transcribe_fn = RemoteTranscriber(hub, video.parent, check=hooks.check)
        render_fn = None
        if self.hub is not None:                                 # each clip goes to a matching render worker when one is online
            from .workers import RemoteRenderer
            render_fn = RemoteRenderer(hub, check=hooks.check, say=say, max_clip_seconds=_worker_clip_limit())
        review = out / "cleanup.review.json"                      # a person went through "What was cut": rebuild from the saved words with exactly their choices
        recut: dict = {}
        if review.is_file():
            try:
                from ..cleancut import cut_from_report
                data = json.loads(review.read_text(encoding="utf-8"))
                saved = out / "transcript.original.json"
                recut = dict(transcript_path=saved if saved.is_file() else out / "transcript.json",
                             clean_cuts=[cut_from_report(c) for c in data["cuts"]], clean_reading=data.get("reading") or {})
            except (OSError, ValueError, KeyError, TypeError):
                recut = {}
        if getattr(self, "store", None) is not None and not recut:            # the same file again: reuse its transcript (the rest of the job runs as always)
            from .asr_cache import CachedTranscriber
            from .workers import _local_transcribe
            transcribe_fn = CachedTranscriber(transcribe_fn or _local_transcribe, self.store, job, out, fresh=job.params.fresh_transcript)
        if self.hub is not None:                                              # a worker with a GPU does the voice work; else it is done here
            from .workers import RemoteSpeakers
            recut.setdefault("speakers_fn", RemoteSpeakers(hub, job.id, check=hooks.check))
        extra: dict = {}
        if job.resume_from and getattr(self, "store", None) is not None:       # continue from a finished step: the files on disk are the proof (service/resume.py)
            from .resume import plan_for
            rp = plan_for(job, out, self.store.working_video(job.id))
            if rp is not None:
                say(f"continuing from the {rp.step} step: the earlier steps are kept")
                video, recut, transcribe_fn = rp.video, {}, None               # the working video (the cleaned cut when the cleanup ran); a saved re-cut does not apply here
                extra = dict(clean=False)                                      # the cleanup is already in the saved words and video
                if rp.transcript_path is not None:
                    extra["transcript_path"] = rp.transcript_path
                if rp.edl_path is not None:
                    extra.update(edl_path=rp.edl_path, skip_frame=rp.skip_frame, skip_render=rp.rendered)
            else:
                say("the earlier steps could not be reused: starting from the beginning")
        kw = dict(language=job.params.language, clean_take=job.params.clean_take, **recut, llm=Interruptible(AnthropicLLM(), hooks.check), opts=planner_options(job, out.parent / "pictures"),
                    hooks=hooks, say=say, multi=job.params.multi, graphics=job.params.graphics, clean=job.params.clean, clean_pace=job.params.clean_pace, transcribe_fn=transcribe_fn, render_fn=render_fn,
                    images=self._images() if job.params.ai_images else None,
                    gsap_js=os.environ.get("CLIPCROW_GSAP", "vendor/gsap.min.js"),
                    refine_text=os.environ.get("CLIPCROW_TEXT_REFINE", "1") != "0")      # 0 = keep Whisper's Marathi/Hindi words as they are
        kw.update(extra)
        return make(video, out, **kw)
