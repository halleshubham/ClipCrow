"""Subtitles must not sit on top of a screenshot: while a picture is up, a caption group it would cover moves to a free band below it (or above it)."""
import pytest

from clipcrow.captions import CaptionGroup, CaptionWord
from clipcrow.compile import CAPTION_GAP, CAPTION_SAFE_BOTTOM, CAPTION_SAFE_TOP, _caption_height, caption_tops, _captions_scene
from clipcrow.edl import Clip

H, TOP = 1920, 1240


def group(start, end, words=("तुम्हाला", "हे", "माहीत", "आहे")):
    return CaptionGroup([CaptionWord(t, start + i * 0.2, start + i * 0.2 + 0.2, i) for i, t in enumerate(words)], start, end)


def test_a_group_that_a_picture_covers_moves_below_it_and_the_others_stay():
    groups = [group(0, 2), group(3, 5), group(6, 8)]
    picture = (2.5, 5.5, 230, 1350)                                   # a band picture shown while the second group is spoken
    tops = caption_tops(groups, [picture], TOP, H)
    assert set(tops) == {1}                                           # only the group at the same time as the picture
    assert tops[1] == 1350 + CAPTION_GAP
    assert tops[1] + _caption_height(sum(len(w.text) for w in groups[1].words) + 3) <= H - CAPTION_SAFE_BOTTOM


def test_a_picture_that_does_not_reach_the_caption_band_moves_nothing():
    assert caption_tops([group(0, 2)], [(0, 3, 200, 1100)], TOP, H) == {}                 # the usual panel zone ends well above the captions
    assert caption_tops([group(0, 2)], [(5, 8, 230, 1350)], TOP, H) == {}                 # a picture at another time


def test_with_no_room_below_the_words_go_above_the_picture_but_not_over_the_face():
    pic = (0, 3, 700, 1700)                                           # leaves no room below
    g = group(0, 2)
    tops = caption_tops([g], [pic], TOP, H)
    ch = _caption_height(sum(len(w.text) for w in g.words) + len(g.words) - 1)
    assert tops[0] + ch + CAPTION_GAP <= 700 and tops[0] >= CAPTION_SAFE_TOP and tops[0] + ch + CAPTION_GAP > 690          # just above the picture
    assert caption_tops([group(0, 2)], [pic], TOP, H, face_band=(300, 650)) == {}        # the face is up there: stay put rather than cover it


def test_a_full_frame_cutaway_has_no_free_band_so_nothing_moves():
    assert caption_tops([group(0, 2)], [(0, 3, 0, H)], TOP, H) == {}


def test_the_moved_group_carries_its_own_top_in_the_page_and_the_others_do_not(tmp_path):
    clip = Clip.model_validate({"id": "c1", "title": "t", "source_in": 0, "source_out": 10})
    html = _captions_scene(clip, [group(0, 2), group(3, 5)], '"Noto Serif Devanagari"', "", 1080, 1920, {1: 1374})
    assert "top:1374px" in html and html.count("top:1374px") == 1


def test_the_same_inputs_always_give_the_same_places():
    g = [group(i * 2, i * 2 + 1.8) for i in range(5)]
    p = [(1.0, 6.0, 230, 1350)]
    assert caption_tops(g, p, TOP, H) == caption_tops(g, p, TOP, H)
