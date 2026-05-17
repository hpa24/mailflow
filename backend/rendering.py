"""Template-Rendering-Pipeline.

Zwei Phasen:
- Phase 1 (Pre-Compose): Sections strippen + Snippets aufloesen + globale
  Variablen ersetzen. Kontakt-Variablen ({{name}}, {{email}}) bleiben als
  Platzhalter.
- Phase 2 (Pre-Send pro Empfaenger): Kontakt-Variablen ersetzen.

Beim Direkt-Versand laufen beide Phasen am Stueck pro Empfaenger.

Syntax:
- {{variable}}     -> Lookup in email_variables; bei contact != None
                      auch in Kontakt-Feldern (name, email).
- {{> snippet}}    -> Lookup in email_snippets, HTML-Inline-Inject.
- <!-- @section X -->...<!-- @end -->  -> Bereich, der per active_sections
                      ein/ausgeblendet werden kann.

Unbekannte Variablen/Snippets bleiben als Original-Platzhalter stehen
(weiches Strict). Aufrufer kann hart pruefen via find_unresolved().
"""

import re
from typing import Optional

import pb_client

_SECTION_RE = re.compile(
    r"<!--\s*@section\s+([\w.-]+)(?:\s+if=([\w.:-]+))?\s*-->(.*?)<!--\s*@end\s*-->",
    re.DOTALL,
)
_PLACEHOLDER_RE = re.compile(r"\{\{\s*(>?)\s*([\w.]+)\s*\}\}")
_KONTAKT_VARS = {"name", "email"}


async def load_snippets_map() -> dict:
    """Lade alle Snippets als {name: html}-Dict."""
    data = await pb_client.pb_get(
        "/api/collections/email_snippets/records",
        params={"perPage": 500},
    )
    return {s["name"]: s.get("html") or "" for s in data.get("items", [])}


async def load_variables_map() -> dict:
    """Lade alle globalen Variablen als {name: value}-Dict."""
    data = await pb_client.pb_get(
        "/api/collections/email_variables/records",
        params={"perPage": 500},
    )
    return {v["name"]: v.get("value") or "" for v in data.get("items", [])}


def strip_sections(html: str, active_sections: Optional[list]) -> str:
    """Entfernt deaktivierte Sections. Marker selbst werden immer entfernt,
    Inhalt bleibt nur wenn Section aktiv. active_sections=None -> alle aktiv."""
    active_set = set(active_sections) if active_sections is not None else None

    def replace(m: re.Match) -> str:
        section_id = m.group(1)
        # if= ist Zukunfts-Vorbereitung fuer Rollen-basierte Sections; heute ignoriert
        body = m.group(3)
        if active_set is None or section_id in active_set:
            return body
        return ""

    return _SECTION_RE.sub(replace, html)


def resolve_snippets(html: str, snippets: dict) -> str:
    """Loest {{> name}} auf. Einmaliger Pass; Snippets enthalten selbst keine
    Snippet-Refs (kein Re-Resolve, vermeidet Rekursion)."""
    def replace(m: re.Match) -> str:
        is_snippet = bool(m.group(1))
        if not is_snippet:
            return m.group(0)
        name = m.group(2)
        return snippets.get(name, m.group(0))

    return _PLACEHOLDER_RE.sub(replace, html)


def resolve_variables(
    text: str,
    variables: dict,
    contact: Optional[dict] = None,
) -> str:
    """Ersetzt {{name}} mit Variablen-Werten oder Kontakt-Feldern (falls
    contact gesetzt). Unbekannte Variablen bleiben als Platzhalter."""
    def replace(m: re.Match) -> str:
        is_snippet = bool(m.group(1))
        if is_snippet:
            return m.group(0)
        name = m.group(2)
        if contact is not None and name in _KONTAKT_VARS:
            return contact.get(name) or ""
        if name in variables:
            return variables[name]
        # Wenn Kontakt-Var ohne Kontakt: bleibt Platzhalter
        return m.group(0)

    return _PLACEHOLDER_RE.sub(replace, text)


def render_phase1(
    html: str,
    snippets: dict,
    variables: dict,
    active_sections: Optional[list] = None,
) -> str:
    """Sections strippen + Snippets aufloesen + globale Vars ersetzen.
    Kontakt-Variablen bleiben als Platzhalter."""
    html = strip_sections(html, active_sections)
    html = resolve_snippets(html, snippets)
    html = resolve_variables(html, variables, contact=None)
    return html


def render_phase2(text: str, variables: dict, contact: dict) -> str:
    """Kontakt-Variablen ersetzen (idempotent fuer alles andere)."""
    return resolve_variables(text, variables, contact=contact)


def render_full(
    html: str,
    snippets: dict,
    variables: dict,
    active_sections: Optional[list] = None,
    contact: Optional[dict] = None,
) -> str:
    """Phase 1 + Phase 2 in einem Rutsch (fuer Direkt-Versand pro Empfaenger
    oder Live-Preview im Editor)."""
    out = render_phase1(html, snippets, variables, active_sections)
    if contact is not None:
        out = render_phase2(out, variables, contact)
    return out


def find_unresolved(text: str) -> list:
    """Liste aller Variablen-Platzhalter, die im Text uebrig geblieben sind."""
    found = []
    for m in _PLACEHOLDER_RE.finditer(text):
        is_snippet = bool(m.group(1))
        name = m.group(2)
        found.append({
            "type": "snippet" if is_snippet else "variable",
            "name": name,
            "placeholder": m.group(0),
        })
    return found
