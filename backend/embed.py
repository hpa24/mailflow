import logging
import re

from openai import AsyncOpenAI

logger = logging.getLogger(__name__)

EMBED_MODEL = "text-embedding-3-small"
EMBED_DIMS = 1536

_client: AsyncOpenAI | None = None

# Marker für den Beginn der zitierten Original-Mail
_QUOTE_PATTERNS = [
    re.compile(r"^Am .{5,100} schrieb ", re.IGNORECASE),
    re.compile(r"^On .{5,100} wrote:", re.IGNORECASE),
    re.compile(r"^-----+\s*(Ursprüngliche Nachricht|Original Message)", re.IGNORECASE),
    re.compile(r"^Von:\s+\S", re.IGNORECASE),
    re.compile(r"^From:\s+\S", re.IGNORECASE),
    re.compile(r"^>{1,3}\s"),
    re.compile(r"^_{10,}"),
]


def _get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        _client = AsyncOpenAI()
    return _client


def split_reply_from_quote(body: str) -> tuple[str, str]:
    """Trennt Stefans Antworttext vom zitierten Original.
    Gibt (reply, quoted) zurück."""
    lines = body.splitlines()
    for i, line in enumerate(lines):
        for pattern in _QUOTE_PATTERNS:
            if pattern.match(line):
                return "\n".join(lines[:i]).strip(), "\n".join(lines[i:]).strip()
    return body.strip(), ""


def build_embed_text(email: dict) -> str:
    """Erstellt den Embedding-Text aus Betreff + Stefans eigenem Antworttext."""
    subject = email.get("subject") or ""
    body = email.get("body_plain") or ""
    reply, _ = split_reply_from_quote(body)
    text = reply if reply else body[:2000]
    return f"{subject}\n\n{text[:2000]}"


async def embed_text(text: str) -> list[float]:
    resp = await _get_client().embeddings.create(model=EMBED_MODEL, input=text)
    return resp.data[0].embedding


async def embed_batch(texts: list[str]) -> list[list[float]]:
    if not texts:
        return []
    resp = await _get_client().embeddings.create(model=EMBED_MODEL, input=texts)
    resp.data.sort(key=lambda x: x.index)
    return [d.embedding for d in resp.data]
