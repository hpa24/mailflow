"""
AI-Hilfsfunktionen für Mailflow:
- E-Mail-Kategorisierung (Triage)
- Antwortvorschläge generieren
- Entwürfe verfeinern
"""
import logging
import os
from functools import lru_cache
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
    """Lädt eine optionale Kontext-Datei (graceful fallback wenn nicht vorhanden)."""
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


def _load_triage_prompts_raw() -> str | None:
    return load_optional_context("triage_prompts.md")


@lru_cache(maxsize=1)
def load_triage_config() -> dict:
    """Parst triage_prompts.md und gibt Config-Dict zurück.

    Returns:
        {
          "categories": [{"slug": "focus", "name": "Fokus", "description": "..."}],
          "main_prompt": "...",
          "rule_extract_prompt": "...",
          "consolidation_prompt": "...",
        }
    Fällt auf eingebaute Defaults zurück wenn Datei fehlt.
    """
    raw = _load_triage_prompts_raw()
    if not raw:
        logger.warning("triage_prompts.md nicht gefunden — verwende eingebaute Defaults")
        return _default_triage_config()

    sections: dict[str, str] = {}
    current_key: str | None = None
    current_lines: list[str] = []

    for line in raw.splitlines():
        if line.startswith("## "):
            if current_key is not None:
                sections[current_key] = "\n".join(current_lines).strip()
            current_key = line[3:].strip()
            current_lines = []
        else:
            current_lines.append(line)
    if current_key is not None:
        sections[current_key] = "\n".join(current_lines).strip()

    # Kategorien parsen
    categories = []
    for line in sections.get("Kategorien", "").splitlines():
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) >= 4:
            categories.append({
                "key": parts[0],
                "slug": parts[1],
                "name": parts[2],
                "description": parts[3],
            })

    if not categories:
        logger.warning("Keine Kategorien in triage_prompts.md gefunden — verwende Defaults")
        return _default_triage_config()

    return {
        "categories": categories,
        "main_prompt": sections.get("Haupt-Kategorisierungsprompt", ""),
        "rule_extract_prompt": sections.get("Regelextraktions-Prompt", ""),
        "consolidation_prompt": sections.get("Konsolidierungs-Prompt", ""),
    }


def _default_triage_config() -> dict:
    return {
        "categories": [
            {"key": "kat1", "slug": "focus",      "name": "Fokus",  "description": "Tiefgehende fachliche Fragen oder komplexe Anliegen"},
            {"key": "kat2", "slug": "quick-reply", "name": "Schnell","description": "Kurze organisatorische Fragen, Terminbestätigungen"},
            {"key": "kat3", "slug": "office",      "name": "Office", "description": "Rechnungen, Buchhaltung, Verträge"},
            {"key": "kat4", "slug": "info-trash",  "name": "Info",   "description": "Newsletter, Werbung, automatische Benachrichtigungen"},
        ],
        "main_prompt": "",
        "rule_extract_prompt": "",
        "consolidation_prompt": "",
    }


def get_category_slugs() -> list[str]:
    return [c["slug"] for c in load_triage_config()["categories"]]


async def categorize_email(subject: str, body: str, from_email: str, rules: list[str] | None = None) -> str:
    """Kategorisiert eine einzelne E-Mail dynamisch anhand der konfigurierten Kategorien."""
    config = load_triage_config()
    categories = config["categories"]

    categories_block = "\n".join(
        f'{c["slug"]}: {c["description"]}' for c in categories
    )

    rules_block = ""
    if rules:
        lines = ["Gelernte Regeln für dieses Postfach:"]
        lines += [f"- {r}" for r in rules]
        rules_block = "\n".join(lines) + "\n\n"

    main_prompt_template = config.get("main_prompt", "")
    if main_prompt_template:
        safe_body = (body[:800] if body else "(kein Inhalt)").replace("{", "{{").replace("}", "}}")
        prompt = main_prompt_template.format(
            n=len(categories),
            categories_block=categories_block,
            rules_block=rules_block,
            from_email=from_email,
            subject=subject.replace("{", "{{").replace("}", "}}"),
            body=safe_body,
        )
    else:
        # Fallback-Prompt wenn Template leer
        slugs = ", ".join(c["slug"] for c in categories)
        prompt = (
            f"Klassifiziere diese E-Mail in genau eine der folgenden {len(categories)} Kategorien:\n\n"
            f"{categories_block}\n\n"
            f"{rules_block}"
            f"Von: {from_email}\nBetreff: {subject}\nInhalt (gekürzt): {body[:800] if body else '(kein Inhalt)'}\n\n"
            f"Antworte NUR mit dem Kategorie-Slug ({slugs}). Kein Satzzeichen, kein Erklärungstext."
        )

    response = await client.messages.create(
        model=MODEL,
        max_tokens=20,
        messages=[{"role": "user", "content": prompt}],
    )
    result = response.content[0].text.strip().lower()

    valid = set(get_category_slugs())
    if result not in valid:
        for slug in valid:
            if slug in result:
                return slug
        logger.warning("Unbekannte Kategorie '%s', Fallback auf letzte Kategorie", result)
        return categories[-1]["slug"]

    return result


async def extract_rule(from_email: str, subject: str, body_snippet: str, category_slug: str) -> str:
    """Extrahiert eine allgemeine Lernregel aus einer manuellen Korrektur."""
    config = load_triage_config()
    categories = config["categories"]

    category_name = next(
        (c["name"] for c in categories if c["slug"] == category_slug),
        category_slug
    )

    template = config.get("rule_extract_prompt", "")
    if template:
        prompt = template.format(
            from_email=from_email,
            subject=subject.replace("{", "{{").replace("}", "}}"),
            body_snippet=body_snippet[:300].replace("{", "{{").replace("}", "}}"),
            category_name=category_name,
            category_slug=category_slug,
        )
    else:
        prompt = (
            f'Leite aus dieser manuellen E-Mail-Korrektur eine allgemeine Regel ab.\n\n'
            f'E-Mail: Von {from_email}, Betreff: "{subject}", Inhalt: {body_snippet[:300]}\n'
            f'Korrekte Kategorie: {category_name}\n\n'
            f'Schreibe eine einzige kurze Regel (max. 15 Wörter). Nur die Regel, kein Erklärungstext.'
        )

    response = await client.messages.create(
        model=MODEL,
        max_tokens=60,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text.strip()


async def consolidate_rules(rules: list[str], category_slug: str) -> list[str]:
    """Konsolidiert ≥15 Regeln auf max. 7 Kernregeln."""
    config = load_triage_config()
    categories = config["categories"]

    category_name = next(
        (c["name"] for c in categories if c["slug"] == category_slug),
        category_slug
    )

    rules_list = "\n".join(rules)
    template = config.get("consolidation_prompt", "")
    if template:
        prompt = template.format(
            n=len(rules),
            category_name=category_name,
            rules_list=rules_list.replace("{", "{{").replace("}", "}}"),
        )
    else:
        prompt = (
            f'Fasse diese {len(rules)} Lernregeln für Kategorie "{category_name}" zu maximal 7 Kernregeln zusammen.\n'
            f'Behalte nur die wichtigsten, allgemeinsten Muster. Eliminiere Duplikate.\n'
            f'Gib jede Regel auf einer eigenen Zeile aus — keine Nummerierung, kein Bindestrich.\n\n'
            f'Regeln:\n{rules_list}'
        )

    response = await client.messages.create(
        model=MODEL,
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}],
    )
    consolidated = [
        line.strip().lstrip("-").strip()
        for line in response.content[0].text.strip().splitlines()
        if line.strip()
    ]
    return consolidated[:7]


async def suggest_reply(
    email: dict,
    thread_emails: list,
    contact_history: list,
    tone: str = "neutral",
) -> str:
    """Generiert einen Antwortvorschlag auf eine E-Mail."""
    company_knowledge = load_optional_context("company_knowledge.md")
    tonality_profiles = load_optional_context("tonality_profiles.md")

    tone_descriptions = {
        "neutral": "sachlich und klar",
        "formal": "formell und professionell (Sie-Ansprache)",
        "friendly": "freundlich und persönlich",
        "short": "sehr kurz und prägnant (maximal 3 Sätze)",
    }
    tone_desc = tone_descriptions.get(tone, "sachlich und klar")

    sections = []
    sections.append(f"Du schreibst eine E-Mail-Antwort auf Deutsch (du-Ansprache, außer bei formal). Ton: {tone_desc}.")
    sections.append("Gib NUR den E-Mail-Text zurück — keine Betreffzeile, keine Anrede 'Hier ist mein Vorschlag:', keine Meta-Kommentare.")

    if company_knowledge:
        sections.append(f"\n## Unternehmenskontext\n{company_knowledge}")
    if tonality_profiles:
        sections.append(f"\n## Tonalitätsprofile\n{tonality_profiles}")

    if thread_emails:
        thread_text = "\n\n---\n\n".join(
            f"Von: {e.get('from_email', '')}\nBetreff: {e.get('subject', '')}\n\n{(e.get('body_plain') or '')[:600]}"
            for e in thread_emails[-5:]
        )
        sections.append(f"\n## Bisheriger Thread-Verlauf\n{thread_text}")

    if contact_history:
        history_text = "\n\n---\n\n".join(
            f"Von: {e.get('from_email', '')}\nBetreff: {e.get('subject', '')}\n\n{(e.get('body_plain') or '')[:300]}"
            for e in contact_history
        )
        sections.append(f"\n## Frühere E-Mails von diesem Absender\n{history_text}")

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
    """Verfeinert einen bestehenden E-Mail-Entwurf."""
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
