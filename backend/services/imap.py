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
import base64
import logging
import quopri
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


# ----------------------------------------------------------------------
# BODYSTRUCTURE-Parser (B9: gezielte Anhang-Fetches statt komplettem BODY[])
# ----------------------------------------------------------------------

def _bs_str(v) -> str:
    if v is None:
        return ""
    if isinstance(v, bytes):
        return v.decode("utf-8", errors="replace")
    return str(v)


def _bs_params(params) -> dict:
    """IMAPClient liefert Content-Type-Parameter als flache Tuple-Liste
    `(b'NAME', b'foo.pdf', b'CHARSET', b'utf-8')`. In ein dict umwandeln."""
    if not params:
        return {}
    items = list(params)
    d: dict = {}
    for i in range(0, len(items) - 1, 2):
        k = _bs_str(items[i]).lower()
        v = _bs_str(items[i + 1])
        if k:
            d[k] = v
    return d


def _walk_bodystructure(bs, prefix: str = ""):
    """Yield (part_id, info) für jedes Leaf einer IMAPClient-BODYSTRUCTURE,
    Depth-First, kompatibel zur Reihenfolge von email.message.Message.walk().

    `info` enthält: maintype, subtype, params, content_id, encoding, size,
    disposition, disposition_params.
    """
    # Multipart: erstes Element ist selbst ein Tupel (Subpart-BodyData)
    if len(bs) > 0 and isinstance(bs[0], tuple):
        idx = 1
        for elem in bs:
            if isinstance(elem, tuple):
                sub_id = f"{prefix}.{idx}" if prefix else str(idx)
                yield from _walk_bodystructure(elem, sub_id)
                idx += 1
            else:
                break
        return

    part_id = prefix or "1"
    maintype = _bs_str(bs[0]).lower() if len(bs) > 0 else ""
    subtype = _bs_str(bs[1]).lower() if len(bs) > 1 else ""
    params = _bs_params(bs[2]) if len(bs) > 2 else {}
    content_id = _bs_str(bs[3]).strip("<>") if len(bs) > 3 and bs[3] else ""
    encoding = _bs_str(bs[5]).lower() if len(bs) > 5 and bs[5] else "7bit"
    size = bs[6] if len(bs) > 6 and isinstance(bs[6], int) else 0

    # Disposition-Tupel `(b'attachment', (b'FILENAME', b'foo.pdf'))` liegt in
    # den Extension-Feldern hinter octets/text_lines. Position variiert je
    # Server — daher von vorn durchsuchen, alles annehmen das wie ein
    # 2-Tupel mit bekannter Disposition aussieht.
    disposition = ""
    disposition_params: dict = {}
    for elem in list(bs)[7:]:
        if isinstance(elem, tuple) and len(elem) == 2:
            disp = _bs_str(elem[0]).lower()
            if disp in ("attachment", "inline"):
                disposition = disp
                disposition_params = _bs_params(elem[1])
                break

    yield part_id, {
        "maintype": maintype,
        "subtype": subtype,
        "params": params,
        "content_id": content_id,
        "encoding": encoding,
        "size": size,
        "disposition": disposition,
        "disposition_params": disposition_params,
    }


def _decode_part_body(raw: bytes, encoding: str) -> bytes:
    """Dekodiert die rohen Bytes eines IMAP-`BODY[<part-id>]`-Fetches anhand
    des Content-Transfer-Encoding aus der BODYSTRUCTURE."""
    enc = (encoding or "7bit").lower()
    if enc == "base64":
        try:
            return base64.b64decode(raw, validate=False)
        except Exception:
            return raw
    if enc in ("quoted-printable", "qp"):
        try:
            return quopri.decodestring(raw)
        except Exception:
            return raw
    return raw


def _is_attachment_leaf(info: dict) -> bool:
    """Spiegelt mime_parser._is_attachment_part: Disposition=attachment ODER
    Filename vorhanden, plus jeder weitere Leaf, der kein Body (text/plain,
    text/html) und keine CID-Inline-Ressource ist (z.B. text/calendar ohne
    Dateiname). Muss exakt mit extract_attachment_meta übereinstimmen, sonst
    zeigt der part_index aus PocketBase auf den falschen Part."""
    if info["disposition"] == "attachment":
        return True
    fn = info["disposition_params"].get("filename") or info["params"].get("name")
    if fn:
        return True
    ctype = f"{info['maintype']}/{info['subtype']}".lower()
    if ctype in ("text/plain", "text/html"):
        return False
    if info["disposition"] == "inline" and info["content_id"]:
        return False
    return True


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
    # Sent-APPEND (nach erfolgreichem SMTP-Versand aufgerufen)
    # ------------------------------------------------------------------
    def append_sent(self, msg_bytes: bytes) -> None:
        """Best-effort: hängt eine gesendete E-Mail in den Sent-Ordner.
        Bei fehlenden Credentials oder fehlendem Sent-Ordner wird nichts
        geworfen, nur geloggt — Aufrufer (smtp_sender.send_email) startet
        diese Methode im Executor und ignoriert das Ergebnis."""
        if not all([self.acc.get("imap_host"), self.acc.get("imap_user"), self.acc.get("imap_pass")]):
            return
        with imap_session(self.acc) as srv:
            sent = find_imap_folder(
                srv,
                [b"\\Sent"],
                ["Sent", "Sent Items", "Sent Messages", "INBOX.Sent"],
            )
            if sent:
                srv.append(
                    sent,
                    msg_bytes,
                    flags=[b"\\Seen"],
                    msg_time=datetime.now(timezone.utc),
                )
                logger.info("E-Mail in Ordner '%s' gespeichert.", sent)
            else:
                logger.warning("Kein Sent-Ordner gefunden — APPEND übersprungen.")

    # ------------------------------------------------------------------
    # Attachments / Inline (B9: gezielt per BODYSTRUCTURE + BODY[<part-id>])
    # ------------------------------------------------------------------
    def fetch_attachment(self, folder: str, imap_uid: int, part_index: int) -> bytes:
        """Lädt einen Anhang. Holt zuerst BODYSTRUCTURE (~1 KB), bestimmt
        die MIME-Part-ID des Nten Anhangs, fetcht dann nur diese Part. Fällt
        auf `BODY[]` zurück, wenn BODYSTRUCTURE fehlt/unbrauchbar ist oder
        der Index außerhalb liegt."""
        with imap_session(self.acc) as srv:
            srv.select_folder(folder, readonly=True)
            meta = srv.fetch([imap_uid], [b"BODYSTRUCTURE"])
            bs = meta.get(imap_uid, {}).get(b"BODYSTRUCTURE")
            if not bs:
                logger.warning("fetch_attachment: keine BODYSTRUCTURE für UID %s — Fallback BODY[]", imap_uid)
                return self._fetch_attachment_full(srv, imap_uid, part_index)

            attachments = [
                (pid, info) for pid, info in _walk_bodystructure(bs)
                if _is_attachment_leaf(info)
            ]
            if part_index < 0 or part_index >= len(attachments):
                logger.warning(
                    "fetch_attachment: part_index %s außerhalb (%d Anhänge laut BODYSTRUCTURE) — Fallback BODY[]",
                    part_index, len(attachments),
                )
                return self._fetch_attachment_full(srv, imap_uid, part_index)

            part_id, info = attachments[part_index]
            body_key = f"BODY[{part_id}]".encode()
            data = srv.fetch([imap_uid], [body_key])
            raw = data.get(imap_uid, {}).get(body_key) or b""
            logger.info(
                "fetch_attachment: UID %s part %s (%d B raw, encoding=%s)",
                imap_uid, part_id, len(raw), info["encoding"],
            )
            return _decode_part_body(raw, info["encoding"])

    def fetch_inline(self, folder: str, imap_uid: int, cid: str) -> tuple[bytes, str]:
        """Lädt ein Inline-Bild per Content-ID gezielt: BODYSTRUCTURE → Part
        mit passender CID finden → nur diese Part fetchen. Fallback auf
        `BODY[]` falls BODYSTRUCTURE keine passende CID liefert."""
        cid_clean = cid.strip("<>")
        with imap_session(self.acc) as srv:
            srv.select_folder(folder, readonly=True)
            meta = srv.fetch([imap_uid], [b"BODYSTRUCTURE"])
            bs = meta.get(imap_uid, {}).get(b"BODYSTRUCTURE")
            if not bs:
                logger.warning("fetch_inline: keine BODYSTRUCTURE für UID %s — Fallback BODY[]", imap_uid)
                return self._fetch_inline_full(srv, imap_uid, cid)

            match = None
            for part_id, info in _walk_bodystructure(bs):
                if info["content_id"] and info["content_id"] == cid_clean:
                    match = (part_id, info)
                    break
            if not match:
                logger.warning("fetch_inline: CID %s nicht in BODYSTRUCTURE — Fallback BODY[]", cid_clean)
                return self._fetch_inline_full(srv, imap_uid, cid)

            part_id, info = match
            body_key = f"BODY[{part_id}]".encode()
            data = srv.fetch([imap_uid], [body_key])
            raw = data.get(imap_uid, {}).get(body_key) or b""
            payload = _decode_part_body(raw, info["encoding"])
            mime_type = (
                f"{info['maintype']}/{info['subtype']}"
                if info["maintype"] else "application/octet-stream"
            )
            logger.info(
                "fetch_inline: UID %s CID %s part %s (%d B raw, %s)",
                imap_uid, cid_clean, part_id, len(raw), mime_type,
            )
            return payload, mime_type

    def fetch_raw(self, folder: str, imap_uid: int) -> bytes:
        """Lädt die komplette Roh-Mail (RFC822-Quelltext: alle Header + MIME-
        Bodies). readonly-Select + BODY.PEEK[] markiert die Mail nicht als
        gelesen. Wird live geholt — der Rohtext wird nicht in PocketBase
        gespeichert."""
        with imap_session(self.acc) as srv:
            srv.select_folder(folder, readonly=True)
            data = srv.fetch([imap_uid], [b"BODY.PEEK[]"])
            rec = data.get(imap_uid, {}) or {}
            raw = rec.get(b"BODY[]") or rec.get(b"BODY.PEEK[]") or b""
            logger.info("fetch_raw: UID %s (%d B)", imap_uid, len(raw))
            return raw

    # Fallback-Helfer (alter BODY[]-Pfad) für die Fälle, in denen
    # BODYSTRUCTURE nicht funktioniert.
    @staticmethod
    def _fetch_attachment_full(srv, imap_uid: int, part_index: int) -> bytes:
        data = srv.fetch([imap_uid], [b"BODY[]"])
        raw = data.get(imap_uid, {}).get(b"BODY[]") or b""
        payload, _, _ = get_attachment_payload(raw, part_index)
        return payload

    @staticmethod
    def _fetch_inline_full(srv, imap_uid: int, cid: str) -> tuple[bytes, str]:
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
