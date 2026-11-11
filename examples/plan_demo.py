"""Planner -> EDL -> MP4 with a SCRIPTED model (no API key): proves the whole chain.
    python examples/gen_assets.py && scripts/fetch_gsap.sh && PYTHONPATH=. python examples/plan_demo.py
"""
import json
import re
import sys
from pathlib import Path

from clipcrow.pipeline import render_clip
from clipcrow.planner import FakeLLM, PlannerOptions, SourceInfo, load_transcript, plan_edl
from clipcrow.planner.models import DetailResult, ScoreResult

ROOT = Path(__file__).resolve().parent.parent
tr = load_transcript(ROOT / "examples" / "demo_transcript.json")


def respond(purpose, prompt, schema):
    if purpose == "score":
        ids = [int(x) for x in re.findall(r'<window id="(\d+)">', prompt)]
        return ScoreResult(windows=[dict(id=i, hook=8, completeness=8, standalone=8, emotion=7, why="") for i in ids])
    sents = re.findall(r"\[S(\d+) \| ([\d.]+)-([\d.]+)s\] (.*)", prompt)
    first, last = int(sents[0][0]), int(sents[-1][0])
    word = sents[0][3].split()[1].strip("।")
    return DetailResult(clips=[dict(
        first_sentence=first, last_sentence=last, title="आयुष्य बदलणारे क्षण", virality=8,
        hook_text="हे ऐकल्यावर आयुष्य बदलेल", why="a strong claim", emphasis=[dict(sentence=first, word=word)],
        behind=dict(sentence=first, word=word), stat=dict(sentence=first, text="१०x"))])


plan = plan_edl(tr, SourceInfo("speaker.mp4", 12, 1080, 1920, 30), FakeLLM(respond),
                PlannerOptions(job_id="plan-demo", min_clip=5, max_clip=11, target_clips=1, cutout=True,
                               speaker_name="शुभम हळ्ळे", speaker_title="संस्थापक", workers=1))
out = ROOT / "build" / "plan_demo"
out.mkdir(parents=True, exist_ok=True)
(out / "edl.json").write_text(plan.edl.model_dump_json(indent=2, exclude_none=True), encoding="utf-8")
print(json.dumps(plan.report["clips"], ensure_ascii=False, indent=1))
if "--render" in sys.argv:
    mp4, _ = render_clip(plan.edl, plan.edl.clips[0].id, out / "clip.mp4", workdir=out / "work",
                         media_root=ROOT / "examples" / "assets", gsap_js=ROOT / "vendor" / "gsap.min.js", workers=3)
    print("rendered", mp4)
