"""Guards for docs/ui.md: a new kind of control, or a literal colour, cannot ship without someone looking at it."""
import re

from clipcrow.service.api import STATIC

# Input types the stylesheet has been checked against (see docs/ui.md). Add one only after looking at it in dark AND light next to a text field.
STYLED_INPUT_TYPES = {"text", "password", "number", "checkbox", "file", "range", "color", "datetime-local", "date", "hidden"}


def read(name):
    return (STATIC / name).read_text(encoding="utf-8")


def test_every_input_type_the_ui_uses_has_been_styled():
    used = set(re.findall(r"el\('input',\s*\{[^}]*?type:\s*'([a-z-]+)'", read("app.js")))
    used |= set(re.findall(r"<input[^>]*type=\"([a-z-]+)\"", read("dashboard.html")))
    unknown = used - STYLED_INPUT_TYPES
    assert not unknown, (f"new input type(s) {sorted(unknown)}: style them in style.css so they look like the text field (docs/ui.md), "
                         f"then add them to STYLED_INPUT_TYPES")
    assert "datetime-local" in used                                   # the guard really sees the inputs


def test_text_like_inputs_are_styled_by_exclusion_so_a_new_type_is_never_left_native():
    css = read("style.css")
    assert ':is(input:not([type="checkbox"]' in css and "select)" in css
    assert 'input[type="datetime-local"]' in css and "::-webkit-calendar-picker-indicator" in css


def test_no_literal_colours_in_the_script_or_below_the_tokens_in_the_stylesheet():
    assert not re.findall(r"#[0-9a-fA-F]{6}\b|#[0-9a-fA-F]{3}\b", read("app.js")), "use a CSS token, not a colour literal"
    css = read("style.css")
    body = re.sub(r":root\s*\{[^}]*\}", "", css)                      # the token definitions (both themes) are the one place for literals
    body = re.sub(r"url\(\"data:[^\"]*\"\)", "", body)               # inline SVG icons carry their own colours
    found = re.findall(r"#[0-9a-fA-F]{6}\b|#[0-9a-fA-F]{3}\b", body)
    assert not found, f"literal colours outside the tokens: {found[:5]}: define a token for both themes"


def test_the_script_parses():
    """A syntax error in app.js blanks the whole UI and no Python test would notice."""
    import shutil
    import subprocess
    import pytest
    node = shutil.which("node")
    if node is None:
        pytest.skip("needs node")
    r = subprocess.run([node, "--check", str(STATIC / "app.js")], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[:400]


def test_the_options_people_use_every_time_are_not_hidden_behind_more_options():
    """New job: language, clip count, reel length, people, brand toggles, raw-recording clean-up, the GPU choice and the cost are on the page itself."""
    js = read("app.js")
    start = js.index("async function newJobView()")
    body = js[start:]
    more = body.index("el('details', { class: 'adv' }")
    for needle in ("id: 'language'", "id: 'clips'", "id: 'min_clip'", "id: 'max_clip'", "segmented('multi'", "chip('logo'", "chip('cover'", "chip('show_banner'", "chip('outro'", "chip('clean'"):
        assert body.index(needle) < more, f"{needle} must sit above 'More options'"
    after = body[more:]
    assert "id: 'gpu'" in after and "id: 'cost-val'" in after                     # beside Generate clips
    assert body.index("id: 'gpu'") > more and body.index("id: 'cost-val'") > body.index("class: 'actionbar'")


def test_a_select_is_drawn_as_a_control_you_open_not_as_a_text_box():
    css = read("style.css")
    rule = css[css.index("select:not(.plain)"):].split("}", 1)[0]
    assert "background-image" in rule and "--raised" in rule and "cursor: pointer" in rule         # a chevron, a raised face, a hand


def test_a_false_value_is_never_appended_to_the_page_as_text():
    """`el()` skips false in its children, but `node.append(cond && el(...))` writes the word "false" into the header."""
    assert not re.findall(r"\.append\([^)\n]*\b\w+(?:\.\w+)* && el\(", read("app.js")), "use ...(cond ? [el(...)] : []) with append"
