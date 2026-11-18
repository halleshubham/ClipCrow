"""The font catalogue: every Devanagari family on Google Fonts and the best-known Latin ones, chosen per script on the Brand page."""
import json
from pathlib import Path

import pytest

from clipcrow import fonts
from clipcrow.branding import BrandKit, BrandingChoice, apply_brand, brand_font
from clipcrow.service.brand import BrandStore, BrandError
from tests.test_branding import CLIP, build  # noqa: F401

OWNER = "a" * 32


def test_the_catalogue_is_consistent():
    names = [c["name"] for c in fonts.CATALOG]
    assert len(names) == len(set(names)) and len(names) >= 100
    assert sum(1 for c in fonts.CATALOG if c["deva"]) >= 50                         # every Devanagari family on Google Fonts that npm carries
    assert {"Noto Serif Devanagari", "Noto Sans Devanagari", "Noto Serif", "Noto Sans", "Hind", "Poppins", "Montserrat", "Inter"} <= set(names)
    for c in fonts.CATALOG:
        assert c["pkg"] and c["ver"] and c["reg"].endswith(".ttf") and (c["bold"] is None or c["bold"].endswith(".ttf")), c["name"]
        assert set(fonts.KNOWN[c["name"]]) == {400, 700}
    assert fonts.KNOWN["Noto Serif Devanagari"][700] == "NotoSerifDevanagari-Bold.ttf"      # the names the first four families always had
    assert fonts.DEFAULT_DEVANAGARI in fonts.DEVANAGARI_FAMILIES


def test_the_catalogue_file_is_in_the_package_data_and_the_installer_reads_it():
    assert "fonts_catalog.json" in Path("pyproject.toml").read_text()
    src = Path("scripts/install_fonts.py").read_text()
    assert "fonts_catalog.json" in src and "-Regular.ttf" in src and "-Bold.ttf" in src
    assert "fonts_catalog.json" in Path("Dockerfile").read_text()                           # the image build copies it next to the installer


def test_installed_lists_only_what_is_on_this_machine(tmp_path, monkeypatch):
    (tmp_path / "Hind-Regular.ttf").write_bytes(b"x")
    monkeypatch.setenv("CLIPCROW_FONT_DIR", str(tmp_path))
    got = {f["name"]: f for f in fonts.installed()}
    assert "Hind" in got and got["Hind"]["deva"] and got["Hind"]["latin"]
    assert "Montserrat" not in got


def test_a_latin_font_is_swapped_for_a_devanagari_one_only_when_the_text_needs_it():
    assert fonts.effective_family("Montserrat", ["Hello"]) == "Montserrat"
    assert fonts.effective_family("Montserrat", ["नमस्कार"]) == fonts.DEFAULT_DEVANAGARI
    assert fonts.effective_family("Hind", ["नमस्कार hello"]) == "Hind"


def test_the_brand_gives_each_language_its_own_font(make_edl, tmp_path):
    kit = BrandKit(captions={"font_family": "Hind", "color": "#00FF00"}, caption_font_latin="Montserrat")
    assert brand_font(kit, "mr") == "Hind" and brand_font(kit, "hi") == "Hind" and brand_font(kit, "en") == "Montserrat"
    assert brand_font(BrandKit(captions={"font_family": "Hind"}), "en") == "Hind"               # no English font chosen: the one font for all
    assert brand_font(BrandKit(), "mr") == ""
    mr = apply_brand(make_edl(clips=[CLIP]), kit, BrandingChoice(captions=True), tmp_path / "a")
    assert mr.clips[0].captions.font_family == "Hind" and mr.clips[0].captions.color == "#00FF00"
    en_edl = make_edl(clips=[CLIP])
    en_edl = en_edl.model_copy(update={"language": en_edl.language.model_copy(update={"primary": "en", "script": "Latn"})})
    en = apply_brand(en_edl, kit, BrandingChoice(captions=True), tmp_path / "b")
    assert en.clips[0].captions.font_family == "Montserrat"
    only_latin = apply_brand(en_edl, BrandKit(caption_font_latin="Lora"), BrandingChoice(captions=True), tmp_path / "c")
    assert only_latin.clips[0].captions.font_family == "Lora"


def test_the_brand_validates_both_fonts(tmp_path):
    st = BrandStore(tmp_path)
    got = st.set_settings(OWNER, {"caption_font": "Yatra One", "caption_font_latin": "Bebas Neue"})
    assert got["caption_font"] == "Yatra One" and st.kit(OWNER).caption_font_latin == "Bebas Neue" and st.kit(OWNER).captions["font_family"] == "Yatra One"
    for bad in ({"caption_font": "Comic Sans"}, {"caption_font_latin": "../../etc/passwd"}):
        with pytest.raises(BrandError):
            st.set_settings(OWNER, bad)
    assert "font_list" in st.describe(OWNER) and st.describe(OWNER)["fonts"].count("Hind") == 1


def test_a_font_missing_on_this_machine_falls_back_with_a_note(make_edl, tmp_path, gsap, monkeypatch):
    from clipcrow.compile import compile_clip
    from tests.test_branding import with_assets, png
    d = tmp_path / "fonts"
    d.mkdir()
    for n in ("NotoSerifDevanagari-Regular.ttf", "NotoSerifDevanagari-Bold.ttf", "NotoSerif-Regular.ttf", "NotoSerif-Bold.ttf"):
        (d / n).write_bytes(b"x")
    monkeypatch.setenv("CLIPCROW_FONT_DIR", str(d))
    clip = {**CLIP, "captions": {"font_family": "Hind"}}
    (tmp_path / "brand").mkdir()
    png(tmp_path / "brand" / "logo.png")
    edl = with_assets(make_edl, clip)
    res = compile_clip(edl, edl.clips[0], tmp_path / "p", media_root=tmp_path, gsap_js=gsap, asset_root=tmp_path, prepare=False)
    assert res.font_family == "Noto Serif Devanagari" and any("Hind" in w for w in res.warnings)


def test_the_font_file_route_serves_only_catalogue_fonts(tmp_path, monkeypatch):
    from tests.test_service import client
    d = tmp_path / "fonts"
    d.mkdir()
    (d / "Hind-Regular.ttf").write_bytes(b"\x00\x01\x00\x00fake")
    monkeypatch.setenv("CLIPCROW_FONT_DIR", str(d))
    c, _ = client(tmp_path / "data")
    assert c.get("/api/fonts/file", params={"name": "Hind"}).status_code == 200
    assert c.get("/api/fonts/file", params={"name": "Montserrat"}).status_code == 404        # in the catalogue, not on this machine
    assert c.get("/api/fonts/file", params={"name": "../../etc/passwd"}).status_code == 404
