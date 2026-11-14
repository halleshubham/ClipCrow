"""Caption motion presets: a closed set; the default is the look every earlier clip had."""
import pytest

from clipcrow.compile import _caption_script
from clipcrow.edl import CaptionStyle, EDL
from clipcrow.service.editor import CaptionsEdit, ClipEdit, apply_edit, clip_view
from tests.conftest import base_edl

TL = [["g0", 0.0, 2.0, [0.0, 0.5]], ["g1", 2.0, 4.0, [2.0]]]
ORIGINAL_POP = '''    const G = [["g0", 0.0, 2.0, [0.0, 0.5]], ["g1", 2.0, 4.0, [2.0]]];
    G.forEach(([id, s, e, times]) => {
      tl.fromTo("#" + id, { opacity: 0, y: 24 }, { opacity: 1, y: 0, duration: 0.18, ease: "power2.out" }, s);
      times.forEach((t, i) => tl.fromTo("#" + id + "w" + i, { scale: 0.7, opacity: 0.3 },
        { scale: 1, opacity: 1, duration: 0.16, ease: "back.out(2)" }, Math.max(t, s)));
      tl.to("#" + id, { opacity: 0, duration: 0.12, ease: "power2.in" }, Math.max(s, e - 0.12));
      tl.set("#" + id, { opacity: 0, visibility: "hidden" }, e);
    });'''


def test_the_default_is_pop_and_its_script_is_exactly_the_original():
    assert CaptionStyle().animation == "pop"
    assert _caption_script("pop", TL, "#FFFFFF", "#FFD54A") == ORIGINAL_POP


@pytest.mark.parametrize("name,expect,absent", [
    ("fade", ['opacity: 1, duration: 0.2'], ["scale", "color:"]),
    ("slide", ["y: 70"], ["scale", "color:"]),
    ("karaoke", ['HOT = "#FFD54A"', 'BASE = "#FFFFFF"', "color: HOT", "? HOT : BASE"], []),
])
def test_each_preset_has_its_own_motion_and_always_hides_the_group_at_its_end(name, expect, absent):
    js = _caption_script(name, TL, "#FFFFFF", "#FFD54A")
    assert all(x in js for x in expect) and not any(x in js for x in absent)
    assert 'tl.set("#" + id, { opacity: 0, visibility: "hidden" }, e);' in js and "{{" not in js and "}}" not in js
    assert js.count("tl.fromTo") >= 1 and js.startswith("    const G = ")


def test_karaoke_returns_an_emphasised_word_to_its_highlight_colour():
    js = _caption_script("karaoke", TL, "#FFFFFF", "#FFD54A", [[False, True], [False]])
    assert "EMPH = [[false, true], [false]]" in js and "color: EMPH[gi][i] ? HOT : BASE" in js


def test_an_unknown_preset_is_refused_by_the_schema_and_the_editor():
    with pytest.raises(ValueError):
        CaptionStyle(animation="explode")
    with pytest.raises(ValueError):
        CaptionsEdit(animation="explode")


def test_the_editor_changes_the_motion_and_the_view_reports_it():
    edl = EDL.model_validate(base_edl(source={"uri": "s.mp4", "duration": 60, "width": 1080, "height": 1920, "fps": 30}, clips=[{"id": "c1", "source_in": 0, "source_out": 20}]))
    assert clip_view(edl, "c1", source_duration=60)["captions"]["animation"] == "pop"
    new, _ = apply_edit(edl, "c1", ClipEdit(captions=CaptionsEdit(animation="karaoke")), source_duration=60)
    assert new.clips[0].captions.animation == "karaoke" and clip_view(new, "c1", source_duration=60)["captions"]["animation"] == "karaoke"
    same, _ = apply_edit(edl, "c1", ClipEdit(captions=CaptionsEdit(animation="pop")), source_duration=60)
    assert same is edl
