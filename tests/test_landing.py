"""The landing page is served by the app at /landing/, needs no script and makes no third-party request."""
import re

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from clipcrow.service.api import STATIC, create_app
from tests.test_service import FakePipeline


def test_the_landing_page_is_public_self_contained_and_links_to_the_app(tmp_path):
    from clipcrow.service.accounts import Accounts
    c = TestClient(create_app(tmp_path, FakePipeline(), accounts=Accounts(tmp_path / "x.db")))
    r = c.get("/landing/")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"] and "one long video" in r.text.lower()      # no sign-in needed
    html = r.text
    assert "<script" not in html.replace('<script src="/landing/landing.js" defer></script>', "") and 'style="' not in html                                                              # the CSP allows neither
    assert not re.findall(r'(?:src|href)="https?://', html)                                                             # nothing from another site
    assert 'href="/#/"' in html and "/#/privacy" in html and "/#/terms" in html and "support@shackyapps.in" in html
    for path in re.findall(r'(?:src|href)="(/[^"#]+)"', html):                                                          # every local file it names exists
        if not path.startswith("/#"):
            assert c.get(path.split("?")[0]).status_code == 200, path
    assert "Content-Security-Policy" in r.headers and "script-src 'self'" in r.headers["Content-Security-Policy"]


def test_the_landing_css_respects_reduced_motion_and_both_themes():
    css = (STATIC / "landing" / "landing.css").read_text(encoding="utf-8")
    assert "prefers-reduced-motion: reduce" in css and "prefers-color-scheme: light" in css and "@import" not in css and "url(http" not in css
