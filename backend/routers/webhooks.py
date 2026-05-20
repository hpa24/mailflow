"""Webhook-Endpoints — externer Send-Endpoint mit eigenem Key + CRUD-Verwaltung.

Ausgegliedert aus main.py im Rahmen von C1 (Router-Split).
"""
from __future__ import annotations

import logging
import re
import secrets as _secrets

from fastapi import APIRouter, HTTPException, Request

import pb_client
from rate_limit import limiter
from smtp_sender import send_email as smtp_send_email

logger = logging.getLogger(__name__)

router = APIRouter()

_WEBHOOK_SLUG_RE = re.compile(r"^[a-z0-9-]+$")


async def _webhook_by_slug(slug: str) -> dict | None:
    data = await pb_client.pb_get(
        "/api/collections/webhooks/records",
        params={"filter": f'slug={pb_client.pb_quote(slug)}', "perPage": 1},
    )
    items = data.get("items") or []
    return items[0] if items else None


async def _webhook_log(webhook_id: str, ip: str, status: str,
                       to: str, subject: str,
                       message_id: str = "", error: str = "") -> None:
    try:
        await pb_client.pb_post(
            "/api/collections/webhook_logs/records",
            {
                "webhook": webhook_id,
                "ip": ip[:64],
                "status": status,
                "to": to[:500],
                "subject": subject[:500],
                "message_id": message_id[:200],
                "error": error[:2000],
            },
        )
    except Exception as exc:
        logger.error("webhook_log konnte nicht geschrieben werden: %s", exc)


@router.post("/webhooks/{slug}/send")
@limiter.limit("30/minute")
async def webhook_send(slug: str, request: Request, data: dict):
    """Externer Mail-Versand via Webhook.

    Auth: Header ``X-Webhook-Key`` mit dem in der Webhook-Konfig gespeicherten
    ``api_key``. Diese Route ist von der globalen Auth-Middleware ausgenommen —
    jeder Webhook hat seinen eigenen Schlüssel.
    """
    if not _WEBHOOK_SLUG_RE.match(slug or ""):
        raise HTTPException(status_code=400, detail="Ungültiger Slug")

    wh = await _webhook_by_slug(slug)
    if wh is None or not wh.get("is_active"):
        # Bewusst gleicher Fehler wie 401 — verrät nicht ob Slug existiert
        raise HTTPException(status_code=401, detail="Unauthorized")

    provided_key = request.headers.get("X-Webhook-Key", "")
    if not provided_key or not _secrets.compare_digest(provided_key, wh.get("api_key") or ""):
        raise HTTPException(status_code=401, detail="Unauthorized")

    ip = request.client.host if request.client else ""

    payload_to = (data.get("to") or "").strip()
    to = payload_to if (wh.get("allow_to_override") and payload_to) else (wh.get("default_to") or "").strip()
    if not to:
        await _webhook_log(wh["id"], ip, "error", "", "", error="Empfänger fehlt")
        raise HTTPException(status_code=400, detail="Empfänger fehlt")

    reply_to = (data.get("reply_to") or "").strip() if wh.get("allow_reply_to") else ""
    cc = (data.get("cc") or "").strip() if wh.get("allow_cc") else ""

    subject = (data.get("subject") or "").strip()
    body = data.get("body") or ""
    body_html = data.get("body_html") or ""

    if not subject:
        await _webhook_log(wh["id"], ip, "error", to, "", error="Betreff fehlt")
        raise HTTPException(status_code=400, detail="Betreff fehlt")
    if not body and not body_html:
        await _webhook_log(wh["id"], ip, "error", to, subject, error="Body fehlt")
        raise HTTPException(status_code=400, detail="Body fehlt")

    try:
        message_id = await smtp_send_email(
            smtp_server_id=wh["smtp_server"],
            from_account_id=wh["from_account"],
            to=to,
            cc=cc,
            subject=subject,
            body=body,
            body_html=body_html,
            reply_to=reply_to,
            from_name_override=(wh.get("from_name_override") or "").strip(),
        )
    except Exception as exc:
        logger.error("Webhook-Versand fehlgeschlagen (slug=%s): %s", slug, exc)
        await _webhook_log(wh["id"], ip, "error", to, subject, error=str(exc))
        raise HTTPException(status_code=502, detail=f"SMTP-Fehler: {exc}")

    await _webhook_log(wh["id"], ip, "success", to, subject, message_id=message_id)
    logger.info("Webhook-Versand OK: slug=%s to=%s message_id=%s", slug, to, message_id)
    return {"status": "sent", "message_id": message_id}


@router.get("/webhooks")
async def webhooks_list():
    data = await pb_client.pb_get(
        "/api/collections/webhooks/records",
        params={"perPage": 200, "sort": "-created"},
    )
    return data.get("items", [])


@router.post("/webhooks")
async def webhooks_create(data: dict):
    name = (data.get("name") or "").strip()
    slug = (data.get("slug") or "").strip().lower()
    smtp_server = (data.get("smtp_server") or "").strip()
    from_account = (data.get("from_account") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name fehlt")
    if not _WEBHOOK_SLUG_RE.match(slug):
        raise HTTPException(status_code=400, detail="slug ungültig (nur a-z, 0-9, -)")
    if not smtp_server or not from_account:
        raise HTTPException(status_code=400, detail="smtp_server und from_account erforderlich")

    record = {
        "name": name,
        "slug": slug,
        "smtp_server": smtp_server,
        "from_account": from_account,
        "default_to": (data.get("default_to") or "").strip(),
        "from_name_override": (data.get("from_name_override") or "").strip(),
        "allow_to_override": bool(data.get("allow_to_override", True)),
        "allow_reply_to": bool(data.get("allow_reply_to", True)),
        "allow_cc": bool(data.get("allow_cc", False)),
        "is_active": bool(data.get("is_active", True)),
        "api_key": "whk_" + _secrets.token_urlsafe(32),
    }
    return await pb_client.pb_post("/api/collections/webhooks/records", record)


@router.get("/webhooks/{webhook_id}/logs")
async def webhooks_logs(webhook_id: str, limit: int = 100):
    limit = max(1, min(int(limit), 500))
    data = await pb_client.pb_get(
        "/api/collections/webhook_logs/records",
        params={
            "filter": f'webhook={pb_client.pb_quote(webhook_id)}',
            "perPage": limit,
            "sort": "-created",
        },
    )
    return data.get("items", [])


@router.patch("/webhooks/{webhook_id}")
async def webhooks_update(webhook_id: str, data: dict):
    allowed = {
        "name", "slug", "smtp_server", "from_account", "default_to",
        "from_name_override",
        "allow_to_override", "allow_reply_to", "allow_cc", "is_active",
    }
    patch = {k: v for k, v in data.items() if k in allowed}
    if "slug" in patch:
        s = (patch["slug"] or "").strip().lower()
        if not _WEBHOOK_SLUG_RE.match(s):
            raise HTTPException(status_code=400, detail="slug ungültig")
        patch["slug"] = s
    if data.get("rotate_api_key"):
        patch["api_key"] = "whk_" + _secrets.token_urlsafe(32)
    return await pb_client.pb_patch(f"/api/collections/webhooks/records/{webhook_id}", patch)


@router.delete("/webhooks/{webhook_id}")
async def webhooks_delete(webhook_id: str):
    await pb_client.pb_delete(f"/api/collections/webhooks/records/{webhook_id}")
    return {"status": "deleted"}
