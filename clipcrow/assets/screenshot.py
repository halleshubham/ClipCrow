"""A screenshot of a public web page, for the "news link" explainer beat.

Playwright is optional (``pip install 'clipcrow[assets]'`` and ``playwright install chromium``). Every request the page
makes (not just the first) is checked with ``assert_public_url`` and refused when it points inside a private network,
downloads and permissions are off, and the shot is a fixed-size first screen, so a page cannot make the picture huge.
"""
from __future__ import annotations

from pathlib import Path

from .urlguard import UnsafeURL, assert_public_url

MAX_BYTES = 10 * 1024 * 1024


class CaptureError(RuntimeError):
    """The page could not be captured. The message is safe to show a person."""


def capture_page(url: str, dest: str | Path, *, width: int = 1080, height: int = 1350, timeout_s: float = 25.0,
                 resolver=None, ports: tuple[int, ...] = (80, 443), chrome_args: tuple[str, ...] = (),
                 playwright_factory=None) -> Path:
    guard = (lambda u: assert_public_url(u, resolver=resolver, ports=ports)) if resolver else \
            (lambda u: assert_public_url(u, ports=ports))
    try:
        guard(url)
    except UnsafeURL as e:
        raise CaptureError(str(e)) from None
    if playwright_factory is None:
        try:
            from playwright.sync_api import sync_playwright as playwright_factory
        except ImportError:
            raise CaptureError("page screenshots need Playwright (pip install 'clipcrow[assets]' and playwright install chromium)") from None
    dest = Path(dest)
    blocked: list[str] = []
    try:
        with playwright_factory() as p:
            browser = p.chromium.launch(args=["--no-sandbox", *chrome_args])
            try:
                ctx = browser.new_context(viewport={"width": width, "height": height}, accept_downloads=False,
                                          service_workers="block", permissions=[], java_script_enabled=True,
                                          user_agent="Mozilla/5.0 (compatible; ClipCrow page preview)")
                page = ctx.new_page()

                def route(r):
                    try:
                        guard(r.request.url)
                    except UnsafeURL:
                        blocked.append(r.request.url)
                        return r.abort("blockedbyclient")
                    return r.continue_()

                page.route("**/*", route)
                page.set_default_timeout(timeout_s * 1000)
                page.goto(url, wait_until="load")
                page.wait_for_timeout(600)                       # let late fonts and lazy images settle
                page.screenshot(path=str(dest), full_page=False, type="png")
            finally:
                browser.close()
    except CaptureError:
        raise
    except Exception:
        raise CaptureError("the page could not be loaded in time") from None
    if not dest.is_file() or not 1000 < dest.stat().st_size <= MAX_BYTES:
        raise CaptureError("the page did not produce a usable picture")
    return dest
