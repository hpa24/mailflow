"""IMAP-Service — kapselt alle blocking IMAP-Operationen pro Account.

Phase 1 (erledigt): `imap_session(acc)`-Context-Manager als zentrales Login.
Phase 2 (diese Datei): vollständige `ImapService`-Klasse mit den Methoden, die
vorher als `_imap_*_sync` in main.py lagen — gleiche Logik, ein gemeinsamer
Ort, plus Platz für BODYSTRUCTURE (B9) in `fetch_attachment` / `fetch_inline`.

Konvention: Methoden sind blocking. Aufrufer wrappen mit
`asyncio.to_thread(svc.method, ...)` oder `loop.run_in_executor`.
Jeder Methodenaufruf öffnet eine eigene IMAP-Verbindung (kein Connection-Pool).
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator

from imapclient import IMAPClient

from imap_utils import find_imap_folder, resolve_imap_path
from mime_parser import (
    get_attachment_payload,
    get_inline_part_by_cid,
)

logger = logging.getLogger(__name__)


@contextmanager
def imap_session(acc: dict) -> Iterator[IMAPClient]:
    """Öffnet eine IMAP-Verbindung für ein Account-Dict (PB-Record).
    Login + automatische Cleanup via with-Statement.

    Beispiel:
        with imap_session(acc) as srv:
            srv.select_folder("INBOX")
            ...
    """
    host = acc["imap_host"]
    port = int(acc.get("imap_port") or 993)
    user = acc["imap_user"]
    password = acc["imap_pass"]
    with IMAPClient(host, port=port, ssl=True) as srv:
        srv.login(user, password)
        yield srv


async def run_blocking(fn, *args, **kwargs):
    """Wrapper um asyncio.to_thread für blocking IMAP-Operationen.
    Bestehende Aufrufer nutzen oft loop.run_in_executor — diese Funktion
    kapselt das einheitlich.
    """
    return await asyncio.to_thread(fn, *args, **kwargs)


class ImapService:
    """Alle IMAP-Operationen für einen Account.

    Methoden sind blocking — Aufrufer wrappt in einem Thread-Pool/Executor.
    Login und Cleanup passieren pro Methodenaufruf via `imap_session(self.acc)`.
    """

    def __init__(self, acc: dict):
        self.acc = acc

    # ------------------------------------------------------------------
    # Drafts
    # ------------------------------------------------------------------
    def append_draft(self, msg_bytes: bytes, message_id: str | None = None) -> None:
        """Blockierender IMAP APPEND in den Drafts-Ordner.
        Löscht vorher eine alte Version mit gleicher Message-ID (verhindert Duplikate)."""
        if not all([self.acc.get("imap_host"), self.acc.get("imap_user"), self.acc.get("imap_pass")]):
            raise ValueError("Unvollständige IMAP-Zugangsdaten")

        with imap_session(self.acc) as srv:
            drafts_folder = find_imap_folder(
                srv,
                [b"\\Drafts", b"\\Draft"],
                ["Drafts", "Draft", "Entwürfe", "INBOX.Drafts"],
            )
            if not drafts_folder:
                raise ValueError("Kein Drafts-Ordner auf dem IMAP-Server gefunden")

            srv.select_folder(drafts_folder)

            if message_id:
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

    # ------------------------------------------------------------------
    # Attachments / Inline
    # ------------------------------------------------------------------
    def fetch_attachment(self, folder: str, imap_uid: int, part_index: int) -> bytes:
        """Lädt einen Anhang per IMAP. Aktuell BODY[] — wird in B9 auf BODYSTRUCTURE umgestellt."""
        with imap_session(self.acc) as srv:
            srv.select_folder(folder, readonly=True)
            data = srv.fetch([imap_uid], [b"BODY[]"])
            raw = data.get(imap_uid, {}).get(b"BODY[]") or b""

        payload, _, _ = get_attachment_payload(raw, part_index)
        return payload

    def fetch_inline(self, folder: str, imap_uid: int, cid: str) -> tuple[bytes, str]:
        """Lädt ein Inline-Bild per Content-ID. Aktuell BODY[] — wird in B9 auf BODYSTRUCTURE umgestellt."""
        with imap_session(self.acc) as srv:
            srv.select_folder(folder, readonly=True)
            data = srv.fetch([imap_uid], [b"BODY[]"])
            raw = data.get(imap_uid, {}).get(b"BODY[]") or b""

        return get_inline_part_by_cid(raw, cid)

    # ------------------------------------------------------------------
    # Flags (Read / Answered)
    # ------------------------------------------------------------------
    def set_read(self, imap_uid: int, folder: str, is_read: bool) -> None:
        with imap_session(self.acc) as srv:
            srv.select_folder(resolve_imap_path(srv, folder))
            if is_read:
                srv.set_flags([imap_uid], [b"\\Seen"])
            else:
                srv.remove_flags([imap_uid], [b"\\Seen"])

    def set_answered(self, imap_uid: int, folder: str) -> None:
        with imap_session(self.acc) as srv:
            srv.select_folder(folder)
            srv.add_flags([imap_uid], [b"\\Answered"])

    def bulk_set_read(self, folder: str, uids: list, is_read: bool) -> None:
        """Setzt/entfernt \\Seen-Flag für mehrere UIDs in einer Verbindung."""
        logger.info("IMAP bulk_set: folder='%s' uids=%s is_read=%s", folder, uids, is_read)
        with imap_session(self.acc) as srv:
            try:
                srv.select_folder(folder)
            except Exception as ex:
                logger.warning(
                    "IMAP bulk_set: select_folder('%s') fehlgeschlagen: %s — versuche INBOX",
                    folder, ex,
                )
                srv.select_folder("INBOX")
            if is_read:
                srv.set_flags(uids, [b"\\Seen"])
            else:
                srv.remove_flags(uids, [b"\\Seen"])
            logger.info(
                "IMAP bulk_set: %d UIDs in '%s' auf is_read=%s gesetzt",
                len(uids), folder, is_read,
            )

    # ------------------------------------------------------------------
    # Move / Trash / Spam
    # ------------------------------------------------------------------
    def move_to_spam(self, imap_uid: int, folder: str, message_id: str) -> tuple[str, int | None]:
        """Verschiebt im IMAP nach Junk/Spam-Folder; gibt immer den normierten UI-Namen 'Spam' zurück
        (analog zum Mapping in imap_sync._IMAP_FLAG_TO_STANDARD)."""
        with imap_session(self.acc) as srv:
            real_source = resolve_imap_path(srv, folder)
            srv.select_folder(real_source)
            spam = find_imap_folder(
                srv,
                [b"\\Junk", b"\\Spam"],
                ["Spam", "Junk", "Junk E-Mail", "INBOX.Spam", "INBOX.Junk"],
            )
            if spam and spam.lower() != real_source.lower():
                caps = srv.capabilities()
                if b"MOVE" in caps:
                    srv.move([imap_uid], spam)
                else:
                    srv.copy([imap_uid], spam)
                    srv.set_flags([imap_uid], [b"\\Deleted"])
                    srv.expunge()
                new_uid = self._search_by_msgid(srv, spam, message_id)
                return "Spam", new_uid
            return "Spam", None

    def move(self, imap_uid: int, source_folder: str, target_folder: str, message_id: str) -> int | None:
        with imap_session(self.acc) as srv:
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
            return self._search_by_msgid(srv, real_target, message_id)

    def trash(self, imap_uid: int, folder: str, message_id: str) -> None:
        with imap_session(self.acc) as srv:
            real_source = resolve_imap_path(srv, folder)
            srv.select_folder(real_source)
            caps = srv.capabilities()
            if b"MOVE" in caps:
                trash = find_imap_folder(
                    srv,
                    [b"\\Trash", b"\\Deleted"],
                    ["Trash", "Deleted", "Deleted Items", "Papierkorb", "INBOX.Trash"],
                )
                if trash and trash.lower() != real_source.lower():
                    srv.move([imap_uid], trash)
                    new_uid = self._search_by_msgid(srv, trash, message_id)
                    if new_uid:
                        srv.select_folder(trash)
                        srv.set_flags([new_uid], [b"\\Seen"])
                        logger.info(
                            "_imap_trash: \\Seen gesetzt auf neuer UID %s in '%s'",
                            new_uid, trash,
                        )
                    return
            srv.set_flags([imap_uid], [b"\\Deleted"])
            srv.expunge()

    # ------------------------------------------------------------------
    # Backfill / Sync-Helpers
    # ------------------------------------------------------------------
    def fetch_uids_with_msgids(self, folder: str) -> dict[int, str]:
        """Holt alle UIDs eines Ordners zusammen mit deren Message-ID-Header.
        Wird vom backfill-Endpoint genutzt. Batches à 200 UIDs."""
        BATCH = 200
        with imap_session(self.acc) as srv:
            srv.select_folder(folder, readonly=True)
            uids = srv.search(["ALL"])
            if not uids:
                return {}
            uid_to_msgid: dict[int, str] = {}
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

    # ------------------------------------------------------------------
    # Interne Helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _search_by_msgid(srv, folder: str, message_id: str) -> int | None:
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
