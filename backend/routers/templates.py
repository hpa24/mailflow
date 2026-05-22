"""Templates-Endpoints — E-Mail-Vorlagen, Snippets, Variablen + Render.

Ausgegliedert aus main.py im Rahmen von C1 Phase 2 (Router-Split).

Auth: PB-User-Token via `pb_user_auth.get_user_token`-Dependency. CRUD-Pfade
respektieren PB-Rules (Phase A11 3a).
"""
from __future__ import annotations

import re

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator

import pb_client
import pb_user_auth
import rendering

router = APIRouter()


# ---------------------------------------------------------------------------
# Variablen
# ---------------------------------------------------------------------------

_VAR_NAME_RE = re.compile(r"^[a-z_][a-z0-9_]*$")
_VAR_RESERVED_NAMES = {"name", "email"}


@router.get("/variables")
async def variables_list(token: str = Depends(pb_user_auth.get_user_token)):
    data = await pb_client.pb_get_as(
        token,
        "/api/collections/email_variables/records",
        params={"perPage": 500, "sort": "name"},
    )
    return data.get("items", [])


class VariableCreateRequest(BaseModel):
    name: str
    value: str = ""

    @field_validator("name")
    @classmethod
    def normalize_and_validate_name(cls, v: str) -> str:
        v = (v or "").strip().lower()
        if not _VAR_NAME_RE.match(v):
            raise ValueError("name ungültig (nur a-z, 0-9, _; Start mit Buchstabe oder _)")
        if v in _VAR_RESERVED_NAMES:
            raise ValueError(f"name '{v}' ist reserviert für Kontakt-Felder")
        return v


@router.post("/variables")
async def variables_create(req: VariableCreateRequest, token: str = Depends(pb_user_auth.get_user_token)):
    record = {"name": req.name, "value": req.value}
    try:
        return await pb_client.pb_post_as(token, "/api/collections/email_variables/records", record)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 400 and "name" in exc.response.text:
            raise HTTPException(status_code=409, detail=f"Variable '{req.name}' existiert bereits")
        raise


class VariableUpdateRequest(BaseModel):
    name: str | None = None
    value: str | None = None

    @field_validator("name")
    @classmethod
    def normalize_name(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip().lower()
        if not _VAR_NAME_RE.match(v):
            raise ValueError("name ungültig (nur a-z, 0-9, _; Start mit Buchstabe oder _)")
        if v in _VAR_RESERVED_NAMES:
            raise ValueError(f"name '{v}' ist reserviert für Kontakt-Felder")
        return v


@router.patch("/variables/{var_id}")
async def variables_update(var_id: str, req: VariableUpdateRequest, token: str = Depends(pb_user_auth.get_user_token)):
    patch = req.model_dump(exclude_unset=True)
    if "value" in patch and patch["value"] is None:
        patch["value"] = ""
    if not patch:
        raise HTTPException(status_code=400, detail="nichts zu ändern")
    try:
        return await pb_client.pb_patch_as(token, f"/api/collections/email_variables/records/{var_id}", patch)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 400 and "name" in exc.response.text:
            raise HTTPException(status_code=409, detail="Variable mit diesem Namen existiert bereits")
        raise


@router.get("/variables/{var_id}/usage")
async def variables_usage(var_id: str, token: str = Depends(pb_user_auth.get_user_token)):
    """Findet alle Templates + Snippets, die diese Variable referenzieren."""
    var = await pb_client.pb_get_as(token, f"/api/collections/email_variables/records/{var_id}")
    name = var.get("name") or ""
    if not name:
        return {"name": "", "templates": [], "snippets": []}
    return await _find_placeholder_usage(token, name, include_snippets=True, snippet_prefix=False)


@router.delete("/variables/{var_id}")
async def variables_delete(var_id: str, token: str = Depends(pb_user_auth.get_user_token)):
    await pb_client.pb_delete_as(token, f"/api/collections/email_variables/records/{var_id}")
    return {"status": "deleted"}


class VariableRenameRequest(BaseModel):
    new_name: str
    replace_in_usage: bool = False

    @field_validator("new_name")
    @classmethod
    def normalize_new_name(cls, v: str) -> str:
        v = (v or "").strip().lower()
        if not _VAR_NAME_RE.match(v):
            raise ValueError("name ungültig (nur a-z, 0-9, _; Start mit Buchstabe oder _)")
        if v in _VAR_RESERVED_NAMES:
            raise ValueError(f"name '{v}' ist reserviert für Kontakt-Felder")
        return v


@router.post("/variables/{var_id}/rename")
async def variables_rename(var_id: str, req: VariableRenameRequest, token: str = Depends(pb_user_auth.get_user_token)):
    """Benennt eine Variable um und ersetzt optional alle `{{old}}`-Vorkommen
    in Templates+Snippets durch `{{new}}`.

    Response: ``{old_name, new_name, replaced_templates, replaced_snippets}``.
    """
    new_name = req.new_name
    replace = req.replace_in_usage

    cur = await pb_client.pb_get_as(token, f"/api/collections/email_variables/records/{var_id}")
    old_name = (cur.get("name") or "").strip().lower()
    if old_name == new_name:
        return {"old_name": old_name, "new_name": new_name,
                "replaced_templates": 0, "replaced_snippets": 0}

    replaced_t = replaced_s = 0
    if replace:
        replaced_t, replaced_s = await _replace_placeholder_refs(
            token, old_name, new_name, is_snippet=False
        )

    try:
        await pb_client.pb_patch_as(
            token,
            f"/api/collections/email_variables/records/{var_id}",
            {"name": new_name},
        )
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 400 and "name" in exc.response.text:
            raise HTTPException(status_code=409, detail=f"Variable '{new_name}' existiert bereits")
        raise

    return {"old_name": old_name, "new_name": new_name,
            "replaced_templates": replaced_t, "replaced_snippets": replaced_s}


# ---------------------------------------------------------------------------
# Placeholder-Helpers (shared zwischen Variablen + Snippets)
# ---------------------------------------------------------------------------


async def _replace_placeholder_refs(token: str, old: str, new: str, *, is_snippet: bool) -> tuple[int, int]:
    """Ersetzt `{{old}}` (Variable) bzw. `{{> old}}` (Snippet) in
    email_templates (subject+html_body) und — nur bei Variablen — auch
    in email_snippets (html). Snippet-in-Snippet ist per Plan verboten,
    daher überspringen wir Snippets, wenn ein Snippet umbenannt wird.

    Returns (templates_modified, snippets_modified).
    """
    old_l = old.strip().lower()
    new_l = new.strip().lower()

    def rewrite(text: str) -> tuple[str, bool]:
        changed = False

        def repl(m: re.Match) -> str:
            nonlocal changed
            is_snip = bool(m.group(1))
            name = (m.group(2) or "").strip().lower()
            if name != old_l:
                return m.group(0)
            if is_snippet != is_snip:
                return m.group(0)
            changed = True
            return f"{{{{> {new_l}}}}}" if is_snip else f"{{{{{new_l}}}}}"

        result = rendering._PLACEHOLDER_RE.sub(repl, text or "")
        return result, changed

    tpl_modified = 0
    tpls = await pb_client.pb_get_as(
        token,
        "/api/collections/email_templates/records",
        params={"perPage": 500, "fields": "id,subject,html_body"},
    )
    for t in tpls.get("items", []):
        new_subj, ch1 = rewrite(t.get("subject") or "")
        new_body, ch2 = rewrite(t.get("html_body") or "")
        if ch1 or ch2:
            patch: dict = {}
            if ch1:
                patch["subject"] = new_subj
            if ch2:
                patch["html_body"] = new_body
            await pb_client.pb_patch_as(
                token, f"/api/collections/email_templates/records/{t['id']}", patch
            )
            tpl_modified += 1

    snip_modified = 0
    if not is_snippet:
        snips = await pb_client.pb_get_as(
            token,
            "/api/collections/email_snippets/records",
            params={"perPage": 500, "fields": "id,html"},
        )
        for s in snips.get("items", []):
            new_html, ch = rewrite(s.get("html") or "")
            if ch:
                await pb_client.pb_patch_as(
                    token,
                    f"/api/collections/email_snippets/records/{s['id']}",
                    {"html": new_html},
                )
                snip_modified += 1

    return tpl_modified, snip_modified


async def _find_placeholder_usage(token: str, name: str, *, include_snippets: bool, snippet_prefix: bool) -> dict:
    """Sucht `{{name}}` (oder `{{> name}}` wenn snippet_prefix=True) in
    email_templates.subject + html_body und optional email_snippets.html.
    Nutzt rendering._PLACEHOLDER_RE: (>?)(name).
    """
    target = name.strip().lower()
    matched_templates: list[dict] = []
    matched_snippets: list[dict] = []

    tpls_resp = await pb_client.pb_get_as(
        token,
        "/api/collections/email_templates/records",
        params={"perPage": 500, "sort": "prefix,name"},
    )
    for t in tpls_resp.get("items", []):
        hits: list[str] = []
        for field in ("subject", "html_body"):
            text = t.get(field) or ""
            for m in rendering._PLACEHOLDER_RE.finditer(text):
                is_snippet = bool(m.group(1))
                placeholder_name = (m.group(2) or "").strip().lower()
                if placeholder_name != target:
                    continue
                if snippet_prefix and not is_snippet:
                    continue
                if not snippet_prefix and is_snippet:
                    continue
                hits.append(field)
                break  # ein Treffer pro Feld reicht
        if hits:
            matched_templates.append({
                "id": t["id"],
                "prefix": t.get("prefix") or "",
                "name": t.get("name") or "",
                "fields": hits,
            })

    if include_snippets:
        snips_resp = await pb_client.pb_get_as(
            token,
            "/api/collections/email_snippets/records",
            params={"perPage": 500, "sort": "name"},
        )
        for s in snips_resp.get("items", []):
            text = s.get("html") or ""
            for m in rendering._PLACEHOLDER_RE.finditer(text):
                is_snippet = bool(m.group(1))
                placeholder_name = (m.group(2) or "").strip().lower()
                if placeholder_name != target:
                    continue
                # Snippet-in-Snippet ist per Plan-Konvention verboten — also
                # zaehlen wir hier nur Nicht-Prefix-Treffer (Variablen).
                if is_snippet:
                    continue
                matched_snippets.append({"id": s["id"], "name": s.get("name") or ""})
                break

    return {"name": target, "templates": matched_templates, "snippets": matched_snippets}


# ---------------------------------------------------------------------------
# Snippets
# ---------------------------------------------------------------------------

_SNIPPET_NAME_RE = re.compile(r"^[a-z_][a-z0-9_]{0,49}$")


@router.get("/snippets")
async def snippets_list(token: str = Depends(pb_user_auth.get_user_token)):
    data = await pb_client.pb_get_as(
        token,
        "/api/collections/email_snippets/records",
        params={"perPage": 500, "sort": "name"},
    )
    return data.get("items", [])


def _normalize_snippet_name(v: str | None) -> str | None:
    if v is None:
        return None
    v = v.strip().lower()
    if not _SNIPPET_NAME_RE.match(v):
        raise ValueError("name ungültig (1–50 Zeichen, nur a-z, 0-9, _; Start mit Buchstabe oder _)")
    return v


class SnippetCreateRequest(BaseModel):
    name: str
    html: str = ""

    @field_validator("name")
    @classmethod
    def normalize_name(cls, v: str) -> str:
        return _normalize_snippet_name(v)


class SnippetUpdateRequest(BaseModel):
    name: str | None = None
    html: str | None = None

    @field_validator("name")
    @classmethod
    def normalize_name(cls, v: str | None) -> str | None:
        return _normalize_snippet_name(v)


@router.post("/snippets")
async def snippets_create(req: SnippetCreateRequest, token: str = Depends(pb_user_auth.get_user_token)):
    record = {"name": req.name, "html": req.html}
    try:
        return await pb_client.pb_post_as(token, "/api/collections/email_snippets/records", record)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 400 and "name" in exc.response.text:
            raise HTTPException(status_code=409, detail=f"Snippet '{req.name}' existiert bereits")
        raise


@router.patch("/snippets/{snippet_id}")
async def snippets_update(snippet_id: str, req: SnippetUpdateRequest, token: str = Depends(pb_user_auth.get_user_token)):
    patch = req.model_dump(exclude_unset=True)
    if "html" in patch and patch["html"] is None:
        patch["html"] = ""
    if not patch:
        raise HTTPException(status_code=400, detail="nichts zu ändern")
    try:
        return await pb_client.pb_patch_as(token, f"/api/collections/email_snippets/records/{snippet_id}", patch)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 400 and "name" in exc.response.text:
            raise HTTPException(status_code=409, detail="Snippet mit diesem Namen existiert bereits")
        raise


@router.get("/snippets/{snippet_id}/usage")
async def snippets_usage(snippet_id: str, token: str = Depends(pb_user_auth.get_user_token)):
    """Findet alle Templates, die dieses Snippet via {{> name}} referenzieren.
    Snippets duerfen keine anderen Snippets includen (Plan-Konvention), deshalb
    wird email_snippets nicht gescannt."""
    snip = await pb_client.pb_get_as(token, f"/api/collections/email_snippets/records/{snippet_id}")
    name = snip.get("name") or ""
    if not name:
        return {"name": "", "templates": [], "snippets": []}
    return await _find_placeholder_usage(token, name, include_snippets=False, snippet_prefix=True)


@router.delete("/snippets/{snippet_id}")
async def snippets_delete(snippet_id: str, token: str = Depends(pb_user_auth.get_user_token)):
    await pb_client.pb_delete_as(token, f"/api/collections/email_snippets/records/{snippet_id}")
    return {"status": "deleted"}


class SnippetRenameRequest(BaseModel):
    new_name: str
    replace_in_usage: bool = False

    @field_validator("new_name")
    @classmethod
    def normalize_new_name(cls, v: str) -> str:
        v = (v or "").strip().lower()
        if not _SNIPPET_NAME_RE.match(v):
            raise ValueError("name ungültig (1–50 Zeichen, nur a-z, 0-9, _; Start mit Buchstabe oder _)")
        return v


@router.post("/snippets/{snippet_id}/rename")
async def snippets_rename(snippet_id: str, req: SnippetRenameRequest, token: str = Depends(pb_user_auth.get_user_token)):
    """Benennt ein Snippet um und ersetzt optional alle `{{> old}}`-Refs
    in Templates durch `{{> new}}`.

    Response: ``{old_name, new_name, replaced_templates}``.
    """
    new_name = req.new_name
    replace = req.replace_in_usage

    cur = await pb_client.pb_get_as(token, f"/api/collections/email_snippets/records/{snippet_id}")
    old_name = (cur.get("name") or "").strip().lower()
    if old_name == new_name:
        return {"old_name": old_name, "new_name": new_name, "replaced_templates": 0}

    replaced_t = 0
    if replace:
        replaced_t, _ = await _replace_placeholder_refs(
            token, old_name, new_name, is_snippet=True
        )

    try:
        await pb_client.pb_patch_as(
            token,
            f"/api/collections/email_snippets/records/{snippet_id}",
            {"name": new_name},
        )
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 400 and "name" in exc.response.text:
            raise HTTPException(status_code=409, detail=f"Snippet '{new_name}' existiert bereits")
        raise

    return {"old_name": old_name, "new_name": new_name, "replaced_templates": replaced_t}


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------

_TEMPLATE_PREFIX_RE = re.compile(r"^[a-z0-9_]{0,30}$")


@router.get("/templates")
async def templates_list(prefix: str = "", search: str = "", token: str = Depends(pb_user_auth.get_user_token)):
    filters = []
    if prefix:
        filters.append(f'prefix={pb_client.pb_quote(prefix)}')
    if search:
        s = pb_client.pb_quote(search)
        filters.append(f'(name~{s} || subject~{s})')
    params = {"perPage": 500, "sort": "prefix,name"}
    if filters:
        params["filter"] = " && ".join(filters)
    data = await pb_client.pb_get_as(
        token,
        "/api/collections/email_templates/records",
        params=params,
    )
    return data.get("items", [])


def _normalize_template_prefix(v: str | None) -> str | None:
    if v is None:
        return None
    v = v.strip().lower()
    if not _TEMPLATE_PREFIX_RE.match(v):
        raise ValueError("prefix ungültig (max 30 Zeichen, nur a-z, 0-9, _)")
    return v


def _normalize_template_name(v: str | None) -> str | None:
    if v is None:
        return None
    v = v.strip()
    if not v or len(v) > 100:
        raise ValueError("name muss 1–100 Zeichen lang sein")
    return v


class TemplateCreateRequest(BaseModel):
    prefix: str = ""
    name: str
    subject: str = ""
    html_body: str = ""
    text_body: str = ""

    @field_validator("prefix")
    @classmethod
    def normalize_prefix(cls, v: str) -> str:
        return _normalize_template_prefix(v) or ""

    @field_validator("name")
    @classmethod
    def normalize_name(cls, v: str) -> str:
        return _normalize_template_name(v) or ""

    @field_validator("subject")
    @classmethod
    def strip_subject(cls, v: str) -> str:
        return (v or "").strip()


class TemplateUpdateRequest(BaseModel):
    prefix: str | None = None
    name: str | None = None
    subject: str | None = None
    html_body: str | None = None
    text_body: str | None = None

    @field_validator("prefix")
    @classmethod
    def normalize_prefix(cls, v: str | None) -> str | None:
        return _normalize_template_prefix(v)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, v: str | None) -> str | None:
        return _normalize_template_name(v)


@router.post("/templates")
async def templates_create(req: TemplateCreateRequest, token: str = Depends(pb_user_auth.get_user_token)):
    record = {
        "prefix": req.prefix,
        "name": req.name,
        "subject": req.subject,
        "html_body": req.html_body,
        "text_body": req.text_body,
    }
    try:
        return await pb_client.pb_post_as(token, "/api/collections/email_templates/records", record)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 400 and ("prefix" in exc.response.text or "name" in exc.response.text):
            raise HTTPException(status_code=409, detail=f"Vorlage '{req.prefix}/{req.name}' existiert bereits")
        raise


@router.patch("/templates/{template_id}")
async def templates_update(template_id: str, req: TemplateUpdateRequest, token: str = Depends(pb_user_auth.get_user_token)):
    patch = req.model_dump(exclude_unset=True)
    if "subject" in patch:
        patch["subject"] = (patch["subject"] or "").strip()
    for key in ("html_body", "text_body"):
        if key in patch and patch[key] is None:
            patch[key] = ""
    if not patch:
        raise HTTPException(status_code=400, detail="nichts zu ändern")
    try:
        return await pb_client.pb_patch_as(token, f"/api/collections/email_templates/records/{template_id}", patch)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 400 and ("prefix" in exc.response.text or "name" in exc.response.text):
            raise HTTPException(status_code=409, detail="Vorlage mit diesem Präfix+Name existiert bereits")
        raise


@router.delete("/templates/{template_id}")
async def templates_delete(template_id: str, token: str = Depends(pb_user_auth.get_user_token)):
    await pb_client.pb_delete_as(token, f"/api/collections/email_templates/records/{template_id}")
    return {"status": "deleted"}


class TemplatesRenderRequest(BaseModel):
    """POST /templates/render — Live-Preview Body. active_sections=None = alle aktiv."""
    html: str = ""
    subject: str = ""
    active_sections: list[str] | None = None
    contact_id: str | None = None


@router.post("/templates/render")
async def templates_render(payload: TemplatesRenderRequest,
                           token: str = Depends(pb_user_auth.get_user_token)):
    """Rendert html + subject mit Snippets, globalen Variablen und optional
    einem Kontakt. Wird vom Frontend fuer Live-Preview genutzt und spaeter
    von Compose/Bulk-Send.
    """
    html = payload.html
    subject = payload.subject
    active_sections = payload.active_sections
    contact_id = payload.contact_id

    # Snippets/Variables: rendering.load_* nutzt absichtlich den Admin-Token,
    # weil dieselben Helper auch aus dem Mail-Versand-Backend (ohne User-Session)
    # aufgerufen werden. PB-Rules sind in 3a auf "any authenticated" gesetzt;
    # der Admin-Bypass ist davon nicht betroffen.
    snippets = await rendering.load_snippets_map()
    variables = await rendering.load_variables_map()

    contact = None
    if contact_id:
        try:
            contact = await pb_client.pb_get_as(token, f"/api/collections/contacts/records/{contact_id}")
        except Exception:
            contact = None

    rendered_html = rendering.render_full(html, snippets, variables, active_sections, contact)
    rendered_subject = rendering.render_full(subject, snippets, variables, active_sections, contact)
    unresolved = rendering.find_unresolved(rendered_html) + rendering.find_unresolved(rendered_subject)

    return {
        "html": rendered_html,
        "subject": rendered_subject,
        "unresolved": unresolved,
    }
