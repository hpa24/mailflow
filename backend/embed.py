import logging

from openai import AsyncOpenAI

logger = logging.getLogger(__name__)

EMBED_MODEL = "text-embedding-3-small"
EMBED_DIMS = 1536

_client: AsyncOpenAI | None = None


def _get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        _client = AsyncOpenAI()
    return _client


def build_embed_text(email: dict) -> str:
    subject = email.get("subject") or ""
    body = email.get("body_plain") or ""
    return f"{subject}\n\n{body[:3000]}"


async def embed_text(text: str) -> list[float]:
    resp = await _get_client().embeddings.create(model=EMBED_MODEL, input=text)
    return resp.data[0].embedding


async def embed_batch(texts: list[str]) -> list[list[float]]:
    if not texts:
        return []
    resp = await _get_client().embeddings.create(model=EMBED_MODEL, input=texts)
    resp.data.sort(key=lambda x: x.index)
    return [d.embedding for d in resp.data]
