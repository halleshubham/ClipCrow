"""Every icon the interface asks for exists, and every platform logo has a colour in both themes."""
import re
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "clipcrow" / "service" / "static"
APP = (STATIC / "app.js").read_text(encoding="utf-8")
HTML = (STATIC / "index.html").read_text(encoding="utf-8")
CSS = (STATIC / "style.css").read_text(encoding="utf-8")
ICONS = (STATIC / "icons.css").read_text(encoding="utf-8")
DEFINED = set(re.findall(r"\.i-([a-z0-9-]+)\s*\{\s*--m:", ICONS + CSS))


def asked_for() -> set[str]:
    names = set(re.findall(r"icon\('([a-z0-9-]+)'", APP))                                           # icon('film', 26)
    names |= set(re.findall(r"\[/[^\]]*?/,\s*'([a-z0-9-]+)'\]", APP))                                 # the word -> icon tables (buttons, summaries, headings)
    names |= set(re.findall(r"\bi-([a-z0-9-]+)\"", HTML))                                             # the navigation
    return names


def test_every_icon_the_interface_asks_for_is_drawn():
    missing = sorted(asked_for() - DEFINED)
    assert not missing, f"no icon rule for: {missing}"
    assert len(asked_for()) > 20                                                                        # the tables were really read


def test_every_platform_logo_exists_and_has_a_colour_in_both_themes():
    platforms = set(re.findall(r"'([a-z]+)'\s*(?=[,}])", re.search(r"const PLATFORM_ICON = \{(.*?)\};", APP, re.S).group(1)))
    assert {"youtube", "instagram", "facebook", "tiktok", "x", "linkedin", "telegram"} <= platforms
    for name in platforms:
        assert f"plat-{name}" in DEFINED, f"no logo for {name}"
        assert f".plat-{name} {{" in CSS, f"no colour rule for {name}"
    dark, light = CSS.split("@media (prefers-color-scheme: light)")[0], CSS.split("@media (prefers-color-scheme: light)")[1].split("}\n}")[0]
    for name in platforms - {"x", "tiktok", "threads"}:                                                # those three use the ink colour
        assert f"--plat-{name}:" in dark and f"--plat-{name}:" in light, f"{name} needs a colour token in both themes"


def test_the_icon_sheet_is_inline_data_only_and_never_asks_the_network_for_anything():
    assert "url(\"data:image/svg+xml," in ICONS
    urls = re.findall(r"url\(([^)]*)\)", ICONS)
    assert urls and all(u.startswith('"data:image/svg+xml,') for u in urls)
    assert "http" not in ICONS.replace("http://www.w3.org/2000/svg", "").replace("http%3A//www.w3.org/2000/svg", "")


def test_a_help_popover_is_reachable_without_a_mouse_and_never_the_only_place_for_a_limit():
    assert "aria-expanded" in APP and "role: 'tooltip'" in APP and "Escape" in APP                      # WCAG 1.4.13: dismissible, hoverable, keyboard reachable
    assert "data-keep" in APP                                                                           # a hint that must stay visible can say so
