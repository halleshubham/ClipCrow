"""The same file twice: the second job starts from the first one's transcript."""
import json

import pytest

from clipcrow import seats
from clipcrow.planner.transcript import parse_transcript
from clipcrow.service.asr_cache import ASR_FILE, CachedTranscriber, file_sha256, find_reusable
from clipcrow.service.jobs import Job, JobStore
from clipcrow.service.workers import ResultIn
from tests.test_service import video_bytes  # noqa: F401  (the pytest fixture)


def job(store, jid, *, owner=None, sha="abc", language=None, fresh=False):
    j = Job(id=jid, name="a.mp4", created_at=f"2026-11-1{jid[0]}T00:00:00.000+00:00", owner=owner, source_sha=sha)
    j.params.fresh_transcript = fresh
    store.dir(jid).mkdir(parents=True, exist_ok=True)
    store.save(j)
    return j


class Inner:
    def __init__(self, engine="whisper"):
        self.calls, self.engine = 0, engine

    def __call__(self, media, language=None, progress=None, check=None):
        self.calls += 1
        tr = parse_transcript({"language": "mr", "words": [{"w": "नमस्कार", "s": 0.0, "e": 0.5}, {"w": "मित्रांनो", "s": 0.5, "e": 1.0}]})
        tr.engine = self.engine
        return tr


def run(store, j, inner, language=None, said=None):
    out = store.dir(j.id) / "out"
    return CachedTranscriber(inner, store, j, out, fresh=j.params.fresh_transcript)("v.mp4", language, progress=(said.append if said is not None else None))


def test_the_second_job_with_the_same_file_does_not_transcribe_again(tmp_path):
    store = JobStore(tmp_path)
    a, b = job(store, "1" * 12), job(store, "2" * 12)
    inner, said = Inner("sravaani"), []
    first = run(store, a, inner)
    second = run(store, b, inner, said=said)
    assert inner.calls == 1 and [w.w for w in second.words] == [w.w for w in first.words] and second.engine == "sravaani" and second.language == "mr"
    assert any("transcribed before" in m and "sravaani" in m for m in said)
    saved = json.loads((store.dir(b.id) / "out" / ASR_FILE).read_text(encoding="utf-8"))
    assert saved["reused_from"] == a.id and saved["sha"] == "abc"
    c = job(store, "3" * 12)                                                         # a third job reuses it too, and an old one is never touched
    run(store, c, inner)
    assert inner.calls == 1


@pytest.mark.parametrize("what", ["language", "owner", "bytes", "fresh", "damaged"])
def test_it_listens_again_when_the_file_the_language_the_owner_or_the_choice_differs(tmp_path, what):
    store = JobStore(tmp_path)
    a = job(store, "1" * 12, owner="u1")
    inner = Inner()
    run(store, a, inner)
    b = job(store, "2" * 12, owner="u2" if what == "owner" else "u1", sha="other" if what == "bytes" else "abc", fresh=what == "fresh")
    if what == "damaged":
        (store.dir(a.id) / "out" / ASR_FILE).write_text("{not json", encoding="utf-8")
    run(store, b, inner, language="hi" if what == "language" else None)
    assert inner.calls == 2


def test_a_job_that_failed_before_transcribing_leaves_nothing_to_reuse_and_the_engine_is_always_recorded(tmp_path):
    store = JobStore(tmp_path)
    a, b = job(store, "1" * 12), job(store, "2" * 12)
    assert find_reusable(store, b, "abc", None) is None
    run(store, a, Inner(engine=""))
    meta = json.loads((store.dir(a.id) / "out" / ASR_FILE).read_text(encoding="utf-8"))
    assert meta["engine"] in ("whisper", "sravaani") and meta["words"]


def test_a_worker_may_say_which_recogniser_it_used():
    assert ResultIn(worker="home-gpu", engine="sravaani").engine == "sravaani"
    with pytest.raises(ValueError):
        ResultIn(worker="home-gpu", engine="Robert'); DROP TABLE")


def test_the_runner_fingerprints_the_upload(tmp_path, video_bytes):
    from tests.test_service import FakePipeline, client, upload, wait
    c, app = client(tmp_path, FakePipeline())
    done = wait(c, upload(c, video_bytes).json()["id"])
    saved = json.loads((tmp_path / "data" / "jobs" / done["id"] / "job.json").read_text(encoding="utf-8"))
    src = next((tmp_path / "data" / "jobs" / done["id"]).glob("source.*"))
    assert saved["source_sha"] == file_sha256(src)


# --- one person or several -------------------------------------------------------------------------------------

def test_a_video_is_one_person_unless_two_faces_are_seen_together_often_enough():
    assert seats.is_one_person([1] * 16) and seats.is_one_person([1, 0, 1, 1, 0, 1, 1, 1, 1, 0, 1, 1, 1, 1, 1, 1])
    assert seats.is_one_person([1] * 13 + [2] * 3)                            # a guest walking through for three frames
    assert not seats.is_one_person([2] * 8 + [1] * 8) and not seats.is_one_person([4] * 16)
    assert not seats.is_one_person([])                                         # nothing seen: do not decide


def test_one_or_two_people_skip_the_people_search_three_or_more_do_not():
    assert seats.is_small_group([2] * 16) and seats.is_small_group([1] * 8 + [2] * 8) and seats.is_small_group([2] * 13 + [3] * 3)
    assert not seats.is_small_group([3] * 8 + [2] * 8) and not seats.is_small_group([4] * 16)
    assert not seats.is_small_group([])                                        # nothing seen: do not decide
