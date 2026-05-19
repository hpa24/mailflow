import logging
import re
import secrets as _secrets
import uuid as _uuid_mod
from contextlib import asynccontextmanager
from urllib.parse import quote as _url_quote

import httpx
from fastapi import FastAPI, BackgroundTasks, HTTPException, Header, Request, UploadFile, File
from pydantic import BaseModel
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response

import asyncio
import json

import pb_client
from pb_client import start_token_refresh, stop_token_refresh
import pb_setup
import rendering
from backfill import run_once_if_needed, rebuild_fts_if_needed, backfill_html_once, run_embed_backfill, get_embed_state
from config import settings
from fts import fts_setup, fts_search, fts_rebuild, fts_delete
from idle_manager import idle_manager, get_sse_queues
from imap_sync import sync_all_accounts, get_sync_status, upsert_contact
from imap_utils import find_imap_folder, resolve_imap_path
from models import HealthResponse, SyncStatusResponse
import spam_filter
from scheduler import start_scheduler, stop_scheduler
from smtp_sender import send_email as smtp_send_email

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)

# Temporärer Speicher für hochgeladene Anhänge (in-memory, max. 25 MB pro Datei)
_temp_uploads: dict[str, dict] = {}  # {temp_id: {filename, content_type, data: bytes}}

# Hintergrund-Sendejobs: {job_id: {status, to, subject}}
_send_jobs: dict[str, dict] = {}


def _pb_safe(q: str) -> str:
    """Entfernt Sonderzeichen für PocketBase-Filter."""
    return q.strip().replace('"', '').replace("'", "").replace("\\", "")


def _email_filters(account: str | None, folder: str | None,
                   is_read: str | None, webhook: str | None = None) -> list[str]:
    """Baut PocketBase-Filter für E-Mail-Abfragen.

    `webhook="true"` → nur Webhook-Versand (Feld nicht leer);
    `webhook="false"` → nur normaler Versand (Feld leer).
    Wird im Sent-Ordner statt is_read als Filter genutzt.
    """
    filters = []
    if account:
        filters.append(f'account="{account}"')
    if folder:
        filters.append(f'folder="{folder}"')
    if is_read == "true":
        filters.append("is_read=true")
    elif is_read == "false":
        filters.append("is_read=false")
    if webhook == "true":
        filters.append('webhook!=""')
    elif webhook == "false":
        filters.append('webhook=""')
    return filters


async def _get_imap_account(account_id: str) -> dict | None:
    """Lädt Account-Daten aus PocketBase. Gibt None zurück wenn nicht gefunden."""
    result = await pb_client.pb_get(
        "/api/collections/accounts/records",
        params={"filter": f'id="{account_id}"', "perPage": 1},
    )
    items = result.get("items", [])
    return items[0] if items else None


async def _update_folder_unread_count(account_id: str, folder: str) -> None:
    """Zählt is_read=false E-Mails für den Ordner und schreibt den Wert in folders.unread_count."""
    count_data = await pb_client.pb_get("/api/collections/emails/records", params={
        "filter": f'account="{account_id}" && folder="{folder}" && is_read=false',
        "perPage": 1,
        "fields": "id",
    })
    new_unread = count_data.get("totalItems", 0)
    folder_data = await pb_client.pb_get("/api/collections/folders/records", params={
        "filter": f'account="{account_id}" && imap_path="{folder}"',
        "perPage": 1,
        "fields": "id",
    })
    folder_items = folder_data.get("items", [])
    if folder_items:
        await pb_client.pb_patch(
            f"/api/collections/folders/records/{folder_items[0]['id']}",
            {"unread_count": new_unread},
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting Mailflow backend...")

    await pb_client.authenticate()
    start_token_refresh()
    await pb_setup.setup_pocketbase_schema(pb_client.get_token())
    fts_setup(settings.PB_DATA_PATH)
    start_scheduler()
    await idle_manager.start()

    if settings.QDRANT_URL:
        try:
            from vector_store import ensure_collection
            await ensure_collection()
            await spam_filter.ensure_spam_collection()
        except Exception as _e:
            logger.warning("Qdrant nicht erreichbar beim Start — Vector Store deaktiviert: %s", _e)

    for coro in (run_once_if_needed(), rebuild_fts_if_needed(), backfill_html_once()):
        task = asyncio.create_task(coro)
        task.add_done_callback(
            lambda t: t.exception() and logger.error("Background-Task fehlgeschlagen: %s", t.exception())
        )

    logger.info("Mailflow backend ready")
    yield
    await idle_manager.stop()
    stop_scheduler()
    stop_token_refresh()
    logger.info("Shutting down Mailflow backend")


def _parse_cors_origins(raw: str) -> list[str]:
    """Parst kommagetrennte CORS-Origins; fügt immer localhost hinzu."""
    origins = [o.strip() for o in raw.split(",") if o.strip()]
    for local in ("http://localhost", "http://127.0.0.1", "null"):
        if local not in origins:
            origins.append(local)
    return origins


app = FastAPI(title="Mailflow API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_parse_cors_origins(settings.CORS_ORIGINS),
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def _api_key_middleware(request: Request, call_next):
    """Erzwingt API-Key-Auth auf allen Routen außer /health.
    Wenn API_KEY in .env leer ist, ist Auth deaktiviert (lokale Entwicklung)."""
    if request.url.path in ("/health", "/config.js") or request.method == "OPTIONS":
        return await call_next(request)
    # Externer Webhook-Send: eigener API-Key pro Webhook im Endpoint selbst
    if request.url.path.startswith("/webhooks/") and request.url.path.endswith("/send"):
        return await call_next(request)
    # Kontakt-Import: akzeptiert zusätzlich X-Import-Key (für externe Quellen wie FileMaker)
    if request.url.path == "/contacts/import" and settings.IMPORT_API_KEY:
        import_key = request.headers.get("X-Import-Key", "")
        if import_key == settings.IMPORT_API_KEY:
            return await call_next(request)
    expected = settings.API_KEY
    if not expected:
        return await call_next(request)
    provided = (
        request.headers.get("X-API-Key")
        or request.query_params.get("key")
        or ""
    )
    if provided != expected:
        return JSONResponse(
            status_code=401,
            content={"detail": "Unauthorized"},
            headers={"Access-Control-Allow-Origin": request.headers.get("origin", "*")},
        )
    return await call_next(request)


@app.exception_handler(Exception)
async def _global_exception_handler(request: Request, exc: Exception):
    """Starlette würde sonst unbehandelte Exceptions ohne CORS-Header zurückgeben."""
    logger.error("Unhandled exception on %s: %s", request.url.path, exc, exc_info=True)
    return JSONResponse(
        status_code=500,
        content={"detail": str(exc)},
        headers={"Access-Control-Allow-Origin": request.headers.get("origin", "*")},
    )


@app.get("/health", response_model=HealthResponse)
async def health():
    return HealthResponse(status="ok")


@app.post("/admin/embed-backfill")
async def start_embed_backfill(background_tasks: BackgroundTasks):
    """Startet den Embed-Backfill aller E-Mails in Qdrant (API-Key-geschützt)."""
    if not settings.QDRANT_URL:
        raise HTTPException(status_code=503, detail="QDRANT_URL nicht konfiguriert")
    state = get_embed_state()
    if state["status"] == "running":
        return {"detail": "Backfill läuft bereits", "state": state}
    background_tasks.add_task(run_embed_backfill)
    return {"detail": "Backfill gestartet — Fortschritt via GET /admin/embed-status"}


@app.get("/admin/embed-status")
async def embed_status():
    """Gibt den aktuellen Fortschritt des Embed-Backfills zurück."""
    return get_embed_state()


@app.get("/admin/embed-search")
async def embed_search(q: str, limit: int = 5):
    """Semantische Testsuche in Qdrant. Gibt Top-N ähnliche Threads zurück."""
    if not settings.QDRANT_URL:
        raise HTTPException(status_code=503, detail="QDRANT_URL nicht konfiguriert")
    from vector_store import search_similar
    results = await search_similar(q, limit=limit)
    return {"query": q, "results": results}


@app.get("/config.js", include_in_schema=False)
async def frontend_config(authorization: str = Header(None, alias="Authorization")):
    from fastapi.responses import PlainTextResponse
    empty = PlainTextResponse("window.MAILFLOW_API_KEY='';", media_type="application/javascript")
    if not settings.API_KEY:
        return empty
    pb_token = authorization[7:] if authorization and authorization.startswith("Bearer ") else None
    if not pb_token:
        return empty
    try:
        async with httpx.AsyncClient(base_url=settings.PB_URL, timeout=5) as client:
            resp = await client.post(
                "/api/collections/users/auth-refresh",
                headers={"Authorization": f"Bearer {pb_token}"},
            )
            if resp.status_code == 200:
                return PlainTextResponse(
                    f"window.MAILFLOW_API_KEY='{settings.API_KEY}';",
                    media_type="application/javascript",
                )
    except Exception:
        pass
    return empty


@app.post("/sync/run")
async def sync_run(background_tasks: BackgroundTasks):
    """Manueller Sync-Trigger für alle Accounts."""
    background_tasks.add_task(sync_all_accounts)
    return {"status": "sync started"}


@app.get("/sync/status", response_model=SyncStatusResponse)
async def sync_status():
    return get_sync_status()


@app.get("/events")
async def sse_events(request: Request):
    """Server-Sent Events — schickt 'new-mail' wenn IDLE neue Nachrichten erkennt."""
    from fastapi.responses import StreamingResponse

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
                    yield ": heartbeat\n\n"
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


_ACCOUNT_SAFE_FIELDS = "id,name,from_email,from_name,signature,color_tag,reply_to_email,imap_host,imap_port,imap_user"

@app.get("/accounts")
async def get_accounts():
    return await pb_client.pb_get("/api/collections/accounts/records",
                                  params={"perPage": 100, "fields": _ACCOUNT_SAFE_FIELDS})


# Tagesversand-Limit von mailbox.org. Wenn sich Stefans Tarif ändert,
# zentral hier anpassen — Frontend liest den Wert aus der Response.
_SEND_DAILY_LIMIT = 10000


@app.get("/accounts/sent-today")
async def accounts_sent_today():
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
    counts: dict[str, int] = {}
    for acc in accounts_data.get("items", []):
        aid = acc["id"]
        cnt_data = await pb_client.pb_get(
            "/api/collections/emails/records",
            params={
                "filter": f'account="{aid}" && folder="Sent" && date_sent>="{cutoff}"',
                "perPage": 1,
                "fields": "id",
            },
        )
        counts[aid] = cnt_data.get("totalItems", 0)
    return {"counts": counts, "limit": _SEND_DAILY_LIMIT, "cutoff_utc": cutoff}


@app.patch("/accounts/{account_id}")
async def update_account(account_id: str, data: dict):
    """Update account fields (name, from_name, signature, etc.)."""
    # Only allow safe fields — never expose IMAP/SMTP credentials via this endpoint
    allowed = {"name", "from_name", "signature", "color_tag", "reply_to_email"}
    filtered = {k: v for k, v in data.items() if k in allowed}
    if not filtered:
        raise HTTPException(status_code=400, detail="No valid fields to update")
    return await pb_client.pb_patch(
        f"/api/collections/accounts/records/{account_id}", filtered
    )


@app.get("/contacts/search")
async def search_contacts(q: str = "", limit: int = 8):
    """Kontaktsuche nach Name oder E-Mail für Autocomplete."""
    if not q or len(q.strip()) < 1:
        return {"items": []}
    safe = _pb_safe(q)
    data = await pb_client.pb_get("/api/collections/contacts/records", params={
        "filter": f'email ~ "{safe}" || name ~ "{safe}"',
        "sort": "-email_count",
        "perPage": limit,
        "fields": "id,email,name,email_count",
    })
    return {"items": data.get("items", [])}


@app.get("/smtp-servers")
async def get_smtp_servers():
    return await pb_client.pb_get("/api/collections/smtp_servers/records",
                                  params={"perPage": 50, "sort": "name"})


@app.get("/folders")
async def get_folders(account: str | None = None):
    params = {"perPage": 200}
    if account:
        params["filter"] = f'account="{account}"'
    return await pb_client.pb_get("/api/collections/folders/records", params=params)


@app.get("/search")
async def search_emails(q: str, account: str | None = None,
                        folder: str | None = None, is_read: str | None = None):
    """Volltextsuche via FTS5-Index mit PocketBase-Fallback."""
    if not q or not q.strip():
        return {"items": [], "totalItems": 0}

    raw = q.strip()
    safe = _pb_safe(raw)
    fts_ids: list[str] = []
    use_fts = False

    # FTS5-Suche: Phrase bei Mehrwort, sonst Einzelwort; Fallback AND-Suche
    phrase = f'"{raw.replace(chr(34), "")}"' if " " in raw else raw
    try:
        fts_ids = fts_search(settings.PB_DATA_PATH, phrase)
        if not fts_ids and " " in raw:
            fts_ids = fts_search(settings.PB_DATA_PATH, raw)
        use_fts = bool(fts_ids)
    except Exception as e:
        logger.warning(f"FTS5 search failed: {e}")

    if use_fts:
        top_ids = fts_ids[:100]
        id_filter = " || ".join(f'id="{i}"' for i in top_ids)
        filters = [f"({id_filter})"]
    else:
        # Fallback: PocketBase-LIKE auf Betreff + Absender (kein body_plain → keine Zitatttreffer)
        logger.info(f"FTS5 empty for '{raw}', falling back to PocketBase LIKE search")
        filters = [f'(subject ~ "{safe}" || from_email ~ "{safe}" || from_name ~ "{safe}")']

    if account:
        filters.append(f'account="{account}"')
    if is_read == "true":
        filters.append("is_read=true")
    elif is_read == "false":
        filters.append("is_read=false")

    fields = ("id,account,folder,message_id,thread_id,from_email,from_name,"
              "reply_to,to_emails,cc_emails,subject,snippet,date_sent,is_read,is_flagged,"
              "is_answered,ai_category,has_attachments,imap_uid,"
              "spam_suggested,spam_score,spam_rule_match")

    data = await pb_client.pb_get("/api/collections/emails/records", params={
        "filter": " && ".join(filters),
        "perPage": 100,
        "sort": "-date_sent",
        "fields": fields,
    })
    items = data.get("items", [])

    # Zusätzlich: im Sent-Ordner auch nach Empfänger (to_emails) suchen,
    # damit "an wen habe ich geschrieben?" funktioniert.
    sent_filters = [f'folder="Sent"', f'to_emails ~ "{safe}"']
    if account:
        sent_filters.append(f'account="{account}"')
    if is_read == "true":
        sent_filters.append("is_read=true")
    elif is_read == "false":
        sent_filters.append("is_read=false")
    sent_data = await pb_client.pb_get("/api/collections/emails/records", params={
        "filter": " && ".join(sent_filters),
        "perPage": 100,
        "sort": "-date_sent",
        "fields": fields,
    })
    seen_ids = {e["id"] for e in items}
    for e in sent_data.get("items", []):
        if e["id"] not in seen_ids:
            items.append(e)
            seen_ids.add(e["id"])
    items.sort(key=lambda e: e.get("date_sent") or "", reverse=True)

    for e in items:
        e["display_thread_id"] = e.get("thread_id") or e.get("message_id") or e["id"]
    return {"items": items, "totalItems": len(items)}


@app.get("/emails")
async def get_emails(account: str | None = None, folder: str | None = None,
                     page: int = 1, limit: int = 50, is_read: str | None = None,
                     webhook: str | None = None):
    filters = _email_filters(account, folder, is_read, webhook)

    params = {
        "perPage": limit,
        "page": page,
        "sort": "-date_sent",
    }
    if filters:
        params["filter"] = " && ".join(filters)

    return await pb_client.pb_get("/api/collections/emails/records", params=params)


_SUBJECT_PREFIX_RE = re.compile(
    r"^\s*(Re|Fwd?|AW|WG|FW|SV|Antw?)\s*:\s*",
    re.IGNORECASE,
)


def _normalize_subject(subject: str) -> str:
    """Strip Re:/Fwd:/AW:/WG: prefixes, return lowercased subject root."""
    s = (subject or "").strip()
    while True:
        s2 = _SUBJECT_PREFIX_RE.sub("", s).strip()
        if s2 == s:
            return s.lower()
        s = s2


def _get_external_participants(email_group: list) -> set[str]:
    """
    Sammelt alle E-Mail-Adressen aus From und Reply-To,
    die NICHT zentrale@hpa24.de sind.
    """
    YOUR_EMAIL = "zentrale@hpa24.de"
    external = set()
    for email in email_group:
        from_email = (email.get("from_email") or "").lower().strip()
        if from_email and from_email != YOUR_EMAIL:
            external.add(from_email)
        # Reply-To auch auswerten (falls vorhanden)
        reply_to = (email.get("reply_to") or "").lower().strip()
        if reply_to and reply_to != YOUR_EMAIL:
            external.add(reply_to)
    return external


def _can_merge(existing: list, members: list) -> bool:
    """
    Prüft, ob zwei Thread-Gruppen zusammengeführt werden dürfen.
    Zwei Gruppen dürfen nur merged werden, wenn sie die gleichen externen
    Teilnehmer haben.

    Wenn beide Gruppen externe Teilnehmer haben, müssen diese identisch sein.
    Wenn eine Gruppe keine externen Teilnehmer hat, lassen wir den Merge zu (neutral).
    """
    external_existing = _get_external_participants(existing)
    external_new = _get_external_participants(members)

    # Wenn beide Gruppen Teilnehmer haben, müssen sie identisch sein.
    # Wenn eine Gruppe leer ist, lassen wir den Merge zu (neutral).
    if external_existing and external_new:
        return external_existing == external_new

    return True  # Einer von beiden ist leer, also kein direkter Konflikt


@app.get("/folders/counts")
async def get_folder_counts():
    """Ungelesen-Zähler aller Ordner + Gesamt-Neu-Zähler (is_new=true)."""
    folders = await pb_client.pb_get(
        "/api/collections/folders/records",
        params={"perPage": 200, "fields": "id,account,imap_path,email_folder,unread_count"}
    )
    new_data = await pb_client.pb_get(
        "/api/collections/emails/records",
        params={"filter": "is_new=true", "perPage": 1, "fields": "id"}
    )
    folders["new_count"] = new_data.get("totalItems", 0)
    return folders


@app.get("/emails/threaded")
async def get_emails_threaded(account: str | None = None, folder: str | None = None,
                              page: int = 1, limit: int = 100,
                              is_read: str | None = None,
                              webhook: str | None = None):
    """
    Returns emails sorted by thread: newest thread first, within thread oldest-first.
    Threads split by Fwd: are merged when normalized subject + participants overlap.
    """
    filters = _email_filters(account, folder, is_read, webhook)

    fields = ("id,account,folder,message_id,thread_id,in_reply_to,from_email,"
              "from_name,reply_to,to_emails,subject,snippet,date_sent,is_read,is_flagged,"
              "is_answered,ai_category,has_attachments,imap_uid,"
              "spam_suggested,spam_score,spam_rule_match")

    params = {
        "perPage": limit,
        "page": page,
        "sort": "-date_sent",
        "fields": fields,
    }
    if filters:
        params["filter"] = " && ".join(filters)

    data = await pb_client.pb_get("/api/collections/emails/records", params=params)
    emails = data.get("items", [])
    total_items = data.get("totalItems", 0)
    total_pages = data.get("totalPages", 1)

    # --- Pass 1: Group by thread_id ---
    thread_map: dict[str, list] = {}
    for email in emails:
        tid = email.get("thread_id") or email.get("message_id") or email["id"]
        email["_tid"] = tid
        if tid not in thread_map:
            thread_map[tid] = []
        thread_map[tid].append(email)

    # Sort each thread newest-first
    for members in thread_map.values():
        members.sort(key=lambda e: e.get("date_sent") or "", reverse=True)

    # --- Pass 2: Merge threads split by Fwd: ---
    # Two thread groups merge if: normalized subject matches AND senders overlap.
    merged: list[list] = []
    norm_index: dict[str, int] = {}  # norm_subject → index in merged list

    for members in thread_map.values():
        if not members:
            continue
        norm = _normalize_subject(members[0].get("subject", ""))

        if len(norm) > 1 and norm in norm_index:
            existing = merged[norm_index[norm]]
            if _can_merge(existing, members):
                # Merge: unified display_thread_id, re-sort newest-first
                root_tid = existing[0].get("display_thread_id") or existing[0]["_tid"]
                existing.extend(members)
                existing.sort(key=lambda e: e.get("date_sent") or "", reverse=True)
                for e in existing:
                    e["display_thread_id"] = root_tid
                continue

        # No merge: display_thread_id = own thread_id
        root_tid = members[0]["_tid"]
        for e in members:
            e["display_thread_id"] = root_tid
        merged.append(members)
        if len(norm) > 1:
            norm_index[norm] = len(merged) - 1

    # Sort merged groups by newest email descending (members[0] is now newest)
    sorted_threads = sorted(
        merged,
        key=lambda members: members[0].get("date_sent") or "",
        reverse=True,
    )
    sorted_emails = [email for thread in sorted_threads for email in thread]

    return {
        "items": sorted_emails,
        "totalItems": total_items,
        "hasMore": page < total_pages,
    }


@app.get("/emails/by-sender")
async def get_emails_by_sender(account: str | None = None, folder: str | None = None,
                               page: int = 1, limit: int = 100,
                               is_read: str | None = None,
                               webhook: str | None = None):
    """
    Returns emails grouped by sender: most-recent-contact first,
    within each sender group newest email first.
    """
    filters = _email_filters(account, folder, is_read, webhook)

    fields = ("id,account,folder,message_id,thread_id,in_reply_to,from_email,"
              "from_name,reply_to,to_emails,subject,snippet,date_sent,is_read,is_flagged,"
              "is_answered,ai_category,has_attachments,imap_uid,"
              "spam_suggested,spam_score,spam_rule_match")

    params = {
        "perPage": limit,
        "page": page,
        "sort": "-date_sent",
        "fields": fields,
    }
    if filters:
        params["filter"] = " && ".join(filters)

    data = await pb_client.pb_get("/api/collections/emails/records", params=params)
    emails = data.get("items", [])
    total_items = data.get("totalItems", 0)
    total_pages = data.get("totalPages", 1)

    # Group by from_email — or reply_to if set (e.g. contact form emails)
    sender_map: dict[str, list] = {}
    sender_order: list[str] = []  # preserves first-seen order
    for email in emails:
        reply_to = (email.get("reply_to") or "").lower().strip()
        sender = reply_to if reply_to else (email.get("from_email") or "").lower().strip()
        email["display_thread_id"] = sender
        if sender not in sender_map:
            sender_map[sender] = []
            sender_order.append(sender)
        sender_map[sender].append(email)

    # Sort each sender group newest-first
    for members in sender_map.values():
        members.sort(key=lambda e: e.get("date_sent") or "", reverse=True)

    # Sort sender groups by newest email descending
    sorted_senders = sorted(
        sender_map.values(),
        key=lambda members: members[0].get("date_sent") or "",
        reverse=True,
    )
    sorted_emails = [email for group in sorted_senders for email in group]

    return {
        "items": sorted_emails,
        "totalItems": total_items,
        "hasMore": page < total_pages,
    }


def _sse_notify_all(event: dict) -> None:
    """Schickt ein Event an alle verbundenen SSE-Clients."""
    for q in list(get_sse_queues()):
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            pass


async def _finalize_for_recipient(to_field: str, subject: str,
                                  body: str, body_html: str) -> tuple[str, str, str]:
    """Phase-2-Rendering vor SMTP-Versand:
    - Bei einem Empfänger: Kontakt-Lookup in DB, {{name}}/{{email}} ersetzen.
    - Bei mehreren oder unbekanntem Empfänger: kein Kontakt-Replace.
    - Anschließend strip_unresolved auf alle Felder, damit Platzhalter nicht
      sichtbar in der Mail landen.
    Variablen/Snippets werden nochmal aufgelöst (idempotent für bereits
    aufgelöste Stellen)."""
    emails = re.findall(r'[\w.+-]+@[\w.-]+\.\w+', to_field or "")
    contact = None
    if len(emails) == 1:
        email_addr = emails[0].lower()
        try:
            resp = await pb_client.pb_get(
                "/api/collections/contacts/records",
                params={"filter": f'email="{email_addr}"', "perPage": 1},
            )
            items = resp.get("items", [])
            if items:
                contact = {"name": items[0].get("name") or "", "email": email_addr}
            else:
                contact = {"name": "", "email": email_addr}
        except Exception as exc:
            logger.warning("Kontakt-Lookup fehlgeschlagen für %s: %s", email_addr, exc)
            contact = {"name": "", "email": email_addr}

    try:
        snippets = await rendering.load_snippets_map()
        variables = await rendering.load_variables_map()
    except Exception as exc:
        logger.warning("Rendering-Maps konnten nicht geladen werden: %s", exc)
        snippets, variables = {}, {}

    rendered_subject = rendering.render_full(subject or "", snippets, variables, None, contact)
    rendered_body = rendering.render_full(body or "", snippets, variables, None, contact) if body else body
    rendered_html = rendering.render_full(body_html or "", snippets, variables, None, contact) if body_html else body_html

    return (
        rendering.strip_unresolved(rendered_subject),
        rendering.strip_unresolved(rendered_body) if body else body,
        rendering.strip_unresolved(rendered_html) if body_html else body_html,
    )


async def _do_send_job(job_id: str, data: dict, attachments: list) -> None:
    """Führt den SMTP-Versand im Hintergrund aus und meldet das Ergebnis via SSE."""
    to      = data["to"]
    subject = data["subject"]
    cc      = data.get("cc", "")
    from_account = data["from_account"]
    smtp_server  = data["smtp_server"]
    body         = data.get("body", "")
    body_html    = data.get("body_html", "")

    # Phase-2-Rendering + unaufgelöste Platzhalter entfernen
    try:
        subject, body, body_html = await _finalize_for_recipient(to, subject, body, body_html)
        data["subject"] = subject
    except Exception as exc:
        logger.warning("Phase-2-Render fehlgeschlagen (job=%s): %s", job_id, exc)

    try:
        await smtp_send_email(
            smtp_server_id=smtp_server,
            from_account_id=from_account,
            to=to,
            cc=cc,
            subject=subject,
            body=body,
            body_html=body_html,
            quote=data.get("quote", ""),
            quote_html=data.get("quote_html", ""),
            attachments=attachments or None,
        )
    except Exception as exc:
        logger.error("SMTP-Versand fehlgeschlagen (job=%s): %s", job_id, exc)
        _send_jobs[job_id]["status"] = "error"
        _sse_notify_all({"type": "send-result", "job_id": job_id,
                         "success": False, "to": to, "subject": subject,
                         "error": str(exc)})
        return

    # Temporäre Uploads bereinigen
    for aid in data.get("attachment_ids") or []:
        _temp_uploads.pop(aid, None)

    # Entwurf löschen falls vorhanden
    draft_id = data.get("draft_id")
    if draft_id:
        try:
            await pb_client.pb_delete(f"/api/collections/emails/records/{draft_id}")
        except Exception:
            pass

    # Empfänger in Contacts upserten
    _m = re.search(r'[\w.+-]+@[\w.-]+\.\w+', to)
    if _m:
        _name_m = re.match(r'^(.+?)\s*<', to.strip())
        _to_name = _name_m.group(1).strip().strip('"') if _name_m else ""
        from datetime import datetime, timezone as _tz
        asyncio.create_task(upsert_contact(_m.group(0).lower(), _to_name,
                                           datetime.now(_tz.utc).strftime("%Y-%m-%d %H:%M:%S")))

    # Ursprungs-E-Mail als beantwortet markieren
    in_reply_to_email_id = data.get("in_reply_to_email_id")
    if in_reply_to_email_id:
        try:
            original = await pb_client.pb_get(
                f"/api/collections/emails/records/{in_reply_to_email_id}"
            )
            if original.get("account") == from_account:
                original = await pb_client.pb_patch(
                    f"/api/collections/emails/records/{in_reply_to_email_id}",
                    {"is_answered": True},
                )
                asyncio.create_task(_imap_set_answered_safe(original))
            else:
                logger.warning("IDOR-Versuch: in_reply_to_email_id %s gehört nicht zu Account %s",
                               in_reply_to_email_id, from_account)
        except Exception as exc:
            logger.warning("is_answered konnte nicht gesetzt werden für %s: %s",
                           in_reply_to_email_id, exc)

    _send_jobs[job_id]["status"] = "done"
    logger.info("Sendejob %s abgeschlossen: to=%s subject=%s", job_id, to, subject)
    _sse_notify_all({"type": "send-result", "job_id": job_id,
                     "success": True, "to": to, "subject": subject})


@app.post("/emails/send")
async def send_email_endpoint(data: dict):
    """Startet den E-Mail-Versand im Hintergrund und gibt sofort eine Job-ID zurück."""
    to           = (data.get("to") or "").strip()
    from_account = (data.get("from_account") or "").strip()
    smtp_server  = (data.get("smtp_server") or "").strip()
    subject      = (data.get("subject") or "").strip()

    if not to:
        raise HTTPException(status_code=400, detail="Empfänger (to) fehlt")
    if not from_account:
        raise HTTPException(status_code=400, detail="Absender-Account fehlt")
    if not smtp_server:
        raise HTTPException(status_code=400, detail="SMTP-Server fehlt")

    data["to"] = to
    data["from_account"] = from_account
    data["smtp_server"]  = smtp_server
    data["subject"]      = subject

    attachment_ids = data.get("attachment_ids") or []
    attachments = [_temp_uploads[aid] for aid in attachment_ids if aid in _temp_uploads]

    job_id = str(_uuid_mod.uuid4())
    _send_jobs[job_id] = {"status": "sending", "to": to, "subject": subject}
    logger.info("Sendejob %s gestartet: to=%s subject=%s", job_id, to, subject)

    asyncio.create_task(_do_send_job(job_id, data, attachments))

    return {"job_id": job_id, "status": "sending"}


_EMAIL_RE = re.compile(r"^[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}$")


async def _do_bulk_send(bulk_id: str, jobs: list[dict], base_data: dict,
                       attachments: list, delay_seconds: float) -> None:
    """Startet die Einzel-Sendejobs sequentiell mit ``delay_seconds`` Abstand.

    Jeder Sub-Job nutzt ``_do_send_job`` wie ein normaler Versand — d.h. die
    bestehenden SSE-Events (``send-result``) feuern pro Empfänger.
    """
    logger.info("Bulk-Send %s gestartet: %d Empfänger, Abstand %.1fs",
                bulk_id, len(jobs), delay_seconds)
    for idx, job in enumerate(jobs):
        if idx > 0:
            await asyncio.sleep(delay_seconds)
        job_id = job["job_id"]
        recipient = job["to"]

        # Pro-Empfänger-Kopie: nur der erste Sub-Job darf Draft löschen und
        # das Original als beantwortet markieren. Attachments-IDs werden in
        # allen Sub-Jobs entfernt, damit der erste nicht die Datei-Refs
        # für die nachfolgenden killt — Bulk-Cleanup übernehmen wir am Ende.
        sub_data = dict(base_data)
        sub_data["to"] = recipient
        sub_data["attachment_ids"] = []
        if idx > 0:
            sub_data.pop("draft_id", None)
            sub_data.pop("in_reply_to_email_id", None)

        _send_jobs[job_id]["status"] = "sending"
        # bewusst nicht awaiten: nächste Mail soll nach delay_seconds starten,
        # unabhängig davon ob der vorherige Sub-Job schon fertig ist.
        asyncio.create_task(_do_send_job(job_id, sub_data, attachments))

    # Attachments einmalig am Ende aus den Temp-Uploads entfernen
    for aid in base_data.get("_bulk_attachment_ids") or []:
        _temp_uploads.pop(aid, None)


@app.post("/emails/bulk-send")
async def bulk_send_endpoint(data: dict):
    """Versendet dieselbe E-Mail einzeln an viele Empfänger mit Zeitversatz.

    Body wie ``/emails/send``, zusätzlich:
      - ``recipients``: list[str] — eine E-Mail-Adresse pro Eintrag
      - ``delay_seconds``: float (default 5.0) — Abstand zwischen den Mails
    """
    recipients_raw = data.get("recipients") or []
    if not isinstance(recipients_raw, list) or not recipients_raw:
        raise HTTPException(status_code=400, detail="recipients fehlt oder leer")

    # Adressen normalisieren, validieren, deduplizieren (Reihenfolge erhalten)
    seen: set[str] = set()
    recipients: list[str] = []
    invalid: list[str] = []
    for raw in recipients_raw:
        addr = (raw or "").strip()
        if not addr:
            continue
        # Erlaubt "Name <addr>" oder reines "addr" — wir prüfen nur die addr
        m = re.search(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", addr)
        if not m or not _EMAIL_RE.match(m.group(0)):
            invalid.append(addr)
            continue
        key = m.group(0).lower()
        if key in seen:
            continue
        seen.add(key)
        recipients.append(addr)

    if invalid:
        raise HTTPException(status_code=400,
                            detail=f"Ungültige Adressen: {', '.join(invalid[:5])}")
    if not recipients:
        raise HTTPException(status_code=400, detail="Keine gültigen Empfänger")

    from_account = (data.get("from_account") or "").strip()
    smtp_server  = (data.get("smtp_server") or "").strip()
    subject      = (data.get("subject") or "").strip()
    if not from_account:
        raise HTTPException(status_code=400, detail="Absender-Account fehlt")
    if not smtp_server:
        raise HTTPException(status_code=400, detail="SMTP-Server fehlt")

    try:
        delay_seconds = float(data.get("delay_seconds", 5.0))
    except (TypeError, ValueError):
        delay_seconds = 5.0
    delay_seconds = max(0.0, min(delay_seconds, 300.0))

    attachment_ids = data.get("attachment_ids") or []
    attachments = [_temp_uploads[aid] for aid in attachment_ids if aid in _temp_uploads]

    bulk_id = str(_uuid_mod.uuid4())
    jobs: list[dict] = []
    for recipient in recipients:
        job_id = str(_uuid_mod.uuid4())
        _send_jobs[job_id] = {
            "status": "queued",
            "to": recipient,
            "subject": subject,
            "bulk_id": bulk_id,
        }
        jobs.append({"job_id": job_id, "to": recipient})

    base_data = dict(data)
    base_data["from_account"] = from_account
    base_data["smtp_server"]  = smtp_server
    base_data["subject"]      = subject
    base_data["cc"]           = ""  # CC ergibt bei N Einzel-Mails keinen Sinn
    base_data.pop("to", None)
    base_data["_bulk_attachment_ids"] = list(attachment_ids)

    logger.info("Bulk-Send angelegt: bulk=%s, n=%d, delay=%.1fs, subject=%s",
                bulk_id, len(jobs), delay_seconds, subject)

    asyncio.create_task(_do_bulk_send(bulk_id, jobs, base_data,
                                      attachments, delay_seconds))

    return {
        "bulk_id": bulk_id,
        "jobs": jobs,
        "delay_seconds": delay_seconds,
    }


@app.post("/emails/draft")
async def create_draft(data: dict):
    """Erstellt einen neuen Entwurf in PocketBase."""
    import uuid
    from datetime import datetime, timezone

    account_id = data.get("from_account", "")
    if not account_id:
        raise HTTPException(status_code=400, detail="Account fehlt")

    # Account-Daten laden, um from_email zu bekommen
    try:
        acc = await pb_client.pb_get(f"/api/collections/accounts/records/{account_id}")
        from_email = acc.get("from_email", "")
        from_name = acc.get("from_name", "")
    except Exception:
        from_email = ""
        from_name = ""

    to = data.get("to", "")
    subject = data.get("subject", "") or "(Kein Betreff)"
    body = data.get("body", "")
    body_html = data.get("body_html", "")
    quote = data.get("quote", "")

    full_body = body
    if quote:
        full_body += "\n\n" + quote

    draft = {
        "account": account_id,
        "folder": "Drafts",
        "message_id": f"<draft-{uuid.uuid4()}@mailflow>",
        "subject": subject,
        "body_plain": full_body,
        "body_html": body_html,
        "snippet": (full_body[:120] if full_body else ""),
        "from_email": from_email,
        "from_name": from_name,
        "to_emails": [to] if to else [],
        "is_read": True,
        "date_sent": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
    }
    return await pb_client.pb_post("/api/collections/emails/records", draft)


@app.post("/emails/draft/{draft_id}/sync")
async def sync_draft_to_imap(draft_id: str):
    """APPENDet einen Entwurf in den IMAP-Drafts-Ordner."""
    import email.utils
    from datetime import datetime, timezone
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText

    draft = await pb_client.pb_get(f"/api/collections/emails/records/{draft_id}")
    account_id = draft.get("account")
    if not account_id:
        raise HTTPException(status_code=400, detail="Kein Account am Entwurf")

    acc = await pb_client.pb_get(f"/api/collections/accounts/records/{account_id}")

    # MIME-Nachricht aufbauen
    msg = MIMEMultipart("mixed")
    body_text = draft.get("body_plain") or ""
    msg.attach(MIMEText(body_text, "plain", "utf-8"))

    from_email = acc.get("from_email", "")
    from_name  = acc.get("from_name", "")
    to_emails  = draft.get("to_emails") or []
    to_str     = ", ".join(to_emails) if isinstance(to_emails, list) else str(to_emails)

    msg["From"]       = email.utils.formataddr((from_name, from_email)) if from_name else from_email
    msg["To"]         = to_str
    msg["Subject"]    = draft.get("subject") or ""
    msg["Date"]       = email.utils.formatdate(localtime=True)
    msg["Message-ID"] = draft.get("message_id") or email.utils.make_msgid()

    msg_bytes = msg.as_bytes()
    message_id = draft.get("message_id") or msg["Message-ID"]

    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(None, _imap_append_draft, acc, msg_bytes, message_id)
    except Exception as exc:
        logger.error("IMAP Draft-APPEND fehlgeschlagen: %s", exc)
        raise HTTPException(status_code=502, detail=f"IMAP-Fehler: {exc}")

    return {"synced": True}


def _imap_append_draft(acc: dict, msg_bytes: bytes, message_id: str = None) -> None:
    """Blockierender IMAP APPEND in den Drafts-Ordner.
    Löscht vorher eine alte Version mit gleicher Message-ID (verhindert Duplikate)."""
    from imapclient import IMAPClient
    from datetime import datetime, timezone

    host     = acc.get("imap_host")
    port     = int(acc.get("imap_port") or 993)
    user     = acc.get("imap_user")
    password = acc.get("imap_pass")

    if not all([host, user, password]):
        raise ValueError("Unvollständige IMAP-Zugangsdaten")

    with IMAPClient(host, port=port, ssl=True) as srv:
        srv.login(user, password)
        drafts_folder = find_imap_folder(srv, [b"\\Drafts", b"\\Draft"], ["Drafts", "Draft", "Entwürfe", "INBOX.Drafts"])
        if not drafts_folder:
            raise ValueError("Kein Drafts-Ordner auf dem IMAP-Server gefunden")

        srv.select_folder(drafts_folder)

        # Alte Version per Message-ID suchen und löschen
        if message_id:
            # Message-ID ohne spitze Klammern für die Suche
            mid_clean = message_id.strip("<>")
            try:
                old_uids = srv.search(["HEADER", "Message-ID", mid_clean])
                if old_uids:
                    srv.delete_messages(old_uids)
                    srv.expunge()
                    logger.info("Alte Draft-Version(en) gelöscht: UIDs %s", old_uids)
            except Exception as e:
                logger.warning("Konnte alte Draft-Version nicht löschen: %s", e)

        srv.append(
            drafts_folder,
            msg_bytes,
            flags=[b"\\Draft", b"\\Seen"],
            msg_time=datetime.now(timezone.utc),
        )
        logger.info("Draft in IMAP-Ordner '%s' gespeichert.", drafts_folder)




@app.patch("/emails/draft/{draft_id}")
async def update_draft(draft_id: str, data: dict):
    """Aktualisiert einen bestehenden Entwurf."""
    to = data.get("to", "")
    subject = data.get("subject", "") or "(Kein Betreff)"
    body = data.get("body", "")
    body_html = data.get("body_html", "")
    quote = data.get("quote", "")

    full_body = body
    if quote:
        full_body += "\n\n" + quote

    patch = {
        "subject": subject,
        "body_plain": full_body,
        "body_html": body_html,
        "snippet": (full_body[:120] if full_body else ""),
        "to_emails": [to] if to else [],
    }
    return await pb_client.pb_patch(
        f"/api/collections/emails/records/{draft_id}", patch
    )


@app.get("/emails/{email_id}/attachments")
async def get_email_attachments(email_id: str):
    """Listet alle Anhänge einer E-Mail aus PocketBase."""
    return await pb_client.pb_get("/api/collections/attachments/records", params={
        "filter": f'email="{email_id}"',
        "perPage": 50,
        "sort": "part_id",
    })


@app.get("/attachments/{attachment_id}/download")
async def download_attachment(attachment_id: str):
    """Lädt einen Anhang von IMAP herunter und streamt ihn."""
    att = await pb_client.pb_get(f"/api/collections/attachments/records/{attachment_id}")
    email_id = att.get("email")
    part_index = int(att.get("part_id") or 0)
    filename = att.get("filename") or "anhang"
    mime_type = att.get("mime_type") or "application/octet-stream"

    email_rec = await pb_client.pb_get(f"/api/collections/emails/records/{email_id}")
    account_id = email_rec.get("account")
    folder = email_rec.get("folder", "INBOX")
    imap_uid = email_rec.get("imap_uid")

    if not imap_uid:
        raise HTTPException(status_code=404, detail="E-Mail hat keine IMAP-UID")

    acc = await _get_imap_account(account_id)
    if not acc:
        raise HTTPException(status_code=404, detail="Account nicht gefunden")

    loop = asyncio.get_running_loop()
    try:
        payload = await loop.run_in_executor(
            None, _imap_fetch_attachment, acc, folder, int(imap_uid), part_index
        )
    except Exception as exc:
        logger.error("Anhang-Download fehlgeschlagen: %s", exc)
        raise HTTPException(status_code=502, detail=f"IMAP-Fehler: {exc}")

    # RFC 5987-kodierter Dateiname für korrekte Unicode-Unterstützung
    encoded_name = _url_quote(filename, safe="")
    return Response(
        content=payload,
        media_type=mime_type,
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{encoded_name}",
            "Access-Control-Allow-Origin": "*",
        },
    )


def _imap_fetch_attachment(acc: dict, folder: str, imap_uid: int, part_index: int) -> bytes:
    """Blockierende IMAP-Verbindung zum Download eines Anhangs."""
    from imapclient import IMAPClient
    from mime_parser import get_attachment_payload

    host = acc.get("imap_host")
    port = int(acc.get("imap_port") or 993)
    user = acc.get("imap_user")
    password = acc.get("imap_pass")

    with IMAPClient(host, port=port, ssl=True) as srv:
        srv.login(user, password)
        srv.select_folder(folder, readonly=True)
        data = srv.fetch([imap_uid], [b"BODY[]"])
        raw = data.get(imap_uid, {}).get(b"BODY[]") or b""

    payload, _, _ = get_attachment_payload(raw, part_index)
    return payload


def _imap_fetch_inline_cid(acc: dict, folder: str, imap_uid: int, cid: str) -> tuple[bytes, str]:
    """Blockierende IMAP-Verbindung zum Abruf eines Inline-Bildes per Content-ID."""
    from imapclient import IMAPClient
    from mime_parser import get_inline_part_by_cid

    host = acc.get("imap_host")
    port = int(acc.get("imap_port") or 993)
    user = acc.get("imap_user")
    password = acc.get("imap_pass")

    with IMAPClient(host, port=port, ssl=True) as srv:
        srv.login(user, password)
        srv.select_folder(folder, readonly=True)
        data = srv.fetch([imap_uid], [b"BODY[]"])
        raw = data.get(imap_uid, {}).get(b"BODY[]") or b""

    return get_inline_part_by_cid(raw, cid)


@app.get("/emails/{email_id}/inline")
async def get_inline_image(email_id: str, cid: str):
    """Gibt ein Inline-Bild (cid:-Referenz) aus einer E-Mail zurück."""
    email_rec = await pb_client.pb_get(f"/api/collections/emails/records/{email_id}")
    account_id = email_rec.get("account")
    folder = email_rec.get("folder", "INBOX")
    imap_uid = email_rec.get("imap_uid")

    if not imap_uid:
        raise HTTPException(status_code=404, detail="E-Mail hat keine IMAP-UID")

    acc = await _get_imap_account(account_id)
    if not acc:
        raise HTTPException(status_code=404, detail="Account nicht gefunden")

    loop = asyncio.get_running_loop()
    try:
        payload, mime_type = await loop.run_in_executor(
            None, _imap_fetch_inline_cid, acc, folder, int(imap_uid), cid
        )
    except Exception as exc:
        logger.error("Inline-Bild-Download fehlgeschlagen: %s", exc)
        raise HTTPException(status_code=502, detail=f"IMAP-Fehler: {exc}")

    if not payload:
        raise HTTPException(status_code=404, detail="Inline-Bild nicht gefunden")

    return Response(
        content=payload,
        media_type=mime_type,
        headers={"Cache-Control": "private, max-age=3600"},
    )


@app.post("/attachments/upload")
async def upload_attachment(file: UploadFile = File(...)):
    """Lädt eine Datei temporär in den Arbeitsspeicher (max. 25 MB)."""
    MAX_SIZE = 25 * 1024 * 1024
    data = await file.read()
    if len(data) > MAX_SIZE:
        raise HTTPException(status_code=413, detail="Datei zu groß (max. 25 MB)")
    temp_id = str(_uuid_mod.uuid4())
    _temp_uploads[temp_id] = {
        "filename": file.filename or "anhang",
        "content_type": file.content_type or "application/octet-stream",
        "data": data,
    }
    logger.info("Temporärer Upload: %s (%d bytes)", file.filename, len(data))
    return {
        "id": temp_id,
        "filename": file.filename or "anhang",
        "size": len(data),
        "content_type": file.content_type,
    }


@app.delete("/attachments/upload/{temp_id}")
async def delete_upload(temp_id: str):
    """Entfernt einen temporären Upload."""
    _temp_uploads.pop(temp_id, None)
    return {"deleted": temp_id}


@app.get("/emails/{email_id}")
async def get_email(email_id: str, background_tasks: BackgroundTasks):
    email = await pb_client.pb_get(f"/api/collections/emails/records/{email_id}")
    if email.get("is_new"):
        background_tasks.add_task(
            pb_client.pb_patch,
            f"/api/collections/emails/records/{email_id}",
            {"is_new": False},
        )
    return email


@app.patch("/emails/{email_id}/category")
async def set_category(email_id: str, data: dict):
    """Setzt die KI-Kategorie einer E-Mail."""
    category = data.get("ai_category", "")
    valid = {"focus", "quick-reply", "office", "info-trash", ""}
    if category not in valid:
        raise HTTPException(status_code=400, detail=f"Ungültige Kategorie: {category}")
    result = await pb_client.pb_patch(
        f"/api/collections/emails/records/{email_id}",
        {"ai_category": category},
    )
    return result


class BulkEmailRef(BaseModel):
    id: str
    account: str = ""
    folder: str = ""
    imap_uid: int | None = None


class BulkReadRequest(BaseModel):
    emails: list[BulkEmailRef]
    is_read: bool = True


@app.patch("/emails/bulk/read")
async def bulk_mark_read(req: BulkReadRequest):
    """Markiert mehrere E-Mails als gelesen/ungelesen.
    PocketBase: parallel; IMAP: eine Verbindung pro Account+Ordner."""
    if not req.emails:
        return {"updated": 0}

    from collections import defaultdict
    from imapclient import IMAPClient
    import concurrent.futures

    emails = [e.model_dump() for e in req.emails]

    # 1. PocketBase-Updates parallel (keine vorherige Abfrage nötig)
    await asyncio.gather(*[
        pb_client.pb_patch(f"/api/collections/emails/records/{e['id']}", {"is_read": req.is_read})
        for e in emails
    ])

    # 3. Betroffene Ordner-Ungelesen-Zähler in folders-Collection aktualisieren
    affected_groups: dict[tuple, list] = defaultdict(list)
    for e in emails:
        uid = e.get("imap_uid")
        if e.get("account") and uid is not None and uid != 0:
            affected_groups[(e["account"], e["folder"])].append(uid)
        elif not uid or uid == 0:
            logger.warning("bulk_mark_read: E-Mail %s hat keine imap_uid — nur PocketBase aktualisiert", e.get("id"))

    await asyncio.gather(*[
        _update_folder_unread_count(account_id, folder)
        for account_id, folder in affected_groups.keys()
    ])

    # 4. Account-Daten vorab laden (damit kein asyncio.run im Thread nötig)
    account_ids = {account_id for account_id, _ in affected_groups.keys()}
    accounts: dict[str, dict] = {}
    for account_id in account_ids:
        acc = await _get_imap_account(account_id)
        if acc:
            accounts[account_id] = acc

    # 5. IMAP: eine Verbindung pro (Account, Ordner), blocking im Thread-Pool
    def _imap_bulk_set(acc: dict, folder: str, uids: list, is_read: bool) -> None:
        logger.info("IMAP bulk_set: folder='%s' uids=%s is_read=%s", folder, uids, is_read)
        with IMAPClient(acc["imap_host"], port=int(acc.get("imap_port") or 993), ssl=True) as srv:
            srv.login(acc["imap_user"], acc["imap_pass"])
            try:
                srv.select_folder(folder)
            except Exception as ex:
                logger.warning("IMAP bulk_set: select_folder('%s') fehlgeschlagen: %s — versuche INBOX", folder, ex)
                srv.select_folder("INBOX")
            if is_read:
                srv.set_flags(uids, [b"\\Seen"])
            else:
                srv.remove_flags(uids, [b"\\Seen"])
            logger.info("IMAP bulk_set: %d UIDs in '%s' auf is_read=%s gesetzt", len(uids), folder, is_read)

    loop = asyncio.get_running_loop()
    with concurrent.futures.ThreadPoolExecutor() as pool:
        futs = [
            loop.run_in_executor(pool, _imap_bulk_set, accounts[account_id], folder, uids, req.is_read)
            for (account_id, folder), uids in affected_groups.items()
            if account_id in accounts
        ]
        results = await asyncio.gather(*futs, return_exceptions=True)
        for r in results:
            if isinstance(r, Exception):
                logger.warning("IMAP bulk-read failed: %s", r)

    return {"updated": len(emails)}


@app.patch("/emails/{email_id}/read")
async def mark_read(email_id: str, is_read: bool = True):
    # PocketBase aktualisieren
    result = await pb_client.pb_patch(
        f"/api/collections/emails/records/{email_id}",
        {"is_read": is_read}
    )
    # Auch auf dem IMAP-Server markieren
    try:
        await _imap_set_read(result, is_read)
    except Exception as e:
        logger.warning(f"IMAP mark-read failed for {email_id}: {e}")
    # Ordner-Zähler aktualisieren
    try:
        await _update_folder_unread_count(result["account"], result["folder"])
    except Exception as e:
        logger.warning(f"folder unread_count update failed for {email_id}: {e}")
    return result


@app.post("/emails/{email_id}/spam")
async def move_to_spam(email_id: str, block_sender: bool = False, block_domain: bool = False):
    """Verschiebt E-Mail in den Spam-Ordner (IMAP + PocketBase) und lernt das Sample."""
    email = await pb_client.pb_get(f"/api/collections/emails/records/{email_id}")
    source_folder = email.get("folder", "INBOX")
    new_folder, new_uid = "Spam", None
    try:
        new_folder, new_uid = await _imap_move_to_spam(email)
    except Exception as e:
        logger.warning(f"IMAP spam move failed for {email_id}: {e}")
    patch = {"folder": new_folder or "Spam", "spam_suggested": False, "is_read": True}
    if new_uid:
        patch["imap_uid"] = new_uid
    try:
        await pb_client.pb_patch(f"/api/collections/emails/records/{email_id}", patch)
    except Exception as e:
        logger.warning(f"move_to_spam: pb_patch fehlgeschlagen (wahrscheinlich Race mit imap_sync): {e}")
    try:
        await _update_folder_unread_count(email["account"], source_folder)
    except Exception as e:
        logger.warning(f"folder unread_count update failed after spam move {email_id}: {e}")

    await spam_filter.add_spam_sample({**email, "id": email_id})
    blocked = None
    if block_sender or block_domain:
        rule = await spam_filter.add_blocklist_entry(
            email.get("account") or "",
            email.get("from_email") or "",
            block_domain=block_domain,
        )
        if rule:
            blocked = {"rule_id": rule.get("id"), "match_type": rule.get("match_type"), "pattern": rule.get("pattern")}

    return {"moved_to": new_folder, "blocked": blocked}


@app.post("/emails/{email_id}/unspam")
async def unspam_email(email_id: str):
    """Holt eine Mail aus dem Spam-Ordner zurück nach INBOX und entfernt das Spam-Sample."""
    email = await pb_client.pb_get(f"/api/collections/emails/records/{email_id}")
    source_folder = email.get("folder", "Spam")
    new_uid = None
    try:
        new_uid = await _imap_move(email, "INBOX")
    except Exception as e:
        logger.warning(f"IMAP unspam move failed for {email_id}: {e}")
    patch = {"folder": "INBOX", "spam_suggested": False, "spam_score": None, "spam_rule_match": ""}
    if new_uid:
        patch["imap_uid"] = new_uid
    try:
        await pb_client.pb_patch(f"/api/collections/emails/records/{email_id}", patch)
    except Exception as e:
        logger.warning(f"unspam: pb_patch fehlgeschlagen: {e}")
    try:
        await _update_folder_unread_count(email["account"], source_folder)
        await _update_folder_unread_count(email["account"], "INBOX")
    except Exception as e:
        logger.warning(f"folder unread_count update failed after unspam {email_id}: {e}")
    await spam_filter.remove_spam_sample(email_id)
    return {"moved_to": "INBOX"}


@app.post("/emails/{email_id}/spam-suggestion/confirm")
async def confirm_spam_suggestion(email_id: str):
    """Bestätigt einen Spam-Vorschlag aus dem Vorschlag-Badge."""
    return await move_to_spam(email_id, block_sender=False, block_domain=False)


@app.post("/emails/{email_id}/spam-suggestion/dismiss")
async def dismiss_spam_suggestion(email_id: str):
    """Verwirft den Spam-Vorschlag — Mail bleibt in INBOX."""
    try:
        await pb_client.pb_patch(
            f"/api/collections/emails/records/{email_id}",
            {"spam_suggested": False, "spam_score": None, "spam_rule_match": ""},
        )
    except Exception as e:
        logger.warning(f"dismiss_spam_suggestion failed for {email_id}: {e}")
    return {"dismissed": True}


@app.get("/spam-rules")
async def list_spam_rules(account: str | None = None):
    """Listet alle Spam-Regeln, optional nach Account gefiltert."""
    params: dict = {"perPage": 500}
    if account:
        params["filter"] = f'account="{account}"'
    result = await pb_client.pb_get("/api/collections/spam_rules/records", params=params)
    return {"items": result.get("items", []), "totalItems": result.get("totalItems", 0)}


@app.delete("/spam-rules/{rule_id}")
async def delete_spam_rule(rule_id: str):
    """Löscht eine Spam-Regel (Absender wieder erlaubt)."""
    await pb_client.pb_delete(f"/api/collections/spam_rules/records/{rule_id}")
    return {"deleted": rule_id}


def _imap_search_by_msgid(srv, folder: str, message_id: str) -> int | None:
    """Sucht eine E-Mail im Ordner per Message-ID, gibt neue UID zurück (oder None)."""
    try:
        srv.select_folder(folder)
        mid = message_id.strip()
        results = srv.search(["HEADER", "Message-ID", mid])
        if not results and mid.startswith("<") and mid.endswith(">"):
            results = srv.search(["HEADER", "Message-ID", mid[1:-1]])
        return results[-1] if results else None
    except Exception as ex:
        logger.warning("IMAP search by Message-ID in '%s' fehlgeschlagen: %s", folder, ex)
        return None


def _imap_move_to_spam_sync(acc: dict, imap_uid: int, folder: str, message_id: str) -> tuple[str, int | None]:
    """Verschiebt im IMAP nach Junk/Spam-Folder; gibt immer den normierten UI-Namen 'Spam' zurück
    (analog zum Mapping in imap_sync._IMAP_FLAG_TO_STANDARD)."""
    from imapclient import IMAPClient
    with IMAPClient(acc["imap_host"], port=int(acc.get("imap_port") or 993), ssl=True) as srv:
        srv.login(acc["imap_user"], acc["imap_pass"])
        real_source = resolve_imap_path(srv, folder)
        srv.select_folder(real_source)
        spam = find_imap_folder(srv, [b"\\Junk", b"\\Spam"], ["Spam", "Junk", "Junk E-Mail", "INBOX.Spam", "INBOX.Junk"])
        if spam and spam.lower() != real_source.lower():
            caps = srv.capabilities()
            if b"MOVE" in caps:
                srv.move([imap_uid], spam)
            else:
                srv.copy([imap_uid], spam)
                srv.set_flags([imap_uid], [b"\\Deleted"])
                srv.expunge()
            new_uid = _imap_search_by_msgid(srv, spam, message_id)
            return "Spam", new_uid
        return "Spam", None


async def _imap_move_to_spam(email: dict) -> tuple[str, int | None]:
    """Verschiebt E-Mail per IMAP in den Spam-Ordner.
    Gibt (spam_folder, neue_imap_uid) zurück."""
    account_id = email.get("account")
    imap_uid = email.get("imap_uid")
    folder = email.get("folder", "INBOX")
    if not account_id or not imap_uid:
        return "Spam", None

    acc = await _get_imap_account(account_id)
    if acc is None:
        return "Spam", None

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _imap_move_to_spam_sync, acc, imap_uid, folder, email.get("message_id", ""))


@app.post("/emails/{email_id}/move")
async def move_email(email_id: str, data: dict):
    """Verschiebt E-Mail in einen anderen Ordner (IMAP + PocketBase).
    Beim Verlassen des Spam-Ordners werden Qdrant-Sample und spam_*-Felder mit aufgeräumt."""
    target_folder = (data.get("target_folder") or "").strip()
    if not target_folder:
        raise HTTPException(status_code=400, detail="target_folder fehlt")

    email = await pb_client.pb_get(f"/api/collections/emails/records/{email_id}")
    source_folder = email.get("folder", "INBOX")
    leaving_spam = source_folder == "Spam" and target_folder != "Spam"

    try:
        new_uid = await _imap_move(email, target_folder)
    except Exception as e:
        logger.warning(f"IMAP move failed for {email_id}: {e}")
        raise HTTPException(status_code=502, detail=f"IMAP-Fehler: {e}")

    patch = {"folder": target_folder, "is_read": True}
    if leaving_spam:
        patch["spam_suggested"] = False
        patch["spam_score"] = None
        patch["spam_rule_match"] = ""
    if new_uid:
        patch["imap_uid"] = new_uid
        logger.info("move_email: %s → '%s', neue imap_uid=%s", email_id, target_folder, new_uid)
    # IMAP: \Seen auf neuer UID setzen
    if new_uid:
        try:
            await _imap_set_read({"account": email["account"], "imap_uid": new_uid, "folder": target_folder}, True)
        except Exception as ex:
            logger.warning("move_email: IMAP mark-read fehlgeschlagen: %s", ex)
    try:
        await pb_client.pb_patch(f"/api/collections/emails/records/{email_id}", patch)
    except Exception as e:
        # Race condition: imap_sync hat den Record bereits gelöscht (UID weg aus Quellordner)
        # IMAP-Move ist trotzdem erfolgt — nächster Sync legt Record im Zielordner neu an
        logger.warning("move_email: pb_patch fehlgeschlagen (wahrscheinlich Race mit imap_sync): %s", e)
    try:
        await asyncio.gather(
            _update_folder_unread_count(email["account"], source_folder),
            _update_folder_unread_count(email["account"], target_folder),
        )
    except Exception as e:
        logger.warning("move_email: folder unread_count update fehlgeschlagen: %s", e)
    if leaving_spam:
        await spam_filter.remove_spam_sample(email_id)
    return {"moved_to": target_folder, "marked_read": True}


def _imap_move_sync(acc: dict, imap_uid: int, source_folder: str, target_folder: str, message_id: str) -> int | None:
    from imapclient import IMAPClient
    with IMAPClient(acc["imap_host"], port=int(acc.get("imap_port") or 993), ssl=True) as srv:
        srv.login(acc["imap_user"], acc["imap_pass"])
        real_source = resolve_imap_path(srv, source_folder)
        real_target = resolve_imap_path(srv, target_folder)
        srv.select_folder(real_source)
        caps = srv.capabilities()
        if b"MOVE" in caps:
            srv.move([imap_uid], real_target)
        else:
            srv.copy([imap_uid], real_target)
            srv.set_flags([imap_uid], [b"\\Deleted"])
            srv.expunge()
        return _imap_search_by_msgid(srv, real_target, message_id)


async def _imap_move(email: dict, target_folder: str) -> int | None:
    """Verschiebt E-Mail per IMAP in den Zielordner. Gibt neue UID zurück."""
    account_id = email.get("account")
    imap_uid = email.get("imap_uid")
    source_folder = email.get("folder", "INBOX")
    if not account_id or not imap_uid:
        return None

    acc = await _get_imap_account(account_id)
    if acc is None:
        return None

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _imap_move_sync, acc, imap_uid, source_folder, target_folder, email.get("message_id", ""))


@app.delete("/emails/{email_id}")
async def delete_email(email_id: str):
    """Löscht E-Mail in PocketBase und verschiebt sie auf dem IMAP-Server in den Papierkorb."""
    email = await pb_client.pb_get(f"/api/collections/emails/records/{email_id}")
    source_folder = email.get("folder", "INBOX")
    was_unread = not email.get("is_read", True)
    if was_unread:
        try:
            await pb_client.pb_patch(
                f"/api/collections/emails/records/{email_id}", {"is_read": True}
            )
        except Exception:
            pass
    try:
        await _imap_trash(email)
    except Exception as e:
        logger.warning(f"IMAP trash failed for {email_id}: {e}")
    async with httpx.AsyncClient(base_url=settings.PB_URL, timeout=10) as client:
        resp = await client.delete(
            f"/api/collections/emails/records/{email_id}",
            headers={"Authorization": f"Bearer {pb_client.get_token()}"}
        )
        resp.raise_for_status()
    fts_delete(settings.PB_DATA_PATH, email_id)
    if was_unread:
        try:
            await _update_folder_unread_count(email["account"], source_folder)
        except Exception as e:
            logger.warning(f"folder unread_count update failed after delete {email_id}: {e}")
    return {"deleted": email_id}


def _imap_trash_sync(acc: dict, imap_uid: int, folder: str, message_id: str) -> None:
    from imapclient import IMAPClient
    with IMAPClient(acc["imap_host"], port=int(acc.get("imap_port") or 993), ssl=True) as srv:
        srv.login(acc["imap_user"], acc["imap_pass"])
        real_source = resolve_imap_path(srv, folder)
        srv.select_folder(real_source)
        caps = srv.capabilities()
        if b"MOVE" in caps:
            trash = find_imap_folder(srv, [b"\\Trash", b"\\Deleted"], ["Trash", "Deleted", "Deleted Items", "Papierkorb", "INBOX.Trash"])
            if trash and trash.lower() != real_source.lower():
                srv.move([imap_uid], trash)
                new_uid = _imap_search_by_msgid(srv, trash, message_id)
                if new_uid:
                    srv.select_folder(trash)
                    srv.set_flags([new_uid], [b"\\Seen"])
                    logger.info("_imap_trash: \\Seen gesetzt auf neuer UID %s in '%s'", new_uid, trash)
                return
        srv.set_flags([imap_uid], [b"\\Deleted"])
        srv.expunge()


async def _imap_trash(email: dict) -> None:
    """Verschiebt E-Mail auf dem IMAP-Server in den Papierkorb."""
    account_id = email.get("account")
    imap_uid = email.get("imap_uid")
    folder = email.get("folder", "INBOX")
    if not account_id or not imap_uid:
        return

    acc = await _get_imap_account(account_id)
    if acc is None:
        return

    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _imap_trash_sync, acc, imap_uid, folder, email.get("message_id", ""))




# ---------------------------------------------------------------------------
# Backfill: imap_uid korrigieren
# ---------------------------------------------------------------------------

@app.post("/admin/backfill-imap-uids")
async def backfill_imap_uids():
    """Korrigiert falsche imap_uid-Werte für alle E-Mails.

    Für jeden (Account, Ordner): IMAP öffnen, alle UIDs + Message-IDs laden,
    mit PocketBase abgleichen und abweichende imap_uid-Werte updaten.
    Läuft im Hintergrund (fire-and-forget via BackgroundTask ist hier synchron).
    """
    from imapclient import IMAPClient
    import concurrent.futures

    accounts_data = await pb_client.pb_get("/api/collections/accounts/records", params={"perPage": 50})
    accounts = accounts_data.get("items", [])

    total_fixed = 0
    total_checked = 0
    errors = []

    for acc in accounts:
        account_id = acc["id"]
        # Alle Ordner dieses Accounts aus PocketBase
        folder_data = await pb_client.pb_get("/api/collections/folders/records", params={
            "filter": f'account="{account_id}"',
            "perPage": 200,
            "fields": "id,imap_path",
        })
        folders = [f["imap_path"] for f in folder_data.get("items", []) if f.get("imap_path")]

        for imap_folder in folders:
            try:
                # PocketBase-E-Mails für diesen Ordner laden (Message-ID + imap_uid)
                pb_data = await pb_client.pb_get("/api/collections/emails/records", params={
                    "filter": f'account="{account_id}" && folder="{imap_folder}"',
                    "perPage": 2000,
                    "fields": "id,message_id,imap_uid",
                })
                pb_emails = pb_data.get("items", [])
                if not pb_emails:
                    continue

                pb_by_msgid = {e["message_id"]: e for e in pb_emails if e.get("message_id")}
                total_checked += len(pb_emails)

                # IMAP: alle UIDs + Message-IDs für diesen Ordner holen (blocking, in Batches)
                def _fetch_imap_uids(acc_dict, folder):
                    BATCH = 200
                    with IMAPClient(acc_dict["imap_host"], port=int(acc_dict.get("imap_port") or 993), ssl=True) as srv:
                        srv.login(acc_dict["imap_user"], acc_dict["imap_pass"])
                        srv.select_folder(folder, readonly=True)
                        uids = srv.search(["ALL"])
                        if not uids:
                            return {}
                        uid_to_msgid = {}
                        for i in range(0, len(uids), BATCH):
                            batch = uids[i:i + BATCH]
                            fetch_data = srv.fetch(batch, ["BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)]"])
                            for uid, data in fetch_data.items():
                                raw = data.get(b"BODY[HEADER.FIELDS (MESSAGE-ID)]", b"")
                                line = raw.decode("utf-8", errors="replace").strip()
                                if ":" in line:
                                    mid = line.split(":", 1)[1].strip()
                                    uid_to_msgid[uid] = mid
                        return uid_to_msgid

                loop = asyncio.get_running_loop()
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                    uid_to_msgid = await loop.run_in_executor(pool, _fetch_imap_uids, acc, imap_folder)

                # Abgleich: für jede IMAP-Mail schauen ob PocketBase-Record UID falsch hat
                msgid_to_uid = {mid: uid for uid, mid in uid_to_msgid.items()}
                for msgid, pb_email in pb_by_msgid.items():
                    # Varianten der Message-ID probieren
                    new_uid = msgid_to_uid.get(msgid) or msgid_to_uid.get(f"<{msgid}>") or msgid_to_uid.get(msgid.strip("<>"))
                    if new_uid and new_uid != pb_email.get("imap_uid"):
                        await pb_client.pb_patch(
                            f"/api/collections/emails/records/{pb_email['id']}",
                            {"imap_uid": new_uid},
                        )
                        total_fixed += 1
                        logger.info("backfill: %s Message-ID=%s: imap_uid %s → %s",
                                    imap_folder, msgid, pb_email.get("imap_uid"), new_uid)

            except Exception as ex:
                msg = f"{account_id}/{imap_folder}: {ex}"
                errors.append(msg)
                logger.warning("backfill_imap_uids Fehler: %s", msg)

    return {"checked": total_checked, "fixed": total_fixed, "errors": errors}


# ---------------------------------------------------------------------------
# AI-Endpoints
# ---------------------------------------------------------------------------

import ai_helper


class TriageRequest(BaseModel):
    account_id: str | None = None
    folder: str | None = None


class SuggestRequest(BaseModel):
    email_id: str
    tone: str = "neutral"
    context_elements: list[str] | None = None


class RefineRequest(BaseModel):
    text: str
    instruction: str


@app.get("/categories")
async def get_categories():
    """Liefert die konfigurierten Triage-Kategorien."""
    categories = ai_helper.load_triage_config()["categories"]
    return [{"slug": c["slug"], "name": c["name"], "description": c["description"]} for c in categories]


@app.post("/ai/triage")
async def ai_triage(req: TriageRequest):
    """Kategorisiert ungelesene E-Mails ohne ai_category via Claude Haiku.

    Max. 50 E-Mails pro Aufruf (Kostenschutz).
    """
    filters = ['is_read=false', '(ai_category="" || ai_category=null)']
    if req.account_id:
        filters.append(f'account="{req.account_id}"')
    if req.folder:
        filters.append(f'folder="{req.folder}"')

    try:
        data = await pb_client.pb_get("/api/collections/emails/records", params={
            "filter": " && ".join(filters),
            "perPage": 50,
            "sort": "-date_sent",
            "fields": "id,subject,body_plain,from_email,account",
        })
    except Exception as exc:
        logger.error("Triage: PocketBase-Abfrage fehlgeschlagen: %s", exc)
        raise HTTPException(status_code=500, detail=f"PocketBase-Fehler: {exc}")

    emails = data.get("items", [])
    if not emails:
        return {"categorized": 0, "skipped": 0, "errors": 0}

    # Lernregeln für diesen Account laden
    rules: list[str] = []
    try:
        rule_filter = f'account="{req.account_id}"' if req.account_id else ""
        rule_data = await pb_client.pb_get("/api/collections/triage_rules/records", params={
            "filter": rule_filter,
            "perPage": 100,
            "sort": "-created",
            "fields": "rule_text",
        })
        rules = [r["rule_text"] for r in rule_data.get("items", []) if r.get("rule_text")]
    except Exception as exc:
        logger.warning("Triage: Lernregeln konnten nicht geladen werden: %s", exc)

    categorized = 0
    errors = 0
    semaphore = asyncio.Semaphore(5)

    async def _process_one(email: dict) -> None:
        nonlocal categorized, errors
        async with semaphore:
            try:
                category = await ai_helper.categorize_email(
                    subject=email.get("subject") or "",
                    body=email.get("body_plain") or "",
                    from_email=email.get("from_email") or "",
                    rules=rules,
                )
                await pb_client.pb_patch(
                    f"/api/collections/emails/records/{email['id']}",
                    {"ai_category": category},
                )
                categorized += 1
                logger.info("Triage: %s → %s", email["id"], category)
            except Exception as exc:
                errors += 1
                logger.warning("Triage: Fehler bei E-Mail %s: %s", email["id"], exc)

    await asyncio.gather(*[_process_one(e) for e in emails])

    return {"categorized": categorized, "skipped": 0, "errors": errors}


@app.post("/triage/example")
async def save_triage_example(data: dict):
    """Speichert eine manuelle Korrektur als Lernregel für die KI-Triage."""
    email_id = data.get("email_id", "").strip()
    category = data.get("category", "").strip()
    valid_slugs = set(ai_helper.get_category_slugs())
    if not email_id or category not in valid_slugs:
        raise HTTPException(status_code=400, detail="email_id und gültige category erforderlich")

    try:
        email = await pb_client.pb_get(f"/api/collections/emails/records/{email_id}",
                                       params={"fields": "account,from_email,subject,body_plain"})
    except Exception as exc:
        logger.error("triage/example: E-Mail %s konnte nicht geladen werden: %s", email_id, exc)
        status = 404 if "404" in str(exc) else 502
        raise HTTPException(status_code=status, detail=f"E-Mail konnte nicht geladen werden: {exc}")

    # Regel via AI extrahieren
    rule_text = await ai_helper.extract_rule(
        from_email=email.get("from_email", ""),
        subject=email.get("subject", ""),
        body_snippet=(email.get("body_plain") or "")[:300],
        category_slug=category,
    )
    logger.info("Triage-Regel extrahiert: %s → %s", category, rule_text)

    try:
        await pb_client.pb_post("/api/collections/triage_rules/records", {
            "account":       email["account"],
            "category_slug": category,
            "rule_text":     rule_text,
        })
    except Exception as exc:
        logger.error("triage/example: Regel konnte nicht gespeichert werden: %s", exc)
        raise HTTPException(status_code=502, detail=f"Regel konnte nicht gespeichert werden: {exc}")

    # Konsolidierung prüfen: bei ≥15 Regeln für diesen Account + Kategorie
    try:
        count_data = await pb_client.pb_get("/api/collections/triage_rules/records", params={
            "filter": f'account="{email["account"]}" && category_slug="{category}"',
            "perPage": 1,
        })
        total = count_data.get("totalItems", 0)
        if total >= 15:
            asyncio.create_task(_consolidate_rules(email["account"], category))
    except Exception as exc:
        logger.warning("Konsolidierungsprüfung fehlgeschlagen: %s", exc)

    return {"ok": True, "rule": rule_text}


async def _consolidate_rules(account: str, category_slug: str) -> None:
    """Hintergrundaufgabe: Konsolidiert Lernregeln auf max. 7."""
    try:
        data = await pb_client.pb_get("/api/collections/triage_rules/records", params={
            "filter": f'account="{account}" && category_slug="{category_slug}"',
            "perPage": 200,
            "fields": "id,rule_text",
        })
        items = data.get("items", [])
        if len(items) < 15:
            return

        rules = [r["rule_text"] for r in items if r.get("rule_text")]
        consolidated = await ai_helper.consolidate_rules(rules, category_slug)
        logger.info("Konsolidierung %s/%s: %d → %d Regeln", account, category_slug, len(items), len(consolidated))

        # Alle alten löschen
        for item in items:
            await pb_client.pb_delete(f"/api/collections/triage_rules/records/{item['id']}")

        # Neue speichern
        for rule_text in consolidated:
            await pb_client.pb_post("/api/collections/triage_rules/records", {
                "account":       account,
                "category_slug": category_slug,
                "rule_text":     rule_text,
            })
    except Exception as exc:
        logger.error("Konsolidierung fehlgeschlagen (%s/%s): %s", account, category_slug, exc)


class AnalyzeRequest(BaseModel):
    email_id: str


@app.post("/ai/analyze")
async def ai_analyze(req: AnalyzeRequest):
    """Analysiert eine E-Mail strukturell: Elemente + Aktionsvorschläge."""
    try:
        email = await pb_client.pb_get(f"/api/collections/emails/records/{req.email_id}")
    except Exception as exc:
        raise HTTPException(status_code=404, detail=f"E-Mail nicht gefunden: {exc}")

    body = email.get("body_plain") or ""
    if not body and email.get("body_html"):
        html = email["body_html"]
        html = re.sub(r'<br\s*/?>', '\n', html, flags=re.IGNORECASE)
        html = re.sub(r'</(p|div|tr|li)>', '\n', html, flags=re.IGNORECASE)
        html = re.sub(r'<[^>]+>', '', html)
        body = re.sub(r'\n{3,}', '\n\n', html).strip()[:5000]

    try:
        items = await ai_helper.analyze_email(
            subject=email.get("subject") or "",
            body=body,
            from_name=email.get("from_name") or email.get("from_email") or "",
        )
    except Exception as exc:
        logger.error("ai_analyze fehlgeschlagen: %s", exc)
        raise HTTPException(status_code=500, detail=f"KI-Fehler: {exc}")

    return {"items": items}


@app.post("/ai/suggest")
async def ai_suggest(req: SuggestRequest):
    """Generiert einen Antwortvorschlag für eine E-Mail."""
    try:
        email = await pb_client.pb_get(
            f"/api/collections/emails/records/{req.email_id}"
        )
    except Exception as exc:
        raise HTTPException(status_code=404, detail=f"E-Mail nicht gefunden: {exc}")

    # body_plain bevorzugen; Fallback: plain text aus body_html (für HTML-only-E-Mails)
    if not email.get("body_plain") and email.get("body_html"):
        plain = email["body_html"]
        plain = re.sub(r'<br\s*/?>', '\n', plain, flags=re.IGNORECASE)
        plain = re.sub(r'</(p|div|tr|li)>', '\n', plain, flags=re.IGNORECASE)
        plain = re.sub(r'<[^>]+>', '', plain)
        plain = re.sub(r'\n{3,}', '\n\n', plain).strip()
        email["body_plain"] = plain[:10000]

    thread_id = email.get("thread_id") or email.get("message_id")

    # Thread-E-Mails laden (max. 10, ohne die E-Mail selbst)
    thread_emails: list = []
    if thread_id:
        try:
            thread_data = await pb_client.pb_get(
                "/api/collections/emails/records",
                params={
                    "filter": f'thread_id="{thread_id}" && id!="{req.email_id}"',
                    "sort": "date_sent",
                    "perPage": 10,
                    "fields": "id,from_email,subject,body_plain,date_sent",
                },
            )
            thread_emails = thread_data.get("items", [])
        except Exception as exc:
            logger.warning("Konnte Thread-E-Mails nicht laden: %s", exc)

    # Kontakthistorie: letzte 5 E-Mails vom gleichen Absender außerhalb des Threads
    contact_history: list = []
    from_email = email.get("from_email") or ""
    if from_email:
        history_filter = f'from_email="{from_email}"'
        if thread_id:
            history_filter += f' && thread_id!="{thread_id}"'
        try:
            history_data = await pb_client.pb_get(
                "/api/collections/emails/records",
                params={
                    "filter": history_filter,
                    "sort": "-date_sent",
                    "perPage": 5,
                    "fields": "id,from_email,subject,body_plain,date_sent",
                },
            )
            contact_history = history_data.get("items", [])
        except Exception as exc:
            logger.warning("Konnte Kontakthistorie nicht laden: %s", exc)

    last_exc = None
    for attempt in range(3):
        try:
            result = await ai_helper.suggest_reply(
                email=email,
                thread_emails=thread_emails,
                contact_history=contact_history,
                tone=req.tone,
                context_elements=req.context_elements,
            )
            return {"text": result}
        except Exception as exc:
            last_exc = exc
            exc_str = str(exc)
            if "529" in exc_str or "overloaded" in exc_str.lower():
                wait = (attempt + 1) * 4  # 4s, 8s
                logger.warning("KI überlastet (Versuch %d/3), warte %ds …", attempt + 1, wait)
                await asyncio.sleep(wait)
                continue
            break  # Anderer Fehler → sofort abbrechen

    logger.error("suggest_reply fehlgeschlagen: %s", last_exc)
    raise HTTPException(status_code=500, detail=f"KI-Fehler: {last_exc}")


@app.post("/ai/refine")
async def ai_refine(req: RefineRequest):
    """Verfeinert einen bestehenden E-Mail-Entwurf."""
    try:
        result = await ai_helper.refine_reply(text=req.text, instruction=req.instruction)
    except Exception as exc:
        logger.error("refine_reply fehlgeschlagen: %s", exc)
        raise HTTPException(status_code=500, detail=f"KI-Fehler: {exc}")

    return {"text": result}


class SavePatternRequest(BaseModel):
    account_id: str
    element_text: str
    action: str
    draft_text: str
    was_edited: bool = False


@app.post("/response-patterns")
async def save_response_pattern(req: SavePatternRequest):
    """Speichert ein Antwort-Pattern (Element + Entwurf) in PocketBase."""
    try:
        await pb_client.pb_post("/api/collections/response_patterns/records", {
            "account":       req.account_id,
            "element_text":  req.element_text,
            "action":        req.action,
            "draft_text":    req.draft_text,
            "was_edited":    req.was_edited,
        })
    except Exception as exc:
        logger.error("response-patterns: Speichern fehlgeschlagen: %s", exc)
        raise HTTPException(status_code=500, detail=f"Speichern fehlgeschlagen: {exc}")
    return {"ok": True}


@app.get("/xano/user-info")
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


# ---------------------------------------------------------------------------

def _imap_set_read_sync(acc: dict, imap_uid: int, folder: str, is_read: bool) -> None:
    from imapclient import IMAPClient
    with IMAPClient(acc["imap_host"], port=int(acc.get("imap_port") or 993), ssl=True) as srv:
        srv.login(acc["imap_user"], acc["imap_pass"])
        srv.select_folder(resolve_imap_path(srv, folder))
        if is_read:
            srv.set_flags([imap_uid], [b"\\Seen"])
        else:
            srv.remove_flags([imap_uid], [b"\\Seen"])


async def _imap_set_read(email: dict, is_read: bool) -> None:
    """Setzt \\Seen-Flag auf dem IMAP-Server."""
    account_id = email.get("account")
    imap_uid = email.get("imap_uid")
    folder = email.get("folder", "INBOX")
    if not account_id or not imap_uid:
        return

    acc = await _get_imap_account(account_id)
    if acc is None:
        return

    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _imap_set_read_sync, acc, imap_uid, folder, is_read)


async def _imap_set_answered_safe(email: dict) -> None:
    """Wrapper: setzt \\Answered auf IMAP, schluckt Fehler (fire-and-forget)."""
    try:
        await _imap_set_answered(email)
    except Exception as exc:
        logger.warning("IMAP set-answered fehlgeschlagen für UID %s: %s", email.get("imap_uid"), exc)


async def _imap_set_answered(email: dict) -> None:
    """Setzt \\Answered-Flag auf dem IMAP-Server."""
    from imapclient import IMAPClient
    account_id = email.get("account")
    imap_uid = email.get("imap_uid")
    folder = email.get("folder", "INBOX")
    if not account_id or not imap_uid:
        return

    acc = await _get_imap_account(account_id)
    if acc is None:
        return

    with IMAPClient(acc["imap_host"], port=int(acc.get("imap_port") or 993), ssl=True) as srv:
        srv.login(acc["imap_user"], acc["imap_pass"])
        srv.select_folder(folder)
        srv.add_flags([imap_uid], [b"\\Answered"])


# =========================================================================
# Webhooks: externer Versand (Xano etc.) + Verwaltung
# =========================================================================

_WEBHOOK_SLUG_RE = re.compile(r"^[a-z0-9-]+$")


async def _webhook_by_slug(slug: str) -> dict | None:
    data = await pb_client.pb_get(
        "/api/collections/webhooks/records",
        params={"filter": f'slug="{slug}"', "perPage": 1},
    )
    items = data.get("items") or []
    return items[0] if items else None


async def _webhook_log(webhook_id: str, ip: str, status: str,
                       to: str, subject: str,
                       message_id: str = "", error: str = "") -> None:
    try:
        await pb_client.pb_post(
            "/api/collections/webhook_logs/records",
            {
                "webhook": webhook_id,
                "ip": ip[:64],
                "status": status,
                "to": to[:500],
                "subject": subject[:500],
                "message_id": message_id[:200],
                "error": error[:2000],
            },
        )
    except Exception as exc:
        logger.error("webhook_log konnte nicht geschrieben werden: %s", exc)


@app.post("/webhooks/{slug}/send")
async def webhook_send(slug: str, request: Request, data: dict):
    """Externer Mail-Versand via Webhook.

    Auth: Header ``X-Webhook-Key`` mit dem in der Webhook-Konfig gespeicherten
    ``api_key``. Diese Route ist von der globalen Frontend-API-Key-Middleware
    ausgenommen — jeder Webhook hat seinen eigenen Schlüssel.
    """
    if not _WEBHOOK_SLUG_RE.match(slug or ""):
        raise HTTPException(status_code=400, detail="Ungültiger Slug")

    wh = await _webhook_by_slug(slug)
    if wh is None or not wh.get("is_active"):
        # Bewusst gleicher Fehler wie 401 — verrät nicht ob Slug existiert
        raise HTTPException(status_code=401, detail="Unauthorized")

    provided_key = request.headers.get("X-Webhook-Key", "")
    if not provided_key or not _secrets.compare_digest(provided_key, wh.get("api_key") or ""):
        raise HTTPException(status_code=401, detail="Unauthorized")

    ip = request.client.host if request.client else ""

    # To: payload überschreibt nur wenn erlaubt
    payload_to = (data.get("to") or "").strip()
    to = payload_to if (wh.get("allow_to_override") and payload_to) else (wh.get("default_to") or "").strip()
    if not to:
        await _webhook_log(wh["id"], ip, "error", "", "", error="Empfänger fehlt")
        raise HTTPException(status_code=400, detail="Empfänger fehlt")

    reply_to = (data.get("reply_to") or "").strip() if wh.get("allow_reply_to") else ""
    cc = (data.get("cc") or "").strip() if wh.get("allow_cc") else ""

    subject = (data.get("subject") or "").strip()
    body = data.get("body") or ""
    body_html = data.get("body_html") or ""

    if not subject:
        await _webhook_log(wh["id"], ip, "error", to, "", error="Betreff fehlt")
        raise HTTPException(status_code=400, detail="Betreff fehlt")
    if not body and not body_html:
        await _webhook_log(wh["id"], ip, "error", to, subject, error="Body fehlt")
        raise HTTPException(status_code=400, detail="Body fehlt")

    try:
        message_id = await smtp_send_email(
            smtp_server_id=wh["smtp_server"],
            from_account_id=wh["from_account"],
            to=to,
            cc=cc,
            subject=subject,
            body=body,
            body_html=body_html,
            reply_to=reply_to,
            from_name_override=(wh.get("from_name_override") or "").strip(),
        )
    except Exception as exc:
        logger.error("Webhook-Versand fehlgeschlagen (slug=%s): %s", slug, exc)
        await _webhook_log(wh["id"], ip, "error", to, subject, error=str(exc))
        raise HTTPException(status_code=502, detail=f"SMTP-Fehler: {exc}")

    await _webhook_log(wh["id"], ip, "success", to, subject, message_id=message_id)
    logger.info("Webhook-Versand OK: slug=%s to=%s message_id=%s", slug, to, message_id)
    return {"status": "sent", "message_id": message_id}


# ---- Verwaltung (hinter Frontend-API-Key-Middleware) ---------------------

@app.get("/webhooks")
async def webhooks_list():
    data = await pb_client.pb_get(
        "/api/collections/webhooks/records",
        params={"perPage": 200, "sort": "-created"},
    )
    return data.get("items", [])


@app.post("/webhooks")
async def webhooks_create(data: dict):
    name = (data.get("name") or "").strip()
    slug = (data.get("slug") or "").strip().lower()
    smtp_server = (data.get("smtp_server") or "").strip()
    from_account = (data.get("from_account") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name fehlt")
    if not _WEBHOOK_SLUG_RE.match(slug):
        raise HTTPException(status_code=400, detail="slug ungültig (nur a-z, 0-9, -)")
    if not smtp_server or not from_account:
        raise HTTPException(status_code=400, detail="smtp_server und from_account erforderlich")

    record = {
        "name": name,
        "slug": slug,
        "smtp_server": smtp_server,
        "from_account": from_account,
        "default_to": (data.get("default_to") or "").strip(),
        "from_name_override": (data.get("from_name_override") or "").strip(),
        "allow_to_override": bool(data.get("allow_to_override", True)),
        "allow_reply_to": bool(data.get("allow_reply_to", True)),
        "allow_cc": bool(data.get("allow_cc", False)),
        "is_active": bool(data.get("is_active", True)),
        "api_key": "whk_" + _secrets.token_urlsafe(32),
    }
    return await pb_client.pb_post("/api/collections/webhooks/records", record)


@app.get("/webhooks/{webhook_id}/logs")
async def webhooks_logs(webhook_id: str, limit: int = 100):
    limit = max(1, min(int(limit), 500))
    data = await pb_client.pb_get(
        "/api/collections/webhook_logs/records",
        params={
            "filter": f'webhook="{webhook_id}"',
            "perPage": limit,
            "sort": "-created",
        },
    )
    return data.get("items", [])


@app.patch("/webhooks/{webhook_id}")
async def webhooks_update(webhook_id: str, data: dict):
    allowed = {
        "name", "slug", "smtp_server", "from_account", "default_to",
        "from_name_override",
        "allow_to_override", "allow_reply_to", "allow_cc", "is_active",
    }
    patch = {k: v for k, v in data.items() if k in allowed}
    if "slug" in patch:
        s = (patch["slug"] or "").strip().lower()
        if not _WEBHOOK_SLUG_RE.match(s):
            raise HTTPException(status_code=400, detail="slug ungültig")
        patch["slug"] = s
    if data.get("rotate_api_key"):
        patch["api_key"] = "whk_" + _secrets.token_urlsafe(32)
    return await pb_client.pb_patch(f"/api/collections/webhooks/records/{webhook_id}", patch)


@app.delete("/webhooks/{webhook_id}")
async def webhooks_delete(webhook_id: str):
    await pb_client.pb_delete(f"/api/collections/webhooks/records/{webhook_id}")
    return {"status": "deleted"}


# ---------------------------------------------------------------------------
# E-Mail-Vorlagen, Snippets, Variablen
# ---------------------------------------------------------------------------

_VAR_NAME_RE = re.compile(r"^[a-z_][a-z0-9_]*$")
_VAR_RESERVED_NAMES = {"name", "email"}


@app.get("/variables")
async def variables_list():
    data = await pb_client.pb_get(
        "/api/collections/email_variables/records",
        params={"perPage": 500, "sort": "name"},
    )
    return data.get("items", [])


@app.post("/variables")
async def variables_create(data: dict):
    name = (data.get("name") or "").strip().lower()
    if not _VAR_NAME_RE.match(name):
        raise HTTPException(status_code=400, detail="name ungültig (nur a-z, 0-9, _; Start mit Buchstabe oder _)")
    if name in _VAR_RESERVED_NAMES:
        raise HTTPException(status_code=400, detail=f"name '{name}' ist reserviert für Kontakt-Felder")
    record = {
        "name": name,
        "value": data.get("value") or "",
    }
    try:
        return await pb_client.pb_post("/api/collections/email_variables/records", record)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 400 and "name" in exc.response.text:
            raise HTTPException(status_code=409, detail=f"Variable '{name}' existiert bereits")
        raise


@app.patch("/variables/{var_id}")
async def variables_update(var_id: str, data: dict):
    patch: dict = {}
    if "value" in data:
        patch["value"] = data["value"] or ""
    if "name" in data:
        new_name = (data["name"] or "").strip().lower()
        if not _VAR_NAME_RE.match(new_name):
            raise HTTPException(status_code=400, detail="name ungültig")
        if new_name in _VAR_RESERVED_NAMES:
            raise HTTPException(status_code=400, detail=f"name '{new_name}' ist reserviert")
        patch["name"] = new_name
    if not patch:
        raise HTTPException(status_code=400, detail="nichts zu ändern")
    try:
        return await pb_client.pb_patch(f"/api/collections/email_variables/records/{var_id}", patch)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 400 and "name" in exc.response.text:
            raise HTTPException(status_code=409, detail="Variable mit diesem Namen existiert bereits")
        raise


@app.get("/variables/{var_id}/usage")
async def variables_usage(var_id: str):
    """Findet alle Templates + Snippets, die diese Variable referenzieren."""
    var = await pb_client.pb_get(f"/api/collections/email_variables/records/{var_id}")
    name = var.get("name") or ""
    if not name:
        return {"name": "", "templates": [], "snippets": []}
    return await _find_placeholder_usage(name, include_snippets=True, snippet_prefix=False)


@app.delete("/variables/{var_id}")
async def variables_delete(var_id: str):
    await pb_client.pb_delete(f"/api/collections/email_variables/records/{var_id}")
    return {"status": "deleted"}


@app.post("/variables/{var_id}/rename")
async def variables_rename(var_id: str, data: dict):
    """Benennt eine Variable um und ersetzt optional alle `{{old}}`-Vorkommen
    in Templates+Snippets durch `{{new}}`.

    Body: ``{new_name: str, replace_in_usage: bool}``.
    Response: ``{old_name, new_name, replaced_templates, replaced_snippets}``.
    """
    new_name = (data.get("new_name") or "").strip().lower()
    replace = bool(data.get("replace_in_usage", False))
    if not _VAR_NAME_RE.match(new_name):
        raise HTTPException(status_code=400, detail="name ungültig")
    if new_name in _VAR_RESERVED_NAMES:
        raise HTTPException(status_code=400, detail=f"name '{new_name}' ist reserviert")

    cur = await pb_client.pb_get(f"/api/collections/email_variables/records/{var_id}")
    old_name = (cur.get("name") or "").strip().lower()
    if old_name == new_name:
        return {"old_name": old_name, "new_name": new_name,
                "replaced_templates": 0, "replaced_snippets": 0}

    replaced_t = replaced_s = 0
    if replace:
        replaced_t, replaced_s = await _replace_placeholder_refs(
            old_name, new_name, is_snippet=False
        )

    try:
        await pb_client.pb_patch(
            f"/api/collections/email_variables/records/{var_id}",
            {"name": new_name},
        )
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 400 and "name" in exc.response.text:
            raise HTTPException(status_code=409, detail=f"Variable '{new_name}' existiert bereits")
        raise

    return {"old_name": old_name, "new_name": new_name,
            "replaced_templates": replaced_t, "replaced_snippets": replaced_s}


async def _replace_placeholder_refs(old: str, new: str, *, is_snippet: bool) -> tuple[int, int]:
    """Ersetzt `{{old}}` (Variable) bzw. `{{> old}}` (Snippet) in
    email_templates (subject+html_body) und — nur bei Variablen — auch
    in email_snippets (html). Snippet-in-Snippet ist per Plan verboten,
    daher überspringen wir Snippets, wenn ein Snippet umbenannt wird.

    Returns (templates_modified, snippets_modified).
    """
    old_l = old.strip().lower()
    new_l = new.strip().lower()

    def rewrite(text: str) -> tuple[str, bool]:
        changed = False

        def repl(m: re.Match) -> str:
            nonlocal changed
            is_snip = bool(m.group(1))
            name = (m.group(2) or "").strip().lower()
            if name != old_l:
                return m.group(0)
            if is_snippet != is_snip:
                return m.group(0)
            changed = True
            return f"{{{{> {new_l}}}}}" if is_snip else f"{{{{{new_l}}}}}"

        result = rendering._PLACEHOLDER_RE.sub(repl, text or "")
        return result, changed

    tpl_modified = 0
    tpls = await pb_client.pb_get(
        "/api/collections/email_templates/records",
        params={"perPage": 500, "fields": "id,subject,html_body"},
    )
    for t in tpls.get("items", []):
        new_subj, ch1 = rewrite(t.get("subject") or "")
        new_body, ch2 = rewrite(t.get("html_body") or "")
        if ch1 or ch2:
            patch: dict = {}
            if ch1:
                patch["subject"] = new_subj
            if ch2:
                patch["html_body"] = new_body
            await pb_client.pb_patch(
                f"/api/collections/email_templates/records/{t['id']}", patch
            )
            tpl_modified += 1

    snip_modified = 0
    if not is_snippet:
        snips = await pb_client.pb_get(
            "/api/collections/email_snippets/records",
            params={"perPage": 500, "fields": "id,html"},
        )
        for s in snips.get("items", []):
            new_html, ch = rewrite(s.get("html") or "")
            if ch:
                await pb_client.pb_patch(
                    f"/api/collections/email_snippets/records/{s['id']}",
                    {"html": new_html},
                )
                snip_modified += 1

    return tpl_modified, snip_modified


async def _find_placeholder_usage(name: str, *, include_snippets: bool, snippet_prefix: bool) -> dict:
    """Sucht `{{name}}` (oder `{{> name}}` wenn snippet_prefix=True) in
    email_templates.subject + html_body und optional email_snippets.html.
    Nutzt rendering._PLACEHOLDER_RE: (>?)(name).
    """
    target = name.strip().lower()
    matched_templates: list[dict] = []
    matched_snippets: list[dict] = []

    tpls_resp = await pb_client.pb_get(
        "/api/collections/email_templates/records",
        params={"perPage": 500, "sort": "prefix,name"},
    )
    for t in tpls_resp.get("items", []):
        hits: list[str] = []
        for field in ("subject", "html_body"):
            text = t.get(field) or ""
            for m in rendering._PLACEHOLDER_RE.finditer(text):
                is_snippet = bool(m.group(1))
                placeholder_name = (m.group(2) or "").strip().lower()
                if placeholder_name != target:
                    continue
                if snippet_prefix and not is_snippet:
                    continue
                if not snippet_prefix and is_snippet:
                    continue
                hits.append(field)
                break  # ein Treffer pro Feld reicht
        if hits:
            matched_templates.append({
                "id": t["id"],
                "prefix": t.get("prefix") or "",
                "name": t.get("name") or "",
                "fields": hits,
            })

    if include_snippets:
        snips_resp = await pb_client.pb_get(
            "/api/collections/email_snippets/records",
            params={"perPage": 500, "sort": "name"},
        )
        for s in snips_resp.get("items", []):
            text = s.get("html") or ""
            for m in rendering._PLACEHOLDER_RE.finditer(text):
                is_snippet = bool(m.group(1))
                placeholder_name = (m.group(2) or "").strip().lower()
                if placeholder_name != target:
                    continue
                # Snippet-in-Snippet ist per Plan-Konvention verboten — also
                # zaehlen wir hier nur Nicht-Prefix-Treffer (Variablen).
                if is_snippet:
                    continue
                matched_snippets.append({"id": s["id"], "name": s.get("name") or ""})
                break

    return {"name": target, "templates": matched_templates, "snippets": matched_snippets}


_SNIPPET_NAME_RE = re.compile(r"^[a-z_][a-z0-9_]{0,49}$")


@app.get("/snippets")
async def snippets_list():
    data = await pb_client.pb_get(
        "/api/collections/email_snippets/records",
        params={"perPage": 500, "sort": "name"},
    )
    return data.get("items", [])


@app.post("/snippets")
async def snippets_create(data: dict):
    name = (data.get("name") or "").strip().lower()
    if not _SNIPPET_NAME_RE.match(name):
        raise HTTPException(status_code=400, detail="name ungültig (1–50 Zeichen, nur a-z, 0-9, _; Start mit Buchstabe oder _)")
    record = {
        "name": name,
        "html": data.get("html") or "",
    }
    try:
        return await pb_client.pb_post("/api/collections/email_snippets/records", record)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 400 and "name" in exc.response.text:
            raise HTTPException(status_code=409, detail=f"Snippet '{name}' existiert bereits")
        raise


@app.patch("/snippets/{snippet_id}")
async def snippets_update(snippet_id: str, data: dict):
    patch: dict = {}
    if "html" in data:
        patch["html"] = data["html"] or ""
    if "name" in data:
        new_name = (data["name"] or "").strip().lower()
        if not _SNIPPET_NAME_RE.match(new_name):
            raise HTTPException(status_code=400, detail="name ungültig")
        patch["name"] = new_name
    if not patch:
        raise HTTPException(status_code=400, detail="nichts zu ändern")
    try:
        return await pb_client.pb_patch(f"/api/collections/email_snippets/records/{snippet_id}", patch)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 400 and "name" in exc.response.text:
            raise HTTPException(status_code=409, detail="Snippet mit diesem Namen existiert bereits")
        raise


@app.get("/snippets/{snippet_id}/usage")
async def snippets_usage(snippet_id: str):
    """Findet alle Templates, die dieses Snippet via {{> name}} referenzieren.
    Snippets duerfen keine anderen Snippets includen (Plan-Konvention), deshalb
    wird email_snippets nicht gescannt."""
    snip = await pb_client.pb_get(f"/api/collections/email_snippets/records/{snippet_id}")
    name = snip.get("name") or ""
    if not name:
        return {"name": "", "templates": [], "snippets": []}
    return await _find_placeholder_usage(name, include_snippets=False, snippet_prefix=True)


@app.delete("/snippets/{snippet_id}")
async def snippets_delete(snippet_id: str):
    await pb_client.pb_delete(f"/api/collections/email_snippets/records/{snippet_id}")
    return {"status": "deleted"}


@app.post("/snippets/{snippet_id}/rename")
async def snippets_rename(snippet_id: str, data: dict):
    """Benennt ein Snippet um und ersetzt optional alle `{{> old}}`-Refs
    in Templates durch `{{> new}}`.

    Body: ``{new_name: str, replace_in_usage: bool}``.
    Response: ``{old_name, new_name, replaced_templates}``.
    """
    new_name = (data.get("new_name") or "").strip().lower()
    replace = bool(data.get("replace_in_usage", False))
    if not _SNIPPET_NAME_RE.match(new_name):
        raise HTTPException(status_code=400, detail="name ungültig")

    cur = await pb_client.pb_get(f"/api/collections/email_snippets/records/{snippet_id}")
    old_name = (cur.get("name") or "").strip().lower()
    if old_name == new_name:
        return {"old_name": old_name, "new_name": new_name, "replaced_templates": 0}

    replaced_t = 0
    if replace:
        replaced_t, _ = await _replace_placeholder_refs(
            old_name, new_name, is_snippet=True
        )

    try:
        await pb_client.pb_patch(
            f"/api/collections/email_snippets/records/{snippet_id}",
            {"name": new_name},
        )
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 400 and "name" in exc.response.text:
            raise HTTPException(status_code=409, detail=f"Snippet '{new_name}' existiert bereits")
        raise

    return {"old_name": old_name, "new_name": new_name, "replaced_templates": replaced_t}


_TEMPLATE_PREFIX_RE = re.compile(r"^[a-z0-9_]{0,30}$")


@app.get("/templates")
async def templates_list(prefix: str = "", search: str = ""):
    filters = []
    if prefix:
        filters.append(f'prefix="{prefix}"')
    if search:
        s = search.replace('"', '')
        filters.append(f'(name~"{s}" || subject~"{s}")')
    params = {"perPage": 500, "sort": "prefix,name"}
    if filters:
        params["filter"] = " && ".join(filters)
    data = await pb_client.pb_get(
        "/api/collections/email_templates/records",
        params=params,
    )
    return data.get("items", [])


@app.post("/templates")
async def templates_create(data: dict):
    prefix = (data.get("prefix") or "").strip().lower()
    name = (data.get("name") or "").strip()
    if not _TEMPLATE_PREFIX_RE.match(prefix):
        raise HTTPException(status_code=400, detail="prefix ungültig (max 30 Zeichen, nur a-z, 0-9, _)")
    if not name or len(name) > 100:
        raise HTTPException(status_code=400, detail="name muss 1–100 Zeichen lang sein")
    record = {
        "prefix": prefix,
        "name": name,
        "subject": (data.get("subject") or "").strip(),
        "html_body": data.get("html_body") or "",
        "text_body": data.get("text_body") or "",
    }
    try:
        return await pb_client.pb_post("/api/collections/email_templates/records", record)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 400 and ("prefix" in exc.response.text or "name" in exc.response.text):
            raise HTTPException(status_code=409, detail=f"Vorlage '{prefix}/{name}' existiert bereits")
        raise


@app.patch("/templates/{template_id}")
async def templates_update(template_id: str, data: dict):
    patch: dict = {}
    if "prefix" in data:
        p = (data["prefix"] or "").strip().lower()
        if not _TEMPLATE_PREFIX_RE.match(p):
            raise HTTPException(status_code=400, detail="prefix ungültig")
        patch["prefix"] = p
    if "name" in data:
        n = (data["name"] or "").strip()
        if not n or len(n) > 100:
            raise HTTPException(status_code=400, detail="name muss 1–100 Zeichen lang sein")
        patch["name"] = n
    for key in ("subject", "html_body", "text_body"):
        if key in data:
            patch[key] = data[key] or ""
    if not patch:
        raise HTTPException(status_code=400, detail="nichts zu ändern")
    try:
        return await pb_client.pb_patch(f"/api/collections/email_templates/records/{template_id}", patch)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 400 and ("prefix" in exc.response.text or "name" in exc.response.text):
            raise HTTPException(status_code=409, detail="Vorlage mit diesem Präfix+Name existiert bereits")
        raise


@app.delete("/templates/{template_id}")
async def templates_delete(template_id: str):
    await pb_client.pb_delete(f"/api/collections/email_templates/records/{template_id}")
    return {"status": "deleted"}


_GROUP_NAME_RE = re.compile(r"^[a-z0-9_\-]{1,60}$")


@app.get("/contact-groups")
async def contact_groups_list():
    data = await pb_client.pb_get(
        "/api/collections/contact_groups/records",
        params={"perPage": 500, "sort": "name"},
    )
    return data.get("items", [])


@app.post("/contact-groups")
async def contact_groups_create(data: dict):
    name = (data.get("name") or "").strip().lower()
    if not _GROUP_NAME_RE.match(name):
        raise HTTPException(status_code=400, detail="name ungültig (1–60 Zeichen, nur a-z, 0-9, _, -)")
    record = {
        "name": name,
        "description": (data.get("description") or "").strip(),
    }
    try:
        return await pb_client.pb_post("/api/collections/contact_groups/records", record)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 400 and "name" in exc.response.text:
            raise HTTPException(status_code=409, detail=f"Gruppe '{name}' existiert bereits")
        raise


@app.patch("/contact-groups/{group_id}")
async def contact_groups_update(group_id: str, data: dict):
    patch: dict = {}
    if "name" in data:
        n = (data["name"] or "").strip().lower()
        if not _GROUP_NAME_RE.match(n):
            raise HTTPException(status_code=400, detail="name ungültig")
        patch["name"] = n
    if "description" in data:
        patch["description"] = (data["description"] or "").strip()
    if not patch:
        raise HTTPException(status_code=400, detail="nichts zu ändern")
    try:
        return await pb_client.pb_patch(f"/api/collections/contact_groups/records/{group_id}", patch)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 400 and "name" in exc.response.text:
            raise HTTPException(status_code=409, detail="Gruppe mit diesem Namen existiert bereits")
        raise


@app.delete("/contact-groups/{group_id}")
async def contact_groups_delete(group_id: str):
    # PocketBase: cascadeDelete=False auf contacts.groups → Kontakte bleiben, Relation wird gelöscht
    await pb_client.pb_delete(f"/api/collections/contact_groups/records/{group_id}")
    return {"status": "deleted"}


@app.get("/contact-groups/{group_id}/members")
async def contact_groups_members(group_id: str):
    data = await pb_client.pb_get(
        "/api/collections/contacts/records",
        params={"filter": f'groups~"{group_id}"', "perPage": 1000, "sort": "name"},
    )
    return data.get("items", [])


# ─── Kontakt-Import ──────────────────────────────────────────────────────
# Format pro Zeile: email,name,gruppen
#   - email:    erforderlich
#   - name:     optional, leerer Name = bestehenden Wert nicht überschreiben
#   - gruppen:  optional, mit ; getrennt; mehrfache Zeilen pro email werden gemerged
#
# Modes:
#   add    (default): Kontakt anlegen oder aktualisieren, Gruppen additiv,
#                     name überschreiben wenn nicht leer, unbekannte Gruppen
#                     werden automatisch angelegt
#   remove: Kontakt-Gruppen-Zuordnungen entfernen; Kontakt + andere Gruppen
#           bleiben unverändert; unbekannte Email = "not_found"

_IMPORT_EMAIL_RE = re.compile(r'^[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}$')


def _norm_group_name(raw: str) -> str | None:
    """Lowercase + Whitespace zu _ → fertiger Gruppen-Name. None wenn ungültig."""
    if not raw:
        return None
    name = re.sub(r'\s+', '_', raw.strip().lower())
    if _GROUP_NAME_RE.match(name):
        return name
    return None


def _parse_import_line(line: str, lineno: int) -> tuple | None:
    """Returns (email, name, [group_names], invalid_reason)."""
    parts = [p.strip() for p in line.split(',', 2)]
    while len(parts) < 3:
        parts.append('')
    email_raw, name, groups_raw = parts
    email = email_raw.strip().lower()
    if not email:
        return (None, None, None, "Email leer")
    if not _IMPORT_EMAIL_RE.match(email):
        return (None, None, None, f"Email ungültig: {email_raw}")
    groups = []
    invalid_groups = []
    if groups_raw:
        for g in groups_raw.split(';'):
            normalized = _norm_group_name(g)
            if normalized:
                groups.append(normalized)
            elif g.strip():
                invalid_groups.append(g.strip())
    invalid_reason = None
    if invalid_groups:
        invalid_reason = f"Gruppen-Namen ungültig: {', '.join(invalid_groups)}"
    return (email, name, groups, invalid_reason)


@app.post("/contacts/import")
async def contacts_import(data: dict):
    """Importiert Kontakte + Gruppen-Zuordnungen aus einer Multiline-Liste."""
    lines_raw = data.get("lines") or ""
    mode = (data.get("mode") or "add").lower()
    if mode not in ("add", "remove"):
        raise HTTPException(status_code=400, detail="mode muss 'add' oder 'remove' sein")
    if not lines_raw.strip():
        raise HTTPException(status_code=400, detail="lines fehlt")

    # Parse alle Zeilen, merge nach email
    contacts_map: dict[str, dict] = {}   # email -> {name, groups (set)}
    invalid: list[dict] = []
    for lineno, line in enumerate(lines_raw.splitlines(), start=1):
        if not line.strip():
            continue
        parsed = _parse_import_line(line, lineno)
        email, name, groups, invalid_reason = parsed
        if invalid_reason and not email:
            invalid.append({"line": lineno, "raw": line, "reason": invalid_reason})
            continue
        if email is None:
            continue
        entry = contacts_map.setdefault(email, {"name": "", "groups": set()})
        if name:
            entry["name"] = name  # letzter nicht-leerer Name gewinnt
        for g in groups or []:
            entry["groups"].add(g)
        if invalid_reason and groups is not None and not groups:
            # nur fehlerhafte Gruppen-Namen, kein gültiger Eintrag
            invalid.append({"line": lineno, "raw": line, "reason": invalid_reason})

    # Lade bestehende Gruppen, Auto-Anlegen wo nötig (nur im add-Mode)
    existing_groups_resp = await pb_client.pb_get(
        "/api/collections/contact_groups/records",
        params={"perPage": 500},
    )
    group_name_to_id = {g["name"]: g["id"] for g in existing_groups_resp.get("items", [])}

    auto_created_groups: list[str] = []
    all_used_groups = set()
    for entry in contacts_map.values():
        all_used_groups.update(entry["groups"])

    if mode == "add":
        for gname in all_used_groups:
            if gname not in group_name_to_id:
                try:
                    created = await pb_client.pb_post(
                        "/api/collections/contact_groups/records",
                        {"name": gname, "description": ""},
                    )
                    group_name_to_id[gname] = created["id"]
                    auto_created_groups.append(gname)
                except Exception as exc:
                    logger.warning("Auto-Anlegen Gruppe %s fehlgeschlagen: %s", gname, exc)

    # Pro email: bestehenden Kontakt finden + add/remove anwenden
    counts = {"added": 0, "updated": 0, "unchanged": 0,
              "removed_from": 0, "not_found": 0, "errors": 0}

    for email, entry in contacts_map.items():
        try:
            resp = await pb_client.pb_get(
                "/api/collections/contacts/records",
                params={"filter": f'email="{email}"', "perPage": 1},
            )
            items = resp.get("items", [])
            current = items[0] if items else None

            new_group_ids = []
            for gname in entry["groups"]:
                gid = group_name_to_id.get(gname)
                if gid:
                    new_group_ids.append(gid)

            if mode == "add":
                if current:
                    patch = {}
                    if entry["name"] and entry["name"] != (current.get("name") or ""):
                        patch["name"] = entry["name"]
                    current_groups = set(current.get("groups") or [])
                    merged = current_groups | set(new_group_ids)
                    if merged != current_groups:
                        patch["groups"] = list(merged)
                    if patch:
                        await pb_client.pb_patch(
                            f"/api/collections/contacts/records/{current['id']}",
                            patch,
                        )
                        counts["updated"] += 1
                    else:
                        counts["unchanged"] += 1
                else:
                    await pb_client.pb_post(
                        "/api/collections/contacts/records",
                        {
                            "email": email,
                            "name": entry["name"] or "",
                            "groups": new_group_ids,
                            "unsubscribed": False,
                        },
                    )
                    counts["added"] += 1

            elif mode == "remove":
                if not current:
                    counts["not_found"] += 1
                    continue
                if not new_group_ids:
                    # Keine Gruppen angegeben → no-op, in unchanged zählen
                    counts["unchanged"] += 1
                    continue
                current_groups = set(current.get("groups") or [])
                remaining = current_groups - set(new_group_ids)
                if remaining != current_groups:
                    await pb_client.pb_patch(
                        f"/api/collections/contacts/records/{current['id']}",
                        {"groups": list(remaining)},
                    )
                    counts["removed_from"] += 1
                else:
                    counts["unchanged"] += 1

        except Exception as exc:
            logger.warning("Import-Fehler für %s: %s", email, exc)
            counts["errors"] += 1

    return {
        "mode": mode,
        "counts": counts,
        "invalid": invalid,
        "auto_created_groups": auto_created_groups,
        "total_lines_parsed": len(contacts_map) + len(invalid),
    }


@app.post("/templates/render")
async def templates_render(data: dict):
    """Rendert html + subject mit Snippets, globalen Variablen und optional
    einem Kontakt. Wird vom Frontend fuer Live-Preview genutzt und spaeter
    von Compose/Bulk-Send.

    Body:
      html (str)
      subject (str, optional)
      active_sections (list[str], optional — None = alle aktiv)
      contact_id (str, optional — fuer Phase-2-Rendering)
    """
    html = data.get("html") or ""
    subject = data.get("subject") or ""
    active_sections = data.get("active_sections")
    contact_id = data.get("contact_id")

    snippets = await rendering.load_snippets_map()
    variables = await rendering.load_variables_map()

    contact = None
    if contact_id:
        try:
            contact = await pb_client.pb_get(f"/api/collections/contacts/records/{contact_id}")
        except Exception:
            contact = None

    rendered_html = rendering.render_full(html, snippets, variables, active_sections, contact)
    rendered_subject = rendering.render_full(subject, snippets, variables, active_sections, contact)
    unresolved = rendering.find_unresolved(rendered_html) + rendering.find_unresolved(rendered_subject)

    return {
        "html": rendered_html,
        "subject": rendered_subject,
        "unresolved": unresolved,
    }
