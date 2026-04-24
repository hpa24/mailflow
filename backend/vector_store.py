import logging
import uuid
from datetime import datetime

from qdrant_client import AsyncQdrantClient
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    MatchValue,
    PointStruct,
    VectorParams,
)

from config import settings
from embed import EMBED_DIMS, build_thread_embed_text, embed_batch, embed_text, split_reply_from_quote

logger = logging.getLogger(__name__)

COLLECTION = "mailflow_emails"
_NS = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")

_client: AsyncQdrantClient | None = None


def _get_client() -> AsyncQdrantClient:
    global _client
    if _client is None:
        kwargs: dict = {"url": settings.QDRANT_URL}
        if settings.QDRANT_API_KEY:
            kwargs["api_key"] = settings.QDRANT_API_KEY
        _client = AsyncQdrantClient(**kwargs)
    return _client


def _point_id(thread_id: str) -> str:
    return str(uuid.uuid5(_NS, thread_id))


def _is_sent(folder: str) -> bool:
    f = (folder or "").lower()
    return any(kw in f for kw in ("sent", "gesendet", "gesendete"))


def _date_ts(email: dict) -> int:
    raw = email.get("date_sent") or ""
    try:
        return int(datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp())
    except Exception:
        return 0


def _thread_payload(thread_id: str, emails: list[dict]) -> dict:
    """Baut den Payload für einen Thread-Vektor.

    last_reply_text: Stefans letzte gesendete Antwort im Thread (für Prompt-Beispiele).
    has_reply: True wenn mindestens eine gesendete Nachricht im Thread vorhanden.
    """
    sorted_emails = sorted(emails, key=lambda e: e.get("date_sent") or "")

    last_sent = next(
        (e for e in reversed(sorted_emails) if _is_sent(e.get("folder", ""))),
        None,
    )
    if last_sent:
        body = last_sent.get("body_plain") or ""
        reply, _ = split_reply_from_quote(body)
        last_reply_text = (reply if reply else body)[:1500]
    else:
        last_reply_text = ""

    last = sorted_emails[-1]
    return {
        "thread_id": thread_id,
        "subject": sorted_emails[0].get("subject") or "",
        "last_reply_text": last_reply_text,
        "has_reply": bool(last_sent),
        "last_from_email": last.get("from_email") or "",
        "message_count": len(sorted_emails),
        "account_id": sorted_emails[0].get("account") or "",
        "date_ts": _date_ts(last),
    }


async def ensure_collection() -> None:
    if not settings.QDRANT_URL:
        return
    client = _get_client()
    existing = await client.get_collections()
    if COLLECTION not in {c.name for c in existing.collections}:
        await client.create_collection(
            collection_name=COLLECTION,
            vectors_config=VectorParams(size=EMBED_DIMS, distance=Distance.COSINE),
        )
        logger.info("Qdrant: collection '%s' created", COLLECTION)


async def upsert_thread(thread_id: str, emails: list[dict]) -> None:
    """Bettet einen Thread als einzelnen Vektor ein und upserted ihn in Qdrant."""
    if not settings.QDRANT_URL or not emails:
        return
    text = build_thread_embed_text(emails)
    if not text.strip():
        return
    vector = await embed_text(text)
    sorted_emails = sorted(emails, key=lambda e: e.get("date_sent") or "")
    payload = _thread_payload(thread_id, sorted_emails)
    await _get_client().upsert(
        collection_name=COLLECTION,
        points=[PointStruct(id=_point_id(thread_id), vector=vector, payload=payload)],
    )


async def upsert_threads_batch(threads: list[tuple[str, list[dict]]]) -> int:
    """Bettet eine Liste von (thread_id, emails)-Paaren als Batch ein."""
    if not settings.QDRANT_URL or not threads:
        return 0

    texts = [build_thread_embed_text(emails) for _, emails in threads]
    valid = [(t_id, emails, text) for (t_id, emails), text in zip(threads, texts) if text.strip()]
    if not valid:
        return 0

    vectors = await embed_batch([text for _, _, text in valid])

    points = [
        PointStruct(
            id=_point_id(t_id),
            vector=vector,
            payload=_thread_payload(
                t_id, sorted(emails, key=lambda e: e.get("date_sent") or "")
            ),
        )
        for (t_id, emails, _), vector in zip(valid, vectors)
    ]
    await _get_client().upsert(collection_name=COLLECTION, points=points)
    return len(points)


async def search_similar(text: str, limit: int = 5, only_with_reply: bool = True) -> list[dict]:
    """Sucht semantisch ähnliche Threads. Gibt Payloads mit Score zurück."""
    if not settings.QDRANT_URL:
        return []
    vector = await embed_text(text)
    query_filter = (
        Filter(must=[FieldCondition(key="has_reply", match=MatchValue(value=True))])
        if only_with_reply
        else None
    )
    results = await _get_client().search(
        collection_name=COLLECTION,
        query_vector=vector,
        query_filter=query_filter,
        limit=limit,
        with_payload=True,
    )
    return [{"score": r.score, **r.payload} for r in results]
