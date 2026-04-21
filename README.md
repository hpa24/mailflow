# Mailflow

E-Mail-Client auf Basis von FastAPI + PocketBase + Vanilla JS, deployed via Coolify.

**Dokumentation:** `/Users/hpa24ms1/Syncthing/Claude/Wissens-Dateien/HPA24/20_Apps/mailflow/`

| Datei | Inhalt |
|---|---|
| `README.md` | Architektur, Collections, Suche, Performance, Fallstricke, offene Punkte |
| `briefing.md` | Ursprüngliches Briefing, vollständiges PocketBase-Schema |
| `MAILFLOW-KIINTEGRATION-PLAN.md` (im Repo) | KI-Triage, Antwortvorschlag, Xano-Plan |

## Refactoring 2026-04-21

### API-Key-Schutz

Der API-Key wird nicht mehr im Frontend-Code gespeichert. Stattdessen:

- `backend/main.py` liefert `GET /config.js` — validiert den PocketBase-Token aus dem `Authorization`-Header gegen PocketBase auth-refresh. Nur eingeloggte User erhalten den echten Key, alle anderen bekommen einen leeren String.
- `frontend/js/api.js` lädt den Key lazy beim ersten API-Call via `_loadApiKey()` — schickt den PB-Token aus `localStorage['mf_auth']` mit. Das Ergebnis wird als Promise gecacht, sodass der Key nur einmal abgerufen wird.
- `/config.js` ist in der Auth-Middleware von der API-Key-Prüfung ausgenommen (Henne-Ei-Problem).

### Blockierende IMAP-Operationen in Executor ausgelagert

Vier Funktionen in `backend/main.py` blockierten den asyncio-Event-Loop direkt während IMAP-Verbindungen (1–5 s):

| Funktion | Rückgabe |
|---|---|
| `_imap_move_to_spam` | `(spam_folder, neue_uid)` |
| `_imap_move` | `neue_uid` |
| `_imap_trash` | — |
| `_imap_set_read` | — |

Lösung: synchrone IMAP-Logik in je eine `_*_sync`-Hilfsfunktion ausgelagert, async-Wrapper ruft sie per `await loop.run_in_executor(None, _*_sync, ...)` auf. Entspricht dem bereits etablierten Muster aus `imap_sync.py` und Draft-Append.
