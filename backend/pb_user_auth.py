"""Validiert User-PB-Tokens gegen PocketBase mit In-Memory-Cache.

Separat von pb_client.py, das den **Admin-Token** für Backend-Operationen verwaltet.
Hier geht es um Tokens, die der Browser im Authorization-Header mitschickt.
"""
from __future__ import annotations

import hashlib
import time

import httpx
from fastapi import Header, HTTPException

from config import settings


_CACHE_TTL = 60  # Sekunden — kurz halten, PB-Logout soll spürbar werden
_cache: dict[str, int] = {}  # token_hash -> cache_exp_epoch


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _gc(now: int) -> None:
    if len(_cache) <= 500:
        return
    for k in [k for k, exp in _cache.items() if exp <= now]:
        _cache.pop(k, None)


async def validate(token: str) -> bool:
    """True wenn PB den Token als gültig akzeptiert (mit 60s-Cache)."""
    if not token:
        return False
    now = int(time.time())
    h = _hash(token)
    exp = _cache.get(h)
    if exp and exp > now:
        return True
    try:
        async with httpx.AsyncClient(base_url=settings.PB_URL, timeout=5) as client:
            resp = await client.post(
                "/api/collections/users/auth-refresh",
                headers={"Authorization": f"Bearer {token}"},
            )
        if resp.status_code == 200:
            _cache[h] = now + _CACHE_TTL
            _gc(now)
            return True
    except httpx.HTTPError:
        pass
    return False


def get_user_token(authorization: str | None = Header(default=None)) -> str:
    """FastAPI-Dependency: extrahiert den PB-User-Token aus Authorization-Header.
    Setzt voraus, dass die Auth-Middleware den Token bereits validiert hat —
    diese Dependency reicht ihn an Endpoints durch, die PocketBase per
    `pb_*_as(token, …)` im User-Kontext aufrufen (statt mit dem Admin-Token).
    401, wenn Header fehlt (z.B. Signed-URL-Routen — die taugen nicht für pb_user).
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Bearer token required")
    return authorization[7:]
