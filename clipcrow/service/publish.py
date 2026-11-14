"""Publishing finished clips to social channels through SocioBird (a Postiz-style scheduler with a public API).

Verified against SocioBird's own published tool schemas (the per-platform settings below are exactly what it says each
platform requires), NOT against its live REST API from the build environment (no credentials there): the request
shapes follow the Postiz-style public API that those tools mirror (``GET /public/v1/integrations``,
``POST /public/v1/upload`` multipart, ``POST /public/v1/posts``) and every network call goes through one injectable
function, so the tests use a fake and a mismatch would be fixed in this one file.

Scope and safety:
  * The API key belongs to the OPERATOR's SocioBird workspace, so it is single-tenant. With accounts turned on, posting
    stays off unless the operator opts in (``CLIPCROW_SOCIOBIRD_SHARED=1``: everyone who can sign in may post to those
    channels). Per-user keys are future work.
  * Only platforms whose required settings were checked are allowed (``SUPPORTED``).
  * Captions are plain text from a person: they are escaped and wrapped in ``<p>`` here, never passed through as HTML.
  * If a clip includes AI-generated media the person must confirm they will label it; TikTok's own AI label is set for them.
  * Errors shown to a person are fixed sentences, never the service's raw answer.
"""
from __future__ import annotations

import html
import json
import os
import re
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

SUPPORTED = ("youtube", "instagram", "instagram-standalone", "tiktok")
MAX_UPLOAD = 250 * 1024 * 1024
LIMITS = {"youtube": 5000, "instagram": 2200, "instagram-standalone": 2200, "tiktok": 2000}
_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


class PublishError(RuntimeError):
    """A sentence that is safe to show a person."""


@dataclass
class Channel:
    id: str
    name: str
    platform: str


def caption_html(text: str, platform: str) -> str:
    """Plain text in, the small HTML subset SocioBird accepts out: one ``<p>`` per line, everything escaped."""
    text = _CTRL.sub(" ", text or "").strip()[: LIMITS.get(platform, 2000)]
    lines = [ln.strip() for ln in text.splitlines()]
    return "".join(f"<p>{html.escape(ln, quote=False)}</p>" for ln in lines if ln) or "<p></p>"


def settings_for(platform: str, *, title: str, privacy: str = "public", made_for_kids: bool = False,
                 ai_generated: bool = False) -> dict:
    """The per-platform settings SocioBird's schema requires. ``privacy``: public | unlisted | private."""
    if privacy not in ("public", "unlisted", "private"):
        raise PublishError("Choose public, unlisted or private.")
    title = _CTRL.sub(" ", title or "").strip()
    if platform == "youtube":
        if len(title) < 2:
            raise PublishError("YouTube needs a title of at least 2 characters.")
        return {"__type": platform, "title": title[:100], "type": privacy,
                "selfDeclaredMadeForKids": "yes" if made_for_kids else "no"}
    if platform in ("instagram", "instagram-standalone"):
        return {"__type": platform, "post_type": "post"}                         # a video post is published as a Reel
    if platform == "tiktok":
        level = {"public": "PUBLIC_TO_EVERYONE", "unlisted": "FOLLOWER_OF_CREATOR", "private": "SELF_ONLY"}[privacy]
        return {"__type": platform, "title": title[:90], "privacy_level": level, "duet": True, "stitch": True, "comment": True,
                "autoAddMusic": "no", "brand_content_toggle": False, "brand_organic_toggle": False,
                "content_posting_method": "DIRECT_POST", "video_made_with_ai": bool(ai_generated)}
    raise PublishError("That platform is not supported yet.")


USER_AGENT = "ClipCrow/1.0 (+https://github.com/halleshubham/ClipCrow)"


class SocioBird:
    def __init__(self, base_url: str, api_key: str, *, request: Callable[..., dict] | None = None):
        if not (base_url and api_key):
            raise ValueError("SocioBird needs a base URL and an API key")
        if not base_url.startswith("https://") and not base_url.startswith("http://127.0.0.1"):
            raise ValueError("the SocioBird address must be https")
        self.base, self._key = base_url.rstrip("/"), api_key
        self._request = request or self._http

    @classmethod
    def from_env(cls) -> Optional["SocioBird"]:
        url, key = os.environ.get("SOCIOBIRD_API_URL", ""), os.environ.get("SOCIOBIRD_API_KEY", "")
        return cls(url, key) if url and key else None

    # --- transport ----------------------------------------------------------------------------------
    def _http(self, method: str, path: str, *, json_body: dict | None = None, file: tuple[str, bytes, str] | None = None) -> dict | list:
        headers = {"Authorization": self._key, "User-Agent": USER_AGENT}      # Cloudflare in front of SocioBird refuses urllib's own (error 1010)
        data = None
        if file is not None:
            name, content, ctype = file
            boundary = uuid.uuid4().hex
            data = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{name}\"\r\n"
                    f"Content-Type: {ctype}\r\n\r\n").encode() + content + f"\r\n--{boundary}--\r\n".encode()
            headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
        elif json_body is not None:
            data = json.dumps(json_body).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base + path, method=method, data=data, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=300 if file else 30) as r:     # noqa: S310 (operator-configured https)
                return json.loads(r.read(5_000_000) or b"{}")
        except urllib.error.HTTPError as e:
            raise PublishError(f"SocioBird refused the request ({e.code}).") from None
        except (urllib.error.URLError, TimeoutError, ValueError):
            raise PublishError("SocioBird could not be reached.") from None

    # --- calls --------------------------------------------------------------------------------------
    def channels(self) -> list[Channel]:
        raw = self._request("GET", "/public/v1/integrations")
        out = []
        for c in raw if isinstance(raw, list) else []:
            if not isinstance(c, dict) or c.get("disabled"):
                continue
            platform = str(c.get("identifier") or c.get("providerIdentifier") or "")
            if platform in SUPPORTED and c.get("id"):
                out.append(Channel(str(c["id"]), str(c.get("name") or platform)[:80], platform))
        return out

    def upload(self, path: Path) -> dict:
        size = path.stat().st_size
        if size > MAX_UPLOAD:
            raise PublishError("That video is too large to upload.")
        res = self._request("POST", "/public/v1/upload", file=(path.name, path.read_bytes(), "video/mp4"))
        if not isinstance(res, dict) or not res.get("id") or not res.get("path"):
            raise PublishError("SocioBird did not accept the video.")
        return {"id": str(res["id"]), "path": str(res["path"])}

    def post(self, channel: Channel, content: str, media: dict, settings: dict, *, when: datetime | None) -> str:
        body = {"type": "schedule" if when else "now", "date": (when or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                "shortLink": False, "tags": [],
                "posts": [{"integration": {"id": channel.id}, "value": [{"content": content, "image": [media]}], "settings": settings}]}
        res = self._request("POST", "/public/v1/posts", json_body=body)
        first = (res[0] if isinstance(res, list) and res else res) or {}
        return str(first.get("postId") or first.get("id") or "") if isinstance(first, dict) else ""


def validate_when(when: Optional[str]) -> Optional[datetime]:
    if not when:
        return None
    try:
        dt = datetime.fromisoformat(when.replace("Z", "+00:00"))
    except ValueError:
        raise PublishError("That date is not valid.") from None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    if dt < now + timedelta(minutes=2):
        raise PublishError("Pick a time at least a few minutes from now, or leave it empty to post now.")
    if dt > now + timedelta(days=365):
        raise PublishError("That is more than a year away.")
    return dt.astimezone(timezone.utc)
