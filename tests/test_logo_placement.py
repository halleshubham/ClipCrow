"""The logo keeps off the face: the corner when it is clear, else down the side, else smaller."""
from clipcrow import branding, compile as comp
from clipcrow.branding import fit_logo
from clipcrow.edl import Logo, Layout
from clipcrow.reframe import Box, Sample, face_zone_of_path

W, H = 1080, 1920


def logo(**kw):
    return Logo(**{"asset": "brand-logo", "position": "tr", "size_pct": 18, "opacity": 0.9, "margin_px": 60, **kw})


def test_the_safe_zones_match_the_compiler():
    assert (branding.LOGO_TOP_SAFE, branding.LOGO_BOTTOM_SAFE) == (comp._LOGO_TOP_SAFE, comp._LOGO_BOTTOM_SAFE)


def test_no_zone_or_a_clear_corner_leaves_the_logo_where_the_brand_put_it():
    assert fit_logo(logo(), None, 1240, 1.0).top_px is None
    small_face = (0.3, 0.45, 0.6, 0.6)                                # a face in the middle: the top right is free
    got = fit_logo(logo(), small_face, 1240, 1.0)
    assert got.top_px is None and got.shrink == 1.0 and got.position == "tr"


def test_a_head_filling_the_top_pushes_the_logo_down_the_side_or_makes_it_smaller():
    close_up = (0.1, 0.14, 0.82, 0.57)                                 # the face in the guest's close-up
    got = fit_logo(logo(), close_up, 1240, 1.0)
    assert got.top_px is not None or got.shrink < 1.0
    wpx = W * got.size_pct * got.shrink / 100
    gap = 60 if got.side_px is None else got.side_px
    side_l = gap if got.position[1] == "l" else W - gap - wpx
    top = got.top_px if got.top_px is not None else 310
    box = (side_l, top, side_l + wpx, top + wpx)
    assert got.shrink >= 0.4
    face = (0.1 * W - 0.04 * W, 0.14 * H - 0.08 * H, 0.82 * W + 0.04 * W, 0.57 * H + 0.03 * H)
    overlap_w, overlap_h = min(box[2], face[2]) - max(box[0], face[0]), min(box[3], face[3]) - max(box[1], face[1])
    assert not (overlap_w > 0 and overlap_h > 0)                        # clear of the face
    assert not (box[3] > 1240 - 30 and box[1] < 1240 + 330)             # and of the captions


def test_when_nothing_is_clear_the_smallest_logo_with_the_least_overlap_is_used():
    everything = (0.02, 0.1, 0.98, 0.9)
    got = fit_logo(logo(), everything, 1240, 1.0)
    assert got.shrink == 0.4 and got.top_px is not None


def test_captions_off_do_not_keep_anything_clear():
    zone = (0.1, 0.14, 0.82, 0.57)
    with_caps = fit_logo(logo(position="br"), zone, 1240, 1.0)
    no_caps = fit_logo(logo(position="br"), zone, None, 1.0)
    assert no_caps.top_px is None or no_caps.top_px >= with_caps.top_px or no_caps.shrink >= with_caps.shrink


def test_the_face_zone_is_the_face_as_it_sits_in_the_crop_window():
    crop = (720 * 9 / 16) / 1280                                        # a 1280x720 source: the crop is 405 px wide
    samples = [Sample(float(t), [Box(600, 150, 100, 130)]) for t in range(4)]
    subject = [(float(t), 650 / 1280) for t in range(4)]
    zone = face_zone_of_path(samples, subject, [(0.0, 650 / 1280)], 1280, 720, crop)
    assert zone is not None and 0.3 < zone[0] < 0.5 and 0.5 < zone[2] < 0.7 and abs(zone[1] - 150 / 720) < 0.01
    assert face_zone_of_path(samples, subject, [], 1280, 720, crop) is None


def test_a_layout_rejects_a_zone_outside_the_frame():
    import pytest
    with pytest.raises(Exception):
        Layout(face_zone=(0.5, 0.2, 0.4, 0.6))
    assert Layout(face_zone=(0.1, 0.2, 0.4, 0.6)).face_zone == (0.1, 0.2, 0.4, 0.6)


def test_the_compiler_places_the_fitted_logo():
    css = comp._logo_scene(fit_logo(logo(), (0.1, 0.14, 0.82, 0.57), 1240, 1.0), "assets/brand/logo.png", 0.0, 5.0, 1080, 1920)
    assert "top:310px" in css and "right:24px" in css and "width:107px" in css.replace(" ", "")
    plain = comp._logo_scene(logo(), "assets/brand/logo.png", 0.0, 5.0, 1080, 1920)
    assert "top:310px" in plain and "right:60px" in plain and "width:194px" in plain.replace(" ", "")
