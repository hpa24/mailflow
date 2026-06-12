"""System / Infrastruktur-Endpoints — Health, Sign, Sync, SSE, Accounts,
SMTP-Server, Folders, Xano-Lookup.

Ausgegliedert aus main.py im Rahmen von C1 Phase 2 (Router-Split).

Auth-Mix in diesem Router:
- `/health`, `/sign`, `/sync/*`, `/events` hängen an der globalen Middleware
  (kein User-Token nötig — `/sign` validiert nur SIGN_SECRET, `/events`
  liest aus dem Shared-Queue, der per IDLE-Push gefüllt wird).
- `/accounts*`, `/smtp-servers`, `/folders*`: PB-User-Token via
  `pb_user_auth.get_user_token`.
- `/accounts/{id}` PATCH: bewusst noch Admin-Token (auf der C2-Phase-3-
  Liste für späteren Refactor mit Pydantic-Request-Modell).
- `/xano/user-info`: ruft Xano via httpx mit `XANO_API_KEY`, kein PB.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re as _re

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

import pb_client
import pb_user_auth
import signed_url
from config import settings
from idle_manager import get_sse_queues
from imap_sync import get_sync_skips, get_sync_status, sync_all_accounts
from models import HealthResponse, SyncStatusResponse

logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


@router.get("/health", response_model=HealthResponse)
async def health():
    return HealthResponse(status="ok")


# ---------------------------------------------------------------------------
# Signed URLs — für SSE/<img>/<iframe>, die keine Header senden können
# ---------------------------------------------------------------------------


class SignRequest(BaseModel):
    path: str
    ttl: int = 300
    method: str = "GET"


class UpdateAccountRequest(BaseModel):
    name: str | None = None
    from_name: str | None = None
    signature: str | None = None
    color_tag: str | None = None
    reply_to_email: str | None = None


# S3 (2026-05-23): /sign signiert nur noch Pfade aus dieser Allowlist und nur
# für GET. Vorher konnte jeder PB-Bearer einen Token für beliebigen Pfad +
# (durch die method-lose Verify-Logik) effektiv jede Methode bekommen, was
# Endpoints wie /attachments/upload (POST, ohne eigene User-Auth) ungewollt
# über signed URLs erreichbar gemacht hätte.
_SIGNABLE_GET_PATHS = (
    _re.compile(r"^/events$"),
    _re.compile(r"^/attachments/[a-zA-Z0-9]+/download$"),
    _re.compile(r"^/emails/[a-zA-Z0-9]+/inline$"),
    _re.compile(r"^/emails/[a-zA-Z0-9]+/source\.eml$"),
)


@router.post("/sign")
async def sign_url(payload: SignRequest):
    """Gibt einen kurzlebigen signierten URL-Token für genau diesen path+method zurück.
    Frontend nutzt das für SSE-EventSource, Inline-Bilder und Attachment-Downloads —
    Stellen, an denen keine Authorization-Header möglich sind.
    Die Route selbst hängt an der Auth-Middleware (PB-Bearer).
    """
    if not settings.SIGN_SECRET:
        raise HTTPException(status_code=503, detail="SIGN_SECRET nicht konfiguriert")
    if not payload.path.startswith("/"):
        raise HTTPException(status_code=400, detail="path muss mit / beginnen")
    method = (payload.method or "GET").upper()
    if method != "GET":
        raise HTTPException(status_code=400, detail="nur GET signierbar")
    if not any(p.match(payload.path) for p in _SIGNABLE_GET_PATHS):
        raise HTTPException(status_code=400, detail="path nicht signierbar")
    token, exp = signed_url.sign(payload.path, payload.ttl, method=method)
    return {"token": token, "exp": exp}


# ---------------------------------------------------------------------------
# IMAP-Sync (Trigger + Status)
# ---------------------------------------------------------------------------


@router.post("/sync/run")
async def sync_run(background_tasks: BackgroundTasks):
    """Manueller Sync-Trigger für alle Accounts."""
    background_tasks.add_task(sync_all_accounts)
    return {"status": "sync started"}


@router.get("/sync/status", response_model=SyncStatusResponse)
async def sync_status():
    return get_sync_status()


@router.get("/diagnostics/sync-skips")
async def diagnostics_sync_skips(token: str = Depends(pb_user_auth.get_user_token)):
    """Letzte ~500 Sync-Auffälligkeiten (Duplikat-Skips + Fetch-Fehler) als Ringpuffer.

    Befüllt aus imap_sync._record_sync_event. Daten leben in-memory und werden bei
    jedem Backend-Restart geleert. Diagnose-Endpoint für das Diagnose-Panel im
    Frontend.
    """
    skips = get_sync_skips()
    return {"count": len(skips), "items": skips}


# ---------------------------------------------------------------------------
# Server-Sent Events — IMAP-IDLE-Push an Frontend
# ---------------------------------------------------------------------------


@router.get("/events")
async def sse_events(request: Request):
    """Server-Sent Events — schickt 'new-mail' wenn IDLE neue Nachrichten erkennt."""
    queue: asyncio.Queue = asyncio.Queue(maxsize=20)
    queues = get_sse_queues()
    queues.append(queue)

    async def stream():
        try:
            yield "data: {\"type\":\"connected\"}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=25)
                    yield f"data: {json.dumps(event)}\n\n"
                except asyncio.TimeoutError:
                    # Echtes Event statt SSE-Kommentar: Kommentare sind für die
                    # EventSource-API unsichtbar — der Client braucht sichtbare
                    # Pings für seinen Halbtot-Watchdog (sse.js).
                    yield "data: {\"type\":\"ping\"}\n\n"
        finally:
            try:
                queues.remove(queue)
            except ValueError:
                pass

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------

_ACCOUNT_SAFE_FIELDS = "id,name,from_email,from_name,signature,color_tag,reply_to_email,imap_host,imap_port,imap_user,default_smtp_server,send_only"

# Tagesversand-Limit von mailbox.org. Wenn sich Stefans Tarif ändert,
# zentral hier anpassen — Frontend liest den Wert aus der Response.
_SEND_DAILY_LIMIT = 10000


@router.get("/accounts")
async def get_accounts(token: str = Depends(pb_user_auth.get_user_token)):
    # S1 (2026-05-23): accounts-Rules sind dicht (sensible imap_pass/smtp_pass).
    # Backend liest via Admin-Token, Authz hängt am Depends(get_user_token).
    return await pb_client.pb_get("/api/collections/accounts/records",
                                  params={"perPage": 100, "fields": _ACCOUNT_SAFE_FIELDS})


@router.get("/accounts/sent-today")
async def accounts_sent_today(token: str = Depends(pb_user_auth.get_user_token)):
    """Anzahl heute gesendete Mails pro Account aus dem Sent-Ordner.

    Cutoff ist Mitternacht Europa/Berlin → UTC. PocketBase speichert
    ``date_sent`` als UTC-Timestamp ``YYYY-MM-DD HH:MM:SS``.
    """
    from datetime import datetime, timezone
    try:
        from zoneinfo import ZoneInfo
        now_local = datetime.now(ZoneInfo("Europe/Berlin"))
    except Exception:
        now_local = datetime.now(timezone.utc)
    midnight_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    cutoff = midnight_local.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    accounts_data = await pb_client.pb_get(
        "/api/collections/accounts/records",
        params={"perPage": 100, "fields": "id"},
    )
    # P-Perf-4 (2026-05-23): pro-Account-Counts parallel statt seriell laden.
    # Bei 5 Accounts: ~50ms statt ~250ms (Latenzen addieren sich nicht mehr).
    account_ids = [acc["id"] for acc in accounts_data.get("items", [])]

    async def _count_for(aid: str) -> tuple[str, int]:
        cnt_data = await pb_client.pb_get_as(
            token,
            "/api/collections/emails/records",
            params={
                "filter": f'account={pb_client.pb_quote(aid)} && folder="Sent" && date_sent>={pb_client.pb_quote(cutoff)}',
                "perPage": 1,
                "fields": "id",
            },
        )
        return aid, cnt_data.get("totalItems", 0)

    results = await asyncio.gather(*(_count_for(aid) for aid in account_ids))
    counts: dict[str, int] = dict(results)
    return {"counts": counts, "limit": _SEND_DAILY_LIMIT, "cutoff_utc": cutoff}


@router.patch("/accounts/{account_id}")
async def update_account(account_id: str, payload: UpdateAccountRequest):
    """Update account fields (name, from_name, signature, etc.).

    A11-TODO: noch Admin-Token. User-Token-Migration steht separat auf der
    A11-Phase-2-Liste; Whitelist (UpdateAccountRequest) verhindert seitdem
    schon Schreibzugriff auf IMAP/SMTP-Credentials via diesen Endpoint.
    """
    filtered = payload.model_dump(exclude_unset=True)
    if not filtered:
        raise HTTPException(status_code=400, detail="No valid fields to update")
    return await pb_client.pb_patch(
        f"/api/collections/accounts/records/{account_id}", filtered
    )


# ---------------------------------------------------------------------------
# SMTP-Server
# ---------------------------------------------------------------------------


@router.get("/smtp-servers")
async def get_smtp_servers(token: str = Depends(pb_user_auth.get_user_token)):
    # S1 (2026-05-23): smtp_servers-Rules sind dicht (`password`-Feld).
    # Backend liest via Admin-Token; `fields`-Whitelist filtert zusätzlich.
    return await pb_client.pb_get("/api/collections/smtp_servers/records",
                                  params={"perPage": 50, "sort": "name",
                                          "fields": "id,name,is_default"})


# ---------------------------------------------------------------------------
# Folders
# ---------------------------------------------------------------------------


@router.get("/folders")
async def get_folders(account: str | None = None, token: str = Depends(pb_user_auth.get_user_token)):
    params = {"perPage": 200}
    if account:
        params["filter"] = f'account={pb_client.pb_quote(account)}'
    return await pb_client.pb_get_as(token, "/api/collections/folders/records", params=params)


@router.get("/folders/counts")
async def get_folder_counts(token: str = Depends(pb_user_auth.get_user_token)):
    """Ungelesen-Zähler aller Ordner + Gesamt-Neu-Zähler (is_new=true)."""
    folders = await pb_client.pb_get_as(
        token,
        "/api/collections/folders/records",
        params={"perPage": 200, "fields": "id,account,imap_path,email_folder,unread_count"}
    )
    new_data = await pb_client.pb_get_as(
        token,
        "/api/collections/emails/records",
        params={"filter": "is_new=true", "perPage": 1, "fields": "id"}
    )
    folders["new_count"] = new_data.get("totalItems", 0)
    return folders


# ---------------------------------------------------------------------------
# Xano-Lookup (externe HPA24-Userdaten anhand E-Mail)
# ---------------------------------------------------------------------------


@router.get("/xano/user-info")
async def xano_user_info(email: str):
    """Holt HPA24-Userdaten aus Xano anhand der Absender-E-Mail."""
    if not settings.XANO_API_KEY or not settings.XANO_USER_ROLES_URL:
        return {"userdata": None}
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get(
                settings.XANO_USER_ROLES_URL,
                params={"email": email, "key": settings.XANO_API_KEY},
            )
            resp.raise_for_status()
            return resp.json()
    except Exception as exc:
        logger.warning("Xano-Abfrage fehlgeschlagen für %s: %s", email, exc)
        return {"userdata": None}
