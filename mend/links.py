"""
Signed links from Telegram to the wallet page.

A link carries what the page is for (fund a treasury, approve an incident),
for whom, and when it expires, signed with the server's secret. The page needs
no login: holding the link is the permission, and the wallet signature is the
proof of ownership on-chain.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any, Dict, Optional


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def make_token(secret: str, payload: Dict[str, Any], ttl_seconds: int = 3600) -> str:
    body = dict(payload, exp=int(time.time()) + ttl_seconds)
    raw = _b64(json.dumps(body, separators=(",", ":"), sort_keys=True).encode())
    sig = _b64(hmac.new(secret.encode(), raw.encode(), hashlib.sha256).digest()[:18])
    return f"{raw}.{sig}"


def read_token(secret: str, token: str) -> Optional[Dict[str, Any]]:
    try:
        raw, sig = token.split(".", 1)
    except ValueError:
        return None
    expected = _b64(hmac.new(secret.encode(), raw.encode(), hashlib.sha256).digest()[:18])
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        body = json.loads(_unb64(raw))
    except ValueError:
        return None
    if not isinstance(body, dict) or int(body.get("exp", 0)) < time.time():
        return None
    return body
