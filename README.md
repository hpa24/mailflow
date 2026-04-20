# Mailflow

E-Mail-Client auf Basis von FastAPI + PocketBase + Vanilla JS, deployed via Coolify.

## Architektur

| Schicht | Technologie | URL (Prod) |
|---|---|---|
| Frontend | nginx + Vanilla JS | https://mailflow.barres.de |
| Backend | FastAPI (Python 3.11) | https://mailflow-api.barres.de |
| Datenbank | PocketBase (SQLite) | https://mailflow-pb.barres.de |

**Deployment:** GitHub Push → Coolify Auto-Deploy (kein lokales Docker nötig)

## Wichtige Dateien

| Datei | Inhalt |
|---|---|
| `backend/main.py` | FastAPI-Endpoints, CORS/Auth-Middleware, Lifespan |
| `backend/imap_sync.py` | IMAP-Sync (inkrementell, Flag-Sync, Ordner-Normierung) |
| `backend/idle_manager.py` | IMAP IDLE pro Account (asyncio-Task, SSE-Notification) |
| `backend/fts.py` | FTS5-Volltext-Index (setup, insert, delete, search, rebuild) |
| `backend/pb_setup.py` | PocketBase-Schema + DB-Indizes beim Start anlegen |
| `backend/pb_client.py` | PocketBase HTTP-Client (Token-Auth + Auto-Refresh alle 55 min) |
| `backend/scheduler.py` | APScheduler: Flag-Sync + Ordner alle 2 Minuten |
| `backend/backfill.py` | Einmalige Start-Tasks: Backfill, FTS-Rebuild, HTML-Backfill |
| `backend/smtp_sender.py` | SMTP-Versand + IMAP APPEND in Sent-Ordner |
| `backend/ai_helper.py` | Claude Haiku: Kategorisierung, Antwortvorschlag, Refinement |
| `frontend/js/inbox.js` | Gesamte Frontend-Logik (~2000 Zeilen) |
| `frontend/js/api.js` | API-Wrapper (alle Backend-Calls, X-API-Key-Header) |

## Suche

Die Suche nutzt einen **FTS5-SQLite-Index** (`/app/fts/fts.db`) für Volltext über Subject, Body, Absender:

- **Einzelwort:** direkte FTS5-Suche (`felix`)
- **Mehrwort:** zuerst Phrase-Suche (`"felix kugel"`), Fallback AND-Suche
- **Fallback:** wenn FTS5 leer (z.B. nach Container-Neustart während Rebuild), sucht der Endpoint direkt in PocketBase auf `subject`, `from_email`, `from_name` — kein `body_plain`, damit keine Treffer aus zitierten Texten in Threads auftauchen

**Wichtig:** Der FTS5-Index liegt im Backend-Container. Bei jedem Neustart ist er initial leer und wird via `rebuild_fts_if_needed()` im Hintergrund neu aufgebaut (da Marker-File `/tmp/mailflow_fts_rebuilt` ephemer). Erst nach dem Rebuild sind alle alten E-Mails durchsuchbar — neue werden direkt beim Sync eingefügt.

**Bind Mount in Coolify:** `/root/mailflow/fts` → `/app/fts` im Backend-Container.

**Frontend-Cache-Fix:** `_getFromCache()` in `inbox.js` gibt beim aktiven Suchquery immer `null` zurück, damit nicht der gecachte Ordnerinhalt statt der Suchergebnisse angezeigt wird.

## Ladegeschwindigkeit

**Zweistufiges Laden:**
1. **Stage 1:** 50 E-Mails sofort anzeigen (`FIRST_PAGE_SIZE = 50`) → schnelle initiale Anzeige
2. **Stage 2:** Restliche Seiten parallel laden (bis `PAGE_SIZE = 500`), Thread-Gruppierung danach erneut berechnen

**Ordner-Cache:** Geladene Ordner bleiben 3 Minuten im Speicher (`_folderCache`). Rückkehr zu bereits geladenem Ordner ist instant, kein API-Call.

**DB-Indizes** (via `pb_setup.py` beim Start angelegt):
```sql
idx_emails_account_folder_date         ON emails (account, folder, date_sent DESC)
idx_emails_account_folder_read_date    ON emails (account, folder, is_read, date_sent DESC)
```

## Optimistic UI

Alle schreibenden Aktionen aktualisieren das UI sofort, ohne auf die API-Antwort zu warten. Bei Fehler wird der alte Zustand wiederhergestellt (Rollback):

| Aktion | Optimistic Update |
|---|---|
| Gelesen/Ungelesen | E-Mail-Badge sofort, Sidebar-Zähler ±1, Tab-Titel |
| Löschen | E-Mail sofort aus Liste, Sidebar-Zähler −1 wenn ungelesen |
| Spam | E-Mail sofort aus Liste, Sidebar-Zähler −1 wenn ungelesen |
| In Ordner verschieben | E-Mail sofort aus Liste, als gelesen markiert, Sidebar ±1 |

**Tab-Titel:** Zeigt Gesamtzahl ungelesener E-Mails: `Mailflow – 7`. Wird nach jeder Zähler-Änderung aktualisiert.

## Ungelesen-Zähler

`folders.unread_count` in PocketBase ist ein gecachter Wert (kein Live-Compute). Er wird bei allen schreibenden Aktionen sofort aktualisiert:

- `_update_folder_unread_count(account_id, folder)` — gemeinsame Hilfsfunktion in `main.py`
- Wird aufgerufen nach: `mark_read`, `bulk_mark_read`, `move_to_spam`, `move_email`, `delete_email`
- **IMAP-Sync:** `_count_unread()` in `imap_sync.py` liest aus PocketBase (nicht IMAP UNSEEN), damit Flag-Sync-Fenster keine Phantomwerte erzeugt

## Thread-Gruppierung

Thread-Gruppen werden serverseitig per Batch berechnet. Betreffe werden normiert (Re:/Fwd: entfernt, Leerzeichen minimiert). Gruppen werden gebildet wenn normierter Betreff **länger als 1 Zeichen** ist (`> 1` Schwellwert) und Absender übereinstimmen.

E-Mails werden beim Verschieben in einen anderen Ordner automatisch als **gelesen** markiert.

## IMAP IDLE + SSE

- `idle_manager.py` hält pro Account eine persistente IMAP-IDLE-Verbindung (28-min Timeout, dann Reconnect)
- Neue E-Mails triggern `GET /events` (Server-Sent Events) → `silentRefresh()` im Frontend
- `_refreshing`-Flag verhindert parallele Refreshes (Race Condition)
- Polling (alle 2 Min) als Fallback für Flag-Sync, Sent/Drafts/Trash

## Bekannte Eigenheiten

- `PocketBase max=0` = Default-Limit (5000 Zeichen), NICHT unbegrenzt. Für unbegrenzte Felder: `max=999_999_999_999_999` (als `MAX_UNLIMITED` in `pb_setup.py`)
- PocketBase-Auth-Token läuft nach 1h ab → `pb_client.py` erneuert alle 55 min + 401-Fallback
- Temporäre Anhänge (Compose) liegen im RAM (max. 25 MB), gehen bei Neustart verloren — by design
- EventSource `?key=...` Query-Parameter statt Header (EventSource-API unterstützt keine Custom-Header)
