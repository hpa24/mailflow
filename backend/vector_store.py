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
from embed import EMBED_DIMS, build_embed_text, embed_batch, embed_text, split_reply_from_quote

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


def _point_id(pb_id: str) -> str:
    return str(uuid.uuid5(_NS, pb_id))


def _is_sent(folder: str) -> bool:
    f = (folder or "").lower()
    return any(kw in f for kw in ("sent", "gesendet", "gesendete"))


def _date_ts(email: dict) -> int:
    raw = email.get("date_sent") or ""
    try:
        return int(datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp())
    except Exception:
        return 0


def _payload(email: dict) -> dict:
    body = email.get("body_plain") or ""
    reply, _ = split_reply_from_quote(body)
    return {
        "pb_id": email["id"],
        "account_id": email.get("account", ""),
        "folder": email.get("folder", ""),
        "is_sent": _is_sent(email.get("folder", "")),
        "thread_id": email.get("thread_id", ""),
        "from_email": email.get("from_email", ""),
        "subject": email.get("subject", ""),
        "snippet": email.get("snippet", ""),
        "date_ts": _date_ts(email),
        "reply_text": reply[:1500] if reply else "",
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


async def upsert_email(email: dict) -> None:
    if not settings.QDRANT_URL:
        return
    text = build_embed_text(email)
    if not text.strip():
        return
    vector = await embed_text(text)
    await _get_client().upsert(
        collection_name=COLLECTION,
        points=[PointStruct(id=_point_id(email["id"]), vector=vector, payload=_payload(email))],
    )


async def upsert_emails_batch(emails: list[dict]) -> int:
    if not settings.QDRANT_URL:
        return 0
    pairs = [(e, build_embed_text(e)) for e in emails]
    pairs = [(e, t) for e, t in pairs if t.strip()]
    if not pairs:
        return 0

    vectors = await embed_batch([t for _, t in pairs])
    points = [
        PointStruct(id=_point_id(e["id"]), vector=v, payload=_payload(e))
        for (e, _), v in zip(pairs, vectors)
    ]
    await _get_client().upsert(collection_name=COLLECTION, points=points)
    return len(points)


async def search_similar(text: str, limit: int = 5, only_sent: bool = True) -> list[dict]:
    if not settings.QDRANT_URL:
        return []
    vector = await embed_text(text)
    query_filter = (
        Filter(must=[FieldCondition(key="is_sent", match=MatchValue(value=True))])
        if only_sent
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
