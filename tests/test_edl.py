import pytest
from pydantic import ValidationError

from reelcore.edl import EDL, json_schema
from tests.conftest import base_edl


def mk(**over):
    return EDL.model_validate(base_edl(**over))


def test_minimal_edl_is_valid():
    assert mk().clips[0].duration == 10


@pytest.mark.parametrize("bad", ["red", "#fff", "#GGGGGG", "#ffd54a; background:url(x)"])
def test_colors_must_be_six_digit_hex(bad):
    clip = {"id": "c1", "source_in": 0, "source_out": 5, "captions": {"color": bad}}
    with pytest.raises(ValidationError):
        mk(clips=[clip])


def test_ids_and_font_names_cannot_carry_markup():
    with pytest.raises(ValidationError):
        mk(clips=[{"id": 'a"><script>', "source_in": 0, "source_out": 5}])
    with pytest.raises(ValidationError):
        mk(clips=[{"id": "c", "source_in": 0, "source_out": 5, "captions": {"font_family": 'X"; }'}}])


def test_clip_must_end_after_it_starts_and_inside_the_source():
    with pytest.raises(ValidationError):
        mk(clips=[{"id": "c", "source_in": 5, "source_out": 5}])
    with pytest.raises(ValidationError):
        mk(clips=[{"id": "c", "source_in": 0, "source_out": 25}])


def test_a_clip_longer_than_three_minutes_is_not_a_short():
    with pytest.raises(ValidationError):
        mk(source={"uri": "s", "duration": 900, "width": 1, "height": 1},
           clips=[{"id": "c", "source_in": 0, "source_out": 400}])


def test_duplicate_ids_are_rejected():
    with pytest.raises(ValidationError):
        mk(clips=[{"id": "c", "source_in": 0, "source_out": 5}, {"id": "c", "source_in": 5, "source_out": 9}])
    ov = {"kind": "stat_pill", "id": "o", "t_in": 1, "text": "x"}
    with pytest.raises(ValidationError):
        mk(clips=[{"id": "c", "source_in": 0, "source_out": 5, "overlays": [ov, ov]}])


def test_overlay_needs_an_anchor():
    with pytest.raises(ValidationError):
        mk(clips=[{"id": "c", "source_in": 0, "source_out": 5,
                   "overlays": [{"kind": "stat_pill", "id": "o", "text": "x"}]}])


def test_text_behind_needs_cutout_layout():
    ov = {"kind": "text_behind", "id": "b", "t_in": 1, "text": "x"}
    with pytest.raises(ValidationError):
        mk(clips=[{"id": "c", "source_in": 0, "source_out": 5, "overlays": [ov]}])
    mk(clips=[{"id": "c", "source_in": 0, "source_out": 5, "overlays": [ov],
               "layout": {"mode": "cutout_behind"}}])


def test_unknown_fields_are_rejected():
    with pytest.raises(ValidationError):
        mk(surprise=1)


def test_json_schema_exports():
    assert "Clip" in json_schema()["$defs"]
