import asyncio
import logging

import httpx

from config import settings

logger = logging.getLogger(__name__)

_token: str | None = None
_refresh_task: asyncio.Task | None = None


async def authenticate() -> str:
    """Authenticate with PocketBase. Retries up to 10x with backoff (handles Docker startup races)."""
    global _token

    for attempt in range(10):
        try:
            async with httpx.AsyncClient(base_url=settings.PB_URL, timeout=10) as client:
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


async def _refresh_loop() -> None:
    """Erneuert den Token alle 55 Minuten (PocketBase-Token läuft nach 1h ab)."""
    while True:
        await asyncio.sleep(55 * 60)
        try:
            await authenticate()
            logger.info("PocketBase token refreshed")
        except Exception as exc:
            logger.error("PocketBase token refresh fehlgeschlagen: %s", exc)


def start_token_refresh() -> None:
    """Startet den Hintergrund-Refresh-Task. Einmalig beim App-Start aufrufen."""
    global _refresh_task
    _refresh_task = asyncio.create_task(_refresh_loop())
    _refresh_task.add_done_callback(
        lambda t: t.exception() and logger.error("Token-Refresh-Task abgestürzt: %s", t.exception())
    )


def stop_token_refresh() -> None:
    if _refresh_task and not _refresh_task.done():
        _refresh_task.cancel()


def get_token() -> str | None:
    return _token


def _auth_headers() -> dict:
    return {"Authorization": f"Bearer {_token}"} if _token else {}


async def _request_with_reauth(method: str, path: str, **kwargs) -> httpx.Response:
    """Führt einen HTTP-Request aus; bei 401 einmalig re-authentifizieren und nochmal versuchen."""
    async with httpx.AsyncClient(base_url=settings.PB_URL, timeout=30) as client:
        resp = await getattr(client, method)(path, headers=_auth_headers(), **kwargs)
        if resp.status_code == 401:
            logger.warning("PocketBase 401 — Token abgelaufen, erneuere...")
            await authenticate()
            resp = await getattr(client, method)(path, headers=_auth_headers(), **kwargs)
        return resp


async def pb_get(path: str, params: dict | None = None) -> dict:
    resp = await _request_with_reauth("get", path, params=params)
    resp.raise_for_status()
    return resp.json()


class DuplicateRecordError(Exception):
    """Raised when PocketBase rejects a record due to unique constraint."""
    pass


async def pb_post(path: str, data: dict) -> dict:
    resp = await _request_with_reauth("post", path, json=data)
    if not resp.is_success:
        body = resp.text
        if "not_unique" in body or "validation_not_unique" in body:
            raise DuplicateRecordError(body)
        logger.error(f"pb_post {path} → {resp.status_code}: {body[:500]}")
    resp.raise_for_status()
    return resp.json()


async def pb_patch(path: str, data: dict) -> dict:
    resp = await _request_with_reauth("patch", path, json=data)
    if not resp.is_success:
        logger.error(f"pb_patch {path} → {resp.status_code}: {resp.text[:500]}")
    resp.raise_for_status()
    return resp.json()


async def pb_delete(path: str) -> None:
    resp = await _request_with_reauth("delete", path)
    resp.raise_for_status()
