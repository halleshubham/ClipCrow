"""Outbound HTTP that never follows a redirect, and an address check that is not a string prefix.

Calls that carry a secret (the worker token, a SocioBird or Razorpay key, a mail key) go to addresses the operator configured. If such a server answered with a redirect,
urllib would send the Authorization header on to wherever it points, even over plain http. ``install()`` makes every urllib call refuse redirects: the answer becomes an
HTTPError, which the callers already treat as a failure."""
from __future__ import annotations

import ipaddress
import urllib.request
from urllib.parse import urlsplit


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):          # noqa: D401
        return None


def install() -> None:
    urllib.request.install_opener(urllib.request.build_opener(_NoRedirect))


def https_or_loopback(url: str) -> bool:
    """True for an https address, or for http only when the host really is this machine ("http://127.0.0.1.evil.com" is not)."""
    try:
        p = urlsplit(url)
        host = p.hostname or ""
    except ValueError:
        return False
    if p.scheme == "https" and host:
        return True
    if p.scheme != "http":
        return False
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False
