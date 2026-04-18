import asyncio
import logging

import httpx

from config import settings

logger = logging.getLogger(__name__)

_token: str | None = None


async def authenticate() -> str:
    """Authenticate with PocketBase. Retries up to 10x with backoff (handles Docker startup races)."""
    global _token

    for attempt in range(10):
        try:
            async with httpx.AsyncClient(base_url=settings.PB_URL, timeout=10) as client:
                # PocketBase v0.22+ superusers endpoint
                try:
                    resp = await client.post(
                        "/api/collections/_superusers/auth-with-password",
                        json={"identity": settings.PB_ADMIN_EMAIL, "password": settings.PB_ADMIN_PASSWORD},
                    )
                    if resp.status_code == 200:
                        _token = resp.json()["token"]
                        logger.info("Authenticated with PocketBase (superusers API)")
                        return _token
                except Exception:
                    pass

                # Fallback: PocketBase < v0.22 admins endpoint
                resp = await client.post(
                    "/api/admins/auth-with-password",
                    json={"identity": settings.PB_ADMIN_EMAIL, "password": settings.PB_ADMIN_PASSWORD},
                )
                resp.raise_for_status()
                _token = resp.json()["token"]
                logger.info("Authenticated with PocketBase (admins API)")
                return _token

        except Exception as e:
            wait = min(2 ** attempt, 30)
            logger.warning(f"PocketBase auth attempt {attempt + 1}/10 failed: {e} — retrying in {wait}s")
            await asyncio.sleep(wait)

    raise RuntimeError("Could not authenticate with PocketBase after 10 attempts")


def get_token() -> str | None:
    return _token


def _auth_headers() -> dict:
    return {"Authorization": f"Bearer {_token}"} if _token else {}


async def pb_get(path: str, params: dict | None = None) -> dict:
    async with httpx.AsyncClient(base_url=settings.PB_URL, timeout=30) as client:
        resp = await client.get(path, headers=_auth_headers(), params=params)
        resp.raise_for_status()
        return resp.json()


class DuplicateRecordError(Exception):
    """Raised when PocketBase rejects a record due to unique constraint."""
    pass


async def pb_post(path: str, data: dict) -> dict:
    async with httpx.AsyncClient(base_url=settings.PB_URL, timeout=30) as client:
        resp = await client.post(path, headers=_auth_headers(), json=data)
        if not resp.is_success:
            body = resp.text
            if "not_unique" in body or "validation_not_unique" in body:
                raise DuplicateRecordError(body)
            logger.error(f"pb_post {path} → {resp.status_code}: {body[:500]}")
        resp.raise_for_status()
        return resp.json()


async def pb_patch(path: str, data: dict) -> dict:
    async with httpx.AsyncClient(base_url=settings.PB_URL, timeout=30) as client:
        resp = await client.patch(path, headers=_auth_headers(), json=data)
        if not resp.is_success:
            logger.error(f"pb_patch {path} → {resp.status_code}: {resp.text[:500]}")
        resp.raise_for_status()
        return resp.json()


async def pb_delete(path: str) -> None:
    async with httpx.AsyncClient(base_url=settings.PB_URL, timeout=30) as client:
        resp = await client.delete(path, headers=_auth_headers())
        resp.raise_for_status()
