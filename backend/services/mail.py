"""Mail-Service — Cross-Cutting-Helpers für Send-Pipeline, IMAP-Aktionen,
Bulk-Worker-Loop und Bounce-Verarbeitung.

Ausgelagert aus main.py im Rahmen von C1 Phase 2 (5c.1). Hintergrund:
Diese Helpers werden sowohl von Mail-Endpoints (in Zukunft in
``routers/mail.py``) **als auch** vom Bulk-Worker im ``lifespan`` von
main.py und der Bounce-Verarbeitung in imap_sync.py genutzt. Ein
gemeinsames Service-Modul vermeidet Zirkular-Imports zwischen Router
und main.

A11-Hinweis: Mehrere Helpers nutzen bewusst den Admin-Token
(``pb_client.pb_*``), nicht ``pb_*_as`` — sie laufen entweder
ohne User-Token-Kontext (Bulk-Worker, Bounce-Handler, Background-
Tasks) oder über die Lebenszeit der User-Session hinaus.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid as _uuid_mod
from datetime import datetime, timedelta, timezone

import pb_client
import rendering
from idle_manager import get_sse_queues
from imap_sync import upsert_contact
from services.imap import ImapService
from smtp_sender import send_email as smtp_send_email

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Konstanten
# ---------------------------------------------------------------------------

UPLOAD_TTL_SECONDS = 30 * 60                  # 30 min — danach wird der Eintrag verworfen
UPLOAD_CLEANUP_INTERVAL_SECONDS = 5 * 60      # 5 min — Sweep-Intervall

# B15: Bulk-Worker
BULK_WORKER_INTERVAL_SECONDS = 1.0
BULK_RECIPIENT_LEASE_SECONDS = 5 * 60         # Schutz vor Doppelpick, falls Sub-Job hängt


# ---------------------------------------------------------------------------
# Modul-State (In-Memory)
# ---------------------------------------------------------------------------

# Temporärer Speicher für hochgeladene Anhänge (in-memory)
# {temp_id: {filename, content_type, data: bytes, size: int, created_at: float}}
_temp_uploads: dict[str, dict] = {}

# Hintergrund-Sendejobs: {job_id: {status, to, subject}}
_send_jobs: dict[str, dict] = {}

# B15: Pro bulk_send_id die Anhangsliste, die `_do_send_job` als `attachments`-Arg
# braucht. Lebt nur im aktuellen Prozess — Resume nach Restart bekommt eine leere
# Liste; has_attachments-Aussendungen werden vorher per `_bulk_restart_cleanup`
# abgebrochen.
_bulk_attachments_by_id: dict[str, list] = {}

# Lock pro bulk_send_id verhindert race condition beim parallelen Update
# desselben JSON-Recipients-Arrays durch mehrere Sub-Jobs.
_bulk_send_locks: dict[str, asyncio.Lock] = {}


# ---------------------------------------------------------------------------
# Account / Folder Helpers
# ---------------------------------------------------------------------------


async def _get_imap_account(account_id: str) -> dict | None:
    """Lädt Account-Daten aus PocketBase. Gibt None zurück wenn nicht gefunden."""
    result = await pb_client.pb_get(
        "/api/collections/accounts/records",
        params={"filter": f'id={pb_client.pb_quote(account_id)}', "perPage": 1},
    )
    items = result.get("items", [])
    return items[0] if items else None


async def _update_folder_unread_count(token: str, account_id: str, folder: str) -> None:
    """Zählt is_read=false E-Mails für den Ordner und schreibt den Wert in folders.unread_count."""
    count_data = await pb_client.pb_get_as(token, "/api/collections/emails/records", params={
        "filter": f'account={pb_client.pb_quote(account_id)} && folder={pb_client.pb_quote(folder)} && is_read=false',
        "perPage": 1,
        "fields": "id",
    })
    new_unread = count_data.get("totalItems", 0)
    folder_data = await pb_client.pb_get_as(token, "/api/collections/folders/records", params={
        "filter": f'account={pb_client.pb_quote(account_id)} && imap_path={pb_client.pb_quote(folder)}',
        "perPage": 1,
        "fields": "id",
    })
    folder_items = folder_data.get("items", [])
    if folder_items:
        await pb_client.pb_patch_as(
            token,
            f"/api/collections/folders/records/{folder_items[0]['id']}",
            {"unread_count": new_unread},
        )


# ---------------------------------------------------------------------------
# Temp-Upload-Cleanup
# ---------------------------------------------------------------------------


async def _cleanup_temp_uploads_loop() -> None:
    """Verwirft Einträge in `_temp_uploads`, die älter als UPLOAD_TTL_SECONDS sind.

    Verhindert RAM-Leaks bei Browser-Crash / Compose-Abbruch — ohne dieses
    Aufräumen würden Anhänge bis zum nächsten Backend-Restart belegt bleiben.
    """
    while True:
        try:
            await asyncio.sleep(UPLOAD_CLEANUP_INTERVAL_SECONDS)
            now = time.monotonic()
            stale_ids = [tid for tid, entry in _temp_uploads.items()
                         if now - entry.get("created_at", now) > UPLOAD_TTL_SECONDS]
            for tid in stale_ids:
                entry = _temp_uploads.pop(tid, None)
                if entry:
                    logger.warning(
                        "Temporärer Upload abgelaufen: %s (%d bytes, age=%.0fs)",
                        entry.get("filename"), entry.get("size", 0),
                        now - entry.get("created_at", now),
                    )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Cleanup-Loop für _temp_uploads fehlgeschlagen")


# ---------------------------------------------------------------------------
# B15: Bulk-Worker
# ---------------------------------------------------------------------------
# Persistenter Versand-Pfad: bulk_send_endpoint setzt pro Empfänger
# next_attempt_at + job_id in bulk_sends.recipients[]. Der Worker pollt
# offene bulk_sends, picked fällige queued-Empfänger und ruft _do_send_job.
# Resume nach Backend-Restart ist damit automatisch — der Worker holt sich
# beim nächsten Tick einfach die noch offenen Einträge.


def _parse_pb_dt(s: str | None) -> datetime | None:
    """Parst PB-Datums-Strings tolerant (mit/ohne Z, mit/ohne Microsekunden).

    Gibt aware-datetime in UTC zurück. None für leere/ungültige Werte.
    """
    if not s:
        return None
    raw = str(s).strip()
    if not raw:
        return None
    raw = raw.replace("T", " ").rstrip("Z")
    if "." in raw:
        raw = raw.split(".", 1)[0]
    try:
        return datetime.strptime(raw, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _format_pb_dt(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


async def _bulk_restart_cleanup() -> None:
    """Räumt nach Backend-Restart Aussendungen mit Anhängen auf.

    Anhänge leben in `_temp_uploads` (in-memory) — nach Restart sind sie weg.
    Für bulk_sends mit `has_attachments=true` und offenen `queued`-Empfängern
    setzt diese Funktion die offenen Empfänger auf `status=error` mit Grund
    `backend_restart_with_attachments`. Aussendungen ohne Anhänge laufen
    unverändert weiter — der Worker pickt sie regulär.
    """
    try:
        result = await pb_client.pb_get(
            "/api/collections/bulk_sends/records",
            params={
                "filter": 'is_done!=true && has_attachments=true',
                "sort": "-sent_at",
                "perPage": 200,
            },
        )
    except Exception as exc:
        logger.warning("B15-Restart-Cleanup: bulk_sends-Read fehlgeschlagen: %s", exc)
        return

    items = result.get("items", []) or []
    for bulk in items:
        bulk_id = bulk.get("id")
        if not bulk_id:
            continue
        lock = _bulk_send_locks.setdefault(bulk_id, asyncio.Lock())
        async with lock:
            try:
                fresh = await pb_client.pb_get(
                    f"/api/collections/bulk_sends/records/{bulk_id}"
                )
            except Exception as exc:
                logger.warning("B15-Restart-Cleanup: %s nicht lesbar: %s", bulk_id, exc)
                continue
            recipients = fresh.get("recipients") or []
            changed = 0
            for r in recipients:
                if r.get("status") == "queued":
                    r["status"] = "error"
                    r["error"] = "backend_restart_with_attachments"
                    changed += 1
            if changed == 0:
                continue
            sent = sum(1 for r in recipients if r.get("status") == "sent")
            err = sum(1 for r in recipients if r.get("status") == "error")
            bounced = sum(1 for r in recipients if r.get("status") == "bounced")
            total = fresh.get("total_count") or len(recipients)
            patch = {
                "recipients": recipients,
                "sent_count": sent,
                "error_count": err,
                "bounced_count": bounced,
                "is_done": sent + err + bounced >= total,
            }
            try:
                await pb_client.pb_patch(
                    f"/api/collections/bulk_sends/records/{bulk_id}", patch,
                )
                logger.warning(
                    "B15-Restart-Cleanup: bulk=%s %d Empfänger abgebrochen (Anhänge verloren)",
                    bulk_id, changed,
                )
            except Exception as exc:
                logger.warning("B15-Restart-Cleanup: patch %s fehlgeschlagen: %s",
                               bulk_id, exc)


def _build_resume_sub_data(bulk: dict, recipient: dict) -> dict:
    """Rekonstruiert das `data`-Dict für `_do_send_job` aus bulk_sends + Empfänger.

    Nur Felder, die in `bulk_sends` persistiert sind — `quote`, `quote_html`,
    `in_reply_to_email_id`, `draft_id` waren bulk-irrelevant und werden nicht
    übernommen. Anhänge sind separat (in-memory beim Erst-Lauf, leer beim Resume).
    """
    return {
        "to": recipient.get("raw") or recipient.get("email") or "",
        "subject": bulk.get("subject") or "",
        "cc": "",
        "from_account": bulk.get("from_account") or "",
        "smtp_server": bulk.get("smtp_server") or "",
        "body": bulk.get("body_text") or "",
        "body_html": bulk.get("body_html") or "",
        "_bulk_send_id": bulk.get("id"),
    }


async def _bulk_worker_tick(attachments_by_bulk: dict[str, list]) -> None:
    """Ein Worker-Tick: pickt fällige queued-Empfänger und startet `_do_send_job`.

    `attachments_by_bulk` hält die im aktuellen Prozess geladenen Anhänge pro
    bulk_send_id. Beim Restart ist das Dict leer; der Worker sendet dann ohne
    Anhänge — was bei has_attachments-Bulks via `_bulk_restart_cleanup` schon
    vorab als Fehler abgehandelt wurde.
    """
    try:
        result = await pb_client.pb_get(
            "/api/collections/bulk_sends/records",
            params={
                "filter": 'is_done!=true',
                "sort": "-sent_at",
                "perPage": 50,
            },
        )
    except Exception as exc:
        logger.warning("B15-Worker: bulk_sends-Read fehlgeschlagen: %s", exc)
        return

    items = result.get("items", []) or []
    now = datetime.now(timezone.utc)
    lease_until = now + timedelta(seconds=BULK_RECIPIENT_LEASE_SECONDS)
    lease_str = _format_pb_dt(lease_until)

    for bulk in items:
        bulk_id = bulk.get("id")
        if not bulk_id:
            continue
        lock = _bulk_send_locks.setdefault(bulk_id, asyncio.Lock())
        if lock.locked():
            continue  # läuft schon — nächster Tick
        async with lock:
            try:
                fresh = await pb_client.pb_get(
                    f"/api/collections/bulk_sends/records/{bulk_id}"
                )
            except Exception as exc:
                logger.warning("B15-Worker: %s nicht lesbar: %s", bulk_id, exc)
                continue
            recipients = fresh.get("recipients") or []
            total = fresh.get("total_count") or len(recipients)
            sent = sum(1 for r in recipients if r.get("status") == "sent")
            err = sum(1 for r in recipients if r.get("status") == "error")
            bounced = sum(1 for r in recipients if r.get("status") == "bounced")
            if sent + err + bounced >= total:
                # Alle terminal — is_done setzen und weiter
                try:
                    await pb_client.pb_patch(
                        f"/api/collections/bulk_sends/records/{bulk_id}",
                        {"is_done": True},
                    )
                except Exception as exc:
                    logger.warning("B15-Worker: is_done-patch %s fehlgeschlagen: %s",
                                   bulk_id, exc)
                continue

            picks: list[dict] = []
            for r in recipients:
                if r.get("status") != "queued":
                    continue
                due = _parse_pb_dt(r.get("next_attempt_at"))
                if due is not None and due > now:
                    continue
                # Lease setzen, damit der Worker nicht in einem Folgetick neu pickt,
                # wenn _do_send_job länger braucht.
                r["next_attempt_at"] = lease_str
                picks.append(r)

            if not picks:
                continue

            try:
                await pb_client.pb_patch(
                    f"/api/collections/bulk_sends/records/{bulk_id}",
                    {"recipients": recipients},
                )
            except Exception as exc:
                logger.warning("B15-Worker: lease-patch %s fehlgeschlagen: %s",
                               bulk_id, exc)
                continue

        # Sub-Jobs außerhalb des Locks starten (Lock ist nur fürs PB-Patch).
        attachments = attachments_by_bulk.get(bulk_id) or []
        for r in picks:
            sub_data = _build_resume_sub_data(fresh, r)
            job_id = r.get("job_id") or str(_uuid_mod.uuid4())
            existing = _send_jobs.get(job_id) or {}
            existing.update({
                "status": "sending",
                "to": sub_data["to"],
                "subject": sub_data["subject"],
                "bulk_send_id": bulk_id,
            })
            _send_jobs[job_id] = existing
            asyncio.create_task(_do_send_job(job_id, sub_data, attachments))


async def _bulk_worker_loop(attachments_by_bulk: dict[str, list]) -> None:
    """Läuft endlos: pickt alle BULK_WORKER_INTERVAL_SECONDS fällige Empfänger."""
    while True:
        try:
            await _bulk_worker_tick(attachments_by_bulk)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("B15-Worker-Tick fehlgeschlagen")
        try:
            await asyncio.sleep(BULK_WORKER_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            raise


# ---------------------------------------------------------------------------
# SSE-Notify
# ---------------------------------------------------------------------------


def _sse_notify_all(event: dict) -> None:
    """Schickt ein Event an alle verbundenen SSE-Clients."""
    for q in list(get_sse_queues()):
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            pass


# ---------------------------------------------------------------------------
# Send-Pipeline
# ---------------------------------------------------------------------------


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
                params={"filter": f'email={pb_client.pb_quote(email_addr)}', "perPage": 1},
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
    """Führt den SMTP-Versand im Hintergrund aus und meldet das Ergebnis via SSE.

    A11: bewusste Admin-Nutzung — läuft als asyncio-Task ohne User-Token-Kontext;
    kann minutenlang dauern (Bulk-Send mit Delay) und überlebt damit das User-Session-
    Token-Cache-TTL. Schreibt emails (is_answered, Draft-Cleanup) und bulk_sends-
    Recipient-Status als Admin.
    """
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

    bulk_send_id = data.get("_bulk_send_id")
    try:
        sent_message_id = await smtp_send_email(
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
        if bulk_send_id:
            asyncio.create_task(_bulk_record_recipient_result(
                bulk_send_id, to, status="error", error=str(exc)[:500],
            ))
        _sse_notify_all({"type": "send-result", "job_id": job_id,
                         "success": False, "to": to, "subject": subject,
                         "error": str(exc)})
        return

    if bulk_send_id:
        asyncio.create_task(_bulk_record_recipient_result(
            bulk_send_id, to, status="sent", message_id=sent_message_id,
        ))

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
        from datetime import datetime as _dt, timezone as _tz
        asyncio.create_task(upsert_contact(_m.group(0).lower(), _to_name,
                                           _dt.now(_tz.utc).strftime("%Y-%m-%d %H:%M:%S")))

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


# ---------------------------------------------------------------------------
# bulk_sends: Persistenz pro Empfänger
# ---------------------------------------------------------------------------


async def _bulk_record_recipient_result(
    bulk_send_id: str, recipient_to: str, *,
    status: str, message_id: str | None = None, error: str | None = None,
) -> None:
    """Updatet einen Empfänger im bulk_sends-Record.

    Args:
      bulk_send_id: PB-ID des bulk_sends-Records (None → no-op).
      recipient_to: Empfänger im To-Format ("Name <addr>" oder "addr").
      status: queued|sent|error|bounced.
      message_id: Message-ID des SMTP-Versands (für späteren Bounce-Match).
      error: Fehlertext (nur bei status=error/bounced).
    """
    if not bulk_send_id:
        return
    m = re.search(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", recipient_to or "")
    email_l = m.group(0).lower() if m else (recipient_to or "").lower()
    lock = _bulk_send_locks.setdefault(bulk_send_id, asyncio.Lock())
    async with lock:
        try:
            rec = await pb_client.pb_get(
                f"/api/collections/bulk_sends/records/{bulk_send_id}"
            )
        except Exception as exc:
            logger.warning("bulk_sends %s nicht lesbar: %s", bulk_send_id, exc)
            return
        recipients = rec.get("recipients") or []
        found = False
        for r in recipients:
            if (r.get("email") or "").strip().lower() == email_l:
                r["status"] = status
                if message_id is not None:
                    r["message_id"] = message_id
                if error is not None:
                    r["error"] = error
                if status == "sent":
                    r["sent_at"] = _format_pb_dt(datetime.now(timezone.utc))
                found = True
                break
        if not found:
            return  # Empfänger nicht im Audit-Record — z.B. nachträglicher Eintrag
        sent = sum(1 for r in recipients if r.get("status") == "sent")
        err = sum(1 for r in recipients if r.get("status") == "error")
        bounced = sum(1 for r in recipients if r.get("status") == "bounced")
        total = rec.get("total_count") or len(recipients)
        is_done = sent + err + bounced >= total
        try:
            await pb_client.pb_patch(
                f"/api/collections/bulk_sends/records/{bulk_send_id}",
                {
                    "recipients": recipients,
                    "sent_count": sent,
                    "error_count": err,
                    "bounced_count": bounced,
                    "is_done": is_done,
                },
            )
        except Exception as exc:
            logger.warning("bulk_sends %s update fehlgeschlagen: %s", bulk_send_id, exc)
            return
    # Außerhalb des Locks: bei is_done den In-Memory-Anhang-Cache freigeben.
    if is_done:
        _bulk_attachments_by_id.pop(bulk_send_id, None)


# ---------------------------------------------------------------------------
# Phase 3b: Bounce-Match
# ---------------------------------------------------------------------------
# Aufgerufen vom IMAP-Sync, wenn eine DSN-Mail erkannt wurde. Sucht den
# zugehörigen Empfänger in bulk_sends, patcht status=bounced + bounced_at +
# bounced_reason, und flaggt bei permanentem Fehler (5.x.x) den Kontakt.


async def _find_bulk_recipient_match(
    message_id: str | None, failed_recipient: str | None,
) -> tuple[str, str] | None:
    """Findet Empfänger in bulk_sends per Message-ID oder Email (Fallback).

    Returns (bulk_send_id, email_lower) oder None. Email-Fallback nur in den
    letzten 7 Tagen, um zufällige Treffer auf alte Aussendungen zu vermeiden.
    """
    if message_id:
        clean_id = message_id.strip().strip("<>")
        if clean_id:
            try:
                res = await pb_client.pb_get(
                    "/api/collections/bulk_sends/records",
                    params={
                        "filter": f'recipients ~ {pb_client.pb_quote(clean_id)}',
                        "perPage": 10,
                        "sort": "-sent_at",
                    },
                )
            except Exception as exc:
                logger.warning("Bounce-Match (msg_id) Read fehlgeschlagen: %s", exc)
                res = None
            if res:
                for bulk in res.get("items", []) or []:
                    for r in bulk.get("recipients") or []:
                        rec_mid = (r.get("message_id") or "").strip().strip("<>")
                        if rec_mid and rec_mid == clean_id:
                            return (bulk["id"], (r.get("email") or "").lower())

    if failed_recipient:
        email_l = failed_recipient.strip().lower()
        if email_l:
            cutoff = (datetime.now(timezone.utc) - timedelta(days=7))
            try:
                res = await pb_client.pb_get(
                    "/api/collections/bulk_sends/records",
                    params={
                        "filter": (
                            f'recipients ~ {pb_client.pb_quote(email_l)} && '
                            f'sent_at >= {pb_client.pb_quote(_format_pb_dt(cutoff))}'
                        ),
                        "perPage": 10,
                        "sort": "-sent_at",
                    },
                )
            except Exception as exc:
                logger.warning("Bounce-Match (email) Read fehlgeschlagen: %s", exc)
                res = None
            if res:
                for bulk in res.get("items", []) or []:
                    for r in bulk.get("recipients") or []:
                        rec_email = (r.get("email") or "").strip().lower()
                        if rec_email == email_l and r.get("status") == "sent":
                            return (bulk["id"], rec_email)

    return None


async def _patch_bulk_recipient_bounced(
    bulk_id: str, email_lower: str, reason: str,
) -> None:
    """Patcht bulk_sends.recipients[i] mit status=bounced + bounced_at + bounced_reason.

    Nutzt denselben Lock wie `_bulk_record_recipient_result` gegen Race mit dem Worker.
    """
    lock = _bulk_send_locks.setdefault(bulk_id, asyncio.Lock())
    async with lock:
        try:
            rec = await pb_client.pb_get(
                f"/api/collections/bulk_sends/records/{bulk_id}"
            )
        except Exception as exc:
            logger.warning("Bounce-Patch: bulk_sends %s nicht lesbar: %s", bulk_id, exc)
            return
        recipients = rec.get("recipients") or []
        found = False
        for r in recipients:
            if (r.get("email") or "").strip().lower() == email_lower:
                r["status"] = "bounced"
                r["bounced_at"] = _format_pb_dt(datetime.now(timezone.utc))
                r["bounced_reason"] = (reason or "")[:500]
                found = True
                break
        if not found:
            return
        sent = sum(1 for r in recipients if r.get("status") == "sent")
        err = sum(1 for r in recipients if r.get("status") == "error")
        bounced = sum(1 for r in recipients if r.get("status") == "bounced")
        try:
            await pb_client.pb_patch(
                f"/api/collections/bulk_sends/records/{bulk_id}",
                {
                    "recipients": recipients,
                    "sent_count": sent,
                    "error_count": err,
                    "bounced_count": bounced,
                },
            )
        except Exception as exc:
            logger.warning("Bounce-Patch: bulk_sends %s update fehlgeschlagen: %s",
                           bulk_id, exc)


async def _flag_contact_bounced(email_lower: str, reason: str) -> None:
    """Setzt contacts.bounced=true + bounced_at + bounced_reason.

    No-op wenn Kontakt nicht existiert (bouncte Adresse war nie im
    Kontakt-Stamm — z.B. einmaliger Massenversand an Fremdliste).
    """
    try:
        res = await pb_client.pb_get(
            "/api/collections/contacts/records",
            params={
                "filter": f'email = {pb_client.pb_quote(email_lower)}',
                "perPage": 1,
            },
        )
    except Exception as exc:
        logger.warning("Contact-Bounce-Flag Read fehlgeschlagen %s: %s", email_lower, exc)
        return
    items = res.get("items") or []
    if not items:
        return
    contact_id = items[0]["id"]
    try:
        await pb_client.pb_patch(
            f"/api/collections/contacts/records/{contact_id}",
            {
                "bounced": True,
                "bounced_at": _format_pb_dt(datetime.now(timezone.utc)),
                "bounced_reason": (reason or "")[:500],
            },
        )
        logger.info("Kontakt %s als bounced markiert: %s", email_lower, reason[:100] if reason else "")
    except Exception as exc:
        logger.warning("Contact-Bounce-Flag Patch fehlgeschlagen %s: %s", email_lower, exc)


async def apply_bounce(dsn: dict) -> None:
    """Public Entry-Point für den IMAP-Sync. Matched DSN gegen bulk_sends und
    flaggt bei permanentem Fehler (5.x.x) den Kontakt.

    DSN-Schema (siehe bounce_parser.parse_dsn):
      {message_id, failed_recipient, diagnostic, status}
    """
    from bounce_parser import is_permanent_failure
    message_id = dsn.get("message_id")
    failed = dsn.get("failed_recipient")
    status = dsn.get("status")
    reason = (dsn.get("diagnostic") or status or "DSN")[:500]

    if not message_id and not failed:
        logger.info("DSN ohne Message-ID und Final-Recipient — kein Match möglich")
        return

    match = await _find_bulk_recipient_match(message_id, failed)
    if match:
        bulk_id, email_lower = match
        await _patch_bulk_recipient_bounced(bulk_id, email_lower, reason)
        if is_permanent_failure(status):
            await _flag_contact_bounced(email_lower, reason)
        logger.info("Bounce verarbeitet: bulk=%s email=%s status=%s permanent=%s",
                    bulk_id, email_lower, status, is_permanent_failure(status))
        return

    # Kein bulk-Match — wenn email + permanent: Kontakt trotzdem flaggen.
    if failed and is_permanent_failure(status):
        await _flag_contact_bounced(failed.lower(), reason)
        logger.info("Bounce ohne bulk-Match, Kontakt geflaggt: %s status=%s",
                    failed, status)
    else:
        logger.info("Bounce ohne Match (msg_id=%s, email=%s, status=%s) — ignoriert",
                    message_id, failed, status)


# ---------------------------------------------------------------------------
# IMAP-Aktions-Helper (Wrapper um ImapService-Methoden mit Account-Lookup)
# ---------------------------------------------------------------------------


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

    await asyncio.to_thread(ImapService(acc).set_read, imap_uid, folder, is_read)


async def _imap_set_answered(email: dict) -> None:
    """Setzt \\Answered-Flag auf dem IMAP-Server."""
    account_id = email.get("account")
    imap_uid = email.get("imap_uid")
    folder = email.get("folder", "INBOX")
    if not account_id or not imap_uid:
        return

    acc = await _get_imap_account(account_id)
    if acc is None:
        return

    await asyncio.to_thread(ImapService(acc).set_answered, imap_uid, folder)


async def _imap_set_answered_safe(email: dict) -> None:
    """Wrapper: setzt \\Answered auf IMAP, schluckt Fehler (fire-and-forget)."""
    try:
        await _imap_set_answered(email)
    except Exception as exc:
        logger.warning("IMAP set-answered fehlgeschlagen für UID %s: %s", email.get("imap_uid"), exc)


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

    return await asyncio.to_thread(
        ImapService(acc).move, imap_uid, source_folder, target_folder, email.get("message_id", ""),
    )


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

    return await asyncio.to_thread(
        ImapService(acc).move_to_spam, imap_uid, folder, email.get("message_id", ""),
    )


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

    await asyncio.to_thread(
        ImapService(acc).trash, imap_uid, folder, email.get("message_id", ""),
    )
