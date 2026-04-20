"""SMTP-Versand + IMAP APPEND in Sent-Ordner."""
import asyncio
import email.utils
import logging
import smtplib
import ssl
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import pb_client
from imap_utils import find_imap_folder

logger = logging.getLogger(__name__)


async def send_email(
    smtp_server_id: str,
    from_account_id: str,
    to: str,
    subject: str,
    body: str,
    body_html: str = "",
    quote: str = "",
    cc: str = "",
    attachments: list[dict] | None = None,
) -> str:
    """
    Sendet eine E-Mail via SMTP und hängt sie per IMAP APPEND in den Sent-Ordner.
    Gibt die Message-ID zurück.
    """
    smtp_cfg = await pb_client.pb_get(
        f"/api/collections/smtp_servers/records/{smtp_server_id}"
    )
    acc = await pb_client.pb_get(
        f"/api/collections/accounts/records/{from_account_id}"
    )

    from_email = acc.get("from_email", "")
    from_name = acc.get("from_name", "")

    # E-Mail aufbauen
    msg = MIMEMultipart("mixed")
    full_body = body
    if quote:
        full_body += "\n\n" + quote

    if body_html:
        # HTML-Zitat anhängen
        full_html = body_html
        if quote:
            q_escaped = (quote
                         .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                         .replace("\n", "<br>"))
            full_html += (
                '<br><br><blockquote style="border-left:3px solid #ccc;'
                'margin-left:0;padding-left:12px;color:#555">'
                + q_escaped + "</blockquote>"
            )
        alt = MIMEMultipart("alternative")
        alt.attach(MIMEText(full_body, "plain", "utf-8"))
        alt.attach(MIMEText(full_html, "html", "utf-8"))
        msg.attach(alt)
    else:
        msg.attach(MIMEText(full_body, "plain", "utf-8"))

    # Anhänge einhängen
    if attachments:
        from email.mime.base import MIMEBase
        from email import encoders as _encoders
        for att in attachments:
            main_type, _, sub_type = (att.get("content_type") or "application/octet-stream").partition("/")
            mime_part = MIMEBase(main_type or "application", sub_type or "octet-stream")
            mime_part.set_payload(att["data"])
            _encoders.encode_base64(mime_part)
            mime_part.add_header(
                "Content-Disposition", "attachment",
                filename=att.get("filename", "anhang"),
            )
            msg.attach(mime_part)

    msg["From"] = (
        email.utils.formataddr((from_name, from_email)) if from_name else from_email
    )
    msg["To"] = to
    if cc:
        msg["Cc"] = cc
    msg["Subject"] = subject
    msg["Date"] = email.utils.formatdate(localtime=True)
    msg["Message-ID"] = email.utils.make_msgid()

    message_id = msg["Message-ID"]
    msg_bytes = msg.as_bytes()

    # Alle Empfänger (To + CC) für SMTP-Übergabe zusammenstellen
    all_recipients = to
    if cc:
        all_recipients = f"{to},{cc}" if to else cc

    # SMTP-Versand (blockierend → Thread-Pool)
    logger.info("SMTP-Versand: von=%s an=%s server=%s:%s",
                from_email, all_recipients, smtp_cfg.get("host"), smtp_cfg.get("port"))
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(
        None, _send_smtp, smtp_cfg, from_email, all_recipients, msg_bytes
    )
    logger.info("SMTP-Versand erfolgreich: message_id=%s", message_id)

    # IMAP APPEND in Sent-Ordner (best-effort, im Hintergrund)
    loop.run_in_executor(None, _imap_append_sent, acc, msg_bytes)

    return message_id


def _send_smtp(
    smtp_cfg: dict, from_addr: str, to_addr: str, msg_bytes: bytes
) -> None:
    """Blockierende SMTP-Verbindung — wird im Thread-Pool ausgeführt."""
    host = smtp_cfg["host"]
    port = int(smtp_cfg.get("port") or 587)
    user = smtp_cfg.get("user", "")
    password = smtp_cfg.get("password", "")
    use_tls = smtp_cfg.get("use_tls", False)
    use_starttls = smtp_cfg.get("use_starttls", True)

    to_list = [a.strip() for a in to_addr.split(",") if a.strip()]

    if use_tls:
        ctx = ssl.create_default_context()
        with smtplib.SMTP_SSL(host, port, context=ctx, timeout=30) as srv:
            if user:
                srv.login(user, password)
            refused = srv.sendmail(from_addr, to_list, msg_bytes)
            if refused:
                logger.warning("SMTP: abgelehnte Empfänger: %s", refused)
            else:
                logger.info("SMTP: alle Empfänger akzeptiert")
    else:
        with smtplib.SMTP(host, port, timeout=30) as srv:
            srv.ehlo()
            if use_starttls:
                srv.starttls()
                srv.ehlo()
            if user:
                srv.login(user, password)
            refused = srv.sendmail(from_addr, to_list, msg_bytes)
            if refused:
                logger.warning("SMTP: abgelehnte Empfänger: %s", refused)
            else:
                logger.info("SMTP: alle Empfänger akzeptiert")


def _imap_append_sent(acc: dict, msg_bytes: bytes) -> None:
    """Hängt die gesendete E-Mail per IMAP APPEND in den Sent-Ordner."""
    from imapclient import IMAPClient

    host = acc.get("imap_host")
    port = int(acc.get("imap_port") or 993)
    user = acc.get("imap_user")
    password = acc.get("imap_pass")

    if not all([host, user, password]):
        return

    with IMAPClient(host, port=port, ssl=True) as srv:
        srv.login(user, password)
        sent = find_imap_folder(srv, [b"\\Sent"], ["Sent", "Sent Items", "Sent Messages", "INBOX.Sent"])
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


