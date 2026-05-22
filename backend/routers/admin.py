"""Admin-Endpoints — embed-Backfill, embed-Suche, IMAP-UID-Backfill.

Ausgegliedert aus main.py im Rahmen von C1 Phase 2 (Router-Split).

Auth: Pfad-basiert in der globalen Middleware in main.py. `/admin/*` erfordert
Header `X-Admin-Key` mit dem `ADMIN_API_KEY` aus den Settings. PB-Bearer reicht
hier NICHT. Daher braucht der Router selbst keinen zusätzlichen Auth-Check.
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, BackgroundTasks, HTTPException

import pb_client
from backfill import get_embed_state, run_embed_backfill
from config import settings
from services.imap import ImapService

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/admin/embed-backfill")
async def start_embed_backfill(background_tasks: BackgroundTasks):
    """Startet den Embed-Backfill aller E-Mails in Qdrant (API-Key-geschützt)."""
    if not settings.QDRANT_URL:
        raise HTTPException(status_code=503, detail="QDRANT_URL nicht konfiguriert")
    state = get_embed_state()
    if state["status"] == "running":
        return {"detail": "Backfill läuft bereits", "state": state}
    background_tasks.add_task(run_embed_backfill)
    return {"detail": "Backfill gestartet — Fortschritt via GET /admin/embed-status"}


@router.get("/admin/embed-status")
async def embed_status():
    """Gibt den aktuellen Fortschritt des Embed-Backfills zurück."""
    return get_embed_state()


@router.get("/admin/embed-search")
async def embed_search(q: str, limit: int = 5):
    """Semantische Testsuche in Qdrant. Gibt Top-N ähnliche Threads zurück."""
    if not settings.QDRANT_URL:
        raise HTTPException(status_code=503, detail="QDRANT_URL nicht konfiguriert")
    from vector_store import search_similar
    results = await search_similar(q, limit=limit)
    return {"query": q, "results": results}


@router.post("/admin/backfill-imap-uids")
async def backfill_imap_uids():
    """Korrigiert falsche imap_uid-Werte für alle E-Mails.

    Für jeden (Account, Ordner): IMAP öffnen, alle UIDs + Message-IDs laden,
    mit PocketBase abgleichen und abweichende imap_uid-Werte updaten.
    Läuft im Hintergrund (fire-and-forget via BackgroundTask ist hier synchron).
    """
    accounts_data = await pb_client.pb_get("/api/collections/accounts/records", params={"perPage": 50})
    accounts = accounts_data.get("items", [])

    total_fixed = 0
    total_checked = 0
    errors = []

    for acc in accounts:
        account_id = acc["id"]
        # Alle Ordner dieses Accounts aus PocketBase
        folder_data = await pb_client.pb_get("/api/collections/folders/records", params={
            "filter": f'account={pb_client.pb_quote(account_id)}',
            "perPage": 200,
            "fields": "id,imap_path",
        })
        folders = [f["imap_path"] for f in folder_data.get("items", []) if f.get("imap_path")]

        for imap_folder in folders:
            try:
                # PocketBase-E-Mails für diesen Ordner laden (Message-ID + imap_uid)
                pb_data = await pb_client.pb_get("/api/collections/emails/records", params={
                    "filter": f'account={pb_client.pb_quote(account_id)} && folder={pb_client.pb_quote(imap_folder)}',
                    "perPage": 2000,
                    "fields": "id,message_id,imap_uid",
                })
                pb_emails = pb_data.get("items", [])
                if not pb_emails:
                    continue

                pb_by_msgid = {e["message_id"]: e for e in pb_emails if e.get("message_id")}
                total_checked += len(pb_emails)

                # IMAP: alle UIDs + Message-IDs für diesen Ordner holen (blocking)
                uid_to_msgid = await asyncio.to_thread(
                    ImapService(acc).fetch_uids_with_msgids, imap_folder,
                )

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
