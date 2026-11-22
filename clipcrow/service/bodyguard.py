"""Stop a request before its body is read when it cannot be a good one.

FastAPI reads a whole multipart or JSON body BEFORE it checks who is asking, and Starlette spools big parts to disk: so an anonymous client could send gigabytes at any
``POST`` and fill the temp disk before getting a 401. This is a plain ASGI middleware (no buffering, no per-chunk copy) that, for requests that change something under
``/api``:
- answers 401 at once when there is no session cookie and no Authorization header (sign-in, the payment webhook, OAuth return and the GPU worker paths are exempt: they
  authenticate in their own way, and the worker uploads are large and streamed);
- answers 413 when Content-Length is over the route's cap, and counts the bytes of a chunked body (no Content-Length) and stops it at the cap.
"""
from __future__ import annotations

import json
from typing import Callable

MB = 1024 * 1024
EXEMPT = ("/api/auth/", "/api/billing/razorpay/webhook", "/api/sociobird/callback", "/api/worker/")
METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def caps(upload_mb: int) -> dict[str, int]:
    """Largest body (bytes) per upload route; every other route is JSON and gets 2 MB."""
    return {"/api/jobs": (upload_mb + 8) * MB, "/api/pictures": 12 * MB, "/api/brand/logo": 4 * MB, "/api/brand/overlay": 10 * MB, "/api/brand/outro": 95 * MB, "/api/brand/music": 35 * MB,
            "/api/styles": 210 * MB}


class BodyGuard:
    def __init__(self, app, upload_mb: int, session_cookies: tuple[str, ...], require_sign_in: bool = True):
        self.app, self.caps, self.cookies, self.require = app, caps(upload_mb), session_cookies, require_sign_in

    def _signed(self, headers: dict) -> bool:
        if headers.get(b"authorization"):
            return True
        jar = headers.get(b"cookie", b"").decode("latin-1")
        return any(f"{name}=" in jar for name in self.cookies)

    async def _reply(self, send: Callable, status: int, message: str) -> None:
        body = json.dumps({"detail": message}).encode()
        await send({"type": "http.response.start", "status": status, "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        if scope["type"] != "http" or scope["method"] not in METHODS or not path.startswith("/api/") or path.startswith("/api/worker/"):
            return await self.app(scope, receive, send)
        headers = dict(scope["headers"])
        if self.require and not path.startswith(EXEMPT) and not self._signed(headers):
            return await self._reply(send, 401, "Sign in first.")
        cap = self.caps.get(path.rstrip("/"), 2 * MB)
        try:
            declared = int(headers.get(b"content-length", b"0") or 0)
        except ValueError:
            return await self._reply(send, 400, "Bad Content-Length.")
        if declared > cap:
            return await self._reply(send, 413, "That request is too large.")
        seen = 0
        started = False
        exceeded = False

        async def counted():
            nonlocal seen, exceeded
            msg = await receive()
            if msg["type"] == "http.request":
                seen += len(msg.get("body", b""))
                if seen > cap:
                    exceeded = True
                    raise _TooBig()
            return msg

        async def tracked(msg):
            nonlocal started
            if msg["type"] == "http.response.start":
                started = True
                if exceeded:                                           # the framework turned our stop into its own "bad body" answer: say what really happened
                    msg = {**msg, "status": 413}
            await send(msg)

        try:
            await self.app(scope, counted, tracked)
        except _TooBig:
            if not started:
                await self._reply(send, 413, "That request is too large.")


class _TooBig(Exception):
    pass
