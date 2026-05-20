# Mailflow — Refactor- und Hardening-Plan

**Quelle:** GPT-Codereview vom 2026-05-20 (zusammen mit Stefan). Ergänzend zu den vier sofortigen Security-Fixes (Commits `e137884`, `e4659bf`, `940b24b`, `182241d` am 2026-05-20).

## Status (Stand 2026-05-20 abend)

**Erledigt, live, smoke-getestet:**
- ✅ A1 — Auth-Modell-Umbau (Commits `9b64ee3` + `77592ea`)
- ✅ A6 — Rate-Limits (Commit `edd4eb3`)
- ✅ A7, A13, B8, B12 — Hardening-Quartett (Commit `e2e7cff`)
- ✅ C5 — Config-Strategie (mit A1.8-Cleanup)
- ✅ C1 / C3 / C4 — jeweils Phase 1 (Commit `e2ad2da`): Webhook-Router raus, `imap_session(acc)`-Context-Manager, `webhooks.js` raus aus `inbox.js`
- ✅ A10 — Admin-Endpoints mit separatem `ADMIN_API_KEY` (Commit `fd816a1`): `X-Admin-Key`-Check in Middleware, PB-Bearer reicht für `/admin/*` nicht mehr
- ✅ A11 Phase 1 — Foundation für PB-User-Token-Trennung: `pb_*_as(token, …)` in `pb_client.py` + `get_user_token`-Dependency in `pb_user_auth.py`. Endpoints noch nicht migriert.
- ✅ A11 Phase 2 — Pilot `GET /accounts` auf User-Token + PB-Rule auf `accounts.list/viewRule` (Commit `174a757`). `_ensure_rules`-Helper patcht PB-Rules idempotent.
- ✅ A11 Phase 3a — Vorlagen-Cluster (variables/snippets/templates): full User-CRUD via PB-Rules, 16 Endpoints + 2 Helper migriert (Commit `8543fb7`).
- ✅ A11 Phase 3b — Kontakte-Cluster (contacts/contact_groups): 7 User-Endpoints migriert, admin-Pfade (Import, IMAP-Upsert) dokumentiert (Commit `9494d00`).
- ✅ A11 Phase 3c — Kleinkram-Cluster (5 Collections): 5 User-Endpoints migriert, Backend-Schreiber dokumentiert (Commit `dabf66c`).
- ✅ A11 Phase 3d.1 — emails+attachments PB-Rules + 8 Read-Endpoints auf User-Token (Commit `6933cbe`). Signed-URL-Endpoints bleiben Admin.
- ✅ A11 Phase 3d.2 — 9 emails-State-Writes migriert, `_update_folder_unread_count(token, …)` durchgereicht (Commit `f45fbca`).
- ✅ A11 Phase 3d.3 — Drafts (3), AI (4), Send (2) migriert; Background-Helper (`_do_send_job` etc.) bleiben dokumentiert Admin (Commit `69cb0f3`).

**Offen — nächster Chat startet hier:**
- A11 Phase 3e (Audit/Bulk: bulk_sends, webhook_logs, webhooks)
- B9, B14, B15 (BODYSTRUCTURE / Temp-Upload-TTL / Bulk-Jobs persistent)
- C1 / C3 / C4 — jeweils Phase 2 (weitere Router, ImapService-Klasse, weitere JS-Module)
- C2 (Pydantic-Request-Modelle, verteilt)

Lessons aus den erledigten Schritten stehen als Checkliste für neue Web-Apps in `Wissen/20_Apps/_shared/sicherheit.md` (Abschnitt `#backend-patterns`).

Reihenfolge nach Priorität: **Security zuerst, dann Robustheit, dann Architektur/Cleanup**. Jeder Punkt = ein abschließbarer Block. Bei größeren Punkten Teil-Schritte einplanen.

---

## A. Security (höchste Priorität)

### A1 — Auth-Modell ablösen: globaler API_KEY → PB-Session-Token + signierte URLs

**Problem:** Backend nutzt globalen `API_KEY`, der per `/config.js` an den Browser gegeben wird. SSE-Stream, Attachments und Inline-Bilder hängen ihn als `?key=...` an URLs an. Query-Keys landen in Logs, History und Referrer-Headern.

**Plan:**
- Auth-Middleware liest `Authorization: Bearer <pb-token>` und validiert gegen PocketBase `auth-refresh`. Globaler `API_KEY` nur noch für externe Aufrufer (FileMaker etc., bleibt per `X-Import-Key` separat).
- **SSE / `<img src>` / `<iframe src>`** können keine Header senden — Lösung: **kurzlebige signierte Download-URLs**. Endpoint `POST /sign?path=...` gibt `?token=<jwt>&exp=<ts>` zurück (HS256 mit Server-Secret, TTL z.B. 5 min). Frontend signiert pro Inline-Image/Attachment/SSE-Connection einmal.
- `/config.js` und der Lazy-Load-Mechanismus in `frontend/js/api.js` können dann komplett raus.

**Aufwand:** 1–2 h. Berührt `backend/main.py` Auth-Middleware, SSE-Endpoint, alle `?key=`-Aufrufer im Frontend (`api.js`, `inbox.js`).

### A6 — Rate-Limits für Webhooks und Kontakt-Import

**Problem:** `POST /webhooks/{slug}/send` und `POST /contacts/import` sind unbegrenzt aufrufbar. Risiko: Mail-Spam, Brute-Force auf Webhook-Keys, SMTP-Last.

**Plan:** `fastapi-limiter` oder `slowapi` integrieren. Per IP + pro Webhook-Slug. Vorschlag: 30/min pro IP, zusätzlich Tageslimit pro Webhook (z.B. 500/day, konfigurierbar).

### A7 — Generische Fehlermeldungen am Client

**Problem:** Globaler Exception-Handler gibt `{"detail": str(exc)}` zurück. Interne Pfade, PocketBase-Details, Stack-Hinweise leaken.

**Plan:** Globaler Handler liefert generisch `{"detail": "Interner Fehler", "ref": "<uuid>"}`. Volle Exception + UUID nur ins Backend-Log. Bekannte `HTTPException` mit explizitem Status bleiben durchgereicht.

### A10 — Admin-Endpoints abgrenzen ✅ (2026-05-20, live + smoke-getestet)

**Problem:** `/admin/backfill-imap-uids`, `/admin/embed-backfill`, `/admin/embed-search`, `/admin/embed-status` hingen am gleichen PB-Bearer wie die normalen User-Calls. Frontend-Token-Kompromittierung hätte Admin-Funktionen mit geöffnet.

**Umsetzung:** Separater `ADMIN_API_KEY` (env, im Coolify gesetzt). Auth-Middleware kurz-schliesst `/admin/*` mit timing-safem `X-Admin-Key`-Check (`_secrets.compare_digest`) **vor** der PB-Bearer-Prüfung. PB-Bearer reicht für `/admin/*` nicht mehr. Leere Env → 503 statt stiller Durchlass. PB-Rollen-Variante zurückgestellt — käme mit A11 (PB-Superuser-Trennung), bleibt für später als zweite Schicht denkbar.

**Smoke-Test:** kein Header → 401, falscher Key → 401, korrekter Key → 200, Bearer-only auf `/admin/*` → 401, `/health` unverändert 200.

### A11 — PB-Superuser-Token als Single Point of Failure

**Problem:** Backend nutzt dauerhaft PB-Admin-Credentials für alle DB-Operationen. Bei Backend-/Server-Kompromiss ist die gesamte DB offen.

**Plan (mittelfristig):** in 3 Phasen geteilt:

**Phase 1 — Foundation ✅ (2026-05-20):**
- `pb_*_as(token, …)`-API in `pb_client.py` parallel zu admin-`pb_*` (kein Re-Auth, 401 bubbelt durch)
- FastAPI-Dependency `get_user_token` in `pb_user_auth.py` (zieht Bearer-Token aus Header)
- **Noch keine Endpoint-Migration, noch keine PB-Rule-Änderung** — nur Boden bereitgestellt

**Phase 2 — Pilot ✅ (2026-05-20):**
- `GET /accounts` ist der Pilot — nutzt `pb_get_as(token, …)` via `Depends(get_user_token)`
- PB-Rule auf `accounts.listRule` + `accounts.viewRule` = `'@request.auth.id != ""'` (jeder eingeloggte User darf lesen; create/update/deleteRule bleiben admin-only)
- `_ensure_rules`-Helper in `pb_setup.py` patcht PB-Rules idempotent — Code-Schema ist Source of Truth
- Smoke: negativ-Tests (kein Header → 401, bogus Bearer → 401) grün; positiv-Test über Frontend-Reload bestätigt

**Phase 3 — Volle Migration (laufend, per Collection-Cluster):**

- **3a — Vorlagen ✅ (2026-05-20):** `email_variables`, `email_snippets`, `email_templates`. Alle 5 Rules je Collection auf `@request.auth.id != ""`. 16 Endpoints + 2 Helper migriert. Pattern: reine User-CRUD-Collection ohne Backend-Schreiber.
- **3b — Kontakte ✅ (2026-05-20):** `contacts`, `contact_groups`. 7 User-Endpoints migriert (`/contacts/search`, `/contact-groups` list/create/update/delete/members, `/templates/render` contacts-Lookup). Admin-Pfade explizit dokumentiert: `/contacts/import` (X-Import-Key), `imap_sync.upsert_contact` (Backend-Job). `/emails/by-sender` verschoben auf 3d (cross mit emails).
- **3c — Kleinkram-Cluster ✅ (2026-05-20):** `folders`, `smtp_servers`, `triage_rules`, `spam_rules`, `response_patterns`. Alle 5 Rules je Collection auf `@request.auth.id != ""`. 5 User-Endpoints migriert (`GET /smtp-servers`, `GET /folders`, `GET /spam-rules`, `DELETE /spam-rules/{id}`, `POST /response-patterns`). Backend-Schreiber dokumentiert (`imap_sync._get_or_create_folder`, `spam_filter`, `smtp_sender`, `cleanup_folders`). Cross-emails-Endpoints (`/folders/counts`, `/ai/triage`, `/triage/example`, `_update_folder_unread_count`) verschoben auf 3d. FIXME im `smtp_servers`-Schema: GET-Endpoint reicht aktuell das `password`-Feld ohne `fields`-Filter durch — bestehende Lücke, separat zu adressieren.
- **3d — Mails (laufend, in 3 Sub-Brocken):** `emails`, `attachments`. PB-Rules in 3d.1 vorgeschaltet (all-5 auf `@request.auth.id != ""`).
  - **3d.1 — Reads ✅ (2026-05-20):** 8 Read-Endpoints (`/search`, `/emails`, `/emails/threaded`, `/emails/by-sender`, `/emails/{id}`, `/emails/{id}/attachments`, `/folders/counts`, `/accounts/sent-today`) auf `pb_get_as`. Signed-URL-Endpoints (`/emails/{id}/inline`, `/attachments/{id}/download`) bleiben Admin (kein Bearer im URL). Commit `6933cbe`.
  - **3d.2 — State-Writes ✅ (2026-05-20):** 9 Endpoints (`category`, `bulk/read`, `read`, `spam`/`unspam`, `spam-suggestion/{confirm,dismiss}`, `move`, `DELETE /emails/{id}`) auf `pb_*_as`. Helper `_update_folder_unread_count(token, …)` durchgereicht (8 Call-Sites). `delete_email` nutzt jetzt `pb_delete_as` statt direkten httpx-Call. Commit `f45fbca`.
  - **3d.3 — Drafts + AI + Send ✅ (2026-05-20):** 9 Endpoints (Drafts ×3, AI ×4, Send ×2) auf `pb_*_as`. Background-Helper bleiben Admin und sind im Docstring markiert: `_do_send_job` (überlebt User-Session bei Bulk-Send-Delay), `_do_bulk_send`, `_bulk_record_recipient_result`, `_consolidate_rules`, `_finalize_for_recipient`. `bulk_send_endpoint` mischt User-Token (accounts-Lookup) und Admin (bulk_sends-Audit, wird in 3e migriert). Commit `69cb0f3`.
- **3e — Audit-/Bulk-Collections (offen):** `bulk_sends`, `webhook_logs`, `webhooks`. Backend schreibt (Bulk-Send-Status, Webhook-Log), User liest. Webhook-CRUD aus UI → user-token.

Am Ende: Admin-Token nur noch für IMAP-Sync, Bulk-Backend, Webhook-Send-Backend und ähnliche reine Backend-Operationen.

### A13 — Filter-Escape konsistent

**Problem:** PocketBase-Filter werden teils per String-Interpolation gebaut. `_pb_safe()` existiert, wird aber nicht überall genutzt. Bei Sonderzeichen in Mail-Adressen/Subjects → kaputte Filter, im schlimmsten Fall Filter-Injection mit unerwarteten Treffern.

**Plan:** Alle `filter=`-Stellen auditieren (`grep -n 'filter.*"' backend/`). Eine zentrale `pb_filter(template, **kwargs)`-Funktion bauen, die Werte sicher escaped. Direkt-Interpolationen ersetzen.

---

## B. Robustheit

### B8 — Schema für `webhooks`/`webhook_logs` in `pb_setup.py`

**Problem:** `backend/main.py` nutzt die Collections, aber `pb_setup.py` legt sie nicht an. Neuinstallation/Recovery schlägt unvollständig fehl.

**Plan:** `_webhooks_schema()` und `_webhook_logs_schema()` ergänzen, inkl. unique-Indizes auf `slug` und `api_key`. Gegen frische PB-Instanz testen.

### B9 — Anhänge/Inline-Bilder via BODYSTRUCTURE, nicht ganze Mail

**Problem:** `_imap_fetch_attachment()` und `_imap_fetch_inline_cid()` holen `BODY[]`, also die komplette Mail. Bei großen Anhängen langsam und speicherfressend.

**Plan:** Erst `BODYSTRUCTURE` parsen → MIME-Part-ID des gewünschten Anhangs ermitteln → `BODY[<part-id>]` gezielt fetchen. Bibliothek: `imap_tools` oder direkt `imaplib.fetch(..., '(BODYSTRUCTURE)')`.

### B12 — Schema-Setup als echtes Manifest, nicht historisch gewachsen

**Problem:** Einige Felder werden per `_add_missing_fields()` ergänzt, andere existieren nur historisch. „Läuft nur auf diesem einen PB-Stand."

**Plan:**
- Alle Schemas in `pb_setup.py` vollständig deklarieren (auch jene, die historisch existieren)
- Gegen frische PB-Instanz im Coolify-Setup testen — Dump des aktuellen Schemas als Vergleichsbasis
- Kombiniert mit B8 erledigt

### B14 — Temporäre Uploads (`_temp_uploads`) mit TTL

**Problem:** Anhänge bleiben in-memory, bis send/delete läuft. Browser-Crash oder Abbruch → Speicher belegt.

**Plan:** Hintergrund-Task (`asyncio.create_task` beim Startup) räumt Einträge älter als 30 min auf. Globales Größen-Limit (z.B. 200 MB total). Bei sehr großen Uploads: Disk-Spool (`tempfile.NamedTemporaryFile`) statt RAM.

### B15 — Bulk-Jobs persistent statt in-memory

**Problem:** `_send_jobs` und Bulk-Subjobs leben im Prozessspeicher. Bei Backend-Restart mitten im Bulk gehen offene Sub-Jobs verloren.

**Plan:**
- Job-State in PocketBase persistieren (Collection `send_jobs` mit `status`, `recipient`, `bulk_id`, `next_attempt_at`)
- Beim Backend-Startup: queued/in-progress-Jobs wieder aufnehmen
- Worker-Loop statt Sub-Task pro Job (saubereres Resume)

---

## C. Architektur / Cleanup

### C1 — `backend/main.py` in Router/Services aufteilen

**Problem:** ~3300 Zeilen mit Auth, Mail, IMAP, AI, Webhooks, Templates, Bulk, Kontakte.

**Plan:**
- `routers/mail.py`, `routers/webhooks.py`, `routers/templates.py`, `routers/contacts.py`, `routers/bulk.py`, `routers/admin.py`
- `services/imap.py`, `services/send.py`, `services/pb.py`, `services/ai.py`
- Schrittweise: erst Webhook-Router rausziehen (kleiner Block, klare Grenze), dann Templates, zuletzt der dicke Mail/IMAP-Block

### C2 — Pydantic-Request-Modelle konsequent

**Problem:** Viele Endpoints nehmen `data: dict` und validieren manuell.

**Plan:** Pro Endpoint eigenes Request-Modell (`class SendEmailRequest(BaseModel): ...`). FastAPI generiert dann Validierung, Defaults, OpenAPI-Doku automatisch. Schrittweise — kein Big-Bang.

### C3 — Zentraler IMAP-Service

**Problem:** IMAP-Login/Folder-Auflösung/Executor-Handling ist in Sync, Move, Trash, Read, Draft, Attachments, Sent-Append wiederholt.

**Plan:** `services/imap.py` mit:
- `ImapService.with_account(account_id) → AsyncContextManager` (Login + Cleanup)
- `run_blocking(fn, *args)` (Executor-Wrapper)
- Methoden: `move()`, `trash()`, `set_read()`, `append_sent()`, `fetch_attachment()`, `fetch_inline()`
- Dies behebt auch B9 als Nebeneffekt (BODYSTRUCTURE-Logik zentral)

### C4 — Frontend `inbox.js` weiter zerlegen

**Problem:** ~4000 Zeilen. Inbox, Compose, Webhooks, Spam, Bulk, KI, Attachments.

**Plan:** Reihenfolge nach Schnittstellen-Klarheit:
1. `webhooks.js` (separat, ohnehin eigenes Modal)
2. `compose.js` (Compose-Logik + Bulk + Templates)
3. `email_detail.js` (Detail-Pane inkl. iframe-Höhe, KI-Sidebar)
4. `spam.js` (Spam-Block-Flow)
5. `sse.js` (EventSource + Reconnect, kann von vielen Modulen genutzt werden)

### C5 — Eine einzige Config/Auth-Strategie

**Problem:** Drei Pfade nebeneinander: Backend `/config.js`, alte Frontend `/js/config.js` (heute durch A.B. entfernt), `config.example.js`, nginx-Template. Verwirrt + fehleranfällig.

**Plan:** **Entscheidung an A1 gekoppelt.** Wenn A1 durch ist (PB-Token-Auth), ist `/config.js` komplett tot. Dann:
- `frontend/js/config.example.js` löschen
- `_loadApiKey()` aus `api.js` raus
- Backend-Route `GET /config.js` raus

---

## Reihenfolge-Vorschlag

1. **A1** (Auth-Umbau) — größter Impact, größter Aufwand. Erst danach lassen sich A10, A11, C5 sinnvoll abschließen.
2. **A7, A13, B8, B12** — kleine Hardening-Schritte, parallel zum Refactor möglich.
3. **A6** — sobald Webhooks im Alltag genutzt werden.
4. **C1, C3, C4** — Refactor in der Reihenfolge: erst Webhooks-Router raus (klein), dann IMAP-Service (greift in viele Stellen), dann Frontend-Module.
5. **B14, B15, B9** — Robustheit am Schluss, wenn alles stabil läuft.

---

## Notizen

- Bei jedem Schritt: vorher Coolify-Deploy abwarten, danach kurzen Smoke-Test (Login + eine Mail öffnen + eine senden).
- Atomare Commits — ein logischer Schritt pro Commit, damit Coolify-Rollback einfach bleibt.
- Bei A1: erst PB-Token-Auth einbauen + per Feature-Flag toggleable machen, dann sukzessive Endpunkte umstellen, am Ende globalen API_KEY abschalten.
