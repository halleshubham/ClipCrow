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


def test_a_head_filling_the_top_keeps_the_logos_size_and_fades_it_when_no_spot_is_clear():
    close_up = (0.1, 0.14, 0.82, 0.57)                                 # the face in the guest's close-up
    got = fit_logo(logo(), close_up, 1240, 1.0)
    assert got.shrink == 1.0                                           # never smaller than the brand made it
    assert got.top_px is not None or got.faded_opacity is not None
    wpx = W * got.size_pct / 100
    gap = 60 if got.side_px is None else got.side_px
    side_l = gap if got.position[1] == "l" else W - gap - wpx
    top = got.top_px if got.top_px is not None else 310
    box = (side_l, top, side_l + wpx, top + wpx)
    face = (0.1 * W - 0.04 * W, 0.14 * H - 0.08 * H, 0.82 * W + 0.04 * W, 0.57 * H + 0.03 * H)
    overlaps = min(box[2], face[2]) - max(box[0], face[0]) > 0 and min(box[3], face[3]) - max(box[1], face[1]) > 0
    assert (not overlaps) or got.faded_opacity is not None             # either clear of the face, or faded where it cannot be


def test_a_logo_that_must_stay_over_a_face_fades_and_keeps_its_size():
    everything = (0.02, 0.1, 0.98, 0.9)
    got = fit_logo(logo(), everything, 1240, 1.0)
    assert got.shrink == 1.0 and got.faded_opacity == branding.FADED and got.top_px is not None
    css = comp._logo_scene(got, "assets/brand/logo.png", 0.0, 5.0, 1080, 1920)
    assert "opacity: 0.3" in css and "width: 194px" in css.replace("  ", " ") or "width:194px" in css.replace(" ", "")
    assert fit_logo(got, None, 1240, 1.0).faded_opacity is None        # framed again onto a clear shot: back to the brand's own


def test_captions_off_do_not_keep_anything_clear():
    zone = (0.1, 0.14, 0.82, 0.57)
    with_caps = fit_logo(logo(position="br"), zone, 1240, 1.0)
    no_caps = fit_logo(logo(position="br"), zone, None, 1.0)
    assert no_caps.top_px is None or no_caps.top_px >= with_caps.top_px or with_caps.faded_opacity is not None


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
    got = fit_logo(logo(), (0.1, 0.14, 0.82, 0.57), 1240, 1.0)
    css = comp._logo_scene(got, "assets/brand/logo.png", 0.0, 5.0, 1080, 1920)
    assert "width:194px" in css.replace(" ", "") and "opacity" in css                  # the brand's size, whatever the spot
    plain = comp._logo_scene(logo(), "assets/brand/logo.png", 0.0, 5.0, 1080, 1920)
    assert "top:310px" in plain and "right:60px" in plain and "width:194px" in plain.replace(" ", "")


def test_a_clip_planned_when_the_logo_still_shrank_is_drawn_full_size_and_faded():
    old = logo(top_px=310, shrink=0.4, side_px=24)
    css = comp._logo_scene(old, "assets/brand/logo.png", 0.0, 5.0, 1080, 1920).replace(" ", "")
    assert "width:194px" in css and "opacity:0.3" in css
