"""Zweiter PocketBase-Client: die Activity-PB (Kalender-Store).

Eine **separate** PB-Instanz, getrennt von der mailflow-eigenen `pb_client`.
Dient ausschließlich dazu, Kalender-Einladungen aus Mails als `termine`-Records
in den eigenen Kalender zu übernehmen.

Auth als normaler User (`stefan@hpa24.de`) über die `users`-Collection — **kein**
Superuser. Record-Schreibrechte genügen; das Schema (Felder `join_url`, `ics_uid`)
wird vorausgesetzt und hier nie geändert.

Das Feature ist deaktiviert, solange `ACTIVITY_PB_IDENTITY`/`ACTIVITY_PB_PASSWORD`
leer sind (`is_configured()` → False) — dann liefern die Endpoints 503.
"""
import logging

import httpx

from config import settings

logger = logging.getLogger(__name__)

_token: str | None = None


def is_configured() -> bool:
    return bool(
        settings.ACTIVITY_PB_URL
        and settings.ACTIVITY_PB_IDENTITY
        and settings.ACTIVITY_PB_PASSWORD
    )


def _quote(value: object) -> str:
    """Quotet einen String-Wert für einen PB-Filter (Backslash/Quote escaped)."""
    s = str(value)
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


async def _authenticate() -> str:
    global _token
    async with httpx.AsyncClient(base_url=settings.ACTIVITY_PB_URL, timeout=15) as client:
        resp = await client.post(
            "/api/collections/users/auth-with-password",
            json={
                "identity": settings.ACTIVITY_PB_IDENTITY,
                "password": settings.ACTIVITY_PB_PASSWORD,
            },
        )
        resp.raise_for_status()
        _token = resp.json()["token"]
        logger.info("Authenticated with Activity-PB (users API)")
        return _token


async def _request(method: str, path: str, **kwargs) -> httpx.Response:
    """Request mit lazy Login; bei 401 einmal re-authentifizieren."""
    global _token
    if not _token:
        await _authenticate()
    async with httpx.AsyncClient(base_url=settings.ACTIVITY_PB_URL, timeout=20) as client:
        headers = {"Authorization": f"Bearer {_token}"}
        resp = await getattr(client, method)(path, headers=headers, **kwargs)
        if resp.status_code == 401:
            await _authenticate()
            headers = {"Authorization": f"Bearer {_token}"}
            resp = await getattr(client, method)(path, headers=headers, **kwargs)
        return resp


async def list_manual_calendars() -> list[dict]:
    """Nur manuelle Kalender (`typ == "manuell"`) — externe (Google/Ferien) sind
    read-only und kein gültiges Ziel."""
    resp = await _request(
        "get",
        "/api/collections/kalender/records",
        params={"perPage": 200, "sort": "name", "filter": 'typ="manuell"'},
    )
    resp.raise_for_status()
    items = resp.json().get("items", [])
    return [
        {"id": k["id"], "name": k.get("name", ""), "farbe": k.get("farbe", "")}
        for k in items
    ]


async def find_termin_by_uid(uid: str) -> dict | None:
    """Doppelklick-Schutz: existierender termine-Record mit dieser VEVENT-UID?"""
    if not uid:
        return None
    resp = await _request(
        "get",
        "/api/collections/termine/records",
        params={"perPage": 1, "filter": f"ics_uid={_quote(uid)}"},
    )
    resp.raise_for_status()
    items = resp.json().get("items", [])
    return items[0] if items else None


async def create_termin(data: dict) -> dict:
    resp = await _request("post", "/api/collections/termine/records", json=data)
    resp.raise_for_status()
    return resp.json()
