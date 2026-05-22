"""Webhook-Endpoints — externer Send-Endpoint mit eigenem Key + CRUD-Verwaltung.

Ausgegliedert aus main.py im Rahmen von C1 (Router-Split).
"""
from __future__ import annotations

import logging
import re
import secrets as _secrets

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, field_validator

import pb_client
import pb_user_auth
from rate_limit import limiter
from smtp_sender import send_email as smtp_send_email

logger = logging.getLogger(__name__)

router = APIRouter()

_WEBHOOK_SLUG_RE = re.compile(r"^[a-z0-9-]+$")


async def _webhook_by_slug(slug: str) -> dict | None:
    # A11: bewusste Admin-Nutzung — webhook_send wird per X-Webhook-Key authentifiziert,
    # ohne PB-User-Token. Slug-Lookup muss daher den Admin-Token verwenden.
    data = await pb_client.pb_get(
        "/api/collections/webhooks/records",
        params={"filter": f'slug={pb_client.pb_quote(slug)}', "perPage": 1},
    )
    items = data.get("items") or []
    return items[0] if items else None


async def _webhook_log(webhook_id: str, ip: str, status: str,
                       to: str, subject: str,
                       message_id: str = "", error: str = "") -> None:
    # A11: bewusste Admin-Nutzung — Audit-Eintrag nach extern getriggertem webhook_send.
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


class WebhookSendRequest(BaseModel):
    to: str | None = None
    subject: str | None = None
    body: str | None = None
    body_html: str | None = None
    reply_to: str | None = None
    cc: str | None = None


@router.post("/webhooks/{slug}/send")
@limiter.limit("30/minute")
async def webhook_send(slug: str, request: Request, req: WebhookSendRequest):
    """Externer Mail-Versand via Webhook.

    Auth: Header ``X-Webhook-Key`` mit dem in der Webhook-Konfig gespeicherten
    ``api_key``. Diese Route ist von der globalen Auth-Middleware ausgenommen —
    jeder Webhook hat seinen eigenen Schlüssel.

    Validierung der Pflichtfelder (Empfänger/Betreff/Body) bleibt bewusst im
    Endpoint-Body statt im Pydantic-Modell, damit `_webhook_log` bei
    Validierungsfehlern weiterhin einen Audit-Eintrag schreiben kann.
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

    payload_to = (req.to or "").strip()
    to = payload_to if (wh.get("allow_to_override") and payload_to) else (wh.get("default_to") or "").strip()
    if not to:
        await _webhook_log(wh["id"], ip, "error", "", "", error="Empfänger fehlt")
        raise HTTPException(status_code=400, detail="Empfänger fehlt")

    reply_to = (req.reply_to or "").strip() if wh.get("allow_reply_to") else ""
    cc = (req.cc or "").strip() if wh.get("allow_cc") else ""

    subject = (req.subject or "").strip()
    body = req.body or ""
    body_html = req.body_html or ""

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
async def webhooks_list(token: str = Depends(pb_user_auth.get_user_token)):
    # S1 (2026-05-23): webhooks-Rules sind dicht (`api_key`-Feld). Admin-Token,
    # Authz via Depends(get_user_token).
    data = await pb_client.pb_get(
        "/api/collections/webhooks/records",
        params={"perPage": 200, "sort": "-created"},
    )
    return data.get("items", [])


class WebhookCreateRequest(BaseModel):
    name: str
    slug: str
    smtp_server: str
    from_account: str
    default_to: str = ""
    from_name_override: str = ""
    allow_to_override: bool = True
    allow_reply_to: bool = True
    allow_cc: bool = False
    is_active: bool = True

    @field_validator("name")
    @classmethod
    def _name_nonempty(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("name fehlt")
        return v

    @field_validator("smtp_server", "from_account")
    @classmethod
    def _id_nonempty(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("smtp_server und from_account erforderlich")
        return v

    @field_validator("default_to", "from_name_override")
    @classmethod
    def _strip_optional(cls, v: str | None) -> str:
        return (v or "").strip()

    @field_validator("slug")
    @classmethod
    def _validate_slug(cls, v: str) -> str:
        s = (v or "").strip().lower()
        if not _WEBHOOK_SLUG_RE.match(s):
            raise ValueError("slug ungültig (nur a-z, 0-9, -)")
        return s


@router.post("/webhooks")
async def webhooks_create(req: WebhookCreateRequest,
                          token: str = Depends(pb_user_auth.get_user_token)):
    record = {
        "name": req.name,
        "slug": req.slug,
        "smtp_server": req.smtp_server,
        "from_account": req.from_account,
        "default_to": req.default_to,
        "from_name_override": req.from_name_override,
        "allow_to_override": req.allow_to_override,
        "allow_reply_to": req.allow_reply_to,
        "allow_cc": req.allow_cc,
        "is_active": req.is_active,
        "api_key": "whk_" + _secrets.token_urlsafe(32),
    }
    return await pb_client.pb_post("/api/collections/webhooks/records", record)


@router.get("/webhooks/{webhook_id}/logs")
async def webhooks_logs(webhook_id: str, limit: int = 100,
                        token: str = Depends(pb_user_auth.get_user_token)):
    limit = max(1, min(int(limit), 500))
    data = await pb_client.pb_get_as(
        token,
        "/api/collections/webhook_logs/records",
        params={
            "filter": f'webhook={pb_client.pb_quote(webhook_id)}',
            "perPage": limit,
            "sort": "-created",
        },
    )
    return data.get("items", [])


class WebhookUpdateRequest(BaseModel):
    name: str | None = None
    slug: str | None = None
    smtp_server: str | None = None
    from_account: str | None = None
    default_to: str | None = None
    from_name_override: str | None = None
    allow_to_override: bool | None = None
    allow_reply_to: bool | None = None
    allow_cc: bool | None = None
    is_active: bool | None = None
    rotate_api_key: bool | None = None

    @field_validator("slug")
    @classmethod
    def _validate_slug(cls, v: str | None) -> str | None:
        if v is None:
            return None
        s = v.strip().lower()
        if not _WEBHOOK_SLUG_RE.match(s):
            raise ValueError("slug ungültig")
        return s


@router.patch("/webhooks/{webhook_id}")
async def webhooks_update(webhook_id: str, req: WebhookUpdateRequest,
                          token: str = Depends(pb_user_auth.get_user_token)):
    # rotate_api_key ist ein Control-Flag, kein PB-Feld — separat behandeln.
    patch = req.model_dump(exclude_unset=True, exclude={"rotate_api_key"})
    if req.rotate_api_key:
        patch["api_key"] = "whk_" + _secrets.token_urlsafe(32)
    return await pb_client.pb_patch(f"/api/collections/webhooks/records/{webhook_id}", patch)


@router.delete("/webhooks/{webhook_id}")
async def webhooks_delete(webhook_id: str, token: str = Depends(pb_user_auth.get_user_token)):
    await pb_client.pb_delete(f"/api/collections/webhooks/records/{webhook_id}")
    return {"status": "deleted"}
