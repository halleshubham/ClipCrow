import json
import re

import pytest

from clipcrow import textfix
from clipcrow.make import make
from clipcrow.planner import FakeLLM, PlannerOptions, parse_transcript
from clipcrow.textfix import Fixed, Fixes, acceptable, refine
from tests.test_planner import make_transcript, scripted

cv2 = pytest.importorskip("cv2")


def tr_of(*sentences, language="mr", gap=2.0):
    """One sentence per tuple of words, each word 0.5 s, sentences a pause apart."""
    words, t = [], 0.0
    for sent in sentences:
        for w in sent:
            words.append({"w": w, "s": t, "e": t + 0.5})
            t += 0.5
        t += gap
    return parse_transcript({"language": language, "words": words})


def llm_returning(mapping, calls=None):
    """Answers each numbered sentence with ``mapping[id]`` (or its own text when absent)."""
    def respond(purpose, prompt, schema):
        if calls is not None:
            calls.append(prompt)
        ids = re.findall(r"^(\d+): (.*)$", prompt, re.M)
        return Fixes(items=[Fixed(id=int(i), text=mapping.get(int(i), t)) for i, t in ids])
    return FakeLLM(respond)


def test_a_corrected_sentence_with_the_same_word_count_keeps_every_word_time():
    tr = tr_of(("मधे", "वचन", "दिला", "हुता"))
    out, n = refine(tr, llm_returning({0: "मध्ये वचन दिलं होतं"}))
    assert n == 1 and [w.w for w in out.words] == ["मध्ये", "वचन", "दिलं", "होतं"]
    assert [(w.s, w.e) for w in out.words] == [(w.s, w.e) for w in tr.words]


def test_a_different_word_count_is_spread_over_the_sentence_span_in_order():
    tr = tr_of(("फाइदली", "तुडवल", "गेला"))
    out, n = refine(tr, llm_returning({0: "फायद्याची तूट भरून निघाली"}))
    assert n == 1 and len(out.words) == 4
    assert out.words[0].s == tr.words[0].s and out.words[-1].e == pytest.approx(tr.words[-1].e)
    assert all(a.e <= b.s + 1e-9 and a.s < a.e for a, b in zip(out.words, out.words[1:]))


def test_unchanged_text_is_not_counted_and_the_same_transcript_comes_back():
    tr = tr_of(("नमस्कार", "मित्रांनो"))
    out, n = refine(tr, llm_returning({}))
    assert n == 0 and out is tr


@pytest.mark.parametrize("bad", [
    "hello this is english now",           # lost the script
    "",                                      # nothing
    "नमस्कार\nमित्रांनो",                      # a newline
    "अ" * 300,                              # far longer than the sentence
    "न",                                     # far shorter
])
def test_an_answer_that_is_not_the_same_sentence_is_ignored(bad):
    tr = tr_of(("नमस्कार", "मित्रांनो", "आज", "आपण"))
    out, n = refine(tr, llm_returning({0: bad}))
    assert n == 0 and [w.w for w in out.words] == ["नमस्कार", "मित्रांनो", "आज", "आपण"]


def test_acceptable_allows_english_words_inside_a_marathi_sentence():
    assert acceptable("हा GPS बंद आहे", "हा GPS बंद आहे का")
    assert not acceptable("हा बँक बंद आहे", "this bank is closed")


def test_ids_outside_the_batch_and_repeated_ids_are_ignored():
    tr = tr_of(("नहीं", "आहे"), ("दुसरे", "वाक्य"))
    llm = FakeLLM(lambda p, prompt, schema: Fixes(items=[Fixed(id=99, text="चुकीचे वाक्य येथे"), Fixed(id=0, text="नाही आहे"),
                                                         Fixed(id=0, text="दुसरेच काही वाक्य")]))
    out, n = refine(tr, llm)
    assert n == 1 and [w.w for w in out.words][:2] == ["नाही", "आहे"]


def test_a_failing_call_keeps_the_original_words_and_says_so():
    tr = tr_of(("नहीं", "आहे"))
    said = []

    def boom(purpose, prompt, schema):
        raise RuntimeError("rate limited")
    out, n = refine(tr, FakeLLM(boom), say=said.append)
    assert n == 0 and out is tr and said and "skipped" in said[0] and "rate limited" not in said[0]


def test_sentences_go_in_batches_and_other_languages_are_left_alone():
    many = tr_of(*[(f"शब्द{i}", "आहे") for i in range(60)])
    calls = []
    refine(many, llm_returning({}, calls))
    assert len(calls) == 3 and calls[0].splitlines()[0].startswith("0: ")
    en = tr_of(("hello", "world"), language="en")
    calls.clear()
    out, n = refine(en, llm_returning({}, calls))
    assert n == 0 and out is en and not calls


def test_the_prompt_names_the_language_and_does_not_ask_for_translation():
    seen = {}

    def respond(purpose, prompt, schema):
        return Fixes(items=[])
    llm = FakeLLM(respond)
    orig = llm.generate
    llm.generate = lambda system, user, schema, purpose="": (seen.update(system=system, purpose=purpose), orig(system, user, schema, purpose=purpose))[1]
    refine(tr_of(("नहीं", "आहे")), llm)
    assert "Marathi" in seen["system"] and "translate" in seen["system"] and seen["purpose"] == "text-refine"


# --- through make() ----------------------------------------------------------------------------------------------------------------

from clipcrow.make import Hooks          # noqa: E402
from tests.test_make import video        # noqa: E402,F401  (the pytest fixture)


def _make_with(video, out, tr, llm, **kw):
    return make(video, out, llm=llm, opts=PlannerOptions(target_clips=1, workers=1),
                transcribe_fn=lambda media, language=None, progress=None: tr, render=False, **kw)


def _corrector(fail=False):
    base = scripted()

    def respond(purpose, prompt, schema):
        if purpose == "text-refine":
            if fail:
                raise RuntimeError("down")
            ids = re.findall(r"^(\d+): (.*)$", prompt, re.M)
            return Fixes(items=[Fixed(id=int(i), text=t.replace("आयुष्य", "जीवन")) for i, t in ids])
        return base.respond(purpose, prompt, schema)
    return FakeLLM(respond)


def test_make_corrects_marathi_keeps_the_original_and_reports_it(tmp_path, video):
    stages = []
    hk = Hooks(stage=lambda n, s, d="": stages.append((n, s, d)))
    res = _make_with(video, tmp_path / "o", make_transcript(), _corrector(), hooks=hk)
    out = tmp_path / "o"
    fixed = json.loads((out / "transcript.json").read_text(encoding="utf-8"))["words"]
    raw = json.loads((out / "transcript.raw.json").read_text(encoding="utf-8"))["words"]
    assert any("जीवन" in w["w"] for w in fixed) and not any("जीवन" in w["w"] for w in raw)
    assert any("आयुष्य" in w["w"] for w in raw) and res.edl.clips
    done = [d for n, st, d in stages if n == "transcribe" and st == "done"]
    assert len(done) == 1 and "corrected" in done[0]
    assert [(n, st) for n, st, _ in stages if n == "transcribe"] == [("transcribe", "running"), ("transcribe", "done")]


def test_make_without_correction_or_with_a_failing_model_still_produces_clips(tmp_path, video):
    res = _make_with(video, tmp_path / "a", make_transcript(), scripted(), refine_text=False)
    assert res.edl.clips and not (tmp_path / "a" / "transcript.raw.json").exists()
    res = _make_with(video, tmp_path / "b", make_transcript(), _corrector(fail=True))
    assert res.edl.clips and not (tmp_path / "b" / "transcript.raw.json").exists()


def test_a_given_transcript_is_never_sent_for_correction(tmp_path, video):
    tr = make_transcript()
    t = tmp_path / "t.json"
    t.write_text(json.dumps({"language": "mr", "words": [w.model_dump(exclude_none=True) for w in tr.words]}, ensure_ascii=False),
                 encoding="utf-8")
    llm = scripted()
    make(video, tmp_path / "o2", transcript_path=t, llm=llm, opts=PlannerOptions(target_clips=1, workers=1), render=False)
    assert all(p != "text-refine" for p, _ in llm.calls)
