"""Contacts-Endpoints — Kontakte, Kontakt-Gruppen, Bounce-Verwaltung, Import.

Ausgegliedert aus main.py im Rahmen von C1 Phase 2 (Router-Split).

Auth:
- User-Endpoints (CRUD, search, bounced, clear-bounce): PB-User-Token via
  `pb_user_auth.get_user_token`-Dependency, respektiert PB-Rules.
- `POST /contacts/import`: bewusste A11-Ausnahme. Wird sowohl extern von
  FileMaker/Xano via `X-Import-Key` (geprüft in Middleware) als auch
  intern aufgerufen. Nutzt deshalb den Admin-Token (`pb_client.pb_*`).
"""
from __future__ import annotations

import logging
import re

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, field_validator

import pb_client
import pb_user_auth
from rate_limit import limiter

logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Kontakt-Suche (für Compose-Autocomplete)
# ---------------------------------------------------------------------------


@router.get("/contacts/search")
async def search_contacts(q: str = "", limit: int = 8, token: str = Depends(pb_user_auth.get_user_token)):
    """Kontaktsuche nach Name oder E-Mail für Autocomplete."""
    if not q or len(q.strip()) < 1:
        return {"items": []}
    qq = pb_client.pb_quote(q.strip())
    data = await pb_client.pb_get_as(token, "/api/collections/contacts/records", params={
        "filter": f'email ~ {qq} || name ~ {qq}',
        "sort": "-email_count",
        "perPage": limit,
        "fields": "id,email,name,email_count",
    })
    return {"items": data.get("items", [])}


# ---------------------------------------------------------------------------
# Kontakt-Gruppen
# ---------------------------------------------------------------------------

_GROUP_NAME_RE = re.compile(r"^[a-z0-9_\-]{1,60}$")


@router.get("/contact-groups")
async def contact_groups_list(token: str = Depends(pb_user_auth.get_user_token)):
    data = await pb_client.pb_get_as(
        token,
        "/api/collections/contact_groups/records",
        params={"perPage": 500, "sort": "name"},
    )
    return data.get("items", [])


def _normalize_group_name(v: str | None) -> str | None:
    if v is None:
        return None
    v = v.strip().lower()
    if not _GROUP_NAME_RE.match(v):
        raise ValueError("name ungültig (1–60 Zeichen, nur a-z, 0-9, _, -)")
    return v


class ContactGroupCreateRequest(BaseModel):
    name: str
    description: str = ""

    @field_validator("name")
    @classmethod
    def normalize_name(cls, v: str) -> str:
        return _normalize_group_name(v) or ""

    @field_validator("description")
    @classmethod
    def strip_description(cls, v: str) -> str:
        return (v or "").strip()


class ContactGroupUpdateRequest(BaseModel):
    name: str | None = None
    description: str | None = None

    @field_validator("name")
    @classmethod
    def normalize_name(cls, v: str | None) -> str | None:
        return _normalize_group_name(v)


@router.post("/contact-groups")
async def contact_groups_create(req: ContactGroupCreateRequest, token: str = Depends(pb_user_auth.get_user_token)):
    record = {"name": req.name, "description": req.description}
    try:
        return await pb_client.pb_post_as(token, "/api/collections/contact_groups/records", record)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 400 and "name" in exc.response.text:
            raise HTTPException(status_code=409, detail=f"Gruppe '{req.name}' existiert bereits")
        raise


@router.patch("/contact-groups/{group_id}")
async def contact_groups_update(group_id: str, req: ContactGroupUpdateRequest, token: str = Depends(pb_user_auth.get_user_token)):
    patch = req.model_dump(exclude_unset=True)
    if "description" in patch:
        patch["description"] = (patch["description"] or "").strip()
    if not patch:
        raise HTTPException(status_code=400, detail="nichts zu ändern")
    try:
        return await pb_client.pb_patch_as(token, f"/api/collections/contact_groups/records/{group_id}", patch)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 400 and "name" in exc.response.text:
            raise HTTPException(status_code=409, detail="Gruppe mit diesem Namen existiert bereits")
        raise


@router.delete("/contact-groups/{group_id}")
async def contact_groups_delete(group_id: str, token: str = Depends(pb_user_auth.get_user_token)):
    # PocketBase: cascadeDelete=False auf contacts.groups → Kontakte bleiben, Relation wird gelöscht
    await pb_client.pb_delete_as(token, f"/api/collections/contact_groups/records/{group_id}")
    return {"status": "deleted"}


@router.get("/contact-groups/{group_id}/members")
async def contact_groups_members(group_id: str, token: str = Depends(pb_user_auth.get_user_token)):
    data = await pb_client.pb_get_as(
        token,
        "/api/collections/contacts/records",
        params={"filter": f'groups~{pb_client.pb_quote(group_id)}', "perPage": 1000, "sort": "name"},
    )
    return data.get("items", [])


# ---------------------------------------------------------------------------
# Bounce-Verwaltung
# ---------------------------------------------------------------------------


@router.get("/contacts/bounced")
async def list_bounced_contacts(token: str = Depends(pb_user_auth.get_user_token)):
    """Phase 3b: alle Kontakte mit bounced=true, sortiert nach bounced_at desc."""
    data = await pb_client.pb_get_as(
        token,
        "/api/collections/contacts/records",
        params={
            "filter": "bounced=true",
            "perPage": 500,
            "sort": "-bounced_at",
            "fields": "id,email,name,bounced,bounced_at,bounced_reason",
        },
    )
    return data.get("items", [])


@router.post("/contacts/{contact_id}/clear-bounce")
async def clear_contact_bounce(contact_id: str, token: str = Depends(pb_user_auth.get_user_token)):
    """Phase 3b: setzt bounced=false, bounced_at='', bounced_reason='' — manuelles
    Reset nach falsch geflagger Adresse (z.B. temporäres Mailbox-voll wurde fälschlich
    als permanent geparsed)."""
    await pb_client.pb_patch_as(
        token,
        f"/api/collections/contacts/records/{contact_id}",
        {"bounced": False, "bounced_at": "", "bounced_reason": ""},
    )
    return {"ok": True}


# ---------------------------------------------------------------------------
# Kontakt-Import
# ---------------------------------------------------------------------------
# Format pro Zeile: email,name,gruppen
#   - email:    erforderlich
#   - name:     optional, leerer Name = bestehenden Wert nicht überschreiben
#   - gruppen:  optional, mit ; getrennt; mehrfache Zeilen pro email werden gemerged
#
# Modes:
#   add    (default): Kontakt anlegen oder aktualisieren, Gruppen additiv,
#                     name überschreiben wenn nicht leer, unbekannte Gruppen
#                     werden automatisch angelegt
#   remove: Kontakt-Gruppen-Zuordnungen entfernen; Kontakt + andere Gruppen
#           bleiben unverändert; unbekannte Email = "not_found"

_IMPORT_EMAIL_RE = re.compile(r'^[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}$')


def _norm_group_name(raw: str) -> str | None:
    """Lowercase + Whitespace zu _ → fertiger Gruppen-Name. None wenn ungültig."""
    if not raw:
        return None
    name = re.sub(r'\s+', '_', raw.strip().lower())
    if _GROUP_NAME_RE.match(name):
        return name
    return None


def _parse_import_line(line: str, lineno: int) -> tuple | None:
    """Returns (email, name, [group_names], invalid_reason)."""
    parts = [p.strip() for p in line.split(',', 2)]
    while len(parts) < 3:
        parts.append('')
    email_raw, name, groups_raw = parts
    email = email_raw.strip().lower()
    if not email:
        return (None, None, None, "Email leer")
    if not _IMPORT_EMAIL_RE.match(email):
        return (None, None, None, f"Email ungültig: {email_raw}")
    groups = []
    invalid_groups = []
    if groups_raw:
        for g in groups_raw.split(';'):
            normalized = _norm_group_name(g)
            if normalized:
                groups.append(normalized)
            elif g.strip():
                invalid_groups.append(g.strip())
    invalid_reason = None
    if invalid_groups:
        invalid_reason = f"Gruppen-Namen ungültig: {', '.join(invalid_groups)}"
    return (email, name, groups, invalid_reason)


@router.post("/contacts/import")
@limiter.limit("30/minute")
async def contacts_import(request: Request, data: dict):
    """Importiert Kontakte + Gruppen-Zuordnungen aus einer Multiline-Liste.

    Auth: X-Import-Key (extern, FileMaker/Xano) ODER PB-User-Bearer.
    Nutzt absichtlich den Admin-Token (`pb_client.pb_*`), weil der externe
    Import-Pfad keinen User-Token mitbringt. Bewusste A11-Ausnahme.
    """
    lines_raw = data.get("lines") or ""
    mode = (data.get("mode") or "add").lower()
    if mode not in ("add", "remove"):
        raise HTTPException(status_code=400, detail="mode muss 'add' oder 'remove' sein")
    if not lines_raw.strip():
        raise HTTPException(status_code=400, detail="lines fehlt")

    # Parse alle Zeilen, merge nach email
    contacts_map: dict[str, dict] = {}   # email -> {name, groups (set)}
    invalid: list[dict] = []
    for lineno, line in enumerate(lines_raw.splitlines(), start=1):
        if not line.strip():
            continue
        parsed = _parse_import_line(line, lineno)
        email, name, groups, invalid_reason = parsed
        if invalid_reason and not email:
            invalid.append({"line": lineno, "raw": line, "reason": invalid_reason})
            continue
        if email is None:
            continue
        entry = contacts_map.setdefault(email, {"name": "", "groups": set()})
        if name:
            entry["name"] = name  # letzter nicht-leerer Name gewinnt
        for g in groups or []:
            entry["groups"].add(g)
        if invalid_reason and groups is not None and not groups:
            # nur fehlerhafte Gruppen-Namen, kein gültiger Eintrag
            invalid.append({"line": lineno, "raw": line, "reason": invalid_reason})

    # Lade bestehende Gruppen, Auto-Anlegen wo nötig (nur im add-Mode)
    existing_groups_resp = await pb_client.pb_get(
        "/api/collections/contact_groups/records",
        params={"perPage": 500},
    )
    group_name_to_id = {g["name"]: g["id"] for g in existing_groups_resp.get("items", [])}

    auto_created_groups: list[str] = []
    all_used_groups = set()
    for entry in contacts_map.values():
        all_used_groups.update(entry["groups"])

    if mode == "add":
        for gname in all_used_groups:
            if gname not in group_name_to_id:
                try:
                    created = await pb_client.pb_post(
                        "/api/collections/contact_groups/records",
                        {"name": gname, "description": ""},
                    )
                    group_name_to_id[gname] = created["id"]
                    auto_created_groups.append(gname)
                except Exception as exc:
                    logger.warning("Auto-Anlegen Gruppe %s fehlgeschlagen: %s", gname, exc)

    # Pro email: bestehenden Kontakt finden + add/remove anwenden
    counts = {"added": 0, "updated": 0, "unchanged": 0,
              "removed_from": 0, "not_found": 0, "errors": 0}

    for email, entry in contacts_map.items():
        try:
            resp = await pb_client.pb_get(
                "/api/collections/contacts/records",
                params={"filter": f'email={pb_client.pb_quote(email)}', "perPage": 1},
            )
            items = resp.get("items", [])
            current = items[0] if items else None

            new_group_ids = []
            for gname in entry["groups"]:
                gid = group_name_to_id.get(gname)
                if gid:
                    new_group_ids.append(gid)

            if mode == "add":
                if current:
                    patch = {}
                    if entry["name"] and entry["name"] != (current.get("name") or ""):
                        patch["name"] = entry["name"]
                    current_groups = set(current.get("groups") or [])
                    merged = current_groups | set(new_group_ids)
                    if merged != current_groups:
                        patch["groups"] = list(merged)
                    if patch:
                        await pb_client.pb_patch(
                            f"/api/collections/contacts/records/{current['id']}",
                            patch,
                        )
                        counts["updated"] += 1
                    else:
                        counts["unchanged"] += 1
                else:
                    await pb_client.pb_post(
                        "/api/collections/contacts/records",
                        {
                            "email": email,
                            "name": entry["name"] or "",
                            "groups": new_group_ids,
                            "unsubscribed": False,
                        },
                    )
                    counts["added"] += 1

            elif mode == "remove":
                if not current:
                    counts["not_found"] += 1
                    continue
                if not new_group_ids:
                    # Keine Gruppen angegeben → no-op, in unchanged zählen
                    counts["unchanged"] += 1
                    continue
                current_groups = set(current.get("groups") or [])
                remaining = current_groups - set(new_group_ids)
                if remaining != current_groups:
                    await pb_client.pb_patch(
                        f"/api/collections/contacts/records/{current['id']}",
                        {"groups": list(remaining)},
                    )
                    counts["removed_from"] += 1
                else:
                    counts["unchanged"] += 1

        except Exception as exc:
            logger.warning("Import-Fehler für %s: %s", email, exc)
            counts["errors"] += 1

    return {
        "mode": mode,
        "counts": counts,
        "invalid": invalid,
        "auto_created_groups": auto_created_groups,
        "total_lines_parsed": len(contacts_map) + len(invalid),
    }
