from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .edl import EDLError, json_schema, load_edl


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="clipcrow")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("schema", help="print the EDL JSON schema")
    v = sub.add_parser("validate", help="validate an EDL file")
    v.add_argument("edl")
    c = sub.add_parser("compile", help="write the HyperFrames project for a clip (no render)")
    c.add_argument("edl"); c.add_argument("--clip", required=True); c.add_argument("--out", required=True)
    c.add_argument("--media-root", default="."); c.add_argument("--gsap")
    c.add_argument("--no-media", action="store_true", help="HTML only: skip ffmpeg and the matte")
    t = sub.add_parser("transcribe", help="speech to a word-timed transcript JSON (faster-whisper, Indic-aware)")
    t.add_argument("media"); t.add_argument("--out", required=True); t.add_argument("--language")
    mk = sub.add_parser("make", help="video -> clips: transcribe, plan, frame, render")
    mk.add_argument("video"); mk.add_argument("--out", required=True); mk.add_argument("--language")
    mk.add_argument("--transcript", help="skip speech recognition: use this transcript JSON")
    mk.add_argument("--edl", help="skip transcription and planning: use this EDL")
    mk.add_argument("--clips", type=int); mk.add_argument("--min-clip", type=int, default=15)
    mk.add_argument("--max-clip", type=int, default=60); mk.add_argument("--cutout", action="store_true")
    mk.add_argument("--spread", type=float, default=0.5, help="spread clips across the talk (0 = off)")
    mk.add_argument("--speaker-name"); mk.add_argument("--speaker-title", default="")
    mk.add_argument("--model"); mk.add_argument("--score-model"); mk.add_argument("--gsap")
    mk.add_argument("--workers", type=int); mk.add_argument("--no-render", action="store_true")
    u = sub.add_parser("usage", help="show the AI-usage record of an EDL")
    u.add_argument("edl")
    pl = sub.add_parser("plan", help="transcript -> EDL with an LLM (needs ANTHROPIC_API_KEY)")
    pl.add_argument("transcript"); pl.add_argument("--source", required=True, help="the source video file")
    pl.add_argument("--out", required=True); pl.add_argument("--media-root", default=".")
    pl.add_argument("--duration", type=float, help="source length in seconds, when the video file is not available")
    pl.add_argument("--job-id", default="job"); pl.add_argument("--language")
    pl.add_argument("--clips", type=int, help="how many clips (default: about one per 2 minutes)")
    pl.add_argument("--min-clip", type=int, default=15); pl.add_argument("--max-clip", type=int, default=60)
    pl.add_argument("--cutout", action="store_true", help="allow text behind the speaker (a cutout will exist)")
    pl.add_argument("--speaker-name"); pl.add_argument("--speaker-title", default="")
    pl.add_argument("--spread", type=float, default=0.5,
                    help="spread clips across the talk (0 = off, 0.5 = default): min gap = spread x length / clips")
    pl.add_argument("--model", help="default: $CLIPCROW_MODEL or claude-opus-5-5")
    pl.add_argument("--score-model", help="cheaper model for the window-scoring pass")
    pl.add_argument("--report", help="write the planning report (scores, drops, warnings) here")
    pl.add_argument("--answers", help="answer prompts from files in this directory instead of calling an API "
                                       "(queues unanswered prompts in DIR/pending/)")
    r = sub.add_parser("render", help="compile, check and render a clip to MP4")
    r.add_argument("edl"); r.add_argument("--clip", required=True); r.add_argument("--out", required=True)
    r.add_argument("--media-root", default="."); r.add_argument("--workdir", default="build")
    r.add_argument("--gsap"); r.add_argument("--workers", type=int)
    r.add_argument("--matte-device", default="auto"); r.add_argument("--no-check", action="store_true")
    a = ap.parse_args(argv)

    try:
        if a.cmd == "schema":
            print(json.dumps(json_schema(), indent=2)); return 0
        if a.cmd == "transcribe":
            from .asr import release_models, transcribe
            tr = transcribe(a.media, a.language, progress=lambda m: print(f"  {m}", file=sys.stderr))
            Path(a.out).write_text(json.dumps({"language": tr.language, "words": [w.model_dump(exclude_none=True) for w in tr.words]},
                                              ensure_ascii=False, indent=1), encoding="utf-8")
            release_models()
            print(f"{len(tr.words)} words, {len(tr.sentences)} sentences, language {tr.language} -> {a.out}")
            return 0
        if a.cmd == "make":
            from .make import make
            from .planner import AnthropicLLM, PlannerOptions
            opts = PlannerOptions(min_clip=a.min_clip, max_clip=a.max_clip, target_clips=a.clips, cutout=a.cutout,
                                  spread=a.spread,
                                  speaker_name=a.speaker_name, speaker_title=a.speaker_title)
            need_llm = not a.edl
            res = make(a.video, a.out, language=a.language, transcript_path=a.transcript, edl_path=a.edl, opts=opts,
                       llm=AnthropicLLM(a.model) if need_llm else None,
                       score_llm=AnthropicLLM(a.score_model) if need_llm and a.score_model else None,
                       render=not a.no_render, gsap_js=a.gsap, workers=a.workers,
                       say=lambda m: print(f"  {m}", file=sys.stderr))
            print(f"{len(res.edl.clips)} clip(s) -> {a.out}" + (f"; rendered {len(res.videos)}" if res.videos else ""))
            return 0
        edl = None if a.cmd in ("plan",) else load_edl(a.edl)
        if a.cmd == "usage":
            from .provenance import summary
            print(summary(edl)); return 0
        if a.cmd == "validate":
            print(f"ok: {len(edl.clips)} clip(s), {len(edl.transcript)} words"); return 0
        if a.cmd == "plan":
            from .planner import AnthropicLLM, PlannerOptions, plan_edl, load_transcript
            from .planner.source import probe_source
            tr = load_transcript(a.transcript, a.language)
            src_path = Path(a.media_root) / a.source
            if src_path.is_file():
                source = probe_source(src_path, uri=a.source)
            elif a.duration:
                from .planner import SourceInfo
                source = SourceInfo(uri=a.source, duration=a.duration)
            else:
                raise ValueError(f"{src_path} not found; pass --duration to plan from the transcript alone")
            if a.answers:
                from .planner import ReplayLLM
                llm, score_llm = ReplayLLM(a.answers), None
            else:
                llm = AnthropicLLM(a.model)
                score_llm = AnthropicLLM(a.score_model) if a.score_model else None
            opts = PlannerOptions(job_id=a.job_id, min_clip=a.min_clip, max_clip=a.max_clip, target_clips=a.clips,
                                  spread=a.spread,
                                  cutout=a.cutout, speaker_name=a.speaker_name, speaker_title=a.speaker_title,
                                  on_progress=lambda m: print(f"  {m}", file=sys.stderr))
            try:
                plan = plan_edl(tr, source, llm, opts, score_llm=score_llm)
            except Exception:
                if a.answers and getattr(llm, "pending", None):
                    print(f"{len(llm.pending)} prompt(s) need an answer: see {a.answers}/pending/", file=sys.stderr)
                raise
            Path(a.out).write_text(plan.edl.model_dump_json(indent=2, exclude_none=True), encoding="utf-8")
            if a.report:
                Path(a.report).write_text(json.dumps(plan.report, ensure_ascii=False, indent=2), encoding="utf-8")
            from .provenance import summary
            print(f"planned {len(plan.edl.clips)} clip(s) -> {a.out}")
            print(summary(plan.edl))
            for w in plan.report["warnings"]:
                print(f"  warning: {w}", file=sys.stderr)
            return 0
        if a.cmd == "compile":
            from .compile import compile_clip
            clip = next((x for x in edl.clips if x.id == a.clip), None)
            if clip is None:
                raise EDLError(f"no clip {a.clip!r}")
            res = compile_clip(edl, clip, a.out, media_root=a.media_root, gsap_js=a.gsap,
                               prepare=not a.no_media)
            print(f"wrote {res.project_dir} ({res.duration:.2f}s, scenes: {', '.join(res.scenes) or 'none'})")
            return 0
        if a.cmd == "render":
            from .pipeline import render_clip
            out, _ = render_clip(edl, a.clip, a.out, workdir=a.workdir, media_root=a.media_root,
                                 gsap_js=a.gsap, workers=a.workers, matte_device=a.matte_device,
                                 run_check=not a.no_check)
            print(f"rendered {out}"); return 0
    except (EDLError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr); return 2
    except Exception as e:  # RenderError and friends
        print(f"error: {e}", file=sys.stderr); return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
