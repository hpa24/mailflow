# Mailflow

E-Mail-Client auf Basis von FastAPI + PocketBase + Vanilla JS, deployed via Coolify.

---

## Architektur

| Schicht | Technologie | URL (Prod) |
|---|---|---|
| Frontend | nginx + Vanilla JS | https://mailflow.barres.de |
| Backend | FastAPI (Python 3.11) | https://mailflow-api.barres.de |
| Datenbank | PocketBase (SQLite) | https://mailflow-pb.barres.de |

**Deployment:** GitHub Push → Coolify Auto-Deploy (kein lokales Docker nötig)

---

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

---

## Collections in PocketBase

- **accounts**: imap_host, imap_port, imap_user, imap_pass, from_email, from_name, signature, color_tag, reply_to_email
- **emails**: account (rel), imap_uid, uidvalidity, folder, message_id (UNIQUE), thread_id, in_reply_to, from_email, from_name, reply_to, to_emails, cc_emails, subject, body_plain, body_html, snippet, date_sent, is_read, is_flagged, is_answered, ai_category (select), has_attachments
- **attachments**: email (rel, cascade-delete), filename, mime_type, size_bytes, part_id
- **folders**: account (rel), imap_path, display_name, unread_count, last_sync_uid, uidvalidity, no_select
- **smtp_servers**: name, host, port, user, password, use_tls, use_starttls, is_default
- **contacts**: email (UNIQUE), name, email_count, last_contact
- **triage_rules**: Gelernte KI-Regeln (aus Nutzerfeedback konsolidiert)

---

## Suche

Volltext via **FTS5-SQLite-Index** (`/app/fts/fts.db`):

- **Einzelwort:** direkte FTS5-Suche (`felix`)
- **Mehrwort:** zuerst Phrase-Suche (`"felix kugel"`), Fallback AND-Suche
- **Fallback:** wenn FTS5 leer (z.B. nach Container-Neustart während Rebuild), sucht der Endpoint direkt in PocketBase auf `subject`, `from_email`, `from_name` — kein `body_plain`, damit keine Treffer aus zitierten Texten in Threads

Der FTS5-Index liegt im Backend-Container. Bei jedem Neustart ist er initial leer und wird via `rebuild_fts_if_needed()` im Hintergrund neu aufgebaut. Bind Mount in Coolify: `/root/mailflow/fts` → `/app/fts`.

**Frontend-Cache:** `_getFromCache()` in `inbox.js` gibt bei aktivem Suchquery immer `null` zurück, damit nicht der gecachte Ordnerinhalt statt der Suchergebnisse erscheint.

---

## Ladegeschwindigkeit

**Zweistufiges Laden:**
1. Stage 1: 50 E-Mails sofort anzeigen (`FIRST_PAGE_SIZE = 50`) → schnelle initiale Anzeige
2. Stage 2: Restliche Seiten parallel laden (bis `PAGE_SIZE = 500`), Threading danach neu berechnet

**Ordner-Cache:** Geladene Ordner bleiben 3 Minuten im Speicher (`_folderCache`). Rückkehr zu bereits geladenem Ordner ist instant, kein API-Call. Cache wird bei aktiver Suche übersprungen.

**DB-Indizes** (via `pb_setup.py` beim Start angelegt):
```sql
idx_emails_account_folder_date         ON emails (account, folder, date_sent DESC)
idx_emails_account_folder_read_date    ON emails (account, folder, is_read, date_sent DESC)
```

---

## Optimistic UI

Alle schreibenden Aktionen aktualisieren das UI sofort ohne Warten auf API-Antwort. Bei Fehler: Rollback.

| Aktion | Optimistic Update |
|---|---|
| Gelesen/Ungelesen | E-Mail-Badge sofort, Sidebar-Zähler ±1, Tab-Titel |
| Löschen | E-Mail sofort aus Liste, Sidebar-Zähler −1 wenn ungelesen |
| Spam | E-Mail sofort aus Liste, Sidebar-Zähler −1 wenn ungelesen |
| In Ordner verschieben | E-Mail sofort aus Liste, als gelesen markiert, Sidebar ±1 |

**Tab-Titel:** Zeigt Gesamtzahl ungelesener E-Mails: `Mailflow – 7`.

---

## Ungelesen-Zähler

`folders.unread_count` ist ein gecachter Wert. Wird bei allen schreibenden Aktionen sofort aktualisiert via `_update_folder_unread_count(account_id, folder)` — aufgerufen nach `mark_read`, `bulk_mark_read`, `move_to_spam`, `move_email`, `delete_email`. `_count_unread()` liest aus PocketBase (nicht IMAP UNSEEN).

---

## Thread-Gruppierung

Betreffe werden normiert (Re:/Fwd: entfernt). Gruppen bei normiertem Betreff länger als 1 Zeichen + Absender-Match. E-Mails werden beim Verschieben in einen anderen Ordner automatisch als gelesen markiert.

---

## IMAP IDLE + SSE

- `idle_manager.py`: persistente IMAP-IDLE-Verbindung pro Account, 28-min Timeout, dann Reconnect
- Neue E-Mails → `GET /events` (Server-Sent Events) → `silentRefresh()` im Frontend
- `_refreshing`-Flag verhindert parallele Refreshes
- Polling (alle 2 Min) als Fallback für Flag-Sync, Sent/Drafts/Trash

---

## Frontend-Konstanten

- `FIRST_PAGE_SIZE = 50` — erste Seite für sofortige Anzeige
- `PAGE_SIZE = 500` — vollständige Seite
- `MAX_AUTO_LOAD = 1500` — maximale automatisch geladene E-Mails
- `FLAG_SYNC_WINDOW = 200` — letzte N UIDs per Flag-Sync abgeglichen
- `FOLDER_CACHE_TTL = 180_000` — Ordner-Cache 3 Minuten

---

## API-Authentifizierung

- `API_KEY` als Coolify Environment Variable
- Frontend sendet Key als `X-API-Key`-Header; EventSource als `?key=...`-Query-Parameter
- `/health` ist ohne Auth erreichbar (Coolify-Healthcheck)
- `OPTIONS`-Preflight-Requests werden von der Auth-Middleware übersprungen

---

## Ordner-Normierung

IMAP-Spezial-Flags → normierte Namen: `\Drafts` → `Drafts`, `\Sent` → `Sent`, `\Trash`/`\Deleted` → `Trash`, `\Junk`/`\Spam` → `Spam`, `\Archive` → `Archive`

---

## Security-Fixes (2026-04-18)

| Fix | Details |
|---|---|
| CORS eingeschränkt | War `*`, jetzt `CORS_ORIGINS`-Env-Var |
| API-Key-Auth | Middleware prüft `X-API-Key`-Header; /health + OPTIONS ausgenommen |
| IDOR-Fix | `in_reply_to_email_id`: prüft `email.account == from_account` |
| EventSource-Leak | `beforeunload` schließt Verbindung sauber |
| Race Condition | `_refreshing`-Flag verhindert parallele `silentRefresh()`-Läufe |
| FTS-Divergenz | `fts_delete()` nach jedem `DELETE /emails/:id` |
| Credentials-Filter | `/accounts` gibt `imap_pass` und SMTP-Passwörter nicht zurück |
| Token-Renewal | `_refresh_loop()` alle 55 min + 401-Re-Auth als Fallback |

---

## Bekannte Eigenheiten / Fallstricke

- `PocketBase max=0` = Default-Limit (5000 Zeichen), NICHT unbegrenzt. Für unbegrenzte Felder: `max=999_999_999_999_999` (als `MAX_UNLIMITED` in `pb_setup.py`)
- PocketBase Auth-Token läuft nach 1h ab → `pb_client.py` erneuert alle 55 min + 401-Fallback
- PocketBase-Auth-Endpunkt: v0.22+ `/api/collections/_superusers/auth-with-password`, Fallback `/api/admins/auth-with-password`
- Temporäre Anhänge (Compose) liegen im RAM (max. 25 MB), gehen bei Neustart verloren — by design
- EventSource `?key=...` Query-Parameter statt Header (EventSource-API unterstützt keine Custom-Header)
- FTS-Index nach Neustart leer bis Rebuild abgeschlossen → Fallback zu PocketBase LIKE
- Coolify Bind Mounts: `/root/mailflow/pb_data` → PocketBase, `/root/mailflow/fts` → Backend-FTS

---

## KI-Integration

- **Triage:** Claude Haiku kategorisiert E-Mails in `focus` / `quick-reply` / `office` / `info-trash`
- **Antwortvorschlag:** Thread-Kontext (max. 10) + Kontakthistorie (max. 5) + optional `company_knowledge.md`
- **Refinement-Buttons:** Kürzer, Ausführlicher, +Persönlicher Gruß, Sachlicher, Herzlicher
- **Lernregeln:** `POST /triage/example` extrahiert Lernregeln via Claude Haiku, speichert in `triage_rules`
- Details: `MAILFLOW-KIINTEGRATION-PLAN.md`

---

## Getroffene Entscheidungen

| Thema | Entscheidung |
|---|---|
| Auth | PocketBase-Login für Frontend; API-Key für Backend-API |
| HTML-E-Mails | Sandboxiertes iframe mit `srcdoc` |
| Gesendet-Ordner | Automatische Erkennung via `\Sent`-IMAP-Flag, Fallback `Sent` |
| Temporäre Anhänge | Im Speicher halten (max. 25 MB) |
| PocketBase-Auth | Token-Refresh alle 55 min + 401-Fallback |
| Echtzeit-Sync | IMAP IDLE für INBOX + SSE-Push; 2-Min-Polling für Rest |
| Suche | FTS5 primär + PocketBase-Fallback auf subject/from (kein body) |

---

## Offene Punkte

- [ ] KI-Funktionen in Produktion testen (Checkpoint 12)
- [ ] `company_knowledge.md` befüllen → Firmenwissen im KI-Vorschlag
- [ ] `tonality_profiles.md` erstellen (Analyse ~500 gesendeter Mails)
- [ ] Kategorisierung nach Versand zurücksetzen (`setCategory(id, '')`)
- [ ] Xano-Integration (Kundendaten als KI-Kontext)
- [ ] Alte `triage_examples`-Collection in PocketBase löschen (durch `triage_rules` ersetzt)
