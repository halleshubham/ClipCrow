"""The landing page is the front door at /, the app is at /dashboard; the page needs no inline script and makes no third-party request."""
import re

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from clipcrow.service.api import STATIC, create_app
from tests.test_service import FakePipeline


def test_the_landing_page_is_public_self_contained_and_links_to_the_app(tmp_path):
    from clipcrow.service.accounts import Accounts
    c = TestClient(create_app(tmp_path, FakePipeline(), accounts=Accounts(tmp_path / "x.db")))
    r = c.get("/")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"] and "one long video" in r.text.lower()      # no sign-in needed
    html = r.text
    assert "<script" not in html.replace('<script src="/landing/landing.js" defer></script>', "") and 'style="' not in html                                                              # the CSP allows neither
    assert not re.findall(r'(?:src|href)="https?://', html)                                                             # nothing from another site
    assert 'href="/dashboard"' in html and "/dashboard#/privacy" in html and "/dashboard#/terms" in html and "support@shackyapps.in" in html
    for path in re.findall(r'(?:src|href)="(/[^"#]+)"', html):                                                          # every local file it names exists
        if not path.startswith(("/#", "/dashboard")):
            assert c.get(path.split("?")[0]).status_code == 200, path
    assert "Content-Security-Policy" in r.headers and "script-src 'self'" in r.headers["Content-Security-Policy"]


def test_the_landing_css_respects_reduced_motion_and_both_themes():
    css = (STATIC / "landing" / "landing.css").read_text(encoding="utf-8")
    assert "prefers-reduced-motion: reduce" in css and "prefers-color-scheme: light" in css and "@import" not in css and "url(http" not in css


def test_the_landing_page_names_the_two_use_cases_and_has_a_cost_estimator():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    low = html.lower()
    assert "vox populi" in low and "podcast" in low and "many reels" in low                      # use case 1
    assert "raw take" in low and "clean, branded production" in low and "ums" in low             # use case 2
    assert 'id="est-min"' in html and 'id="est-cost"' in html and 'id="examples"' in html         # the estimator and the examples (filled from /api/pricing)
    js = (STATIC / "landing" / "landing.js").read_text(encoding="utf-8")
    assert "/api/pricing" in js and "innerHTML" not in js                                         # the numbers come from the server; text only


def test_the_dashboard_is_the_app_and_the_old_addresses_still_work(tmp_path):
    from clipcrow.service.accounts import Accounts
    c = TestClient(create_app(tmp_path, FakePipeline(), accounts=Accounts(tmp_path / "x.db")))
    for path in ("/dashboard", "/dashboard/"):
        r = c.get(path)
        assert r.status_code == 200 and 'id="app"' in r.text and "/app.js" in r.text and "text/html" in r.headers["content-type"], path
    assert "one long video" in c.get("/").text.lower() and 'id="app"' not in c.get("/").text                  # / is the landing page, not the app
    for old in ("/landing", "/landing/"):
        r = c.get(old, follow_redirects=False)
        assert r.status_code == 301 and r.headers["location"] == "/"
    assert c.get("/landing/landing.css").status_code == 200 and c.get("/landing/landing.js").status_code == 200        # the page's own files
    js = (STATIC / "landing" / "landing.js").read_text(encoding="utf-8")
    assert "location.replace('/dashboard'" in js and "hashchange" in js                                                                  # an old /#/jobs/... link goes to the app
    assert c.get("/api/health").json() == {"ok": True} and c.get("/api/config").status_code == 200
