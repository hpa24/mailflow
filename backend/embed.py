import logging
import re

from openai import AsyncOpenAI

logger = logging.getLogger(__name__)

EMBED_MODEL = "text-embedding-3-small"
EMBED_DIMS = 1536

_client: AsyncOpenAI | None = None

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


def build_thread_embed_text(emails: list[dict]) -> str:
    """Erstellt den Embedding-Text für einen ganzen Thread.

    Format: Betreff oben, dann chronologisch jede Nachricht als [from] + eigenem Text.
    Jede Nachricht wird auf max. 600 Zeichen begrenzt; Gesamtbudget 4000 Zeichen.
    """
    if not emails:
        return ""

    sorted_emails = sorted(emails, key=lambda e: e.get("date_sent") or "")
    subject = sorted_emails[0].get("subject") or ""
    parts = [subject]
    budget = 4000 - len(subject)

    for email in sorted_emails:
        if budget < 80:
            break
        from_email = email.get("from_email") or ""
        body = email.get("body_plain") or ""
        reply, _ = split_reply_from_quote(body)
        text = (reply if reply else body).strip()
        if not text:
            continue
        entry = f"[{from_email}]\n{text[:min(600, budget - len(from_email) - 4)]}"
        parts.append(entry)
        budget -= len(entry)

    return "\n\n---\n\n".join(parts)


async def embed_text(text: str) -> list[float]:
    resp = await _get_client().embeddings.create(model=EMBED_MODEL, input=text)
    return resp.data[0].embedding


async def embed_batch(texts: list[str]) -> list[list[float]]:
    if not texts:
        return []
    resp = await _get_client().embeddings.create(model=EMBED_MODEL, input=texts)
    resp.data.sort(key=lambda x: x.index)
    return [d.embedding for d in resp.data]
