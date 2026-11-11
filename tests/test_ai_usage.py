import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from clipcrow.costs import estimate_cost, estimate_total
from clipcrow.edl import EDL, Asset, CallUsage
from clipcrow.planner import FakeLLM, PlannerOptions, SourceInfo, plan_edl
from clipcrow.provenance import provenance, summary, write_sidecar
from tests.conftest import base_edl
from tests.test_planner import SRC, make_transcript, scripted


# --- asset provenance is honest by construction ------------------------------------

@pytest.mark.parametrize("origin,ai", [("ai_image", "none"), ("ai_video", "processed"), ("tts", "none"),
                                       ("matte", "none"), ("matte", "generated"), ("upload", "generated")])
def test_an_asset_cannot_understate_or_overstate_its_ai_involvement(origin, ai):
    with pytest.raises(ValidationError):
        Asset(id="a", kind="video", origin=origin, ai=ai)


def test_valid_asset_pairs():
    assert Asset(id="a", kind="image", origin="ai_image", ai="generated").ai == "generated"
    assert Asset(id="m", kind="video", origin="matte", ai="processed").origin == "matte"
    assert Asset(id="u", kind="video", origin="upload").ai == "none"


def test_asset_ids_must_be_unique():
    d = base_edl(assets=[{"id": "a", "kind": "video", "origin": "upload"}, {"id": "a", "kind": "video", "origin": "upload"}])
    with pytest.raises(ValidationError):
        EDL.model_validate(d)


# --- cost ---------------------------------------------------------------------------

def test_cost_estimate_uses_list_prices():
    assert estimate_cost("claude-opus-5-5", 1_000_000, 1_000_000) == pytest.approx(24.0)
    assert estimate_cost("claude-haiku-5-5", 2_000_000, 0) == pytest.approx(0.2)
    assert estimate_cost("some-other-model", 10, 10) is None


def test_a_total_is_unknown_if_any_model_is_unpriced():
    priced = CallUsage(purpose="a", model="claude-opus-5-5", input_tokens=1000, output_tokens=100)
    assert estimate_total([priced]) == pytest.approx(0.006)
    assert estimate_total([priced, CallUsage(purpose="b", model="mystery")]) is None


# --- the LLM client records every billed call ----------------------------------------

def test_every_call_is_recorded_even_a_refusal():
    from clipcrow.planner.llm import AnthropicLLM, LLMError
    from clipcrow.planner.models import ScoreResult

    class Stub:
        messages = None
        def __init__(self): self.messages = self; self.n = 0
        def parse(self, **kw):
            self.n += 1
            return SimpleNamespace(stop_reason="refusal" if self.n == 2 else "end_turn",
                                   parsed_output=ScoreResult(windows=[]),
                                   usage=SimpleNamespace(input_tokens=100 * self.n, output_tokens=10))

    llm = AnthropicLLM("claude-sonnet-5-5", client=Stub())
    llm.generate("s", "u", ScoreResult, purpose="score")
    with pytest.raises(LLMError):
        llm.generate("s", "u", ScoreResult, purpose="detail:1")
    calls = llm.usage()
    assert [(c.purpose, c.model, c.input_tokens) for c in calls] == [
        ("score", "claude-sonnet-5-5", 100), ("detail:1", "claude-sonnet-5-5", 200)]
    assert (llm.input_tokens, llm.output_tokens) == (300, 20)


# --- the plan carries the record -----------------------------------------------------

def test_plan_records_usage_assets_and_what_the_ai_decided():
    llm = scripted()
    plan = plan_edl(make_transcript(), SRC, llm, PlannerOptions(target_clips=1, cutout=True, workers=1))
    edl = plan.edl
    assert edl.ai and len(edl.ai.calls) == len(llm.calls) and edl.ai.models == ["fake"]
    assert edl.ai.est_cost_usd is None                                 # "fake" has no price
    assert {a.id: a.ai for a in edl.assets} == {"source": "none", "cutout": "processed"}
    ai = edl.clips[0].ai
    assert ai and {"cut points", "title", "hook text"} <= set(ai.decided) and ai.score is not None


def test_no_cutout_means_no_matte_asset():
    plan = plan_edl(make_transcript(), SRC, scripted(), PlannerOptions(target_clips=1, cutout=False, workers=1))
    assert [a.id for a in plan.edl.assets] == ["source"]


def test_a_separate_scoring_model_is_counted_once_each():
    score, detail = scripted(), scripted()
    plan = plan_edl(make_transcript(), SRC, detail, PlannerOptions(target_clips=1, workers=1), score_llm=score)
    purposes = [c.purpose for c in plan.edl.ai.calls]
    assert purposes.count("score") == len([1 for p, _ in score.calls if p == "score"]) >= 1
    assert len(purposes) == len(score.calls) + len(detail.calls)


# --- provenance + disclosure ----------------------------------------------------------

def test_no_disclosure_recommended_for_editorial_ai_only():
    plan = plan_edl(make_transcript(), SRC, scripted(), PlannerOptions(target_clips=1, cutout=True, workers=1))
    p = provenance(plan.edl, plan.edl.clips[0])
    assert p["disclosure_recommended"] is False
    assert [a["id"] for a in p["ai_assets"]["processed"]] == ["cutout"] and p["ai_assets"]["generated"] == []


def test_disclosure_turns_on_when_an_asset_is_generated():
    d = base_edl(assets=[{"id": "img", "kind": "image", "origin": "ai_image", "ai": "generated",
                          "tool": "x", "model": "y", "prompt": "a news desk"}])
    edl = EDL.model_validate(d)
    assert provenance(edl, edl.clips[0])["disclosure_recommended"] is True
    assert "platform AI disclosure recommended: yes" in summary(edl)


def test_sidecar_is_written_next_to_the_video(tmp_path):
    plan = plan_edl(make_transcript(), SRC, scripted(), PlannerOptions(target_clips=1, workers=1))
    video = tmp_path / "c1.mp4"
    path = write_sidecar(plan.edl, plan.edl.clips[0], video)
    assert path.name == "c1.mp4.ai.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["clip"] == "c1" and "hook text" in data["ai_decided"]


def test_usage_command_prints_the_record(tmp_path, capsys):
    from clipcrow.cli import main
    plan = plan_edl(make_transcript(), SRC, scripted(), PlannerOptions(target_clips=1, cutout=True, workers=1))
    f = tmp_path / "edl.json"
    f.write_text(plan.edl.model_dump_json(exclude_none=True), encoding="utf-8")
    assert main(["usage", str(f)]) == 0
    out = capsys.readouterr().out
    assert "planner:" in out and "AI-processed: cutout" in out and "AI decided" in out
