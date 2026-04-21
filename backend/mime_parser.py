import email as _email_stdlib
import logging

import mailparser

logger = logging.getLogger(__name__)


def _decode_part(part) -> str:
    """Dekodiert einen MIME-Teil mit dem korrekten Charset aus dem Content-Type-Header."""
    payload = part.get_payload(decode=True)
    if not payload:
        return ""
    charset = part.get_content_charset() or "utf-8"
    for enc in (charset, "utf-8", "latin-1"):
        try:
            return payload.decode(enc, errors="replace")
        except (LookupError, UnicodeDecodeError):
            continue
    return payload.decode("latin-1", errors="replace")


def _extract_html_stdlib(raw_bytes: bytes) -> str:
    """Extrahiert HTML-Body direkt mit Pythons email-Modul (zuverlässiger Charset-Support)."""
    try:
        msg = _email_stdlib.message_from_bytes(raw_bytes)
    except Exception:
        return ""
    for part in msg.walk():
        if part.get_content_type() == "text/html" and part.get_content_disposition() != "attachment":
            return _decode_part(part)
    return ""


def _extract_plain_stdlib(raw_bytes: bytes) -> str:
    """Extrahiert Plain-Text-Body direkt mit Pythons email-Modul."""
    try:
        msg = _email_stdlib.message_from_bytes(raw_bytes)
    except Exception:
        return ""
    for part in msg.walk():
        if part.get_content_type() == "text/plain" and part.get_content_disposition() != "attachment":
            return _decode_part(part)
    return ""


def parse_email(raw_bytes: bytes) -> dict:
    """
    Parse raw email bytes using mail-parser (Metadaten) +
    stdlib email (Body-Dekodierung mit korrektem Charset).
    Returns a dict with the fields needed for PocketBase.
    """
    try:
        mail = mailparser.parse_from_bytes(raw_bytes)
    except Exception as e:
        logger.error(f"mail-parser failed: {e}")
        return {}

    # Plain text body — stdlib für korrektes Charset-Decoding
    body_plain = _extract_plain_stdlib(raw_bytes)
    if not body_plain:
        plain_parts = mail.text_plain
        body_plain = plain_parts[0] if plain_parts else ""
    body_plain = body_plain[:50000]

    # HTML body — stdlib für korrektes Charset-Decoding (verhindert Emoji-Korruption)
    body_html = _extract_html_stdlib(raw_bytes)
    if not body_html:
        html_parts = mail.text_html
        body_html = html_parts[0] if html_parts else ""
    body_html = body_html[:500000]

    # Snippet — aus Plain oder aus HTML-Text ableiten
    if body_plain:
        snippet = body_plain.replace("\n", " ").replace("\r", "").strip()[:200]
    elif body_html:
        import re as _re
        plain_from_html = _re.sub(r'<[^>]+>', ' ', body_html)
        snippet = ' '.join(plain_from_html.split())[:200]
    else:
        snippet = ""

    # Adressen
    to_emails = [addr[1] for addr in (mail.to or []) if addr[1]]
    cc_emails = [addr[1] for addr in (mail.cc or []) if addr[1]]

    # Absender
    from_list = mail.from_ or []
    from_name = from_list[0][0] if from_list else ""
    from_email = from_list[0][1] if from_list else ""

    # Reply-To (kann von From abweichen, z.B. bei Kontaktformularen)
    reply_to_list = getattr(mail, "reply_to", None) or []
    reply_to = reply_to_list[0][1] if reply_to_list else ""

    return {
        "message_id": (mail.message_id or "").strip(),
        "in_reply_to": (getattr(mail, "in_reply_to", "") or "").strip(),
        "subject": (mail.subject or "").strip(),
        "from_name": from_name,
        "from_email": from_email,
        "reply_to": reply_to,
        "to_emails": to_emails,
        "cc_emails": cc_emails,
        "body_plain": body_plain,
        "body_html": body_html,
        "snippet": snippet,
        "date_sent": mail.date.isoformat() if mail.date else None,
        "has_attachments": bool(mail.attachments),
    }


def extract_attachment_meta(raw_bytes: bytes) -> list[dict]:
    """Extrahiert Anhang-Metadaten (ohne Payload) aus rohen E-Mail-Bytes.
    Gibt Liste von {filename, mime_type, size_bytes, part_index} zurück."""
    try:
        msg = _email_stdlib.message_from_bytes(raw_bytes)
    except Exception:
        return []

    result = []
    index = 0
    for part in msg.walk():
        if part.get_content_maintype() == "multipart":
            continue
        cd = (part.get_content_disposition() or "").lower()
        filename = part.get_filename()
        if cd == "attachment" or filename:
            mime_type = part.get_content_type() or "application/octet-stream"
            payload = part.get_payload(decode=True)
            size_bytes = len(payload) if payload else 0
            result.append({
                "filename": filename or f"anhang_{index + 1}",
                "mime_type": mime_type,
                "size_bytes": size_bytes,
                "part_index": index,
            })
            index += 1
    return result


def get_attachment_payload(raw_bytes: bytes, part_index: int) -> tuple[bytes, str, str]:
    """Gibt (payload_bytes, filename, mime_type) für den Anhang bei part_index zurück."""
    try:
        msg = _email_stdlib.message_from_bytes(raw_bytes)
    except Exception:
        return b"", "anhang", "application/octet-stream"

    index = 0
    for part in msg.walk():
        if part.get_content_maintype() == "multipart":
            continue
        cd = (part.get_content_disposition() or "").lower()
        filename = part.get_filename()
        if cd == "attachment" or filename:
            if index == part_index:
                payload = part.get_payload(decode=True) or b""
                mime_type = part.get_content_type() or "application/octet-stream"
                return payload, filename or f"anhang_{part_index + 1}", mime_type
            index += 1

    return b"", "anhang", "application/octet-stream"


def get_inline_part_by_cid(raw_bytes: bytes, content_id: str) -> tuple[bytes, str]:
    """Gibt (payload_bytes, mime_type) für ein Inline-Part mit der angegebenen Content-ID zurück."""
    try:
        msg = _email_stdlib.message_from_bytes(raw_bytes)
    except Exception:
        return b"", "application/octet-stream"
    cid_clean = content_id.strip("<>")
    for part in msg.walk():
        if part.get_content_maintype() == "multipart":
            continue
        part_cid = (part.get("Content-ID") or "").strip("<>")
        if part_cid == cid_clean:
            payload = part.get_payload(decode=True) or b""
            mime_type = part.get_content_type() or "application/octet-stream"
            return payload, mime_type
    return b"", "application/octet-stream"


def find_plain_text_part(bodystructure: dict) -> str:
    """
    Rekursiv die MIME-Teil-ID des ersten text/plain-Teils finden.
    Gibt '1' zurück als sicheren Fallback.
    """
    return _search_part(bodystructure, "1") or "1"


def _search_part(structure, part_id: str) -> str | None:
    if not structure:
        return None
    if isinstance(structure, (list, tuple)):
        # Multipart
        for i, part in enumerate(structure):
            if isinstance(part, (list, tuple)):
                result = _search_part(part, f"{part_id}.{i + 1}")
                if result:
                    return result
        return None
    # Einzelner Teil
    if isinstance(structure, dict):
        mime_type = structure.get("type", "").lower()
        subtype = structure.get("subtype", "").lower()
        if mime_type == "text" and subtype == "plain":
            return part_id
    return None
