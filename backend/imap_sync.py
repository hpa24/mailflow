import logging
from datetime import datetime, timezone

from imapclient import IMAPClient

import pb_client
from config import settings
from fts import fts_insert
from mime_parser import parse_email, extract_attachment_meta

logger = logging.getLogger(__name__)

MAX_CONNECTIONS_PER_ACCOUNT = 4

# IMAP-Spezial-Flags → normierte Ordnernamen (für konsistente Filterung in der UI)
_IMAP_FLAG_TO_STANDARD: dict[bytes, str] = {
    b"\\Drafts": "Drafts",
    b"\\Draft":  "Drafts",
    b"\\Sent":   "Sent",
    b"\\Trash":  "Trash",
    b"\\Deleted": "Trash",
    b"\\Junk":   "Spam",
    b"\\Spam":   "Spam",
    b"\\Archive": "Archive",
}

# Laufender Import-Status (in-memory, wird bei Neustart zurückgesetzt)
_import_status: dict = {
    "running": False,
    "total": 0,
    "done": 0,
    "errors": 0,
    "last_sync": None,  # datetime | None
}


def set_last_sync(ts: datetime) -> None:
    _import_status["last_sync"] = ts


def get_sync_status() -> dict:
    pct = (_import_status["done"] / _import_status["total"] * 100
           if _import_status["total"] > 0 else 0)
    last = _import_status["last_sync"]
    return {
        "running": _import_status["running"],
        "total": _import_status["total"],
        "done": _import_status["done"],
        "percent": round(pct, 1),
        "errors": _import_status["errors"],
        "last_sync": last.isoformat() if last else None,
    }


async def sync_all_accounts() -> None:
    """Inkrementeller Sync aller Accounts — nur neue E-Mails holen."""
    accounts = await _get_all_accounts()
    for account in accounts:
        try:
            await sync_account(account, full_import=False)
        except Exception as e:
            logger.error(f"Sync failed for account {account['id']}: {e}")


async def sync_account(account: dict, full_import: bool = False) -> None:
    """Sync einen Account. full_import=True holt alle UIDs (für Erst-Import)."""
    imap_host = account["imap_host"]
    imap_port = int(account["imap_port"] or 993)
    imap_user = account["imap_user"]
    imap_pass = account["imap_pass"]
    account_id = account["id"]

    logger.info(f"Syncing account {account_id} ({imap_user}) full={full_import}")

    with IMAPClient(imap_host, port=imap_port, ssl=True) as server:
        server.login(imap_user, imap_pass)
        folders = server.list_folders()
        folder_names = [f[2] for f in folders]

        # INBOX explizit zuerst — wird von list_folders() manchmal nicht zurückgegeben
        if "INBOX" not in folder_names:
            folder_names.insert(0, "INBOX")
        else:
            # INBOX immer als erstes syncen
            folder_names.remove("INBOX")
            folder_names.insert(0, "INBOX")

        # Normierten Namen und \NoSelect-Flag je Ordner aus IMAP-Flags bestimmen
        folder_standard: dict[str, str] = {}
        no_select_folders: set[str] = set()
        for folder_flags, _delim, folder_name in folders:
            if b"\\NoSelect" in folder_flags:
                no_select_folders.add(folder_name)
                continue  # Email-Standard-Normierung nicht nötig
            for flag, standard in _IMAP_FLAG_TO_STANDARD.items():
                if flag in folder_flags:
                    folder_standard[folder_name] = standard
                    break
        # INBOX immer explizit normieren
        if "INBOX" in folder_names:
            folder_standard.setdefault("INBOX", "INBOX")

        # \NoSelect-Ordner als Folder-Record anlegen, aber keinen E-Mail-Sync starten
        for folder_name in no_select_folders:
            try:
                await _get_or_create_folder(
                    account_id, folder_name, uidvalidity=0,
                    email_folder=folder_name, no_select=True
                )
                logger.debug(f"NoSelect folder registered: '{folder_name}'")
            except Exception as e:
                logger.warning(f"Could not register NoSelect folder '{folder_name}': {e}")

        for folder_name in folder_names:
            if folder_name in no_select_folders:
                continue  # bereits oben behandelt
            try:
                await _sync_folder(server, account_id, folder_name, full_import, folder_standard)
            except Exception as e:
                logger.error(f"Folder '{folder_name}' sync failed: {e}")
                continue

        # Folder-Records für nicht mehr existierende Ordner aus PocketBase entfernen
        all_imap_paths = set(folder_names) | no_select_folders
        await _cleanup_deleted_folders(account_id, all_imap_paths)


async def _sync_folder(server: IMAPClient, account_id: str,
                       folder_name: str, full_import: bool,
                       folder_standard: dict[str, str] | None = None) -> None:
    select_info = server.select_folder(folder_name, readonly=True)
    current_uidvalidity = select_info.get(b"UIDVALIDITY", 0)

    # Normierter Ordnername für PocketBase (z.B. "INBOX.Drafts" → "Drafts")
    stored_folder_name = (folder_standard or {}).get(folder_name, folder_name)

    # Gespeicherten Ordner-Status holen
    folder_record = await _get_or_create_folder(account_id, folder_name, current_uidvalidity, stored_folder_name)
    stored_uidvalidity = folder_record.get("uidvalidity", 0)
    last_sync_uid = folder_record.get("last_sync_uid", 0)

    # UIDVALIDITY-Check — Ordner wurde auf dem Server zurückgesetzt
    if stored_uidvalidity and current_uidvalidity != stored_uidvalidity:
        logger.warning(f"UIDVALIDITY changed for '{folder_name}' — clearing folder index")
        await _delete_emails_for_folder(account_id, stored_folder_name)
        last_sync_uid = 0

    # UIDs holen: alle (full_import) oder nur neue (inkrementell)
    if full_import or last_sync_uid == 0:
        uids = server.search(["ALL"])
    else:
        uids = server.search(["UID", f"{last_sync_uid + 1}:*"])

    if not uids:
        logger.info(f"No new messages in '{folder_name}' (stored as '{stored_folder_name}')")
        await _update_folder(folder_record["id"], current_uidvalidity, last_sync_uid,
                             unread_count=_count_unread(server))
        await _sync_flags_recent(server, account_id, stored_folder_name, last_sync_uid)
        return

    # Neueste zuerst (für Erst-Import: wichtigste E-Mails früh verfügbar)
    uids = sorted(uids, reverse=True)
    logger.info(f"Fetching {len(uids)} messages from '{folder_name}' (stored as '{stored_folder_name}')")

    new_last_uid = last_sync_uid
    for uid in uids:
        try:
            await _fetch_and_save(server, account_id, stored_folder_name, uid, current_uidvalidity)
            if uid > new_last_uid:
                new_last_uid = uid
            if full_import:
                _import_status["done"] += 1
        except Exception as e:
            logger.error(f"UID {uid} in '{folder_name}' failed: {e}")
            if full_import:
                _import_status["errors"] += 1
            continue

    await _update_folder(folder_record["id"], current_uidvalidity, new_last_uid,
                         unread_count=_count_unread(server))
    await _sync_flags_recent(server, account_id, stored_folder_name, new_last_uid)


async def _fetch_and_save(server: IMAPClient, account_id: str,
                          folder_name: str, uid: int,
                          current_uidvalidity: int = 0) -> None:
    # FLAGS + Header + Body separat laden — kein RFC822 (würde Anhänge mitladen)
    meta_data = server.fetch([uid], [b"FLAGS", b"BODY.PEEK[HEADER]", b"BODY.PEEK[TEXT]"])
    if uid not in meta_data:
        return

    flags = meta_data[uid].get(b"FLAGS", [])
    is_read = b"\\Seen" in flags
    is_flagged = b"\\Flagged" in flags
    is_answered = b"\\Answered" in flags

    header_bytes = meta_data[uid].get(b"BODY[HEADER]") or b""
    body_bytes = meta_data[uid].get(b"BODY[TEXT]") or b""

    # mailparser braucht eine vollständige RFC822-Nachricht (Header + Body)
    raw_bytes = header_bytes + b"\r\n" + body_bytes

    if not header_bytes and not body_bytes:
        logger.warning(f"No content for UID {uid}")
        return

    parsed = parse_email(raw_bytes)
    if not parsed:
        return

    # Doppelte E-Mails verhindern (unique message_id)
    message_id = parsed.get("message_id") or f"no-id-{account_id}-{uid}"

    in_reply_to = parsed.get("in_reply_to", "")
    thread_id = await _compute_thread_id(message_id, in_reply_to)

    body_plain = (parsed.get("body_plain") or "")[:500_000]
    body_html  = (parsed.get("body_html")  or "")[:500_000]
    record = {
        "account": account_id,
        "imap_uid": uid,
        "uidvalidity": current_uidvalidity,
        "folder": folder_name,
        "message_id": message_id,
        "thread_id": thread_id,
        "in_reply_to": parsed.get("in_reply_to", ""),
        "from_email": parsed.get("from_email", ""),
        "from_name": parsed.get("from_name", ""),
        "reply_to": parsed.get("reply_to", ""),
        "to_emails": parsed.get("to_emails", []),
        "cc_emails": parsed.get("cc_emails", []),
        "subject": parsed.get("subject", ""),
        "body_plain": body_plain,
        "body_html": body_html,
        "snippet": parsed.get("snippet", ""),
        "date_sent": parsed.get("date_sent"),
        "is_read": is_read,
        "is_flagged": is_flagged,
        "is_answered": is_answered,
        "has_attachments": parsed.get("has_attachments", False),
    }

    # In PocketBase speichern (doppelte message_id wird durch unique index abgefangen)
    try:
        result = await pb_client.pb_post("/api/collections/emails/records", record)
        email_id = result["id"]

        # FTS5-Index aktualisieren
        fts_insert(
            settings.PB_DATA_PATH,
            email_id,
            record["subject"],
            record["body_plain"],
            record["from_email"],
            record["from_name"],
        )

        # Kontakt aktualisieren
        await upsert_contact(record["from_email"], record["from_name"], record["date_sent"])

        # Anhang-Metadaten speichern
        if record["has_attachments"]:
            att_meta = extract_attachment_meta(raw_bytes)
            for att in att_meta:
                try:
                    await pb_client.pb_post("/api/collections/attachments/records", {
                        "email": email_id,
                        "filename": att["filename"],
                        "mime_type": att["mime_type"],
                        "size_bytes": att["size_bytes"],
                        "part_id": str(att["part_index"]),
                    })
                except Exception as att_exc:
                    logger.warning(f"Anhang-Metadaten für UID {uid} nicht gespeichert: {att_exc}")

    except pb_client.DuplicateRecordError:
        pass  # E-Mail bereits vorhanden — ignorieren
    except Exception as e:
        raise


def _count_unread(server: IMAPClient) -> int:
    try:
        return len(server.search(["UNSEEN"]))
    except Exception:
        return 0


async def _get_all_accounts() -> list[dict]:
    result = await pb_client.pb_get("/api/collections/accounts/records",
                                    params={"perPage": 100})
    return result.get("items", [])


async def _get_or_create_folder(account_id: str, imap_path: str,
                                uidvalidity: int, email_folder: str = "",
                                no_select: bool = False) -> dict:
    """Gibt den Folder-Record zurück. Erstellt ihn falls nötig.
    email_folder: normierter Name der in emails.folder gespeichert wird (z.B. 'Drafts' für 'INBOX.Drafts').
    no_select: True wenn der Ordner \\NoSelect hat (reiner Namensraum, keine E-Mails).
    """
    result = await pb_client.pb_get(
        "/api/collections/folders/records",
        params={"filter": f'account="{account_id}" && imap_path="{imap_path}"', "perPage": 1},
    )
    items = result.get("items", [])
    if items:
        folder = items[0]
        updates = {}
        if email_folder and not folder.get("email_folder"):
            updates["email_folder"] = email_folder
        if no_select and not folder.get("no_select"):
            updates["no_select"] = True
        if updates:
            await pb_client.pb_patch(
                f"/api/collections/folders/records/{folder['id']}", updates
            )
            folder.update(updates)
        return folder

    display_name = imap_path.split("/")[-1].split(".")[-1]
    new_folder = await pb_client.pb_post("/api/collections/folders/records", {
        "account": account_id,
        "imap_path": imap_path,
        "display_name": display_name,
        "email_folder": email_folder or imap_path,
        "no_select": no_select,
        "unread_count": 0,
        "last_sync_uid": 0,
        "uidvalidity": uidvalidity,
    })
    return new_folder


async def _update_folder(folder_id: str, uidvalidity: int,
                         last_sync_uid: int, unread_count: int) -> None:
    await pb_client.pb_patch(f"/api/collections/folders/records/{folder_id}", {
        "uidvalidity": uidvalidity,
        "last_sync_uid": last_sync_uid,
        "unread_count": unread_count,
    })


async def _cleanup_deleted_folders(account_id: str, existing_imap_paths: set[str]) -> None:
    """Entfernt Folder-Records aus PocketBase, die auf dem IMAP-Server nicht mehr existieren."""
    result = await pb_client.pb_get(
        "/api/collections/folders/records",
        params={"filter": f'account="{account_id}"', "perPage": 200,
                "fields": "id,imap_path"},
    )
    for folder in result.get("items", []):
        if folder["imap_path"] not in existing_imap_paths:
            try:
                await pb_client.pb_delete(f"/api/collections/folders/records/{folder['id']}")
                logger.info(f"Removed deleted folder record: '{folder['imap_path']}'")
            except Exception as e:
                logger.warning(f"Could not remove folder record '{folder['imap_path']}': {e}")


async def _delete_emails_for_folder(account_id: str, folder_name: str) -> None:
    result = await pb_client.pb_get(
        "/api/collections/emails/records",
        params={"filter": f'account="{account_id}" && folder="{folder_name}"',
                "perPage": 500},
    )
    for email in result.get("items", []):
        try:
            await pb_client.pb_post(
                f"/api/collections/emails/records/{email['id']}", {}
            )
        except Exception:
            pass


async def _compute_thread_id(message_id: str, in_reply_to: str) -> str:
    """Find thread_id by following in_reply_to chain. Falls back to own message_id."""
    if not in_reply_to:
        return message_id
    try:
        result = await pb_client.pb_get(
            "/api/collections/emails/records",
            params={"filter": f'message_id="{in_reply_to}"', "perPage": 1,
                    "fields": "id,thread_id"}
        )
        items = result.get("items", [])
        if items and items[0].get("thread_id"):
            return items[0]["thread_id"]
    except Exception:
        pass
    return message_id


FLAG_SYNC_WINDOW = 200  # Wie viele der letzten UIDs auf Flag-Änderungen geprüft werden


async def _sync_flags_recent(server: IMAPClient, account_id: str,
                              folder_name: str, last_uid: int) -> None:
    """Vergleicht Flags der letzten FLAG_SYNC_WINDOW UIDs und entfernt
    verschobene/gelöschte E-Mails aus PocketBase."""
    if last_uid <= 0:
        return
    from_uid = max(1, last_uid - FLAG_SYNC_WINDOW + 1)

    # Welche UIDs sind im Fenster noch auf dem Server vorhanden?
    try:
        present_uids = set(server.search(["UID", f"{from_uid}:{last_uid}"]))
    except Exception as e:
        logger.warning(f"UID search failed for '{folder_name}': {e}")
        return

    # Flags nur für vorhandene UIDs holen
    imap_flags: dict = {}
    if present_uids:
        try:
            imap_flags = server.fetch(list(present_uids), [b"FLAGS"])
        except Exception as e:
            logger.warning(f"Flag fetch failed for '{folder_name}': {e}")

    # PocketBase: alle E-Mails in diesem UID-Bereich laden
    pb_result = await pb_client.pb_get(
        "/api/collections/emails/records",
        params={
            "filter": (f'account="{account_id}" && folder="{folder_name}" '
                       f'&& imap_uid>={from_uid} && imap_uid<={last_uid}'),
            "perPage": FLAG_SYNC_WINDOW,
            "fields": "id,imap_uid,is_read,is_flagged,is_answered",
        },
    )
    pb_emails = {e["imap_uid"]: e for e in pb_result.get("items", [])}

    # Verschobene / gelöschte UIDs aus PocketBase entfernen
    removed_uids = set(pb_emails.keys()) - present_uids
    for uid in removed_uids:
        pb_email = pb_emails[uid]
        try:
            await pb_client.pb_delete(
                f"/api/collections/emails/records/{pb_email['id']}"
            )
            logger.info(f"Removed UID {uid} from '{folder_name}' (moved/deleted on server)")
        except Exception as e:
            logger.warning(f"Delete failed for UID {uid}: {e}")

    # Flag-Änderungen auf verbliebenen E-Mails anwenden (\Seen, \Flagged, \Answered)
    for uid, data in imap_flags.items():
        flags = data.get(b"FLAGS", [])
        imap_is_read     = b"\\Seen"     in flags
        imap_is_flagged  = b"\\Flagged"  in flags
        imap_is_answered = b"\\Answered" in flags
        pb_email = pb_emails.get(uid)
        if not pb_email:
            continue
        updates = {}
        if pb_email["is_read"]     != imap_is_read:
            updates["is_read"]     = imap_is_read
        if pb_email["is_flagged"]  != imap_is_flagged:
            updates["is_flagged"]  = imap_is_flagged
        if pb_email.get("is_answered") != imap_is_answered:
            updates["is_answered"] = imap_is_answered
        if updates:
            try:
                await pb_client.pb_patch(
                    f"/api/collections/emails/records/{pb_email['id']}",
                    updates,
                )
                logger.debug(f"Flag sync UID {uid}: {updates}")
            except Exception as e:
                logger.warning(f"Flag sync patch failed for UID {uid}: {e}")


async def upsert_contact(email: str, name: str, last_contact: str | None) -> None:
    if not email:
        return
    try:
        result = await pb_client.pb_get(
            "/api/collections/contacts/records",
            params={"filter": f'email="{email}"', "perPage": 1},
        )
        items = result.get("items", [])
        if items:
            contact = items[0]
            await pb_client.pb_patch(f"/api/collections/contacts/records/{contact['id']}", {
                "email_count": contact.get("email_count", 0) + 1,
                "last_contact": last_contact,
                "name": name or contact.get("name", ""),
            })
        else:
            await pb_client.pb_post("/api/collections/contacts/records", {
                "email": email,
                "name": name,
                "email_count": 1,
                "last_contact": last_contact,
            })
    except Exception as e:
        logger.warning(f"Contact upsert failed for {email}: {e}")
