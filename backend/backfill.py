"""
Backfill-Aufgaben:
- is_answered/is_flagged aus IMAP nachfüllen (einmalig, Marker-Datei)
- HTML-Inhalte nachfüllen (einmalig, Marker-Datei)
- Embed-Backfill: alle E-Mails in Qdrant einbetten (manuell per API-Endpoint)
"""
import logging
import os

from imapclient import IMAPClient

import pb_client
from config import settings
from fts import fts_rebuild

logger = logging.getLogger(__name__)

MARKER_FILE = "/tmp/mailflow_backfill_done"
FTS_MARKER_FILE = "/tmp/mailflow_fts_rebuilt"
HTML_MARKER_FILE = "/tmp/mailflow_html_backfill_v2_done"  # v2: stdlib-Charset-Fix


async def rebuild_fts_if_needed() -> None:
    """FTS5-Index neu aufbauen falls er fehlerhafte Einträge enthält."""
    if os.path.exists(FTS_MARKER_FILE):
        return
    logger.info("FTS rebuild: starting (background task)")
    try:
        page, total = 1, 0
        all_records = []
        while True:
            resp = await pb_client.pb_get(
                "/api/collections/emails/records",
                params={
                    "perPage": 500,
                    "page": page,
                    "fields": "id,subject,body_plain,from_email,from_name",
                },
            )
            items = resp.get("items", [])
            all_records.extend(items)
            if page >= resp.get("totalPages", 1):
                break
            page += 1
        total = fts_rebuild(settings.PB_DATA_PATH, all_records)
        open(FTS_MARKER_FILE, "w").close()
        logger.info(f"FTS rebuild: done — {total} records indexed")
    except Exception as e:
        logger.error(f"FTS rebuild failed: {e}")
PB_PAGE_SIZE = 500  # PocketBase-Seite beim Laden aller E-Mails


async def run_once_if_needed() -> None:
    if os.path.exists(MARKER_FILE):
        return
    logger.info("Backfill: starting one-time is_answered/is_flagged backfill (all emails)")
    try:
        await _run_backfill()
        open(MARKER_FILE, "w").close()
        logger.info("Backfill: done — marker written")
    except Exception as e:
        logger.error(f"Backfill failed: {e}")


async def _run_backfill() -> None:
    accounts_resp = await pb_client.pb_get(
        "/api/collections/accounts/records", params={"perPage": 100}
    )
    for account in accounts_resp.get("items", []):
        try:
            await _backfill_account(account)
        except Exception as e:
            logger.error(f"Backfill failed for account {account['id']}: {e}")


async def _backfill_account(account: dict) -> None:
    with IMAPClient(
        account["imap_host"],
        port=int(account.get("imap_port") or 993),
        ssl=True,
    ) as server:
        server.login(account["imap_user"], account["imap_pass"])
        folder_names = [f[2] for f in server.list_folders()]
        if "INBOX" not in folder_names:
            folder_names.insert(0, "INBOX")

        for folder_name in folder_names:
            try:
                await _backfill_folder(server, account["id"], folder_name)
            except Exception as e:
                logger.warning(f"Backfill: folder '{folder_name}' failed: {e}")


async def _backfill_folder(server: IMAPClient, account_id: str,
                            folder_name: str) -> None:
    try:
        server.select_folder(folder_name, readonly=True)
    except Exception:
        return

    # Alle UIDs auf dem IMAP-Server für diesen Ordner
    all_uids = server.search(["ALL"])
    if not all_uids:
        return

    # FLAGS für alle UIDs holen (IMAP-Range ist effizienter als Liste)
    last_uid = max(all_uids)
    imap_flags = server.fetch([f"1:{last_uid}"], [b"FLAGS"])
    if not imap_flags:
        return

    # Alle PocketBase-Einträge für diesen Ordner seitenweise laden
    pb_emails: dict[int, dict] = {}
    page = 1
    while True:
        resp = await pb_client.pb_get(
            "/api/collections/emails/records",
            params={
                "filter": f'account="{account_id}" && folder="{folder_name}"',
                "perPage": PB_PAGE_SIZE,
                "page": page,
                "fields": "id,imap_uid,is_answered,is_flagged",
            },
        )
        items = resp.get("items", [])
        for e in items:
            pb_emails[e["imap_uid"]] = e
        if page >= resp.get("totalPages", 1):
            break
        page += 1

    updated = 0
    for uid, data in imap_flags.items():
        flags = data.get(b"FLAGS", [])
        imap_is_answered = b"\\Answered" in flags
        imap_is_flagged  = b"\\Flagged"  in flags
        pb_email = pb_emails.get(uid)
        if not pb_email:
            continue
        updates = {}
        if pb_email.get("is_answered") != imap_is_answered:
            updates["is_answered"] = imap_is_answered
        if pb_email.get("is_flagged") != imap_is_flagged:
            updates["is_flagged"] = imap_is_flagged
        if updates:
            await pb_client.pb_patch(
                f"/api/collections/emails/records/{pb_email['id']}", updates
            )
            updated += 1

    if updated:
        logger.info(f"Backfill: '{folder_name}' — {updated} records updated")


# ── HTML-Backfill ─────────────────────────────────────────────

async def backfill_html_once() -> None:
    """Füllt body_html für alle bestehenden E-Mails nach, die dieses Feld noch nicht haben.
    Läuft nur einmal (Marker-Datei verhindert Wiederholungen nach Neustart)."""
    if os.path.exists(HTML_MARKER_FILE):
        return
    logger.info("HTML-Backfill: startet im Hintergrund…")
    try:
        total = await _run_html_backfill()
        open(HTML_MARKER_FILE, "w").close()
        logger.info(f"HTML-Backfill: abgeschlossen — {total} E-Mails aktualisiert")
    except Exception as e:
        logger.error(f"HTML-Backfill fehlgeschlagen: {e}")


async def _run_html_backfill() -> int:
    from mime_parser import parse_email as _parse_email

    accounts_resp = await pb_client.pb_get(
        "/api/collections/accounts/records", params={"perPage": 100}
    )
    total = 0
    for account in accounts_resp.get("items", []):
        try:
            total += await _html_backfill_account(account, _parse_email)
        except Exception as e:
            logger.error(f"HTML-Backfill: Account {account['id']} fehlgeschlagen: {e}")
    return total


async def _html_backfill_account(account: dict, parse_email_fn) -> int:
    # Alle E-Mails des Accounts mit leerem body_html laden (seitenweise)
    to_update: list[dict] = []
    page = 1
    while True:
        resp = await pb_client.pb_get(
            "/api/collections/emails/records",
            params={
                "filter": f'account="{account["id"]}" && imap_uid > 0',
                "perPage": 500,
                "page": page,
                "fields": "id,imap_uid,folder",
            },
        )
        to_update.extend(resp.get("items", []))
        if page >= resp.get("totalPages", 1):
            break
        page += 1

    if not to_update:
        return 0

    logger.info(
        f"HTML-Backfill: {len(to_update)} E-Mails für Account {account['id']} ({account.get('imap_user')})"
    )

    # Nach Ordner gruppieren (IMAP-Verbindung pro Ordner)
    by_folder: dict[str, list[dict]] = {}
    for e in to_update:
        by_folder.setdefault(e.get("folder") or "INBOX", []).append(e)

    updated = 0
    with IMAPClient(
        account["imap_host"],
        port=int(account.get("imap_port") or 993),
        ssl=True,
    ) as server:
        server.login(account["imap_user"], account["imap_pass"])

        for folder, emails in by_folder.items():
            try:
                server.select_folder(folder, readonly=True)
            except Exception as e:
                logger.warning(f"HTML-Backfill: Ordner '{folder}' nicht auswählbar: {e}")
                continue

            uid_map: dict[int, str] = {e["imap_uid"]: e["id"] for e in emails}
            uids = list(uid_map.keys())

            # In 50er-Chunks laden, damit keine IMAP-Timeouts entstehen
            CHUNK = 50
            for i in range(0, len(uids), CHUNK):
                chunk = uids[i : i + CHUNK]
                try:
                    data = server.fetch(chunk, [b"BODY.PEEK[HEADER]", b"BODY.PEEK[TEXT]"])
                except Exception as e:
                    logger.warning(f"HTML-Backfill: fetch in '{folder}' fehlgeschlagen: {e}")
                    continue

                for uid, fdata in data.items():
                    pb_id = uid_map.get(uid)
                    if not pb_id:
                        continue
                    header = fdata.get(b"BODY[HEADER]") or b""
                    body   = fdata.get(b"BODY[TEXT]")   or b""
                    if not body:
                        continue
                    raw = header + b"\r\n" + body
                    parsed = parse_email_fn(raw)
                    html = (parsed.get("body_html") or "")[:500_000]
                    if not html:
                        continue
                    try:
                        await pb_client.pb_patch(
                            f"/api/collections/emails/records/{pb_id}",
                            {"body_html": html},
                        )
                        updated += 1
                    except Exception as e:
                        logger.warning(f"HTML-Backfill: PATCH {pb_id} fehlgeschlagen: {e}")

            logger.info(f"HTML-Backfill: Ordner '{folder}' — {updated} gesamt aktualisiert bisher")

    return updated


# ── Embed-Backfill ────────────────────────────────────────────

_EMBED_BATCH = 100  # E-Mails pro OpenAI-Batch-Request

_embed_state: dict = {
    "status": "idle",   # idle | running | done | error
    "total": 0,
    "done": 0,
    "errors": 0,
    "message": "",
}


def get_embed_state() -> dict:
    return dict(_embed_state)


async def run_embed_backfill() -> None:
    """Startet den Embed-Backfill als Hintergrund-Task.
    Idempotent: läuft nicht doppelt wenn bereits aktiv."""
    global _embed_state
    if _embed_state["status"] == "running":
        logger.warning("Embed-Backfill: läuft bereits, ignoriere neuen Aufruf")
        return
    _embed_state = {"status": "running", "total": 0, "done": 0, "errors": 0, "message": "Starte…"}
    try:
        await _do_embed_backfill()
        _embed_state["status"] = "done"
        _embed_state["message"] = (
            f"Fertig. {_embed_state['done']} eingebettet, {_embed_state['errors']} Fehler."
        )
        logger.info("Embed-Backfill: %s", _embed_state["message"])
    except Exception as e:
        _embed_state["status"] = "error"
        _embed_state["message"] = str(e)
        logger.error("Embed-Backfill fehlgeschlagen: %s", e)


async def _do_embed_backfill() -> None:
    from vector_store import ensure_collection, upsert_emails_batch

    await ensure_collection()

    page = 1
    total_pages = 1

    while page <= total_pages:
        resp = await pb_client.pb_get(
            "/api/collections/emails/records",
            params={
                "page": page,
                "perPage": _EMBED_BATCH,
                "fields": "id,account,folder,subject,body_plain,snippet,thread_id,from_email,date_sent",
            },
        )
        if page == 1:
            _embed_state["total"] = resp.get("totalItems", 0)
            total_pages = resp.get("totalPages", 1)
            logger.info("Embed-Backfill: %d E-Mails total, %d Seiten", _embed_state["total"], total_pages)

        items = resp.get("items", [])
        try:
            count = await upsert_emails_batch(items)
            _embed_state["done"] += count
            _embed_state["message"] = f"{_embed_state['done']}/{_embed_state['total']} eingebettet"
            logger.info("Embed-Backfill: %s", _embed_state["message"])
        except Exception as e:
            _embed_state["errors"] += len(items)
            logger.error("Embed-Backfill: Seite %d fehlgeschlagen: %s", page, e)

        page += 1
