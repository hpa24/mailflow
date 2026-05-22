"""Bulk-Sends-Endpoints — Aussendungs-Historie (Liste/Detail/Löschen).

Ausgegliedert aus main.py im Rahmen von C1 Phase 2 (Router-Split).

Auth: PB-User-Token via `pb_user_auth.get_user_token`-Dependency.

Der eigentliche Bulk-Worker-Loop, Recipient-Result-Tracking,
`_bulk_send_locks`, `_bulk_attachments_by_id` und die Send-Pipeline
bleiben in `main.py` — die hängen am `lifespan` und am SMTP-Pfad und
gehören nicht zu den User-CRUD-Endpoints.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends

import pb_client
import pb_user_auth

router = APIRouter()


@router.get("/bulk-sends")
async def bulk_sends_list(limit: int = 200, token: str = Depends(pb_user_auth.get_user_token)):
    """Liste der Aussendungen, neueste zuerst. Liefert Metadaten ohne
    `recipients`-Array (Performance) — Detail via GET /bulk-sends/{id}."""
    data = await pb_client.pb_get_as(
        token,
        "/api/collections/bulk_sends/records",
        params={
            "perPage": max(1, min(500, limit)),
            "sort": "-sent_at",
            "fields": "id,subject,from_account,from_account_email,smtp_server,sent_at,delay_seconds,total_count,sent_count,error_count,bounced_count,created,updated",
        },
    )
    return data.get("items", [])


@router.get("/bulk-sends/{bulk_id}")
async def bulk_sends_get(bulk_id: str, token: str = Depends(pb_user_auth.get_user_token)):
    """Detail einer Aussendung inkl. recipients-Array."""
    return await pb_client.pb_get_as(token, f"/api/collections/bulk_sends/records/{bulk_id}")


@router.delete("/bulk-sends/{bulk_id}")
async def bulk_sends_delete(bulk_id: str, token: str = Depends(pb_user_auth.get_user_token)):
    await pb_client.pb_delete_as(token, f"/api/collections/bulk_sends/records/{bulk_id}")
    return {"status": "deleted"}
