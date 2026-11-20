"""gzip for the text the browser downloads on every visit (app.js 179 KB, style.css, icons.css, the API's JSON).

A plain ASGI middleware that touches only text-like responses and buffers them whole (they are small): video and other downloads stream untouched, so range requests keep
working. Starlette's own GZipMiddleware would also compress video."""
from __future__ import annotations

import gzip

TEXTY = (b"text/html", b"text/css", b"text/plain", b"application/javascript", b"text/javascript", b"application/json", b"image/svg+xml")
MIN = 1024
MAX = 4 * 1024 * 1024


class TextGzip:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or b"gzip" not in dict(scope["headers"]).get(b"accept-encoding", b""):
            return await self.app(scope, receive, send)
        start, chunks, passthrough = None, [], False

        async def wrapped(msg):
            nonlocal start, passthrough
            if msg["type"] == "http.response.start":
                h = {k.lower(): v for k, v in msg["headers"]}
                ok = (msg["status"] == 200 and b"content-encoding" not in h and h.get(b"content-type", b"").split(b";")[0].strip() in TEXTY
                      and int(h.get(b"content-length", b"0") or 0) <= MAX)
                if not ok:
                    passthrough = True
                    return await send(msg)
                start = msg
                return
            if passthrough:
                return await send(msg)
            chunks.append(msg.get("body", b""))
            if msg.get("more_body"):
                return
            body = b"".join(chunks)
            headers = [(k, v) for k, v in start["headers"] if k.lower() not in (b"content-length", b"content-encoding")]
            if len(body) >= MIN:
                body = gzip.compress(body, 6, mtime=0)
                headers.append((b"content-encoding", b"gzip"))
            headers += [(b"vary", b"Accept-Encoding"), (b"content-length", str(len(body)).encode())]
            await send({**start, "headers": headers})
            await send({"type": "http.response.body", "body": body})

        await self.app(scope, receive, wrapped)
