"""Spam-Lernsystem: Blocklist (PocketBase) + semantische Ähnlichkeit (Qdrant).

Klassifikations-Reihenfolge:
1. Blocklist-Treffer (Adresse oder Domain) → action="move" (sofort verschieben).
2. Vektor-Ähnlichkeit ≥ SPAM_SIMILARITY_THRESHOLD → action="suggest" (Inline-Vorschlag im UI).
3. Sonst → action="none".
"""
import logging
from datetime import datetime, timezone

import pb_client
import vector_store
from config import settings

logger = logging.getLogger(__name__)


def _extract_domain(from_email: str) -> str:
    if not from_email or "@" not in from_email:
        return ""
    return from_email.rsplit("@", 1)[1].strip().lower()


def _normalize_email(from_email: str) -> str:
    return (from_email or "").strip().lower()


async def ensure_spam_collection() -> None:
    await vector_store.ensure_spam_collection()


async def add_spam_sample(email: dict) -> None:
    """Embedded eine Mail und speichert sie als Spam-Trainingsbeispiel."""
    try:
        await vector_store.upsert_spam_sample(email)
    except Exception as e:
        logger.warning(f"add_spam_sample failed for {email.get('id')}: {e}")


async def remove_spam_sample(email_id: str) -> None:
    try:
        await vector_store.delete_spam_sample(email_id)
    except Exception as e:
        logger.warning(f"remove_spam_sample failed for {email_id}: {e}")


async def add_blocklist_entry(account_id: str, from_email: str, *, block_domain: bool = False) -> dict | None:
    """Legt einen Blocklist-Eintrag an. Liefert das erstellte Record oder None bei Fehler/Duplikat."""
    pattern = _extract_domain(from_email) if block_domain else _normalize_email(from_email)
    match_type = "domain" if block_domain else "email"
    if not pattern:
        return None
    try:
        return await pb_client.pb_post(
            "/api/collections/spam_rules/records",
            {
                "account": account_id,
                "match_type": match_type,
                "pattern": pattern,
                "hits": 0,
            },
        )
    except pb_client.DuplicateRecordError:
        logger.info(f"Blocklist-Eintrag existiert bereits: {match_type}={pattern}")
        return None
    except Exception as e:
        logger.warning(f"add_blocklist_entry failed: {e}")
        return None


async def check_blocklist(account_id: str, from_email: str) -> dict | None:
    """Prüft, ob from_email per Adresse oder Domain geblockt ist.
    Liefert das passende Regel-Record oder None."""
    addr = _normalize_email(from_email)
    domain = _extract_domain(addr)
    if not addr:
        return None

    parts = [f'(match_type="email" && pattern="{addr}")']
    if domain:
        parts.append(f'(match_type="domain" && pattern="{domain}")')
    filter_expr = f'account="{account_id}" && ({" || ".join(parts)})'

    try:
        result = await pb_client.pb_get(
            "/api/collections/spam_rules/records",
            params={"filter": filter_expr, "perPage": 1},
        )
        items = result.get("items", [])
        return items[0] if items else None
    except Exception as e:
        logger.warning(f"check_blocklist failed: {e}")
        return None


async def _bump_rule_hit(rule_id: str) -> None:
    """Erhöht hits-Counter und setzt last_hit auf jetzt. Best-effort."""
    try:
        rule = await pb_client.pb_get(f"/api/collections/spam_rules/records/{rule_id}")
        await pb_client.pb_patch(
            f"/api/collections/spam_rules/records/{rule_id}",
            {
                "hits": (rule.get("hits") or 0) + 1,
                "last_hit": datetime.now(timezone.utc).isoformat(),
            },
        )
    except Exception as e:
        logger.warning(f"_bump_rule_hit failed for {rule_id}: {e}")


async def check_similarity(email: dict) -> dict | None:
    """Sucht ähnlichste Spam-Samples. Liefert Top-Treffer (oder None) mit Score ≥ Schwelle."""
    try:
        hits = await vector_store.search_similar_spam(
            email,
            limit=1,
            score_threshold=settings.SPAM_SIMILARITY_THRESHOLD,
        )
    except Exception as e:
        logger.warning(f"check_similarity failed: {e}")
        return None
    return hits[0] if hits else None


async def classify_incoming(email: dict) -> dict:
    """Klassifiziert eine eingehende Mail.
    Returns: {"action": "move"|"suggest"|"none", "reason": str, "score": float|None, "rule_match": str}
    """
    account_id = email.get("account") or ""
    from_email = email.get("from_email") or ""

    rule = await check_blocklist(account_id, from_email)
    if rule:
        await _bump_rule_hit(rule["id"])
        return {
            "action": "move",
            "reason": f'blocklist:{rule["match_type"]}',
            "score": None,
            "rule_match": f'{rule["match_type"]}:{rule["pattern"]}',
        }

    hit = await check_similarity(email)
    if hit:
        return {
            "action": "suggest",
            "reason": "vector",
            "score": float(hit.get("score") or 0.0),
            "rule_match": f'vector:{hit.get("score", 0):.3f}:{hit.get("email_id", "")}',
        }

    return {"action": "none", "reason": "", "score": None, "rule_match": ""}
