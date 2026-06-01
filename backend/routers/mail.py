"""Mail-Endpoints — der dicke Block: /search, /emails/*, /attachments/*,
/spam-rules/*, Spam-Suggestion + Bulk-Read.

Ausgegliedert aus main.py im Rahmen von C1 Phase 2 (5c.2). Vor dem Schnitt
(5c.1) wurden die Cross-Cutting-Helpers nach `services/mail.py` ausgelagert,
damit dieser Router sie sauber importieren kann — kein Zirkular-Import mit
main.py mehr.

Auth-Mix:
- User-Endpoints (CRUD, Reads): PB-User-Token via `pb_user_auth.get_user_token`
- `/attachments/{id}/download` + `/emails/{id}/inline`: bewusste Admin-Nutzung
  (werden per signiertem URL aufgerufen, ohne Bearer-Header möglich)
- `/attachments/upload` + `/attachments/upload/{id}` DELETE: keine User-Auth
  per Dependency — der Endpoint nimmt Files direkt entgegen (global-Middleware
  übernimmt Bearer-Check)
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import re
import time
import uuid as _uuid_mod
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Literal
from urllib.parse import quote as _url_quote

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field, field_validator

import pb_client
import pb_user_auth
import spam_filter
from config import settings
from fts import fts_delete, fts_search
from services.imap import ImapService
from services.mail import (
    _bulk_attachments_by_id,
    _do_send_job,
    _format_pb_dt,
    _get_imap_account,
    _imap_move,
    _imap_move_to_spam,
    _imap_set_read,
    _imap_trash,
    _send_jobs,
    _temp_uploads,
    _update_folder_unread_count,
)

logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Konstanten + Mail-only Helpers
# ---------------------------------------------------------------------------

MAX_UPLOAD_SIZE = 25 * 1024 * 1024            # 25 MB pro Datei
MAX_TOTAL_UPLOAD_SIZE = 200 * 1024 * 1024     # 200 MB über alle aktiven Uploads

# P-Perf-2 (2026-05-23): Listen-Endpoints liefern nur Header/Listen-Felder.
# body_html/body_plain bleiben dem Detail-Endpoint vorbehalten — Marketing-Mails
# haben oft 100 KB+ HTML und das addiert sich bei 100er-Paginierung schnell.
_EMAIL_LIST_FIELDS = (
    "id,account,folder,message_id,thread_id,in_reply_to,from_email,from_name,"
    "reply_to,to_emails,subject,snippet,date_sent,is_read,is_flagged,is_answered,"
    "ai_category,has_attachments,imap_uid,spam_suggested,spam_score,spam_rule_match"
)

_EMAIL_RE = re.compile(r"^[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}$")

_SUBJECT_PREFIX_RE = re.compile(
    r"^\s*(Re|Fwd?|AW|WG|FW|SV|Antw?)\s*:\s*",
    re.IGNORECASE,
)


def _email_filters(account: str | None, folder: str | None,
                   is_read: str | None, webhook: str | None = None) -> list[str]:
    """Baut PocketBase-Filter für E-Mail-Abfragen.

    `webhook="true"` → nur Webhook-Versand (Feld nicht leer);
    `webhook="false"` → nur normaler Versand (Feld leer).
    Wird im Sent-Ordner statt is_read als Filter genutzt.
    """
    filters = []
    if account:
        filters.append(f'account={pb_client.pb_quote(account)}')
    if folder:
        filters.append(f'folder={pb_client.pb_quote(folder)}')
    if is_read == "true":
        filters.append("is_read=true")
    elif is_read == "false":
        filters.append("is_read=false")
    if webhook == "true":
        filters.append('webhook!=""')
    elif webhook == "false":
        filters.append('webhook=""')
    return filters


def _normalize_subject(subject: str) -> str:
    """Strip Re:/Fwd:/AW:/WG: prefixes, return lowercased subject root."""
    s = (subject or "").strip()
    while True:
        s2 = _SUBJECT_PREFIX_RE.sub("", s).strip()
        if s2 == s:
            return s.lower()
        s = s2


def _get_external_participants(email_group: list) -> set[str]:
    """Sammelt alle E-Mail-Adressen aus From und Reply-To, die NICHT zentrale@hpa24.de sind."""
    YOUR_EMAIL = "zentrale@hpa24.de"
    external = set()
    for email in email_group:
        from_email = (email.get("from_email") or "").lower().strip()
        if from_email and from_email != YOUR_EMAIL:
            external.add(from_email)
        reply_to = (email.get("reply_to") or "").lower().strip()
        if reply_to and reply_to != YOUR_EMAIL:
            external.add(reply_to)
    return external


def _can_merge(existing: list, members: list) -> bool:
    """Prüft, ob zwei Thread-Gruppen zusammengeführt werden dürfen.

    Wenn beide Gruppen externe Teilnehmer haben, müssen diese identisch sein.
    Wenn eine Gruppe keine externen Teilnehmer hat, lassen wir den Merge zu (neutral).
    """
    external_existing = _get_external_participants(existing)
    external_new = _get_external_participants(members)
    if external_existing and external_new:
        return external_existing == external_new
    return True


# ---------------------------------------------------------------------------
# Pydantic-Modelle
# ---------------------------------------------------------------------------


class SetCategoryRequest(BaseModel):
    ai_category: Literal["focus", "quick-reply", "office", "info-trash", ""] = ""


class BulkEmailRef(BaseModel):
    id: str
    account: str = ""
    folder: str = ""
    imap_uid: int | None = None


class BulkReadRequest(BaseModel):
    emails: list[BulkEmailRef]
    is_read: bool = True


class MoveEmailRequest(BaseModel):
    target_folder: str = Field(..., min_length=1)

    @field_validator("target_folder")
    @classmethod
    def strip_target(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("target_folder darf nicht leer sein")
        return v


class SendEmailRequest(BaseModel):
    """POST /emails/send — Einzelversand."""
    to: str = Field(..., min_length=1)
    from_account: str = Field(..., min_length=1)
    smtp_server: str = Field(..., min_length=1)
    subject: str = ""
    cc: str = ""
    body: str = ""
    body_html: str = ""
    quote: str = ""
    quote_html: str = ""
    attachment_ids: list[str] = []
    draft_id: str | None = None
    in_reply_to_email_id: str | None = None

    @field_validator("to", "from_account", "smtp_server")
    @classmethod
    def _strip_required(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("darf nicht leer sein")
        return v

    @field_validator("subject", "cc")
    @classmethod
    def _strip_optional(cls, v: str) -> str:
        return (v or "").strip()


class BulkSendRequest(BaseModel):
    """POST /emails/bulk-send — Versand an viele Empfänger mit Zeitversatz.

    Body wie /emails/send, aber statt ``to`` eine ``recipients``-Liste und ein
    ``delay_seconds``-Abstand zwischen den Mails. Die Address-Validierung
    (Format, Dedup, bounced/unsubscribed-Filter) liegt im Endpoint, da
    Pydantic die Reihenfolge-erhaltende Dedup-Semantik nicht abbildet.
    """
    recipients: list[str] = Field(..., min_length=1)
    from_account: str = Field(..., min_length=1)
    smtp_server: str = Field(..., min_length=1)
    subject: str = ""
    body: str = ""
    body_html: str = ""
    attachment_ids: list[str] = []
    delay_seconds: float = 5.0

    @field_validator("from_account", "smtp_server")
    @classmethod
    def _strip_required(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("darf nicht leer sein")
        return v

    @field_validator("subject")
    @classmethod
    def _strip_subject(cls, v: str) -> str:
        return (v or "").strip()

    @field_validator("delay_seconds")
    @classmethod
    def _clamp_delay(cls, v: float) -> float:
        return max(0.0, min(v, 300.0))


class CreateDraftRequest(BaseModel):
    """POST /emails/draft — Entwurf anlegen."""
    from_account: str = Field(..., min_length=1)
    to: str = ""
    subject: str = ""
    body: str = ""
    body_html: str = ""
    quote: str = ""

    @field_validator("from_account")
    @classmethod
    def _strip_account(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("darf nicht leer sein")
        return v


class UpdateDraftRequest(BaseModel):
    """PATCH /emails/draft/{draft_id} — Entwurf aktualisieren. Alle Felder optional."""
    to: str = ""
    subject: str = ""
    body: str = ""
    body_html: str = ""
    quote: str = ""


# ---------------------------------------------------------------------------
# Search (FTS5 + PocketBase-Fallback)
# ---------------------------------------------------------------------------


@router.get("/search")
async def search_emails(q: str, account: str | None = None,
                        folder: str | None = None, is_read: str | None = None,
                        token: str = Depends(pb_user_auth.get_user_token)):
    """Volltextsuche via FTS5-Index mit PocketBase-Fallback."""
    if not q or not q.strip():
        return {"items": [], "totalItems": 0}

    raw = q.strip()
    fts_ids: list[str] = []
    use_fts = False

    # FTS5-Suche: Phrase bei Mehrwort, sonst Einzelwort; Fallback AND-Suche
    phrase = f'"{raw.replace(chr(34), "")}"' if " " in raw else raw
    try:
        # P-Perf-1 (2026-05-23): SQLite-FTS5 ist synchron; im Executor laufen
        # lassen, damit Event-Loop (IMAP-Sync, Scheduler) während der Suche
        # nicht blockiert.
        fts_ids = await asyncio.to_thread(fts_search, settings.PB_DATA_PATH, phrase)
        if not fts_ids and " " in raw:
            fts_ids = await asyncio.to_thread(fts_search, settings.PB_DATA_PATH, raw)
        use_fts = bool(fts_ids)
    except Exception as e:
        logger.warning(f"FTS5 search failed: {e}")

    if use_fts:
        top_ids = fts_ids[:100]
        id_filter = " || ".join(f'id={pb_client.pb_quote(i)}' for i in top_ids)
        filters = [f"({id_filter})"]
    else:
        # Fallback: PocketBase-LIKE auf Betreff + Absender (kein body_plain → keine Zitatttreffer)
        logger.info(f"FTS5 empty for '{raw}', falling back to PocketBase LIKE search")
        qq = pb_client.pb_quote(raw)
        filters = [f'(subject ~ {qq} || from_email ~ {qq} || from_name ~ {qq})']

    if account:
        filters.append(f'account={pb_client.pb_quote(account)}')
    if is_read == "true":
        filters.append("is_read=true")
    elif is_read == "false":
        filters.append("is_read=false")

    fields = ("id,account,folder,message_id,thread_id,from_email,from_name,"
              "reply_to,to_emails,cc_emails,subject,snippet,date_sent,is_read,is_flagged,"
              "is_answered,ai_category,has_attachments,imap_uid,"
              "spam_suggested,spam_score,spam_rule_match")

    data = await pb_client.pb_get_as(token, "/api/collections/emails/records", params={
        "filter": " && ".join(filters),
        "perPage": 100,
        "sort": "-date_sent",
        "fields": fields,
    })
    items = data.get("items", [])

    # Zusätzlich: im Sent-Ordner auch nach Empfänger (to_emails) suchen,
    # damit "an wen habe ich geschrieben?" funktioniert.
    sent_filters = ['folder="Sent"', f'to_emails ~ {pb_client.pb_quote(raw)}']
    if account:
        sent_filters.append(f'account={pb_client.pb_quote(account)}')
    if is_read == "true":
        sent_filters.append("is_read=true")
    elif is_read == "false":
        sent_filters.append("is_read=false")
    sent_data = await pb_client.pb_get_as(token, "/api/collections/emails/records", params={
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


# ---------------------------------------------------------------------------
# Listen-Endpoints: /emails, /emails/threaded, /emails/by-sender
# ---------------------------------------------------------------------------


@router.get("/emails")
async def get_emails(account: str | None = None, folder: str | None = None,
                     page: int = 1, limit: int = 50, is_read: str | None = None,
                     webhook: str | None = None,
                     token: str = Depends(pb_user_auth.get_user_token)):
    filters = _email_filters(account, folder, is_read, webhook)

    params = {
        "perPage": limit,
        "page": page,
        "sort": "-date_sent",
        "fields": _EMAIL_LIST_FIELDS,
    }
    if filters:
        params["filter"] = " && ".join(filters)

    return await pb_client.pb_get_as(token, "/api/collections/emails/records", params=params)


@router.get("/emails/threaded")
async def get_emails_threaded(account: str | None = None, folder: str | None = None,
                              page: int = 1, limit: int = 100,
                              is_read: str | None = None,
                              webhook: str | None = None,
                              token: str = Depends(pb_user_auth.get_user_token)):
    """Returns emails sorted by thread: newest thread first, within thread oldest-first.
    Threads split by Fwd: are merged when normalized subject + participants overlap."""
    filters = _email_filters(account, folder, is_read, webhook)

    params = {
        "perPage": limit,
        "page": page,
        "sort": "-date_sent",
        "fields": _EMAIL_LIST_FIELDS,
    }
    if filters:
        params["filter"] = " && ".join(filters)

    data = await pb_client.pb_get_as(token, "/api/collections/emails/records", params=params)
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

    for members in thread_map.values():
        members.sort(key=lambda e: e.get("date_sent") or "", reverse=True)

    # --- Pass 2: Merge threads split by Fwd: ---
    merged: list[list] = []
    norm_index: dict[str, int] = {}

    for members in thread_map.values():
        if not members:
            continue
        norm = _normalize_subject(members[0].get("subject", ""))

        if len(norm) > 1 and norm in norm_index:
            existing = merged[norm_index[norm]]
            if _can_merge(existing, members):
                root_tid = existing[0].get("display_thread_id") or existing[0]["_tid"]
                existing.extend(members)
                existing.sort(key=lambda e: e.get("date_sent") or "", reverse=True)
                for e in existing:
                    e["display_thread_id"] = root_tid
                continue

        root_tid = members[0]["_tid"]
        for e in members:
            e["display_thread_id"] = root_tid
        merged.append(members)
        if len(norm) > 1:
            norm_index[norm] = len(merged) - 1

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


@router.get("/emails/by-sender")
async def get_emails_by_sender(account: str | None = None, folder: str | None = None,
                               page: int = 1, limit: int = 100,
                               is_read: str | None = None,
                               webhook: str | None = None,
                               token: str = Depends(pb_user_auth.get_user_token)):
    """Returns emails grouped by sender: most-recent-contact first, within group newest first."""
    filters = _email_filters(account, folder, is_read, webhook)

    params = {
        "perPage": limit,
        "page": page,
        "sort": "-date_sent",
        "fields": _EMAIL_LIST_FIELDS,
    }
    if filters:
        params["filter"] = " && ".join(filters)

    data = await pb_client.pb_get_as(token, "/api/collections/emails/records", params=params)
    emails = data.get("items", [])
    total_items = data.get("totalItems", 0)
    total_pages = data.get("totalPages", 1)

    sender_map: dict[str, list] = {}
    sender_order: list[str] = []
    for email in emails:
        reply_to = (email.get("reply_to") or "").lower().strip()
        sender = reply_to if reply_to else (email.get("from_email") or "").lower().strip()
        email["display_thread_id"] = sender
        if sender not in sender_map:
            sender_map[sender] = []
            sender_order.append(sender)
        sender_map[sender].append(email)

    for members in sender_map.values():
        members.sort(key=lambda e: e.get("date_sent") or "", reverse=True)

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


# ---------------------------------------------------------------------------
# Send-Pipeline (Endpoint-Trigger; eigentliche Pipeline in services/mail.py)
# ---------------------------------------------------------------------------


@router.post("/emails/send")
async def send_email_endpoint(payload: SendEmailRequest,
                              token: str = Depends(pb_user_auth.get_user_token)):
    """Startet den E-Mail-Versand im Hintergrund und gibt sofort eine Job-ID zurück.

    Endpoint selbst macht keine direkten PB-Calls; der Background-Job (`_do_send_job`)
    nutzt bewusst weiterhin den Admin-Token, weil er über die Lebenszeit der
    User-Session hinaus laufen kann (z.B. Bulk-Send mit Sekunden-Delays).
    """
    data = payload.model_dump()
    attachments = [_temp_uploads[aid] for aid in data["attachment_ids"] if aid in _temp_uploads]

    job_id = str(_uuid_mod.uuid4())
    _send_jobs[job_id] = {"status": "sending", "to": data["to"], "subject": data["subject"]}
    logger.info("Sendejob %s gestartet: to=%s subject=%s", job_id, data["to"], data["subject"])

    asyncio.create_task(_do_send_job(job_id, data, attachments))

    return {"job_id": job_id, "status": "sending"}


@router.post("/emails/bulk-send")
async def bulk_send_endpoint(payload: BulkSendRequest,
                             token: str = Depends(pb_user_auth.get_user_token)):
    """Versendet dieselbe E-Mail einzeln an viele Empfänger mit Zeitversatz.

    Body wie ``/emails/send``, zusätzlich:
      - ``recipients``: list[str] — eine E-Mail-Adresse pro Eintrag
      - ``delay_seconds``: float (default 5.0) — Abstand zwischen den Mails

    B15: Versand-Zustand lebt in ``bulk_sends.recipients[i]`` mit ``next_attempt_at``
    pro Empfänger. Der ``_bulk_worker_loop`` (lifespan in main.py) pollt diese
    Einträge und spawned ``_do_send_job``. accounts-Read läuft im User-Kontext;
    der bulk_sends-Audit-Record und der Worker-Versand nutzen Admin-Token.
    """
    # Adressen normalisieren, validieren, deduplizieren (Reihenfolge erhalten)
    seen: set[str] = set()
    recipients: list[str] = []
    invalid: list[str] = []
    for raw in payload.recipients:
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

    # Phase 3b: bouncte + unsubscribed-Kontakte rausfiltern.
    filtered_out: list[dict] = []
    try:
        flagged_res = await pb_client.pb_get_as(
            token,
            "/api/collections/contacts/records",
            params={
                "filter": "bounced=true || unsubscribed=true",
                "perPage": 5000,
                "fields": "email,bounced,unsubscribed",
            },
        )
        flagged_map = {(c.get("email") or "").strip().lower():
                       ("bounced" if c.get("bounced") else "unsubscribed")
                       for c in flagged_res.get("items") or []}
    except Exception as exc:
        logger.warning("Filter-Read auf contacts(bounced/unsubscribed) fehlgeschlagen: %s", exc)
        flagged_map = {}
    if flagged_map:
        kept: list[str] = []
        for raw in recipients:
            m = re.search(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", raw)
            key = m.group(0).lower() if m else raw.lower()
            reason = flagged_map.get(key)
            if reason:
                filtered_out.append({"email": key, "raw": raw, "reason": reason})
            else:
                kept.append(raw)
        recipients = kept
        if not recipients:
            raise HTTPException(
                status_code=400,
                detail=(f"Alle {len(filtered_out)} Empfänger sind als bounced/"
                        "unsubscribed markiert — kein Versand möglich."),
            )

    from_account = payload.from_account
    smtp_server  = payload.smtp_server
    subject      = payload.subject
    delay_seconds = payload.delay_seconds

    attachments = [_temp_uploads[aid] for aid in payload.attachment_ids if aid in _temp_uploads]
    has_attachments = bool(attachments)

    bulk_id = str(_uuid_mod.uuid4())
    start_at = datetime.now(timezone.utc)
    pb_recipients: list[dict] = []
    jobs: list[dict] = []
    for idx, raw in enumerate(recipients):
        m = re.search(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", raw)
        email_l = m.group(0).lower() if m else raw.lower()
        name_m = re.match(r'^(.+?)\s*<', raw.strip())
        rec_name = name_m.group(1).strip().strip('"') if name_m else ""
        job_id = str(_uuid_mod.uuid4())
        next_at = start_at + timedelta(seconds=idx * delay_seconds)
        pb_recipients.append({
            "email": email_l,
            "name": rec_name,
            "raw": raw,
            "status": "queued",
            "message_id": None,
            "error": None,
            "sent_at": None,
            "next_attempt_at": _format_pb_dt(next_at),
            "job_id": job_id,
        })
        jobs.append({"job_id": job_id, "to": raw})

    try:
        acc = await pb_client.pb_get(f"/api/collections/accounts/records/{from_account}")
        from_email = acc.get("from_email") or ""
    except Exception:
        from_email = ""

    try:
        bulk_send_rec = await pb_client.pb_post(
            "/api/collections/bulk_sends/records",
            {
                "subject": subject,
                "from_account": from_account,
                "from_account_email": from_email,
                "smtp_server": smtp_server,
                "body_html": payload.body_html,
                "body_text": payload.body,
                "sent_at": _format_pb_dt(start_at),
                "delay_seconds": delay_seconds,
                "recipients": pb_recipients,
                "total_count": len(pb_recipients),
                "sent_count": 0,
                "error_count": 0,
                "bounced_count": 0,
                "has_attachments": has_attachments,
                "is_done": False,
            },
        )
        bulk_send_id = bulk_send_rec.get("id")
    except Exception as exc:
        logger.error("bulk_sends-Record konnte nicht angelegt werden: %s", exc)
        raise HTTPException(status_code=500,
                            detail="Aussendung konnte nicht angelegt werden")

    if attachments:
        _bulk_attachments_by_id[bulk_send_id] = attachments

    for job in jobs:
        _send_jobs[job["job_id"]] = {
            "status": "queued",
            "to": job["to"],
            "subject": subject,
            "bulk_id": bulk_id,
            "bulk_send_id": bulk_send_id,
        }

    logger.info("Bulk-Send angelegt: bulk=%s, audit=%s, n=%d, delay=%.1fs, subject=%s",
                bulk_id, bulk_send_id, len(jobs), delay_seconds, subject)

    return {
        "bulk_id": bulk_id,
        "bulk_send_id": bulk_send_id,
        "jobs": jobs,
        "delay_seconds": delay_seconds,
        "filtered_out": filtered_out,
    }


# ---------------------------------------------------------------------------
# Entwürfe
# ---------------------------------------------------------------------------


@router.post("/emails/draft")
async def create_draft(payload: CreateDraftRequest,
                       token: str = Depends(pb_user_auth.get_user_token)):
    """Erstellt einen neuen Entwurf in PocketBase."""
    import uuid

    account_id = payload.from_account

    try:
        acc = await pb_client.pb_get(f"/api/collections/accounts/records/{account_id}")
        from_email = acc.get("from_email", "")
        from_name = acc.get("from_name", "")
    except Exception:
        from_email = ""
        from_name = ""

    subject = payload.subject or "(Kein Betreff)"
    full_body = payload.body
    if payload.quote:
        full_body += "\n\n" + payload.quote

    draft = {
        "account": account_id,
        "folder": "Drafts",
        "message_id": f"<draft-{uuid.uuid4()}@mailflow>",
        "subject": subject,
        "body_plain": full_body,
        "body_html": payload.body_html,
        "snippet": (full_body[:120] if full_body else ""),
        "from_email": from_email,
        "from_name": from_name,
        "to_emails": [payload.to] if payload.to else [],
        "is_read": True,
        "date_sent": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
    }
    return await pb_client.pb_post_as(token, "/api/collections/emails/records", draft)


@router.post("/emails/draft/{draft_id}/sync")
async def sync_draft_to_imap(draft_id: str, token: str = Depends(pb_user_auth.get_user_token)):
    """APPENDet einen Entwurf in den IMAP-Drafts-Ordner."""
    import email.utils
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText

    draft = await pb_client.pb_get_as(token, f"/api/collections/emails/records/{draft_id}")
    account_id = draft.get("account")
    if not account_id:
        raise HTTPException(status_code=400, detail="Kein Account am Entwurf")

    acc = await pb_client.pb_get(f"/api/collections/accounts/records/{account_id}")

    body_text = draft.get("body_plain") or ""
    body_html = draft.get("body_html") or ""

    msg = MIMEMultipart("mixed")
    if body_html:
        alt = MIMEMultipart("alternative")
        alt.attach(MIMEText(body_text, "plain", "utf-8"))
        alt.attach(MIMEText(body_html, "html", "utf-8"))
        msg.attach(alt)
    else:
        msg.attach(MIMEText(body_text, "plain", "utf-8"))

    from_email = acc.get("from_email", "")
    from_name  = acc.get("from_name", "")
    to_emails  = draft.get("to_emails") or []
    to_str     = ", ".join(to_emails) if isinstance(to_emails, list) else str(to_emails)

    # Message-ID nach erstem Sync in PB persistieren, damit Folge-Syncs
    # dieselbe ID nutzen und append_draft die Vorgängerversion ersetzt.
    existing_msgid = draft.get("message_id") or ""
    message_id = existing_msgid or email.utils.make_msgid()

    msg["From"]       = email.utils.formataddr((from_name, from_email)) if from_name else from_email
    msg["To"]         = to_str
    msg["Subject"]    = draft.get("subject") or ""
    msg["Date"]       = email.utils.formatdate(localtime=True)
    msg["Message-ID"] = message_id

    msg_bytes = msg.as_bytes()

    try:
        await asyncio.to_thread(ImapService(acc).append_draft, msg_bytes, message_id)
    except Exception as exc:
        logger.error("IMAP Draft-APPEND fehlgeschlagen: %s", exc)
        raise HTTPException(status_code=502, detail=f"IMAP-Fehler: {exc}")

    if not existing_msgid:
        try:
            await pb_client.pb_patch_as(
                token, f"/api/collections/emails/records/{draft_id}",
                {"message_id": message_id},
            )
        except Exception as exc:
            logger.warning("Message-ID konnte nicht in PB persistiert werden: %s", exc)

    return {"synced": True}


@router.patch("/emails/draft/{draft_id}")
async def update_draft(draft_id: str, payload: UpdateDraftRequest,
                       token: str = Depends(pb_user_auth.get_user_token)):
    """Aktualisiert einen bestehenden Entwurf."""
    subject = payload.subject or "(Kein Betreff)"
    full_body = payload.body
    if payload.quote:
        full_body += "\n\n" + payload.quote

    patch = {
        "subject": subject,
        "body_plain": full_body,
        "body_html": payload.body_html,
        "snippet": (full_body[:120] if full_body else ""),
        "to_emails": [payload.to] if payload.to else [],
    }
    return await pb_client.pb_patch_as(
        token, f"/api/collections/emails/records/{draft_id}", patch
    )


# ---------------------------------------------------------------------------
# Attachments
# ---------------------------------------------------------------------------


@router.get("/emails/{email_id}/attachments")
async def get_email_attachments(email_id: str, token: str = Depends(pb_user_auth.get_user_token)):
    """Listet alle Anhänge einer E-Mail aus PocketBase."""
    return await pb_client.pb_get_as(token, "/api/collections/attachments/records", params={
        "filter": f'email={pb_client.pb_quote(email_id)}',
        "perPage": 50,
        "sort": "part_id",
    })


@router.get("/attachments/{attachment_id}/download")
async def download_attachment(attachment_id: str):
    """Lädt einen Anhang von IMAP herunter und streamt ihn.

    A11: bewusste Admin-Nutzung — der Endpoint wird per signiertem URL aufgerufen
    (`<a href>`-Download), also ohne Bearer-Header. PB-Rules greifen für Admin nicht.
    """
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

    try:
        payload = await asyncio.to_thread(
            ImapService(acc).fetch_attachment, folder, int(imap_uid), part_index
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


async def _fetch_email_raw(email_id: str) -> bytes:
    """Holt die Roh-Mail (RFC822) live vom IMAP-Server für die Quelltext-
    Ansicht und den .eml-Download. Gemeinsamer Helper beider Endpoints."""
    email_rec = await pb_client.pb_get(f"/api/collections/emails/records/{email_id}")
    account_id = email_rec.get("account")
    folder = email_rec.get("folder", "INBOX")
    imap_uid = email_rec.get("imap_uid")

    if not imap_uid:
        raise HTTPException(status_code=404, detail="E-Mail hat keine IMAP-UID")

    acc = await _get_imap_account(account_id)
    if not acc:
        raise HTTPException(status_code=404, detail="Account nicht gefunden")

    try:
        return await asyncio.to_thread(ImapService(acc).fetch_raw, folder, int(imap_uid))
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Quelltext-Abruf fehlgeschlagen: %s", exc)
        raise HTTPException(status_code=502, detail=f"IMAP-Fehler: {exc}")


@router.get("/emails/{email_id}/source")
async def get_email_source(email_id: str, token: str = Depends(pb_user_auth.get_user_token)):
    """Roh-Quelltext (RFC822) einer E-Mail als Text für die Quelltext-Ansicht.
    Wird live von IMAP geholt (nicht in PocketBase gespeichert)."""
    raw = await _fetch_email_raw(email_id)
    return {"source": raw.decode("utf-8", errors="replace")}


@router.get("/emails/{email_id}/source.eml")
async def download_email_source(email_id: str):
    """Lädt die Roh-Mail als .eml herunter. Wie der Attachment-Download per
    signiertem URL (A11) erreichbar — kein Bearer-Header möglich."""
    raw = await _fetch_email_raw(email_id)
    email_rec = await pb_client.pb_get(f"/api/collections/emails/records/{email_id}")
    subject = (email_rec.get("subject") or "").strip()
    safe = re.sub(r"[^\w.-]+", "_", subject)[:80].strip("_") or "mail"
    encoded_name = _url_quote(f"{safe}.eml", safe="")
    return Response(
        content=raw,
        media_type="message/rfc822",
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{encoded_name}",
            "Access-Control-Allow-Origin": "*",
        },
    )


@router.get("/emails/{email_id}/inline")
async def get_inline_image(email_id: str, cid: str):
    """Gibt ein Inline-Bild (cid:-Referenz) aus einer E-Mail zurück.

    A11: bewusste Admin-Nutzung — Endpoint wird per signiertem URL aus `<img src>`
    aufgerufen, kein Bearer-Header möglich.
    """
    email_rec = await pb_client.pb_get(f"/api/collections/emails/records/{email_id}")
    account_id = email_rec.get("account")
    folder = email_rec.get("folder", "INBOX")
    imap_uid = email_rec.get("imap_uid")

    if not imap_uid:
        raise HTTPException(status_code=404, detail="E-Mail hat keine IMAP-UID")

    acc = await _get_imap_account(account_id)
    if not acc:
        raise HTTPException(status_code=404, detail="Account nicht gefunden")

    try:
        payload, mime_type = await asyncio.to_thread(
            ImapService(acc).fetch_inline, folder, int(imap_uid), cid
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


_UPLOAD_CHUNK = 64 * 1024


@router.post("/attachments/upload")
async def upload_attachment(request: Request, file: UploadFile = File(...)):
    """Lädt eine Datei temporär in den Arbeitsspeicher.

    Limits: ``MAX_UPLOAD_SIZE`` pro Datei, ``MAX_TOTAL_UPLOAD_SIZE`` über alle
    aktiven Uploads. Einträge werden nach ``UPLOAD_TTL_SECONDS`` durch
    den Cleanup-Loop in services/mail.py verworfen.

    S4 (2026-05-23): Body wird in 64-KB-Chunks gelesen und bei Überschreitung
    sofort verworfen — verhindert RAM-Allokation einer kompletten Riesen-Datei,
    bevor das Limit greift. Content-Length-Vorab-Check spart bei ehrlichen
    Clients auch das Multipart-Spooling.
    """
    initial_total = sum(e.get("size", 0) for e in _temp_uploads.values())
    if initial_total >= MAX_TOTAL_UPLOAD_SIZE:
        raise HTTPException(
            status_code=413,
            detail=(
                f"Upload-Speicher voll (max. {MAX_TOTAL_UPLOAD_SIZE // (1024 * 1024)} MB total) "
                "— bitte später erneut versuchen"
            ),
        )

    # Frühabbruch via Content-Length (best-effort — Client kann lügen, daher
    # zusätzlich der Chunk-Loop unten als Defense-in-Depth).
    cl_header = request.headers.get("content-length")
    if cl_header:
        try:
            declared = int(cl_header)
            if declared > MAX_UPLOAD_SIZE:
                raise HTTPException(
                    status_code=413,
                    detail=f"Datei zu groß (max. {MAX_UPLOAD_SIZE // (1024 * 1024)} MB)",
                )
            if initial_total + declared > MAX_TOTAL_UPLOAD_SIZE:
                raise HTTPException(
                    status_code=413,
                    detail=(
                        f"Upload-Speicher voll (max. {MAX_TOTAL_UPLOAD_SIZE // (1024 * 1024)} MB total) "
                        "— bitte später erneut versuchen"
                    ),
                )
        except ValueError:
            pass

    hard_limit = min(MAX_UPLOAD_SIZE, MAX_TOTAL_UPLOAD_SIZE - initial_total)
    chunks: list[bytes] = []
    size = 0
    while True:
        chunk = await file.read(_UPLOAD_CHUNK)
        if not chunk:
            break
        size += len(chunk)
        if size > hard_limit:
            chunks.clear()
            if size > MAX_UPLOAD_SIZE:
                raise HTTPException(
                    status_code=413,
                    detail=f"Datei zu groß (max. {MAX_UPLOAD_SIZE // (1024 * 1024)} MB)",
                )
            raise HTTPException(
                status_code=413,
                detail=(
                    f"Upload-Speicher voll (max. {MAX_TOTAL_UPLOAD_SIZE // (1024 * 1024)} MB total) "
                    "— bitte später erneut versuchen"
                ),
            )
        chunks.append(chunk)

    data = b"".join(chunks)
    temp_id = str(_uuid_mod.uuid4())
    _temp_uploads[temp_id] = {
        "filename": file.filename or "anhang",
        "content_type": file.content_type or "application/octet-stream",
        "data": data,
        "size": size,
        "created_at": time.monotonic(),
    }
    logger.info("Temporärer Upload: %s (%d bytes, total=%d)",
                file.filename, size, initial_total + size)
    return {
        "id": temp_id,
        "filename": file.filename or "anhang",
        "size": size,
        "content_type": file.content_type,
    }


@router.delete("/attachments/upload/{temp_id}")
async def delete_upload(temp_id: str):
    """Entfernt einen temporären Upload."""
    _temp_uploads.pop(temp_id, None)
    return {"deleted": temp_id}


# ---------------------------------------------------------------------------
# Detail + State-Modifikationen pro E-Mail
# ---------------------------------------------------------------------------


@router.get("/emails/{email_id}")
async def get_email(email_id: str, background_tasks: BackgroundTasks,
                    token: str = Depends(pb_user_auth.get_user_token)):
    email = await pb_client.pb_get_as(token, f"/api/collections/emails/records/{email_id}")
    if email.get("is_new"):
        background_tasks.add_task(
            pb_client.pb_patch_as,
            token,
            f"/api/collections/emails/records/{email_id}",
            {"is_new": False},
        )
    return email


@router.patch("/emails/{email_id}/category")
async def set_category(email_id: str, req: SetCategoryRequest, token: str = Depends(pb_user_auth.get_user_token)):
    """Setzt die KI-Kategorie einer E-Mail."""
    return await pb_client.pb_patch_as(
        token,
        f"/api/collections/emails/records/{email_id}",
        {"ai_category": req.ai_category},
    )


@router.patch("/emails/bulk/read")
async def bulk_mark_read(req: BulkReadRequest, token: str = Depends(pb_user_auth.get_user_token)):
    """Markiert mehrere E-Mails als gelesen/ungelesen.
    PocketBase: parallel; IMAP: eine Verbindung pro Account+Ordner."""
    if not req.emails:
        return {"updated": 0}

    emails = [e.model_dump() for e in req.emails]

    # 1. PocketBase-Updates parallel (keine vorherige Abfrage nötig)
    await asyncio.gather(*[
        pb_client.pb_patch_as(token, f"/api/collections/emails/records/{e['id']}", {"is_read": req.is_read})
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
        _update_folder_unread_count(token, account_id, folder)
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
    loop = asyncio.get_running_loop()
    with concurrent.futures.ThreadPoolExecutor() as pool:
        futs = [
            loop.run_in_executor(
                pool, ImapService(accounts[account_id]).bulk_set_read, folder, uids, req.is_read,
            )
            for (account_id, folder), uids in affected_groups.items()
            if account_id in accounts
        ]
        results = await asyncio.gather(*futs, return_exceptions=True)
        for r in results:
            if isinstance(r, Exception):
                logger.warning("IMAP bulk-read failed: %s", r)

    return {"updated": len(emails)}


@router.patch("/emails/{email_id}/read")
async def mark_read(email_id: str, is_read: bool = True, token: str = Depends(pb_user_auth.get_user_token)):
    result = await pb_client.pb_patch_as(
        token,
        f"/api/collections/emails/records/{email_id}",
        {"is_read": is_read}
    )
    try:
        await _imap_set_read(result, is_read)
    except Exception as e:
        logger.warning(f"IMAP mark-read failed for {email_id}: {e}")
    try:
        await _update_folder_unread_count(token, result["account"], result["folder"])
    except Exception as e:
        logger.warning(f"folder unread_count update failed for {email_id}: {e}")
    return result


# ---------------------------------------------------------------------------
# Spam-Aktionen + Spam-Rules
# ---------------------------------------------------------------------------


@router.post("/emails/{email_id}/spam")
async def move_to_spam(email_id: str, block_sender: bool = False, block_domain: bool = False,
                       token: str = Depends(pb_user_auth.get_user_token)):
    """Verschiebt E-Mail in den Spam-Ordner (IMAP + PocketBase) und lernt das Sample."""
    email = await pb_client.pb_get_as(token, f"/api/collections/emails/records/{email_id}")
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
        await pb_client.pb_patch_as(token, f"/api/collections/emails/records/{email_id}", patch)
    except Exception as e:
        logger.warning(f"move_to_spam: pb_patch fehlgeschlagen (wahrscheinlich Race mit imap_sync): {e}")
    try:
        await _update_folder_unread_count(token, email["account"], source_folder)
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


@router.post("/emails/{email_id}/unspam")
async def unspam_email(email_id: str, token: str = Depends(pb_user_auth.get_user_token)):
    """Holt eine Mail aus dem Spam-Ordner zurück nach INBOX und entfernt das Spam-Sample."""
    email = await pb_client.pb_get_as(token, f"/api/collections/emails/records/{email_id}")
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
        await pb_client.pb_patch_as(token, f"/api/collections/emails/records/{email_id}", patch)
    except Exception as e:
        logger.warning(f"unspam: pb_patch fehlgeschlagen: {e}")
    try:
        await _update_folder_unread_count(token, email["account"], source_folder)
        await _update_folder_unread_count(token, email["account"], "INBOX")
    except Exception as e:
        logger.warning(f"folder unread_count update failed after unspam {email_id}: {e}")
    await spam_filter.remove_spam_sample(email_id)
    return {"moved_to": "INBOX"}


@router.post("/emails/{email_id}/spam-suggestion/confirm")
async def confirm_spam_suggestion(email_id: str, token: str = Depends(pb_user_auth.get_user_token)):
    """Bestätigt einen Spam-Vorschlag aus dem Vorschlag-Badge."""
    return await move_to_spam(email_id, block_sender=False, block_domain=False, token=token)


@router.post("/emails/{email_id}/spam-suggestion/dismiss")
async def dismiss_spam_suggestion(email_id: str, token: str = Depends(pb_user_auth.get_user_token)):
    """Verwirft den Spam-Vorschlag — Mail bleibt in INBOX."""
    try:
        await pb_client.pb_patch_as(
            token,
            f"/api/collections/emails/records/{email_id}",
            {"spam_suggested": False, "spam_score": None, "spam_rule_match": ""},
        )
    except Exception as e:
        logger.warning(f"dismiss_spam_suggestion failed for {email_id}: {e}")
    return {"dismissed": True}


@router.get("/spam-rules")
async def list_spam_rules(account: str | None = None, token: str = Depends(pb_user_auth.get_user_token)):
    """Listet alle Spam-Regeln, optional nach Account gefiltert."""
    params: dict = {"perPage": 500}
    if account:
        params["filter"] = f'account={pb_client.pb_quote(account)}'
    result = await pb_client.pb_get_as(token, "/api/collections/spam_rules/records", params=params)
    return {"items": result.get("items", []), "totalItems": result.get("totalItems", 0)}


@router.delete("/spam-rules/{rule_id}")
async def delete_spam_rule(rule_id: str, token: str = Depends(pb_user_auth.get_user_token)):
    """Löscht eine Spam-Regel (Absender wieder erlaubt)."""
    await pb_client.pb_delete_as(token, f"/api/collections/spam_rules/records/{rule_id}")
    return {"deleted": rule_id}


# ---------------------------------------------------------------------------
# Move + Delete
# ---------------------------------------------------------------------------


@router.post("/emails/{email_id}/move")
async def move_email(email_id: str, req: MoveEmailRequest, token: str = Depends(pb_user_auth.get_user_token)):
    """Verschiebt E-Mail in einen anderen Ordner (IMAP + PocketBase).
    Beim Verlassen des Spam-Ordners werden Qdrant-Sample und spam_*-Felder mit aufgeräumt."""
    target_folder = req.target_folder

    email = await pb_client.pb_get_as(token, f"/api/collections/emails/records/{email_id}")
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
    if new_uid:
        try:
            await _imap_set_read({"account": email["account"], "imap_uid": new_uid, "folder": target_folder}, True)
        except Exception as ex:
            logger.warning("move_email: IMAP mark-read fehlgeschlagen: %s", ex)
    try:
        await pb_client.pb_patch_as(token, f"/api/collections/emails/records/{email_id}", patch)
    except Exception as e:
        # Race condition: imap_sync hat den Record bereits gelöscht (UID weg aus Quellordner)
        logger.warning("move_email: pb_patch fehlgeschlagen (wahrscheinlich Race mit imap_sync): %s", e)
    try:
        await asyncio.gather(
            _update_folder_unread_count(token, email["account"], source_folder),
            _update_folder_unread_count(token, email["account"], target_folder),
        )
    except Exception as e:
        logger.warning("move_email: folder unread_count update fehlgeschlagen: %s", e)
    if leaving_spam:
        await spam_filter.remove_spam_sample(email_id)
    return {"moved_to": target_folder, "marked_read": True}


@router.delete("/emails/{email_id}")
async def delete_email(email_id: str, token: str = Depends(pb_user_auth.get_user_token)):
    """Löscht E-Mail in PocketBase und verschiebt sie auf dem IMAP-Server in den Papierkorb."""
    email = await pb_client.pb_get_as(token, f"/api/collections/emails/records/{email_id}")
    source_folder = email.get("folder", "INBOX")
    was_unread = not email.get("is_read", True)
    if was_unread:
        try:
            await pb_client.pb_patch_as(
                token,
                f"/api/collections/emails/records/{email_id}", {"is_read": True}
            )
        except Exception:
            pass
    try:
        await _imap_trash(email)
    except Exception as e:
        logger.warning(f"IMAP trash failed for {email_id}: {e}")
    await pb_client.pb_delete_as(token, f"/api/collections/emails/records/{email_id}")
    await asyncio.to_thread(fts_delete, settings.PB_DATA_PATH, email_id)
    if was_unread:
        try:
            await _update_folder_unread_count(token, email["account"], source_folder)
        except Exception as e:
            logger.warning(f"folder unread_count update failed after delete {email_id}: {e}")
    return {"deleted": email_id}
