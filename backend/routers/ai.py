"""AI-Endpoints — Categories, Triage, Analyse, Antwort-Vorschlag, Refine,
Response-Patterns.

Ausgegliedert aus main.py im Rahmen von C1 Phase 2 (Router-Split).

Auth-Mix:
- `/categories`: kein User-Token nötig (Config-Read aus ai_helper).
- `/ai/triage`, `/triage/example`, `/ai/analyze`, `/ai/suggest`, `/response-patterns`:
  PB-User-Token via `pb_user_auth.get_user_token`.
- `/ai/refine`: kein PB-Call, nur Claude — keine User-Auth nötig (hängt an
  globaler Middleware).
- `_consolidate_rules` läuft als Background-Task ohne User-Session und
  nutzt deshalb bewusst den Admin-Token.
"""
from __future__ import annotations

import asyncio
import logging
import re

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator

import ai_helper
import pb_client
import pb_user_auth

logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Pydantic-Modelle
# ---------------------------------------------------------------------------


class TriageRequest(BaseModel):
    account_id: str | None = None
    folder: str | None = None


class SuggestRequest(BaseModel):
    email_id: str
    tone: str = "neutral"
    context_elements: list[str] | None = None


class RefineRequest(BaseModel):
    text: str
    instruction: str


class TriageExampleRequest(BaseModel):
    email_id: str = Field(..., min_length=1)
    category: str = Field(..., min_length=1)

    @field_validator("email_id", "category")
    @classmethod
    def strip_value(cls, v: str) -> str:
        return (v or "").strip()


class AnalyzeRequest(BaseModel):
    email_id: str


class SavePatternRequest(BaseModel):
    account_id: str
    element_text: str
    action: str
    draft_text: str
    was_edited: bool = False


# ---------------------------------------------------------------------------
# Categories
# ---------------------------------------------------------------------------


@router.get("/categories")
async def get_categories():
    """Liefert die konfigurierten Triage-Kategorien."""
    categories = ai_helper.load_triage_config()["categories"]
    return [{"slug": c["slug"], "name": c["name"], "description": c["description"]} for c in categories]


# ---------------------------------------------------------------------------
# Triage (Auto-Kategorisierung)
# ---------------------------------------------------------------------------


@router.post("/ai/triage")
async def ai_triage(req: TriageRequest, token: str = Depends(pb_user_auth.get_user_token)):
    """Kategorisiert ungelesene E-Mails ohne ai_category via Claude Haiku.

    Max. 50 E-Mails pro Aufruf (Kostenschutz).
    """
    filters = ['is_read=false', '(ai_category="" || ai_category=null)']
    if req.account_id:
        filters.append(f'account={pb_client.pb_quote(req.account_id)}')
    if req.folder:
        filters.append(f'folder={pb_client.pb_quote(req.folder)}')

    try:
        data = await pb_client.pb_get_as(token, "/api/collections/emails/records", params={
            "filter": " && ".join(filters),
            "perPage": 50,
            "sort": "-date_sent",
            "fields": "id,subject,body_plain,from_email,account",
        })
    except Exception as exc:
        logger.error("Triage: PocketBase-Abfrage fehlgeschlagen: %s", exc)
        raise HTTPException(status_code=500, detail=f"PocketBase-Fehler: {exc}")

    emails = data.get("items", [])
    if not emails:
        return {"categorized": 0, "skipped": 0, "errors": 0}

    # Lernregeln für diesen Account laden
    rules: list[str] = []
    try:
        rule_filter = f'account={pb_client.pb_quote(req.account_id)}' if req.account_id else ""
        rule_data = await pb_client.pb_get_as(token, "/api/collections/triage_rules/records", params={
            "filter": rule_filter,
            "perPage": 100,
            "sort": "-created",
            "fields": "rule_text",
        })
        rules = [r["rule_text"] for r in rule_data.get("items", []) if r.get("rule_text")]
    except Exception as exc:
        logger.warning("Triage: Lernregeln konnten nicht geladen werden: %s", exc)

    categorized = 0
    errors = 0
    semaphore = asyncio.Semaphore(5)

    async def _process_one(email: dict) -> None:
        nonlocal categorized, errors
        async with semaphore:
            try:
                category = await ai_helper.categorize_email(
                    subject=email.get("subject") or "",
                    body=email.get("body_plain") or "",
                    from_email=email.get("from_email") or "",
                    rules=rules,
                )
                await pb_client.pb_patch_as(
                    token,
                    f"/api/collections/emails/records/{email['id']}",
                    {"ai_category": category},
                )
                categorized += 1
                logger.info("Triage: %s → %s", email["id"], category)
            except Exception as exc:
                errors += 1
                logger.warning("Triage: Fehler bei E-Mail %s: %s", email["id"], exc)

    await asyncio.gather(*[_process_one(e) for e in emails])

    return {"categorized": categorized, "skipped": 0, "errors": errors}


@router.post("/triage/example")
async def save_triage_example(req: TriageExampleRequest, token: str = Depends(pb_user_auth.get_user_token)):
    """Speichert eine manuelle Korrektur als Lernregel für die KI-Triage."""
    email_id = req.email_id
    category = req.category
    # Category-Slug-Liste ist dynamisch (vom ai_helper) — kann nicht als Literal ins Modell
    valid_slugs = set(ai_helper.get_category_slugs())
    if not email_id or category not in valid_slugs:
        raise HTTPException(status_code=400, detail="email_id und gültige category erforderlich")

    try:
        email = await pb_client.pb_get_as(token, f"/api/collections/emails/records/{email_id}",
                                          params={"fields": "account,from_email,subject,body_plain"})
    except Exception as exc:
        logger.error("triage/example: E-Mail %s konnte nicht geladen werden: %s", email_id, exc)
        status = 404 if "404" in str(exc) else 502
        raise HTTPException(status_code=status, detail=f"E-Mail konnte nicht geladen werden: {exc}")

    # Regel via AI extrahieren
    rule_text = await ai_helper.extract_rule(
        from_email=email.get("from_email", ""),
        subject=email.get("subject", ""),
        body_snippet=(email.get("body_plain") or "")[:300],
        category_slug=category,
    )
    logger.info("Triage-Regel extrahiert: %s → %s", category, rule_text)

    try:
        await pb_client.pb_post_as(token, "/api/collections/triage_rules/records", {
            "account":       email["account"],
            "category_slug": category,
            "rule_text":     rule_text,
        })
    except Exception as exc:
        logger.error("triage/example: Regel konnte nicht gespeichert werden: %s", exc)
        raise HTTPException(status_code=502, detail=f"Regel konnte nicht gespeichert werden: {exc}")

    # Konsolidierung prüfen: bei ≥15 Regeln für diesen Account + Kategorie
    try:
        count_data = await pb_client.pb_get_as(token, "/api/collections/triage_rules/records", params={
            "filter": f'account={pb_client.pb_quote(email["account"])} && category_slug={pb_client.pb_quote(category)}',
            "perPage": 1,
        })
        total = count_data.get("totalItems", 0)
        if total >= 15:
            # _consolidate_rules läuft als Background-Task ohne User-Session → Admin-Token (bewusst).
            asyncio.create_task(_consolidate_rules(email["account"], category))
    except Exception as exc:
        logger.warning("Konsolidierungsprüfung fehlgeschlagen: %s", exc)

    return {"ok": True, "rule": rule_text}


async def _consolidate_rules(account: str, category_slug: str) -> None:
    """Hintergrundaufgabe: Konsolidiert Lernregeln auf max. 7."""
    try:
        data = await pb_client.pb_get("/api/collections/triage_rules/records", params={
            "filter": f'account={pb_client.pb_quote(account)} && category_slug={pb_client.pb_quote(category_slug)}',
            "perPage": 200,
            "fields": "id,rule_text",
        })
        items = data.get("items", [])
        if len(items) < 15:
            return

        rules = [r["rule_text"] for r in items if r.get("rule_text")]
        consolidated = await ai_helper.consolidate_rules(rules, category_slug)
        logger.info("Konsolidierung %s/%s: %d → %d Regeln", account, category_slug, len(items), len(consolidated))

        # Alle alten löschen
        for item in items:
            await pb_client.pb_delete(f"/api/collections/triage_rules/records/{item['id']}")

        # Neue speichern
        for rule_text in consolidated:
            await pb_client.pb_post("/api/collections/triage_rules/records", {
                "account":       account,
                "category_slug": category_slug,
                "rule_text":     rule_text,
            })
    except Exception as exc:
        logger.error("Konsolidierung fehlgeschlagen (%s/%s): %s", account, category_slug, exc)


# ---------------------------------------------------------------------------
# Analyse / Suggest / Refine
# ---------------------------------------------------------------------------


@router.post("/ai/analyze")
async def ai_analyze(req: AnalyzeRequest, token: str = Depends(pb_user_auth.get_user_token)):
    """Analysiert eine E-Mail strukturell: Elemente + Aktionsvorschläge."""
    try:
        email = await pb_client.pb_get_as(token, f"/api/collections/emails/records/{req.email_id}")
    except Exception as exc:
        raise HTTPException(status_code=404, detail=f"E-Mail nicht gefunden: {exc}")

    body = email.get("body_plain") or ""
    if not body and email.get("body_html"):
        html = email["body_html"]
        html = re.sub(r'<br\s*/?>', '\n', html, flags=re.IGNORECASE)
        html = re.sub(r'</(p|div|tr|li)>', '\n', html, flags=re.IGNORECASE)
        html = re.sub(r'<[^>]+>', '', html)
        body = re.sub(r'\n{3,}', '\n\n', html).strip()[:5000]

    try:
        items = await ai_helper.analyze_email(
            subject=email.get("subject") or "",
            body=body,
            from_name=email.get("from_name") or email.get("from_email") or "",
        )
    except Exception as exc:
        logger.error("ai_analyze fehlgeschlagen: %s", exc)
        raise HTTPException(status_code=500, detail=f"KI-Fehler: {exc}")

    return {"items": items}


@router.post("/ai/suggest")
async def ai_suggest(req: SuggestRequest, token: str = Depends(pb_user_auth.get_user_token)):
    """Generiert einen Antwortvorschlag für eine E-Mail."""
    try:
        email = await pb_client.pb_get_as(
            token, f"/api/collections/emails/records/{req.email_id}"
        )
    except Exception as exc:
        raise HTTPException(status_code=404, detail=f"E-Mail nicht gefunden: {exc}")

    # body_plain bevorzugen; Fallback: plain text aus body_html (für HTML-only-E-Mails)
    if not email.get("body_plain") and email.get("body_html"):
        plain = email["body_html"]
        plain = re.sub(r'<br\s*/?>', '\n', plain, flags=re.IGNORECASE)
        plain = re.sub(r'</(p|div|tr|li)>', '\n', plain, flags=re.IGNORECASE)
        plain = re.sub(r'<[^>]+>', '', plain)
        plain = re.sub(r'\n{3,}', '\n\n', plain).strip()
        email["body_plain"] = plain[:10000]

    thread_id = email.get("thread_id") or email.get("message_id")

    # Thread-E-Mails laden (max. 10, ohne die E-Mail selbst)
    thread_emails: list = []
    if thread_id:
        try:
            thread_data = await pb_client.pb_get_as(
                token,
                "/api/collections/emails/records",
                params={
                    "filter": f'thread_id={pb_client.pb_quote(thread_id)} && id!={pb_client.pb_quote(req.email_id)}',
                    "sort": "date_sent",
                    "perPage": 10,
                    "fields": "id,from_email,subject,body_plain,date_sent",
                },
            )
            thread_emails = thread_data.get("items", [])
        except Exception as exc:
            logger.warning("Konnte Thread-E-Mails nicht laden: %s", exc)

    # Kontakthistorie: letzte 5 E-Mails vom gleichen Absender außerhalb des Threads
    contact_history: list = []
    from_email = email.get("from_email") or ""
    if from_email:
        history_filter = f'from_email={pb_client.pb_quote(from_email)}'
        if thread_id:
            history_filter += f' && thread_id!={pb_client.pb_quote(thread_id)}'
        try:
            history_data = await pb_client.pb_get_as(
                token,
                "/api/collections/emails/records",
                params={
                    "filter": history_filter,
                    "sort": "-date_sent",
                    "perPage": 5,
                    "fields": "id,from_email,subject,body_plain,date_sent",
                },
            )
            contact_history = history_data.get("items", [])
        except Exception as exc:
            logger.warning("Konnte Kontakthistorie nicht laden: %s", exc)

    last_exc = None
    for attempt in range(3):
        try:
            result = await ai_helper.suggest_reply(
                email=email,
                thread_emails=thread_emails,
                contact_history=contact_history,
                tone=req.tone,
                context_elements=req.context_elements,
            )
            return {"text": result}
        except Exception as exc:
            last_exc = exc
            exc_str = str(exc)
            if "529" in exc_str or "overloaded" in exc_str.lower():
                wait = (attempt + 1) * 4  # 4s, 8s
                logger.warning("KI überlastet (Versuch %d/3), warte %ds …", attempt + 1, wait)
                await asyncio.sleep(wait)
                continue
            break  # Anderer Fehler → sofort abbrechen

    logger.error("suggest_reply fehlgeschlagen: %s", last_exc)
    raise HTTPException(status_code=500, detail=f"KI-Fehler: {last_exc}")


@router.post("/ai/refine")
async def ai_refine(req: RefineRequest):
    """Verfeinert einen bestehenden E-Mail-Entwurf."""
    try:
        result = await ai_helper.refine_reply(text=req.text, instruction=req.instruction)
    except Exception as exc:
        logger.error("refine_reply fehlgeschlagen: %s", exc)
        raise HTTPException(status_code=500, detail=f"KI-Fehler: {exc}")

    return {"text": result}


# ---------------------------------------------------------------------------
# Response-Patterns (Lernsystem: was wurde mit Vorschlag gemacht)
# ---------------------------------------------------------------------------


@router.post("/response-patterns")
async def save_response_pattern(req: SavePatternRequest, token: str = Depends(pb_user_auth.get_user_token)):
    """Speichert ein Antwort-Pattern (Element + Entwurf) in PocketBase."""
    try:
        await pb_client.pb_post_as(token, "/api/collections/response_patterns/records", {
            "account":       req.account_id,
            "element_text":  req.element_text,
            "action":        req.action,
            "draft_text":    req.draft_text,
            "was_edited":    req.was_edited,
        })
    except Exception as exc:
        logger.error("response-patterns: Speichern fehlgeschlagen: %s", exc)
        raise HTTPException(status_code=500, detail=f"Speichern fehlgeschlagen: {exc}")
    return {"ok": True}
