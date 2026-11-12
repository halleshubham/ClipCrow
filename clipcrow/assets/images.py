"""AI image generation behind a one-method interface. The default is Gemini's image model over plain HTTPS (no extra
dependency); anything else that can write an image file can be dropped in.

Not verified against the live API from the build environment (no credentials there): the request and response
shapes follow Google's documented ``generateContent`` image output, and the tests use a fake HTTP function. The
model name is configurable (``CLIPCROW_IMAGE_MODEL``) because model names change faster than this code.
"""
from __future__ import annotations

import base64
import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Protocol

DEFAULT_MODEL = "gemini-2.5-flash-image"
ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
MAX_BYTES = 12 * 1024 * 1024
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


class ImageError(RuntimeError):
    """Generation failed. The message is safe to show a person."""


class NotConfigured(ImageError):
    pass


class ImageProvider(Protocol):
    name: str
    model: str

    def generate(self, prompt: str, dest: Path, *, aspect: str = "9:16") -> Path:
        """Write an image for ``prompt`` to ``dest`` (extension chosen by the image type) and return the real path."""


def sniff(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return None


def _post(url: str, headers: dict, body: bytes, timeout: float = 90.0) -> dict:
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:                 # noqa: S310 (fixed https endpoint)
            return json.loads(r.read(40 * 1024 * 1024))
    except urllib.error.HTTPError as e:
        raise ImageError(f"the image service answered {e.code}") from None
    except (urllib.error.URLError, TimeoutError, ValueError):
        raise ImageError("the image service could not be reached") from None


class GeminiImages:
    name = "gemini"

    def __init__(self, api_key: str | None = None, model: str | None = None,
                 post: Callable[[str, dict, bytes], dict] | None = None):
        self.api_key = api_key or os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or ""
        self.model = model or os.environ.get("CLIPCROW_IMAGE_MODEL") or DEFAULT_MODEL
        self._post = post or _post

    def generate(self, prompt: str, dest: Path, *, aspect: str = "9:16") -> Path:
        if not self.api_key:
            raise NotConfigured("image generation is not configured (set GEMINI_API_KEY)")
        prompt = _CONTROL.sub(" ", prompt).strip()[:600]
        if not prompt:
            raise ImageError("there was nothing to draw")
        body = json.dumps({
            "contents": [{"parts": [{"text": f"{prompt}\n\nAspect ratio {aspect}. No text, no logos, no real people."}]}],
            "generationConfig": {"responseModalities": ["IMAGE"]},
        }).encode()
        resp = self._post(ENDPOINT.format(model=self.model),
                          {"Content-Type": "application/json", "x-goog-api-key": self.api_key}, body)
        parts = ((resp.get("candidates") or [{}])[0].get("content") or {}).get("parts") or []
        raw = next((p.get("inlineData") or p.get("inline_data") for p in parts if p.get("inlineData") or p.get("inline_data")), None)
        if not raw or not raw.get("data"):
            raise ImageError("the image service returned no picture (the prompt may have been declined)")
        try:
            data = base64.b64decode(raw["data"], validate=True)
        except ValueError:
            raise ImageError("the image service returned something that is not a picture") from None
        suffix = sniff(data)
        if suffix is None or not 1000 < len(data) <= MAX_BYTES:
            raise ImageError("the image service returned something that is not a usable picture")
        out = Path(dest).with_suffix(suffix)
        out.write_bytes(data)
        return out
