import json

import pytest

from clipcrow.make import make
from clipcrow.planner import FakeLLM, PlannerOptions
from tests.test_planner import make_transcript, scripted

cv2 = pytest.importorskip("cv2")


@pytest.fixture
def video(tmp_path):
    """A short vertical dummy video: the framing stage returns 'cover' for it, no face detection needed."""
    import subprocess
    p = tmp_path / "media" / "talk.mp4"
    p.parent.mkdir()
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=gray:s=90x160:r=30:d=250",
                    "-f", "lavfi", "-i", "sine=f=220:d=250", "-shortest", "-c:v", "libx264", "-g", "30",
                    "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", str(p)], check=True)
    return p


def fake_transcribe(tr):
    return lambda media, language=None, progress=None: tr


def test_make_runs_every_stage_and_writes_the_artifacts(video, tmp_path):
    tr = make_transcript()
    rendered = []

    def fake_render(edl, clip_id, out, **kw):
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"mp4")
        rendered.append((clip_id, kw["media_root"]))
        return out, None

    res = make(video, tmp_path / "out", llm=scripted(), opts=PlannerOptions(target_clips=2, workers=1),
               transcribe_fn=fake_transcribe(tr), render_fn=fake_render)
    out = tmp_path / "out"
    assert (out / "transcript.json").is_file() and (out / "edl.json").is_file() and (out / "plan-report.json").is_file()
    assert [c for c, _ in rendered] == [c.id for c in res.edl.clips] and len(res.videos) == len(res.edl.clips)
    assert all(root == video.parent for _, root in rendered)
    assert all(c.layout.framing == "cover" for c in res.edl.clips)        # vertical source: nothing to track
    assert res.edl.ai and res.edl.ai.calls                                  # the AI-usage record survives to disk
    assert json.loads((out / "edl.json").read_text(encoding="utf-8"))["ai"]["calls"]


def test_a_given_transcript_skips_recognition(video, tmp_path):
    tr = make_transcript()
    t = tmp_path / "t.json"
    t.write_text(json.dumps({"language": "mr", "words": [w.model_dump(exclude_none=True) for w in tr.words]},
                            ensure_ascii=False), encoding="utf-8")

    def boom(*a, **k):
        raise AssertionError("must not transcribe")

    res = make(video, tmp_path / "o", transcript_path=t, llm=scripted(), opts=PlannerOptions(target_clips=1, workers=1),
               transcribe_fn=boom, render=False)
    assert res.edl.clips and not res.videos


def test_a_given_edl_skips_both_recognition_and_planning(video, tmp_path):
    first = make(video, tmp_path / "a", llm=scripted(), opts=PlannerOptions(target_clips=1, workers=1),
                 transcribe_fn=fake_transcribe(make_transcript()), render=False)

    def boom(*a, **k):
        raise AssertionError("must not run")

    res = make(video, tmp_path / "b", edl_path=first.edl_path, transcribe_fn=boom, llm=FakeLLM(boom), render=False)
    assert [c.id for c in res.edl.clips] == [c.id for c in first.edl.clips]
