"""Only public web addresses. A link a person pastes ends up in a headless browser on the server: without this it
could be pointed at the server's own network (cloud metadata, an admin page, localhost services).

Checked: scheme (http/https), no credentials in the URL, the standard ports, and that the host name resolves ONLY
to globally routable addresses (not private, loopback, link-local, carrier-grade NAT or reserved ranges).

Known limit: this checks the DNS answer before the browser connects, so a host that changes its answer in between
(DNS rebinding) is not stopped here. Run the service with an egress policy that blocks private ranges as well.
"""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit

MAX_URL = 2000


class UnsafeURL(ValueError):
    """The address is not a public web page. The message is written for a person."""


def assert_public_url(url: str, *, resolver=socket.getaddrinfo, ports: tuple[int, ...] = (80, 443)) -> str:
    url = (url or "").strip()
    if not url or len(url) > MAX_URL:
        raise UnsafeURL("Use a web address of up to 2000 characters.")
    if any(ord(c) < 32 or c.isspace() for c in url):
        raise UnsafeURL("The address contains spaces or control characters.")
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise UnsafeURL("Only http and https addresses are allowed.")
    if parts.username is not None or parts.password is not None:
        raise UnsafeURL("Addresses with a user name or password are not allowed.")
    host = parts.hostname
    if not host:
        raise UnsafeURL("The address has no host name.")
    try:
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError:
        raise UnsafeURL("The address has an invalid port.") from None
    if ports and port not in ports:
        raise UnsafeURL("Only the standard web ports (80 and 443) are allowed.")
    try:
        answers = resolver(host, port, type=socket.SOCK_STREAM)
    except OSError:
        raise UnsafeURL("That web address could not be found.") from None
    addrs = {a[4][0] for a in answers}
    if not addrs:
        raise UnsafeURL("That web address could not be found.")
    for a in addrs:
        try:
            ip = ipaddress.ip_address(a.split("%")[0])
        except ValueError:
            raise UnsafeURL("That web address resolves to something that is not an IP address.") from None
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:      # ::ffff:10.0.0.1 is a private IPv4 address
            ip = ip.ipv4_mapped
        if not ip.is_global:
            raise UnsafeURL("That address points inside a private network, which is not allowed.")
    return url
