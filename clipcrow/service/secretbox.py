"""Encrypting a stored credential (a person's SocioBird key or token) with a key that is not in the database.

Fernet (AES-128-CBC with an HMAC) from the ``cryptography`` package. The key is derived from the server secret that already signs sign-in codes
(``CLIPCROW_SECRET_KEY``, or ``<data>/clipcrow.secret``) with a separate label, so one secret serves both without sharing a key. A stored value is
``v1:<token>``; the version lets a later key rotation re-encrypt. If the secret is lost or changed the values cannot be read: they open as None and the
person is asked to connect again. Nothing here logs or returns a plaintext value.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

PREFIX = "v1:"


def _fernet(secret: bytes, purpose: str) -> Fernet:
    key = hmac.new(secret, f"clipcrow-secretbox:{purpose}".encode(), hashlib.sha256).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def seal(secret: bytes, purpose: str, plaintext: str) -> str:
    return PREFIX + _fernet(secret, purpose).encrypt(plaintext.encode("utf-8")).decode("ascii")


def open_(secret: bytes, purpose: str, stored: str) -> Optional[str]:
    """The plaintext, or None when the value is damaged, from another version, or made with another key."""
    if not stored or not stored.startswith(PREFIX):
        return None
    try:
        return _fernet(secret, purpose).decrypt(stored[len(PREFIX):].encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError):
        return None
