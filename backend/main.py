import logging
import re
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
from backfill import run_once_if_needed, rebuild_fts_if_needed, backfill_html_once
from config import settings
from fts import fts_setup, fts_search, fts_rebuild, fts_delete
from idle_manager import idle_manager, get_sse_queues
from imap_sync import sync_all_accounts, get_sync_status, upsert_contact
from imap_utils import find_imap_folder
from models import HealthResponse, SyncStatusResponse
from scheduler import start_scheduler, stop_scheduler
from smtp_sender import send_email as smtp_send_email

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)

# Temporärer Speicher für hochgeladene Anhänge (in-memory, max. 25 MB pro Datei)
_temp_uploads: dict[str, dict] = {}  # {temp_id: {filename, content_type, data: bytes}}


def _pb_safe(q: str) -> str:
    """Entfernt Sonderzeichen für PocketBase-Filter."""
    return q.strip().replace('"', '').replace("'", "").replace("\\", "")


def _email_filters(account: str | None, folder: str | None, is_read: str | None) -> list[str]:
    """Baut PocketBase-Filter für E-Mail-Abfragen."""
    filters = []
    if account:
        filters.append(f'account="{account}"')
    if folder:
        filters.append(f'folder="{folder}"')
    if is_read == "true":
        filters.append("is_read=true")
    elif is_read == "false":
        filters.append("is_read=false")
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
    for local in ("http://localhost", "http://127.0.0.1"):
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

    data = await pb_client.pb_get("/api/collections/emails/records", params={
        "filter": " && ".join(filters),
        "perPage": 100,
        "sort": "-date_sent",
        "fields": ("id,account,folder,message_id,thread_id,from_email,from_name,"
                   "reply_to,to_emails,subject,snippet,date_sent,is_read,is_flagged,"
                   "is_answered,ai_category,has_attachments,imap_uid"),
    })
    items = data.get("items", [])
    for e in items:
        e["display_thread_id"] = e.get("thread_id") or e.get("message_id") or e["id"]
    return {"items": items, "totalItems": len(items)}


@app.get("/emails")
async def get_emails(account: str | None = None, folder: str | None = None,
                     page: int = 1, limit: int = 50, is_read: str | None = None):
    filters = _email_filters(account, folder, is_read)

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
    """Ungelesen-Zähler aller Ordner aus der folders-Collection."""
    return await pb_client.pb_get(
        "/api/collections/folders/records",
        params={"perPage": 200, "fields": "id,account,imap_path,unread_count"}
    )


@app.get("/emails/threaded")
async def get_emails_threaded(account: str | None = None, folder: str | None = None,
                              page: int = 1, limit: int = 100,
                              is_read: str | None = None):
    """
    Returns emails sorted by thread: newest thread first, within thread oldest-first.
    Threads split by Fwd: are merged when normalized subject + participants overlap.
    """
    filters = _email_filters(account, folder, is_read)

    fields = ("id,account,folder,message_id,thread_id,in_reply_to,from_email,"
              "from_name,reply_to,to_emails,subject,snippet,date_sent,is_read,is_flagged,"
              "is_answered,ai_category,has_attachments,imap_uid")

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
                               is_read: str | None = None):
    """
    Returns emails grouped by sender: most-recent-contact first,
    within each sender group newest email first.
    """
    filters = _email_filters(account, folder, is_read)

    fields = ("id,account,folder,message_id,thread_id,in_reply_to,from_email,"
              "from_name,reply_to,to_emails,subject,snippet,date_sent,is_read,is_flagged,"
              "is_answered,ai_category,has_attachments,imap_uid")

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


@app.post("/emails/send")
async def send_email_endpoint(data: dict):
    """Sendet eine E-Mail via SMTP und speichert sie im Sent-Ordner."""
    logger.info("POST /emails/send empfangen: to=%s subject=%s smtp=%s account=%s",
                data.get("to"), data.get("subject"), data.get("smtp_server"), data.get("from_account"))
    to = data.get("to", "").strip()
    cc = data.get("cc", "").strip()
    subject = data.get("subject", "").strip()
    body = data.get("body", "")
    body_html = data.get("body_html", "")
    quote = data.get("quote", "")
    quote_html = data.get("quote_html", "")
    from_account = data.get("from_account", "")
    smtp_server = data.get("smtp_server", "")

    if not to:
        raise HTTPException(status_code=400, detail="Empfänger (to) fehlt")
    if not from_account:
        raise HTTPException(status_code=400, detail="Absender-Account fehlt")
    if not smtp_server:
        raise HTTPException(status_code=400, detail="SMTP-Server fehlt")

    # Temporäre Uploads zu Anhängen zusammenstellen
    attachment_ids = data.get("attachment_ids") or []
    attachments = [_temp_uploads[aid] for aid in attachment_ids if aid in _temp_uploads]

    try:
        message_id = await smtp_send_email(
            smtp_server_id=smtp_server,
            from_account_id=from_account,
            to=to,
            cc=cc,
            subject=subject,
            body=body,
            body_html=body_html,
            quote=quote,
            quote_html=quote_html,
            attachments=attachments or None,
        )
    except Exception as exc:
        logger.error("SMTP-Versand fehlgeschlagen: %s", exc)
        raise HTTPException(status_code=502, detail=f"SMTP-Fehler: {exc}")

    # Temporäre Uploads nach erfolgreichem Versand bereinigen
    for aid in attachment_ids:
        _temp_uploads.pop(aid, None)

    # Empfänger in Contacts-Collection anlegen oder aktualisieren
    # Empfänger-Adresse aus "Name <email>" oder "email" extrahieren
    _m = re.search(r'[\w.+-]+@[\w.-]+\.\w+', to)
    if _m:
        _name_m = re.match(r'^(.+?)\s*<', to.strip())
        _to_name = _name_m.group(1).strip().strip('"') if _name_m else ""
        from datetime import datetime, timezone as _tz
        asyncio.create_task(upsert_contact(_m.group(0).lower(), _to_name, datetime.now(_tz.utc).strftime("%Y-%m-%d %H:%M:%S")))

    # Ursprungs-E-Mail als beantwortet markieren (PocketBase + IMAP)
    in_reply_to_email_id = data.get("in_reply_to_email_id")
    if in_reply_to_email_id:
        try:
            original = await pb_client.pb_get(
                f"/api/collections/emails/records/{in_reply_to_email_id}"
            )
            # Sicherstellen, dass die E-Mail zum selben Account gehört
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
            logger.warning("is_answered konnte nicht gesetzt werden für %s: %s", in_reply_to_email_id, exc)

    return {"sent": True, "message_id": message_id}



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
async def get_email(email_id: str):
    return await pb_client.pb_get(f"/api/collections/emails/records/{email_id}")


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
async def move_to_spam(email_id: str):
    """Verschiebt E-Mail in den Spam-Ordner (IMAP + PocketBase)."""
    email = await pb_client.pb_get(f"/api/collections/emails/records/{email_id}")
    source_folder = email.get("folder", "INBOX")
    new_folder, new_uid = "Spam", None
    try:
        new_folder, new_uid = await _imap_move_to_spam(email)
    except Exception as e:
        logger.warning(f"IMAP spam move failed for {email_id}: {e}")
    patch = {"folder": new_folder or "Spam"}
    if new_uid:
        patch["imap_uid"] = new_uid
    await pb_client.pb_patch(f"/api/collections/emails/records/{email_id}", patch)
    try:
        await _update_folder_unread_count(email["account"], source_folder)
    except Exception as e:
        logger.warning(f"folder unread_count update failed after spam move {email_id}: {e}")
    return {"moved_to": new_folder}


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
    from imapclient import IMAPClient
    with IMAPClient(acc["imap_host"], port=int(acc.get("imap_port") or 993), ssl=True) as srv:
        srv.login(acc["imap_user"], acc["imap_pass"])
        srv.select_folder(folder)
        spam = find_imap_folder(srv, [b"\\Junk", b"\\Spam"], ["Spam", "Junk", "Junk E-Mail", "INBOX.Spam", "INBOX.Junk"])
        if spam and spam.lower() != folder.lower():
            caps = srv.capabilities()
            if b"MOVE" in caps:
                srv.move([imap_uid], spam)
            else:
                srv.copy([imap_uid], spam)
                srv.set_flags([imap_uid], [b"\\Deleted"])
                srv.expunge()
            new_uid = _imap_search_by_msgid(srv, spam, message_id)
            return spam or "Spam", new_uid
        return spam or "Spam", None


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
    """Verschiebt E-Mail in einen anderen Ordner (IMAP + PocketBase)."""
    target_folder = (data.get("target_folder") or "").strip()
    if not target_folder:
        raise HTTPException(status_code=400, detail="target_folder fehlt")

    email = await pb_client.pb_get(f"/api/collections/emails/records/{email_id}")
    try:
        new_uid = await _imap_move(email, target_folder)
    except Exception as e:
        logger.warning(f"IMAP move failed for {email_id}: {e}")
        raise HTTPException(status_code=502, detail=f"IMAP-Fehler: {e}")

    source_folder = email.get("folder", "INBOX")
    patch = {"folder": target_folder, "is_read": True}
    if new_uid:
        patch["imap_uid"] = new_uid
        logger.info("move_email: %s → '%s', neue imap_uid=%s", email_id, target_folder, new_uid)
    # IMAP: \Seen auf neuer UID setzen
    if new_uid:
        try:
            await _imap_set_read({"account": email["account"], "imap_uid": new_uid, "folder": target_folder}, True)
        except Exception as ex:
            logger.warning("move_email: IMAP mark-read fehlgeschlagen: %s", ex)
    await pb_client.pb_patch(f"/api/collections/emails/records/{email_id}", patch)
    try:
        await asyncio.gather(
            _update_folder_unread_count(email["account"], source_folder),
            _update_folder_unread_count(email["account"], target_folder),
        )
    except Exception as e:
        logger.warning("move_email: folder unread_count update fehlgeschlagen: %s", e)
    return {"moved_to": target_folder, "marked_read": True}


def _imap_move_sync(acc: dict, imap_uid: int, source_folder: str, target_folder: str, message_id: str) -> int | None:
    from imapclient import IMAPClient
    with IMAPClient(acc["imap_host"], port=int(acc.get("imap_port") or 993), ssl=True) as srv:
        srv.login(acc["imap_user"], acc["imap_pass"])
        srv.select_folder(source_folder)
        caps = srv.capabilities()
        if b"MOVE" in caps:
            srv.move([imap_uid], target_folder)
        else:
            srv.copy([imap_uid], target_folder)
            srv.set_flags([imap_uid], [b"\\Deleted"])
            srv.expunge()
        return _imap_search_by_msgid(srv, target_folder, message_id)


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
        srv.select_folder(folder)
        caps = srv.capabilities()
        if b"MOVE" in caps:
            trash = find_imap_folder(srv, [b"\\Trash", b"\\Deleted"], ["Trash", "Deleted", "Deleted Items", "Papierkorb", "INBOX.Trash"])
            if trash and trash.lower() != folder.lower():
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


# ---------------------------------------------------------------------------

def _imap_set_read_sync(acc: dict, imap_uid: int, folder: str, is_read: bool) -> None:
    from imapclient import IMAPClient
    with IMAPClient(acc["imap_host"], port=int(acc.get("imap_port") or 993), ssl=True) as srv:
        srv.login(acc["imap_user"], acc["imap_pass"])
        srv.select_folder(folder)
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
