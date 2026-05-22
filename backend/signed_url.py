"""Kurzlebige signierte URL-Token für Stellen, die keine Header senden können
(SSE-EventSource, <img src>, <iframe src>, <a href>-Downloads).

Format: <base64url-payload>.<base64url-signature>
Payload: {"p": "<path>", "e": <epoch>, "m": "<METHOD>"}
Signatur: HMAC-SHA256(payload, SIGN_SECRET)

Verify-Regeln:
- Signatur stimmt (constant-time-Vergleich)
- exp nicht abgelaufen
- path muss exakt matchen (kein Substring/Prefix)
- method muss exakt matchen (S3, 2026-05-23): verhindert, dass ein signiertes
  GET-Token (Download/SSE) für POST/DELETE umfunktioniert wird.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

from config import settings


_DEFAULT_TTL = 300  # 5 min
_MAX_TTL = 1800  # 30 min


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def _secret() -> bytes:
    sec = (settings.SIGN_SECRET or "").encode("utf-8")
    if not sec:
        raise RuntimeError("SIGN_SECRET not configured")
    return sec


def sign(path: str, ttl_seconds: int = _DEFAULT_TTL, method: str = "GET") -> tuple[str, int]:
    """Gibt (token, exp_epoch) zurück."""
    ttl = max(1, min(int(ttl_seconds), _MAX_TTL))
    exp = int(time.time()) + ttl
    m = (method or "GET").upper()
    payload = json.dumps({"p": path, "e": exp, "m": m}, separators=(",", ":")).encode("utf-8")
    payload_b64 = _b64url_encode(payload)
    sig = hmac.new(_secret(), payload_b64.encode("ascii"), hashlib.sha256).digest()
    return f"{payload_b64}.{_b64url_encode(sig)}", exp


def verify(token: str, path: str, method: str) -> bool:
    """True wenn Token gültig für genau diesen path+method und nicht abgelaufen."""
    if not token or "." not in token:
        return False
    try:
        payload_b64, sig_b64 = token.split(".", 1)
        expected = hmac.new(_secret(), payload_b64.encode("ascii"), hashlib.sha256).digest()
        if not hmac.compare_digest(expected, _b64url_decode(sig_b64)):
            return False
        data = json.loads(_b64url_decode(payload_b64))
        if data.get("p") != path:
            return False
        if (data.get("m") or "").upper() != (method or "").upper():
            return False
        if int(data.get("e", 0)) < int(time.time()):
            return False
        return True
    except (ValueError, KeyError, json.JSONDecodeError):
        return False
