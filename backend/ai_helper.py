"""
AI-Hilfsfunktionen für Mailflow:
- E-Mail-Kategorisierung (Triage)
- Antwortvorschläge generieren
- Entwürfe verfeinern
"""
import logging
import os
from pathlib import Path

from anthropic import AsyncAnthropic

logger = logging.getLogger(__name__)

client = AsyncAnthropic()  # Liest ANTHROPIC_API_KEY automatisch aus der Umgebung

MODEL = "claude-haiku-4-5-20251001"

# Suchpfade für optionale Kontext-Dateien
_CONTEXT_SEARCH_PATHS = [
    Path("/app"),
    Path(__file__).parent,
]


def load_optional_context(filename: str) -> str | None:
    """Lädt eine optionale Kontext-Datei (graceful fallback wenn nicht vorhanden).

    Sucht zuerst in /app/{filename}, dann im Projektverzeichnis.
    Gibt None zurück wenn die Datei nicht existiert (kein Fehler).
    """
    for base in _CONTEXT_SEARCH_PATHS:
        path = base / filename
        if path.is_file():
            try:
                content = path.read_text(encoding="utf-8").strip()
                if content:
                    logger.debug("Kontext-Datei geladen: %s", path)
                    return content
            except Exception as exc:
                logger.warning("Kontext-Datei '%s' konnte nicht gelesen werden: %s", path, exc)
    return None


async def categorize_email(subject: str, body: str, from_email: str) -> str:
    """Kategorisiert eine einzelne E-Mail in eine von vier Kategorien.

    Gibt zurück: "focus" | "quick-reply" | "office" | "info-trash"
    """
    prompt = f"""Klassifiziere diese E-Mail in genau eine der folgenden 4 Kategorien:

focus: Tiefgehende fachliche Fragen oder komplexe Anliegen, die volle Aufmerksamkeit erfordern
quick-reply: Kurze organisatorische Fragen, Terminbestätigungen oder einfache Bestätigungen
office: Rechnungen, Buchhaltung, Verträge, geschäftliche Unterlagen und Dokumente
info-trash: Newsletter, Werbung, automatische Benachrichtigungen ohne direkten Handlungsbedarf

Von: {from_email}
Betreff: {subject}
Inhalt (gekürzt): {body[:800] if body else "(kein Inhalt)"}

Antworte NUR mit dem Kategorie-Namen (focus, quick-reply, office oder info-trash). Kein Satzzeichen, kein Erklärungstext."""

    response = await client.messages.create(
        model=MODEL,
        max_tokens=20,
        messages=[{"role": "user", "content": prompt}],
    )
    result = response.content[0].text.strip().lower()

    # Validierung — Fallback auf "info-trash" bei unerwartetem Wert
    valid = {"focus", "quick-reply", "office", "info-trash"}
    if result not in valid:
        # Versuche partiellen Match
        for cat in valid:
            if cat in result:
                return cat
        logger.warning("Unbekannte Kategorie '%s', Fallback auf 'info-trash'", result)
        return "info-trash"

    return result


async def suggest_reply(
    email: dict,
    thread_emails: list,
    contact_history: list,
    tone: str = "neutral",
) -> str:
    """Generiert einen Antwortvorschlag auf eine E-Mail.

    Args:
        email: Die E-Mail, auf die geantwortet wird
        thread_emails: Alle anderen E-Mails im Thread (chronologisch)
        contact_history: Letzte 5 E-Mails vom selben Absender (außerhalb Thread)
        tone: "neutral" | "formal" | "friendly" | "short"

    Returns:
        Nur der E-Mail-Text (kein Betreff, keine Meta-Kommentare)
    """
    # Optionale Kontext-Dateien laden
    company_knowledge = load_optional_context("company_knowledge.md")
    tonality_profiles = load_optional_context("tonality_profiles.md")

    # Ton-Beschreibungen
    tone_descriptions = {
        "neutral": "sachlich und klar",
        "formal": "formell und professionell (Sie-Ansprache)",
        "friendly": "freundlich und persönlich",
        "short": "sehr kurz und prägnant (maximal 3 Sätze)",
    }
    tone_desc = tone_descriptions.get(tone, "sachlich und klar")

    # Prompt-Abschnitte dynamisch aufbauen
    sections = []

    sections.append(f"Du schreibst eine E-Mail-Antwort auf Deutsch (du-Ansprache, außer bei formal). Ton: {tone_desc}.")
    sections.append("Gib NUR den E-Mail-Text zurück — keine Betreffzeile, keine Anrede 'Hier ist mein Vorschlag:', keine Meta-Kommentare.")

    if company_knowledge:
        sections.append(f"\n## Unternehmenskontext\n{company_knowledge}")

    if tonality_profiles:
        sections.append(f"\n## Tonalitätsprofile\n{tonality_profiles}")

    # Thread-Kontext
    if thread_emails:
        thread_text = "\n\n---\n\n".join(
            f"Von: {e.get('from_email', '')}\nBetreff: {e.get('subject', '')}\n\n{(e.get('body_plain') or '')[:600]}"
            for e in thread_emails[-5:]  # max. 5 Thread-Mails im Kontext
        )
        sections.append(f"\n## Bisheriger Thread-Verlauf\n{thread_text}")

    # Kontakthistorie
    if contact_history:
        history_text = "\n\n---\n\n".join(
            f"Von: {e.get('from_email', '')}\nBetreff: {e.get('subject', '')}\n\n{(e.get('body_plain') or '')[:300]}"
            for e in contact_history
        )
        sections.append(f"\n## Frühere E-Mails von diesem Absender\n{history_text}")

    # Die zu beantwortende E-Mail
    sections.append(
        f"\n## Zu beantwortende E-Mail\n"
        f"Von: {email.get('from_email', '')}\n"
        f"Betreff: {email.get('subject', '')}\n\n"
        f"{(email.get('body_plain') or '')[:1200]}"
    )

    sections.append("\n## Aufgabe\nSchreibe jetzt die Antwort (nur den E-Mail-Text, ohne Betreff und ohne Kommentar):")

    prompt = "\n".join(sections)

    response = await client.messages.create(
        model=MODEL,
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text.strip()


async def refine_reply(text: str, instruction: str) -> str:
    """Verfeinert einen bestehenden E-Mail-Entwurf.

    Args:
        text: Der bestehende Entwurfstext
        instruction: z.B. "kürzer", "ausführlicher", "persönlicher gruß", oder ein Tonalitätswechsel

    Returns:
        Nur der verfeinerte Text (kein Meta-Kommentar)
    """
    prompt = f"""Überarbeite den folgenden E-Mail-Entwurf gemäß dieser Anweisung: "{instruction}"

Gib NUR den überarbeiteten E-Mail-Text zurück — keine Erklärungen, keine Meta-Kommentare, kein "Hier ist die überarbeitete Version:".

## Aktueller Entwurf
{text}

## Überarbeiteter Text:"""

    response = await client.messages.create(
        model=MODEL,
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text.strip()
