"""DSN (Delivery Status Notification) — Erkennen und Parsen von Bounce-Mails.

Wird vom IMAP-Sync verwendet (Phase 3b):
- ``is_bounce(parsed, raw_bytes)`` — Heuristik (From-Adresse, Subject, Content-Type)
- ``parse_dsn(raw_bytes)`` — extrahiert Message-ID, Final-Recipient, Diagnostic, Status
- ``is_permanent_failure(status)`` — 5.x.x = permanent (Kontakt flaggen), 4.x.x = transient
"""

import email
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

_DSN_FROM_RE = re.compile(
    r"^(mailer-daemon|postmaster|noreply|no-reply|mailerdaemon)@",
    re.IGNORECASE,
)
_DSN_SUBJECT_RE = re.compile(
    r"^(Undelivered|Mail Delivery|Returned|Delivery Status|Failure Notice|"
    r"Zustell|Unzustellbar|Nicht zustellbar)",
    re.IGNORECASE,
)
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}")
_MID_HEADER_RE = re.compile(r"Message-ID:\s*<([^>]+)>", re.IGNORECASE)
_STATUS_RE = re.compile(r"\b([2-5]\.\d{1,3}\.\d{1,3})\b")


def is_bounce(parsed: dict, raw_bytes: bytes) -> bool:
    """True, wenn die Mail mit hoher Wahrscheinlichkeit eine DSN/Bounce-Nachricht ist."""
    from_email = (parsed.get("from_email") or "").lower()
    if _DSN_FROM_RE.search(from_email):
        return True
    subject = parsed.get("subject") or ""
    if _DSN_SUBJECT_RE.match(subject.strip()):
        return True
    try:
        msg = email.message_from_bytes(raw_bytes)
        ctype = (msg.get_content_type() or "").lower()
        if ctype == "multipart/report":
            return True
    except Exception:
        pass
    return False


def parse_dsn(raw_bytes: bytes) -> dict[str, Any]:
    """Extrahiert Bounce-Metadaten aus einer DSN-Mail.

    Returns:
        {message_id, failed_recipient, diagnostic, status} — alle optional.
        ``message_id`` ist die *Original*-Message-ID der gebouncten Mail
        (ohne ``<>``), ``failed_recipient`` ist die fehlgeschlagene Empfänger-
        Adresse (lowercased), ``diagnostic`` ein Klartext-Fehlertext (max 500),
        ``status`` ein SMTP-Statuscode ``N.N.N`` (z.B. ``5.1.1``).
    """
    result: dict[str, Any] = {
        "message_id": None,
        "failed_recipient": None,
        "diagnostic": None,
        "status": None,
    }
    try:
        msg = email.message_from_bytes(raw_bytes)
    except Exception as exc:
        logger.warning("DSN-Parse: message_from_bytes fehlgeschlagen: %s", exc)
        return result

    xfr = msg.get("X-Failed-Recipients") or ""
    if xfr:
        m = _EMAIL_RE.search(xfr)
        if m:
            result["failed_recipient"] = m.group(0).lower()

    for part in msg.walk():
        ctype = (part.get_content_type() or "").lower()

        if ctype == "message/delivery-status":
            payload = part.get_payload()
            if isinstance(payload, list):
                for sub in payload:
                    if not hasattr(sub, "get"):
                        continue
                    if not result["failed_recipient"]:
                        fr = sub.get("Final-Recipient") or sub.get("Original-Recipient") or ""
                        m = _EMAIL_RE.search(fr)
                        if m:
                            result["failed_recipient"] = m.group(0).lower()
                    if not result["status"]:
                        st = sub.get("Status") or ""
                        m = _STATUS_RE.search(st)
                        if m:
                            result["status"] = m.group(1)
                    if not result["diagnostic"]:
                        dc = sub.get("Diagnostic-Code") or ""
                        if dc:
                            result["diagnostic"] = dc.strip()[:500]

        elif ctype in ("message/rfc822", "text/rfc822-headers"):
            payload = part.get_payload()
            inner_text: str | None = None
            if isinstance(payload, list) and payload:
                inner = payload[0]
                if hasattr(inner, "get") and not result["message_id"]:
                    mid = (inner.get("Message-ID") or inner.get("Original-Message-ID") or "")
                    mid = mid.strip().strip("<>")
                    if mid:
                        result["message_id"] = mid
            elif isinstance(payload, str):
                inner_text = payload
            elif isinstance(payload, bytes):
                try:
                    inner_text = payload.decode("utf-8", errors="replace")
                except Exception:
                    inner_text = None
            if inner_text and not result["message_id"]:
                m = _MID_HEADER_RE.search(inner_text)
                if m:
                    result["message_id"] = m.group(1).strip()

    # Fallbacks: Plaintext-Body scannen
    if not result["message_id"] or not result["diagnostic"]:
        plain = _extract_plain_text(msg)
        if plain:
            if not result["message_id"]:
                m = _MID_HEADER_RE.search(plain)
                if m:
                    result["message_id"] = m.group(1).strip()
            if not result["diagnostic"]:
                lines = [ln.strip() for ln in plain.splitlines() if ln.strip()]
                if lines:
                    result["diagnostic"] = " ".join(lines[:5])[:500]

    return result


def is_permanent_failure(status: str | None) -> bool:
    """SMTP-Statuscode N.N.N — 5.x.x = permanent (hard bounce), 4.x.x = transient.

    Ohne Statuscode wird konservativ False zurückgegeben (kein Kontakt-Flag).
    """
    return bool(status and status.startswith("5"))


def _extract_plain_text(msg: email.message.Message) -> str:
    """Robuster Plaintext-Extractor — auch wenn die DSN das multipart/report-Format
    nicht hat oder die Top-Level-Mail einen Plaintext-Teil hat."""
    try:
        for part in msg.walk():
            if part.get_content_maintype() == "multipart":
                continue
            if (part.get_content_type() or "").lower() == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    return payload.decode(charset, errors="replace")
    except Exception:
        return ""
    return ""
