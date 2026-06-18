---
title: Mailflow
summary: E-Mail-Client (FastAPI + PocketBase + Vanilla JS, Coolify-Deploy) mit Vorlagen, Snippets, Gruppen, Massenversand und Webhook-Schnittstelle.
tags:
  - app
  - mailflow
  - email
  - pocketbase
  - fastapi
---

# Mailflow

E-Mail-Client auf Basis von FastAPI + PocketBase + Vanilla JS, deployed via Coolify. Diese README ist die **Hand**-Doku: die aktuelle Funktionsweise „wie Mailflow jetzt läuft".

- **Warum / Architektur-Entscheidungen (Kopf):** `~/Syncthing/Claude/Wissen/25_Fields/mailflow/README.md`
- **Datierte Feature-/Refactor-Historie (Verlauf):** `Wissen/25_Fields/mailflow/ChangeLog.md` — jeder Stand mit Datum, Begründung, Test-Plan.
- **Weg nach vorne / offene Punkte:** `Wissen/25_Fields/mailflow/plan-ki.md` (→ `offene-punkte.md`)
- Vertiefend (teils älter, durch diese Referenz für Schema + Datei-Map abgelöst): `briefing.md`, `internals.md` · große Vorhaben `MAILFLOW-KIINTEGRATION-PLAN.md` / `MAILFLOW-TEMPLATES-PLAN.md` (im Repo).

## Sicherheit

Auth-Pattern, PocketBase-Rules und n8n-Tokens folgen dem zentralen Modell in `~/Syncthing/Claude/Wissen/10_Kontext/_shared/sicherheit.md`.

**Single-User-App** (bewusste Architektur-Entscheidung): es gibt keine `user`-Relation auf `accounts`/`emails`/`smtp_servers`/`webhooks`. Authz endet beim Login (`Depends(pb_user_auth.get_user_token)`). Per-Record-/Per-Account-Authz im Backend ist deshalb nicht implementiert (PB-Rules der sensiblen Collections dicht, Backend nutzt Admin-Token — siehe „Auth & signierte URLs"). Falls Mailflow je Multi-User werden soll, ist das ein Datenmodell- + Authz-Refactor; nicht aktuell geplant. Begründung: Wissen-README.

**Bekannte harmlose Konsolen-Meldung** (#iframe-sandbox): `Blocked script execution in 'about:srcdoc' …` beim Öffnen einer E-Mail stammt **nicht aus Mailflow**, sondern von Browser-Extensions (z. B. Google Übersetzer), die in jedes Frame ein Script injizieren — die Sandbox des Mail-iframes (bewusst ohne `allow-scripts`) blockiert das. App-seitig nicht abstellbar. `<script>`-Tags aus Mail-HTML werden vor dem Render gestrippt; nicht auflösbare `cid:`-Referenzen durch Platzhalter ersetzt und als `console.warn` mit E-Mail-ID geloggt.

## Domains & Stack

| | |
|---|---|
| Frontend | https://mailflow.barres.de |
| Backend API | https://mailflow-api.barres.de |
| PocketBase | https://mailflow-pb.barres.de |
| Stack | FastAPI + PocketBase + Vanilla JS + nginx |
| Git (Quelle) | Forgejo `forgejo.barres.de:22222/HPA24-Forgejo-Admin/mailflow` (seit 2026-06-10; `origin`). GitHub nur noch Mirror (`github`) |
| Deployment | Coolify auf Netcup · Auto-Deploy auf `main` via Forgejo-Webhook. **Monorepo:** Backend & Frontend per `watch_paths` (`backend/**` / `frontend/**`) getrennt deployt |

**Kein lokales Docker (Regel seit 2026-06-04):** nicht lokal bauen/testen — direkt an Repo-Dateien arbeiten, pushen, gegen die Live-Instanz verifizieren (Frontend · API-`/health`).

## Secrets / dotenv

Zentrale Konvention: `~/Syncthing/Claude/Wissen/10_Kontext/_shared/sicherheit.md`.

- Lokale Entwicklung: `.env` im Repo (nicht versioniert; `.env.example` nur Variablennamen). Produktion: Coolify Environment Variables der jeweiligen Resource.
- Keys (Backend-API, Claude/OpenAI/Qdrant/Xano, PocketBase) nie in Frontend-Code oder Markdown.
- Variablen u. a.: `API_KEY`, `POCKETBASE_URL`, `POCKETBASE_EMAIL`, `POCKETBASE_PASSWORD`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `QDRANT_URL`, `QDRANT_API_KEY`, `XANO_API_KEY`, `SPAM_AUTO_CLASSIFY`, `SPAM_SIMILARITY_THRESHOLD`, `IMPORT_API_KEY`, `ACTIVITY_PB_URL`, `ACTIVITY_PB_IDENTITY`, `ACTIVITY_PB_PASSWORD`.

## Auth & signierte URLs

- **Kein Frontend-API-Key.** Das Frontend sendet bei normalen Requests nur den PocketBase-User-Token (`Authorization: Bearer <pb_token>`), den die Auth-Middleware in `backend/main.py` gegen PocketBase validiert.
- **Browser-APIs ohne Custom-Header** (`EventSource`, `<img>`, Download-Links) nutzen kurzlebige signierte URLs: `POST /sign` erzeugt ein HMAC-Token für genau einen Pfad **und** eine Methode. `/sign` akzeptiert nur GET und nur drei Allowlist-Pfade: `^/events$`, `^/attachments/[a-zA-Z0-9]+/download$`, `^/emails/[a-zA-Z0-9]+/inline$` (+ `…/source.eml`). `verify(token, path, method)` prüft alle drei. Token-TTL 5–10 min.
- **Externe Integrationen** mit eigenen Keys: Webhooks `X-Webhook-Key`, Kontakt-Import optional `X-Import-Key`, Admin-Endpunkte `X-Admin-Key`.
- **PB-Rules dicht (S1):** `accounts`/`smtp_servers`/`webhooks` haben alle Rules `None` (enthalten Klartext-Secrets) — direkter PB-Zugriff mit User-Token blockiert; das Backend liest/schreibt sie via Admin-Token. `PATCH /accounts/{id}` läuft bewusst als Admin mit enger Feld-Whitelist (`UpdateAccountRequest`: nur `name`, `from_name`, `signature`, `color_tag`, `reply_to_email`).
- **PB-Filter-Guardrail:** `scripts/check_pb_filters.py` (AST-Scan) verhindert Filter-Injection durch f-String-Interpolation ohne `pb_quote()`; Ausnahmen per `# pb-filter-safe`.

## Wichtige Dateien

| Datei | Inhalt |
|---|---|
| `backend/main.py` | FastAPI-Bootstrap (lifespan, CORS/Auth-Middleware, Exception-Handler, Router-Includes) — ~281 Z. nach C1 Phase 2 |
| `backend/routers/admin.py` | `/admin/*` — Embed-Backfill, IMAP-UID-Backfill |
| `backend/routers/ai.py` | `/categories`, `/ai/*`, `/triage/example`, `/response-patterns` |
| `backend/routers/bulk.py` | `/bulk-sends/*` CRUD |
| `backend/routers/contacts.py` | `/contacts/*`, `/contact-groups/*`, `/contacts/import` |
| `backend/routers/mail.py` | `/search`, `/emails/*`, `/attachments/*`, `/spam-rules/*`, `/calendar/*` — der dicke Block (~1175 Z.) |
| `backend/routers/system.py` | health, sign, sync, events, accounts, smtp-servers, folders, xano, diagnostics |
| `backend/routers/templates.py` | `/variables`, `/snippets`, `/templates`, `/templates/render` |
| `backend/routers/webhooks.py` | Externer Webhook-Send + Webhook-CRUD |
| `backend/services/mail.py` | Send-Pipeline-Helpers: `_do_send_job`, IMAP-Aktions-Helper, Bulk-Worker-Loop, Bounce-Match, State-Dicts |
| `backend/services/imap.py` | `ImapService`: blocking IMAP-Operationen (move, trash, set_read, fetch_attachment, fetch_raw …) |
| `backend/imap_sync.py` | IMAP-Sync (inkrementell, Flag-Sync, Ordner-Normierung); Diagnose-Ringpuffer `_sync_skip_log` |
| `backend/idle_manager.py` | IMAP IDLE pro Account (asyncio-Task, SSE-Notification) |
| `backend/fts.py` | FTS5-Volltext-Index (setup, insert, delete, search, rebuild) |
| `backend/pb_setup.py` | PocketBase-Schema + DB-Indizes beim Start (idempotent, `_swap_index` für Migrationen) |
| `backend/pb_client.py` | PocketBase HTTP-Client (Token-Auth + Auto-Refresh alle 55 min) |
| `backend/activity_pb.py` | Zweiter PB-Client (Activity-PB) für den Kalender-Import |
| `backend/scheduler.py` | APScheduler: Flag-Sync + Ordner alle 2 min |
| `backend/smtp_sender.py` | SMTP-Versand + IMAP APPEND in Sent. Recipient-Parser via `email.utils.getaddresses()` |
| `backend/bounce_parser.py` | DSN-Bounce-Erkennung + -Parsing |
| `backend/rendering.py` | Vorlagen-Render-Pipeline (Sections/Snippets/Variablen, Phase 1+2) |
| `backend/ics_parser.py` | dependency-freier VEVENT-Parser für Kalender-Einladungen |
| `backend/ai_helper.py` | Claude Haiku: Kategorisierung, Antwortvorschlag, Refinement |
| `frontend/js/inbox.js` | Frontend-Hauptlogik (~2030 Z. nach C4 Phase 2) — Inbox-Liste, Folder-Cache, KI-Modus, Zoom, Threading, Drag&Drop |
| `frontend/js/email_detail.js` | Detail-Pane (`openEmail`, sandbox-Iframe + CID-Inline, Reply/Forward/Edit-Draft, Event-Karte, Remote-Bild-Block) |
| `frontend/js/compose.js` | Compose (~1350 Z.) — open/close/saveDraft, Send-Notifications, Toolbar, Massenversand-Pipeline, Test-Send, Attachments |
| `frontend/js/spam.js` | `spamEmail` + Spam-Rules-Verwaltung (Modal, Entblocken) |
| `frontend/js/sse.js` | EventSource für `/events` mit Reconnect + Watchdog |
| `frontend/js/api.js` | API-Wrapper (Bearer-Token-Header) |

## Collections in PocketBase

- **accounts**: imap_host, imap_port, imap_user, imap_pass, from_email, from_name, signature, color_tag, reply_to_email, **default_smtp_server** (rel), **send_only** (bool)
- **emails**: account (rel), imap_uid, uidvalidity, folder, message_id (unique je `(account, folder, message_id)`), thread_id, in_reply_to, from_email, from_name, reply_to, to_emails, cc_emails, subject, body_plain, body_html, snippet, date_sent, is_read, is_flagged, is_answered, is_new, ai_category, has_attachments, **spam_suggested**, **spam_score**, **spam_rule_match**, **webhook** (rel → webhooks, beim Sent-Sync via `message_id`-Lookup in `webhook_logs` befüllt)
- **attachments**: email (rel, cascade), filename, mime_type, size_bytes, part_id
- **folders**: account (rel), imap_path, display_name, unread_count, last_sync_uid, uidvalidity, no_select
- **smtp_servers**: name, host, port, user, password, use_tls, use_starttls, is_default
- **contacts**: email (UNIQUE), name, email_count, last_contact, `groups` (multi-rel → contact_groups), `unsubscribed`, **bounced** (+ bounced_at, bounced_reason)
- **triage_rules**: gelernte KI-Regeln (aus Nutzerfeedback konsolidiert)
- **response_patterns**: account (rel), element_text, action, draft_text, was_edited — Element+Antwort-Paare aus der KI-Sidebar (für Qdrant-Embedding)
- **spam_rules**: account (rel), match_type (`email`|`domain`), pattern (lowercase), hits, last_hit — UNIQUE (account, match_type, pattern)
- **webhooks** (`pbc_3653375940`): name, slug (unique, `^[a-z0-9-]+$`), smtp_server (rel), from_account (rel), default_to, from_name_override, allow_to_override/allow_reply_to/allow_cc (bool), api_key (unique, `whk_`+`token_urlsafe(32)`), is_active
- **webhook_logs** (`pbc_305862465`): webhook (rel, cascade), ip, status (`success`|`error`), to, subject, error, message_id, email (rel, optional) — Audit-Trail je Aufruf
- **bulk_sends**: subject, from_account (rel), from_account_email, smtp_server, body_html/body_text (Snapshot), sent_at, delay_seconds, recipients (JSON-Array mit status/message_id/bounced_*), Counts total/sent/error/bounced
- **email_variables** (name unique, value) · **email_snippets** (name unique, html) · **email_templates** (prefix, name, subject, html_body, text_body) · **contact_groups** (name unique, description) — Vorlagen-System

**Qdrant:** `mailflow_emails` (Thread-Vektoren für Antwortvorschläge, text-embedding-3-small, 1536-dim, Cosine) · `mailflow_spam_samples` (Mail-Vektoren manuell markierter Spams).

## KI-Integration

- **Triage:** Claude Haiku → `focus` / `quick-reply` / `office` / `info-trash`.
- **Antwortvorschlag:** Thread-Kontext (max. 10) + Kontakthistorie (max. 5) + optional `company_knowledge.md` + optional `context_elements` (aus Sidebar-Auswahl).
- **Refinement:** Kürzer / Ausführlicher / +Persönlicher Gruß / Sachlicher / Herzlicher.
- **Lernregeln:** `POST /triage/example` → extrahiert Regeln → `triage_rules`. Konsolidierung ab 15 Regeln/Account/Kategorie → Background-Task `_consolidate_rules()` → max. 7 Kernregeln.
- Prompts in `backend/triage_prompts.md`.

### KI-Analyse-Sidebar

Öffnet rechts im Detail-Panel bei aktivem KI-Modus. Oben **Xano-Userkarte** (`GET /xano/user-info?email=…` → Xano, Key serverseitig in `XANO_API_KEY`; bei Rotation: Wert im Function-Stack des Endpoints `GET /user/get/roles`, API-Gruppe `52vvrgF7` ersetzen → Xano Publish → Coolify `XANO_API_KEY` neu setzen → redeploy). Darunter **Analyse-Karten** (ein `POST /ai/analyze`-Call → `[{element, action, draft}]`); pro Karte Element-Text + Aktions-Label + editierbare Textarea, „Speichern" → `response_patterns` (`was_edited=true` bei Änderung). Footer „Antwort generieren" → Compose + `/ai/suggest` mit den gewählten Karten als `context_elements`. Xano- und Analyse-Call parallel (`Promise.allSettled`).

## Spam-Lernsystem

Zwei Ebenen: (1) **Absender-Blocklist (hart)** — `spam_rules`, IMAP-Move sofort in Spam. (2) **Semantische Ähnlichkeit (weich)** — Cosine gegen Qdrant `mailflow_spam_samples`, Schwelle `SPAM_SIMILARITY_THRESHOLD=0.82` → `spam_suggested`+`spam_score` → gelbe Inline-Bar („Spam" / „+ Absender blocken" / „Behalten"). Manuelles Markieren = neues Trainingsbeispiel; Drag-aus-Spam/„Behalten"/`/unspam` entfernt das Sample (Blocklist-Regeln bleiben). Auto-Klassifikation als Hook in `imap_sync.py` hinter `SPAM_AUTO_CLASSIFY`.

| Endpoint | Wirkung |
|---|---|
| `POST /emails/{id}/spam?block_sender=true` | IMAP-Move in Spam, lernt Sample, optional Blocklist |
| `POST /emails/{id}/unspam` | zurück nach INBOX, löscht Sample, resettet Spam-Felder |
| `POST /emails/{id}/spam-suggestion/confirm` \| `/dismiss` | gelben Vorschlag bestätigen (wie `/spam` ohne Block) bzw. verwerfen |
| `GET /spam-rules` · `DELETE /spam-rules/{id}` | Blocklist lesen · entblocken |

**Folder-Auflösung:** `emails.folder` ist der **normalisierte** UI-Name (Spam/Trash/Drafts/Sent); IMAP-Operationen brauchen den echten Pfad (z. B. „Junk"). Helper `resolve_imap_path` (`imap_utils.py`).

## Suche

Volltext via **FTS5-SQLite-Index** (`/app/fts/fts.db`, Bind Mount `/root/mailflow/fts`, contentless-Bug 2026-06-06 behoben → Body-Treffer): Einzelwort direkt; Mehrwort zuerst Phrase, dann AND. **Fallback** bei leerem FTS5 → PocketBase auf `subject`/`from_email`/`from_name` (kein `body_plain`). **Sent-Sonderfall:** parallele PB-Query gegen `to_emails` mit `folder="Sent"` (dedupliziert), da `to_emails` nicht im FTS5-Index. Rebuild läuft bei jedem Container-Start (~80k Mails ~1 min). Bei aktiver Suche gibt `_getFromCache()` immer `null`.

## E-Mail-Versand

### Asynchron + Notifications

`POST /emails/send` → sofort `{job_id}`; SMTP läuft als `asyncio.create_task`, bei Ende SSE-Event `send-result` an alle Clients. Frontend schließt Compose sofort, zeigt `#send-notifications`-Leiste (⏳ grau / ✓ grün auto-dismiss 4 s / ✗ rot bleibt). Draft-Löschung nach Versand macht das Backend (`draft_id` mitgeschickt). **SSE-Robustheit:** sichtbarer `{"type":"ping"}`-Heartbeat + Client-Watchdog (`SSE_STALE_MS` 65 s, Reconnect) + Polling-Fallback `GET /emails/send-status/{job_id}` gegen halbtote Verbindungen.

### Massenversand & Aussendungen

`POST /emails/bulk-send` (`recipients: list[str]`, `delay_seconds` default 5, cap 300): jeder Empfänger sieht nur sich selbst im `To`, 5 s Abstand, eigene `send-result`-Events, nicht-blockierendes Status-Panel. Vor dem Anlegen filtert der Endpoint **bouncte + unsubscribed** Adressen raus (`filtered_out` in der Response). Jeder Versand wird als Audit-Record in **`bulk_sends`** persistiert (Re-Send-Workflow `mfComposeResend`, Subview „Aussendungen"). Persistente Empfänger + `_bulk_worker_loop` setzen Pending-Jobs nach Backend-Restart fort (B15); Anhänge bleiben in-memory (Restart mit Anhängen → `error: backend_restart_with_attachments`). Gruppen-Auswahl im Bulk-Modal (`＋ Gruppe ▾`), Test-Send `✉ Test senden` (`[TEST]`-Prefix an eigene Adresse).

### Bounce-Erkennung

DSN-Mails werden im INBOX-Sync erkannt (`bounce_parser.py`: From-/Subject-Heuristik, `multipart/report`), gegen `bulk_sends.recipients[*]` gematcht (Message-ID, Fallback Email+7 Tage). Permanenter Fehler (5.x.x) → Kontakt `bounced=true`; 4.x.x → nur Empfänger-Status. Bounce-Mails bleiben in INBOX. Subview „Bouncte" im Vorlagen-Tab, `↺ Reset` via `POST /contacts/{id}/clear-bounce`.

### SMTP-Identitäten

- **Default-SMTP pro Account** (`accounts.default_smtp_server`): das Compose-Dropdown wählt den Versandweg passend zum Von-Account (Fallback `default_smtp_server` → globaler `is_default` → erster). Verhindert „Sender address rejected" bei Alias-Absendern.
- **Send-only-Accounts** (`accounts.send_only`): Alias-Identitäten (z. B. `zentrale@post.hpa24.de` via Inxmail) ohne eigenen Sync/IDLE — erscheinen nicht in der Sidebar, aber im Von-Dropdown; Sent-Kopie läuft via Append ins geteilte Postfach. **Architektur-Regel: 1 Account = 1 eigenes IMAP-Postfach** (Doppel-Account auf demselben Postfach dupliziert Mails / triggert Connection-Timeouts).
- **Nur-Text-Modus** im Composer: Toggle → Monospace-Textarea, Versand mit `body_html=''` (reine `text/plain`-Mail). Pro-Mail-Entscheidung, nicht persistiert.
- **Quelltext-Ansicht:** „Mehr ▾ → Quelltext anzeigen" holt die Roh-Mail live via `BODY.PEEK[]` (`GET /emails/{id}/source`), `.eml`-Download per signiertem URL.

## Webhooks (externer Versand)

Externe Workflows (Xano, Kontaktformulare) lösen den Versand über `POST /webhooks/{slug}/send` aus (von der globalen Auth ausgenommen, eigener `X-Webhook-Key` via `secrets.compare_digest`). Payload: `to`, `subject`, `body`/`body_html`, optional `reply_to`/`cc` — Overrides nur bei aktivem `allow_*`-Toggle, sonst `default_to`/leer. `is_active=false` oder unbekannter Slug → `401` (kein Slug-Leak). Pro Webhook fester SMTP-Server + Absender-Account + optionaler `from_name_override`. Versand läuft durch dieselbe `smtp_sender.send_email`-Pipeline → Sent-Kopie via IMAP APPEND. Verwaltung (Topbar „Webhooks": List/Edit/Logs); `api_key` serverseitig generiert, per `PATCH {rotate_api_key:true}` rotierbar. Jeder Aufruf (auch Validierungsfehler) → `webhook_logs`. Im Sent-Ordner trennt der UI-Filter „Alle / Webhook / Normal" (Feld `emails.webhook` per `message_id`-Lookup befüllt). Drei Historie-Ebenen bei Reklamationen: `webhook_logs` → Message-ID → Sent-Folder.

## Vorlagen-System & Gruppen-Versand

Ablöse des FileMaker-Versandtools (Plan: `MAILFLOW-TEMPLATES-PLAN.md`).

- **Collections:** `email_variables` (globale `{{var}}`), `email_snippets` (HTML-Blöcke via `{{> name}}`, kein Snippet-in-Snippet), `email_templates` (Subject + HTML, `(prefix, name)` unique), `contact_groups` (M:N mit `contacts.groups`).
- **Render-Pipeline** (`backend/rendering.py`), zweiphasig: **Phase 1** (Pre-Compose) Sections strippen (`<!-- @section X --> … <!-- @end -->`), Snippets auflösen, globale Variablen ersetzen — `{{name}}`/`{{email}}` bleiben. **Phase 2** (Pre-Send pro Empfänger) Kontakt-Variablen ersetzen + `strip_unresolved`.
- **UI:** Topbar-Tabs Inbox / Vorlagen / Kontakte; Vorlagen-Tab dreispaltig (Untermenü Variablen/Snippets/Vorlagen/Gruppen/Aussendungen/Bouncte/Kontakte · Liste · Editor+Preview). Lösch-Schutz via `/{var|snippet}/{id}/usage`.
- **Compose „Aus Vorlage"** → `POST /templates/render` → Subject+HTML in den Editor; Stefan editiert, Phase 2 läuft beim Senden.
- **Kontakt-Import** `POST /contacts/import` (`{lines, mode: add|remove}`, Format `email,name,gruppen`; Auth `X-API-Key` **oder** `X-Import-Key` für FileMaker).
- **Endpoints:** CRUD `/variables` `/snippets` `/templates` `/contact-groups`, `POST /templates/render`, `GET /contact-groups/{id}/members`, `POST /contacts/import`.

## Kalender-Einladungen (.ics)

- **Event-Vorschau:** Mails mit `text/calendar`-Part zeigen oben eine Event-Karte. `GET /emails/{id}/calendar` (Roh-Mail live von IMAP → `ics_parser.py`, dependency-freier VEVENT-Parser) liefert Titel, Zeit, Ort, Organizer, **Join-URL** (Teams/Zoom/Meet/Webex), Meeting-ID, Passcode. Anhang-Erkennung fängt auch namenlose `text/calendar`-Parts (`mime_parser._is_attachment_part`).
- **In Kalender übernehmen:** Button → Inline-Kalenderauswahl → `POST /emails/{id}/calendar/import` schreibt einen Termin **direkt in die Activity-PB** (`https://activity-pb.barres.de`, Collection `termine`) über den zweiten PB-Client `activity_pb.py` (Auth als `stefan@hpa24.de`). Mapping `date`←`start[:10]`, `title`←(ggf. `HH:MM `+)`summary`, `join_url`←`join`; Dedup über `ics_uid`. Braucht Coolify-Env `ACTIVITY_PB_URL`/`ACTIVITY_PB_IDENTITY`/`ACTIVITY_PB_PASSWORD` (ohne → 503). Antwort `{created, duplicate, termin}`.

## Upload-Limits

Temporäre Anhänge in-memory (`_temp_uploads`), abgesichert: `MAX_UPLOAD_SIZE=25 MB` (pro Datei, 413), `MAX_TOTAL_UPLOAD_SIZE=200 MB` (alle aktiven), `UPLOAD_TTL_SECONDS=30 min`, Sweep alle 5 min (`_cleanup_temp_uploads_loop`). Upload-Endpoint streamt chunked (64 KB) + Content-Length-Vorabprüfung statt Voll-Read (S4). Reverse-Proxy: Caddy-Direktive `request_body max_size 30MB` an der Backend-Resource.

## Ladegeschwindigkeit & Optimistic UI

- **Zweistufiges Laden:** Stage 1 = 50 E-Mails sofort (`FIRST_PAGE_SIZE`), Stage 2 = Rest parallel bis 500. **Ordner-Cache** 3 min TTL (`_folderCache`, bei Suche übersprungen). Listen-Endpoints (`/emails`, `/emails/threaded`, `/emails/by-sender`) mit `_EMAIL_LIST_FIELDS`-Whitelist (kein Body im Listen-Payload).
- **DB-Indizes:** `idx_emails_account_folder_date` (account, folder, date_sent DESC) · `idx_emails_account_folder_read_date` (account, folder, is_read, date_sent DESC).
- **Optimistic UI:** alle Aktionen (Lesen/Löschen/Spam/Verschieben/Entblocken) sofort, Rollback bei Fehler. **Tombstones** (`_removedTombstones`, TTL 10 min) verhindern Wiederauftauchen optimistisch entfernter Mails durch parallelen Sync; jeder Entfern-Pfad setzt `_addTombstone(id)`, Rollback `_clearTombstone(id)`. **404 = Erfolg** in Lösch-/Verschiebe-Handlern (Race mit `imap_sync`). Tab-Titel `(n) Mailflow` (Vivaldi-Badge aus `is_new`-Zähler, max. 99).

## Getroffene Entscheidungen

| Thema | Entscheidung |
|---|---|
| Auth | PocketBase-Login fürs Frontend; signierte URLs für header-lose Browser-APIs; getrennte Keys extern |
| HTML-E-Mails | Sandboxiertes iframe mit `srcdoc` (`allow-scripts` ohne `allow-same-origin`); Remote-Bilder block-by-default (S5), CID via signiertem Proxy |
| Horizontaler Scroll | Scrollbalken auf `body`-Ebene (`overflow-y:hidden` → implizit `overflow-x:auto`); `#layout { min-width: min-content }` |
| Gesendet-Ordner | Auto-Erkennung via `\Sent`-IMAP-Flag; flach chronologisch, zeigt Empfänger statt Absender (`isFlatFolder()`) |
| Suche im Sent | `/search` matcht zusätzlich `to_emails` für `folder="Sent"` (parallele PB-Query) |
| Echtzeit-Sync | IMAP IDLE für INBOX + SSE-Push; 2-min-Polling für den Rest |
| `is_new`-Feld | bei IMAP-Insert `true` (wenn ungelesen); beim Öffnen `false`; Flag-Sync respektiert `\Seen` |
| Zoom-Default | 125 % beim Öffnen (`DEFAULT_ZOOM` in `inbox.js`); Cycle 125→150→75→100→125 |
| message_id-Unique | Index `(account, folder, message_id)`, nicht global — Same-Mail in Sent **und** INBOX via Alias möglich |
| Composer-Schriftgröße | eigene `applyFontSize(px)` statt `execCommand` (O(n²)-Hänger); feste px S=14/M=18/L=24/XL=30 |
| Backend-Struktur | `main.py` nur FastAPI-Bootstrap (~280 Z.); Endpoints in `routers/*.py`, Cross-cutting in `services/mail.py`/`imap.py` |
| Frontend-Struktur | `inbox.js` = Liste+Cache+KI-Modus+Zoom; Compose/Detail/Spam/SSE in eigenen Modulen; klassische Script-Tags (kein ES-Module-Setup) |
| Container | Backend + Frontend laufen non-root (uid 10001 / nginx-unprivileged 101); kein lokales Docker |

## Bekannte Fallstricke

### PocketBase

- `PocketBase max=0` = Default-Limit (5000 Zeichen), NICHT unbegrenzt → `MAX_UNLIMITED` (`max=999_999_999_999_999`) in `pb_setup.py`.
- Auth-Token läuft nach 1 h ab → alle 55 min erneuert + 401-Fallback. Auth-Endpunkt v0.22+ `/api/collections/_superusers/auth-with-password` (Fallback `/api/admins/...`).
- `triage_rules`-Collection muss in `pb_setup.py` registriert sein — sonst `pb_post` 500.
- Sort-Param verlangt einen **Index** auf dem Feld (auch System-Felder wie `created`), sonst 400 → Sort weglassen + clientseitig, oder Index ergänzen. Weitere Eigenheiten: `Wissen/50_Ressourcen/PocketBase/eigenheiten.md`.
- `CREATE VIRTUAL TABLE IF NOT EXISTS` migriert **keine** bestehende FTS5-Tabelle — Schema-Änderung braucht explizite Erkennung+DROP.

### Sync, Flags, Anhänge

- Temporäre Anhänge im RAM (max. 25 MB), gehen bei Neustart verloren — by design.
- FTS-Index nach Neustart leer bis Rebuild → Fallback PocketBase LIKE. Bind Mounts: `/root/mailflow/pb_data`, `/root/mailflow/fts`.
- **`is_answered`-Flag:** Flag-Sync überschreibt PB mit IMAP-Stand → manuell gesetztes `is_answered=true` wird zurückgesetzt, wenn `\Answered` nicht auf dem Server steht. Beim Antworten setzt das Backend `\Answered` via `add_flags` auf IMAP **und** in PB.
- **Mail neu durch den Parser ziehen:** PB-Record löschen, `folders.last_sync_uid` der INBOX knapp unter die UID setzen, Sync abwarten (Multi-Account: am richtigen Folder-Record schrauben; Vorsicht Clobber durch parallelen Sync).
- **BODYSTRUCTURE-Verschachtelung (B9-Anhang-Fetch):** `fetch_attachment` holt gezielt `BODY[<part-id>]` statt der ganzen Mail; die Part-ID kommt aus `_walk_bodystructure` (`services/imap.py`). IMAPClient packt die Multipart-Kinder je nach Version **als Liste in `bs[0]`** (`([c1, c2], subtype, …)`) ODER als direkte Tuple-Elemente — beide Formen werden behandelt. Wird nur `isinstance(bs[0], tuple)` geprüft, wird die Listen-Form (u. a. Apple-Mail `multipart/alternative ⊃ multipart/related ⊃ Bild`) als **ein** Leaf „Part 1" fehlgedeutet → der Download liefert den `text/plain`-Body roh statt des Anhangs (Symptom: „JPEG lädt, lässt sich nicht öffnen"). Diagnose-Hebel: das `logger.info("fetch_attachment: UID … part … encoding=…")` zeigt den tatsächlich geholten Part. Fix-Commit `7922b08`.

### Frontend / iframe / Compose

- EventSource `?key=…` Query-Param (kein Custom-Header möglich).
- **Compose-Scroll:** `#compose-mode` ist das scrollbare Element (`overflow:auto`), NICHT `#ci-scroll-area`.
- **iframe-Höhe:** kein `allow-same-origin` → `contentDocument` wirft SecurityError. Lösung: Script per `injectHeightScript` in `srcdoc`, meldet `scrollHeight` via `postMessage({type:'mf-iframe-h'})`. iframe scrollt intern (akzeptiert).
- **CSS Overflow-Architektur** (`html`/`body`/`#layout`/`#app`) ist fragil — vor Änderungen testen.
- **KI-Antwort-Handler:** `kiSuggestBtn.onclick` muss im `try`-Block von `openEmail` stehen (braucht `full.body_plain`); `openCompose` gibt `false` bei Abbruch.
- **`template.format()` in `ai_helper.py`:** Mail-Inhalte mit `{}` vor dem Aufruf escapen (`replace("{","{{")`), sonst `KeyError` → 500.
- **Kein Paste-Sanitizer** im Composer — eingefügtes Word-/Web-HTML behält allen Ballast (war Auslöser der execCommand-O(n²)-Last; sauberes Paste-Strippen wäre Folgeschritt).
- **Text-Expander (Rocket Typist) im `An:`-Feld:** Chip-/Autocomplete-Feld braucht ggf. Delay zwischen Einfügen und Tab; beim Tab übernimmt Mailflow vorhandenen Text als Chip (`_toField.commitPending()`).
