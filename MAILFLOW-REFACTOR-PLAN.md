# Mailflow — Refactor- und Hardening-Plan

**Quelle:** GPT-Codereview vom 2026-05-20 (zusammen mit Stefan). Ergänzend zu den vier sofortigen Security-Fixes (Commits `e137884`, `e4659bf`, `940b24b`, `182241d` am 2026-05-20).


## Offen nach großem Review-Check am 2026-05-22

> [!important] Neuer Arbeitsblock
> Diese Liste ist die konsolidierte Resteliste nach dem ersten großen Review-Check am 2026-05-22. Viele ursprüngliche Reviewpunkte sind erledigt; die folgenden Punkte sind die **übrig gebliebenen, noch umzusetzenden Reste**. Der ältere Plan darunter bleibt als Historie/Referenz erhalten.

### R1 — UIDVALIDITY-Cleanup vollständig paginieren ✅ (2026-05-22)

**Umsetzung:** `_delete_emails_for_folder` in `backend/imap_sync.py:484` löscht jetzt iterativ: `page=1, perPage=500, fields=id` laden → Batch löschen → neu laden bis `items` leer. Safeguard gegen Endlosschleife: wenn ein Batch komplett scheitert (`deleted_this_batch == 0`), Abbruch. `except Exception: pass` ersetzt durch `logger.warning(...)` mit Email-ID + Folder. Finaler `logger.info` mit Gesamtzahl pro `(account, folder)`. Smoke folgt beim nächsten echten UIDVALIDITY-Wechsel.

### R2 — Webhook-Endpoints auf Pydantic-Request-Modelle umstellen

**Status:** Pydantic-Migration ist fast vollständig erledigt; Webhooks sind Rest.

**Betroffene Stelle:** `backend/routers/webhooks.py`

Aktuelle `data: dict`-Endpoint-Signaturen:
- `webhook_send(slug, request, data: dict)` — `POST /webhooks/{slug}/send`, auth per `X-Webhook-Key`, rate-limited. Payload: `to`, `subject`, `body`, `body_html`, `reply_to`, `cc`.
- `webhooks_create(data: dict, token=...)` — UI-CRUD, auth per PB-Bearer. Pflicht: `name`, `slug`, `smtp_server`, `from_account`; optional: `default_to`, `from_name_override`, `allow_to_override`, `allow_reply_to`, `allow_cc`, `is_active`.
- `webhooks_update(webhook_id, data: dict, token=...)` — PATCH-Semantik; `rotate_api_key: true` erzeugt neuen `whk_...`-Key.

**Fix-Ziel:**
1. Modelle ergänzen: `WebhookSendRequest`, `WebhookCreateRequest`, `WebhookUpdateRequest`.
2. Bestehendes Verhalten erhalten:
   - Slug-Regex `^[a-z0-9-]+$` weiter erzwingen.
   - Create-Pflichtfelder mit klarer Fehlermeldung.
   - Update mit `model_dump(exclude_unset=True)`.
   - `rotate_api_key` im Update-Modell optional, aber nicht als PB-Feld patchen; stattdessen wie bisher neuen `api_key` generieren.
   - Webhook-Send: leere `body` + leere `body_html` weiterhin ablehnen; `to`, `reply_to`, `cc` abhängig von Webhook-Toggles behandeln.
3. Checks: `rg "data: dict" backend/routers` darf keine Webhook-Endpoint-Treffer mehr liefern; Syntax-/Import-Check.

### R3 — IMAP-Service-Reste zentralisieren

**Status:** `services/imap.py` mit `imap_session(...)`, `run_blocking(...)` und `ImapService` existiert; Reststreuung offen.

**Bereits zentralisiert:** `append_draft`, `fetch_attachment`, `fetch_inline`, `set_read`, `set_answered`, `bulk_set_read`, `move_to_spam`, `move`, `trash`, `fetch_uids_with_msgids`.

**Restpunkte:**
1. `backend/smtp_sender.py`
   - eigene Funktion `_imap_append_sent(acc, msg_bytes)` für Sent-Append.
   - `send_email(...)` ruft sie best-effort per `loop.run_in_executor(None, _imap_append_sent, acc, msg_bytes)` auf.
2. `backend/idle_manager.py`
   - direkte `IMAPClient(...)`-Login-/IDLE-Logik.
   - IDLE darf wegen langlebiger Verbindung separat bleiben, aber Login/Cleanup sollte entweder `imap_session` nutzen oder bewusst dokumentieren, warum nicht.
3. `imap_sync.py` und `backfill.py`
   - nutzen bereits `imap_session`; kein zwingender Umbau, nur Doppelungen bei Gelegenheit prüfen.

**Fix-Ziel:**
- `ImapService.append_sent(msg_bytes)` ergänzen (Sent-Ordner via `find_imap_folder(... [b"\\Sent"] ... )`, `flags=[b"\\Seen"]`, `msg_time=datetime.now(timezone.utc)`).
- `_imap_append_sent` entfernen oder zu dünnem Wrapper machen; `send_email(...)` soll `ImapService(acc).append_sent` im Executor starten.
- `idle_manager.py` prüfen und entweder auf `imap_session(acc)` umstellen oder die Ausnahme kommentieren.
- Checks: `rg -n "IMAPClient|imap_session|_imap_append_sent|append_sent" backend/smtp_sender.py backend/idle_manager.py backend/services/imap.py`.

### R4 — Veraltetes `embed-search-test.html` an Admin-Key-Middleware anpassen oder löschen

**Status:** `/admin/*` ist sicher auf `X-Admin-Key` + `ADMIN_API_KEY` umgestellt; altes Test-HTML nutzt noch Query-Key.

**Betroffene Stelle:** `embed-search-test.html`

Aktuell sinngemäß:

```js
fetch(`${url}/admin/embed-search?key=${encodeURIComponent(key)}&limit=${limit}&q=${encodeURIComponent(q)}`)
```

**Problem:** `?key=` wird für `/admin/*` nicht mehr akzeptiert. Das Testtool ist kaputt/verwirrend und dokumentiert ein altes Auth-Muster.

**Fix-Ziel — eine Variante wählen:**
1. **Anpassen:**
   ```js
   fetch(`${url}/admin/embed-search?limit=${limit}&q=${encodeURIComponent(q)}`, {
     headers: { 'X-Admin-Key': key }
   })
   ```
   UI-Text von „API Key“ auf „Admin Key (`X-Admin-Key`)“ ändern; keine Keys in URL/LocalStorage/Beispielen speichern.
2. **Oder löschen**, falls das Testtool nicht mehr gebraucht wird; danach prüfen, ob Doku darauf verweist.

**Wichtig:** Nicht die `/admin/*`-Middleware abschwächen. Checks: `rg "\?key=|X-Admin-Key|embed-search" embed-search-test.html backend/routers/admin.py backend/main.py`.

### R5 — Fresh-PocketBase-Schema-Lücken in `pb_setup.py` schließen

**Status:** `pb_setup.py` ist weitgehend Manifest; Fresh-Install hat aber noch Lücken, weil `existing` am Anfang leer ist.

**Problem:** `_ensure_collection(...)` legt Collections im selben Lauf an, aber spätere `if "..." in existing:`-Migrationsblöcke greifen nur für Collections, die **vor** dem Lauf existierten. Frische PB-Instanzen können dadurch unvollständig bleiben.

**Konkret gefundene Lücken:**
1. `contacts.groups`
   - nicht in `_contacts_schema()` definiert.
   - wird nur via `_add_missing_fields(... "contacts" ...)` mit Relation auf `contact_groups_id` ergänzt.
2. `contacts.unsubscribed`
   - nicht in `_contacts_schema()` definiert.
   - wird nur im gleichen Missing-Fields-Block ergänzt.
3. `emails.webhook`
   - Relation auf `webhooks`, daher nicht in `_emails_schema(accounts_id)`.
   - wird nur ergänzt, wenn `"emails" in existing and "webhooks" in existing`.

**Fix-Ziel:**
- Fresh-Install und bestehende Instanz müssen denselben Endzustand erreichen.
- Einfacher Ansatz: `_ensure_collection` oder Caller aktualisiert `existing[schema["name"]] = collection_id` nach jeder Anlage. Danach greifen die bestehenden Missing-Fields-Blöcke im selben Lauf.
- `contacts.groups`/`contacts.unsubscribed` auch bei frischer Collection ergänzen.
- `emails.webhook` nach Anlage von `webhooks` ergänzen (Relation braucht `webhooks_id`).
- Nicht destruktiv arbeiten; nur additive Felder/Rules/Indexes patchen.
- Checks: `rg -n "groups|unsubscribed|webhook|_add_missing_fields|existing\[" backend/pb_setup.py`; optional frische PB-Instanz starten und Felder kontrollieren.

### R6 — Guardrail-Test/Linter für PocketBase-Filter-Escaping ergänzen

**Status:** Code nutzt weitgehend `pb_client.pb_quote(...)`; Guardrail gegen Regression fehlt.

**Problem:** Neue direkte Filter-Interpolation wie `params={"filter": f'email="{email}"'}` könnte wieder Sonderzeichen-Bugs oder Filter-Injection ermöglichen.

**Fix-Ziel:** kleines statisches Script ergänzen, z.B. `backend/tests/check_pb_filters.py` oder `scripts/check_pb_filters.py`.

**Pragmatischer Ansatz:**
1. Scan `backend/**/*.py` nach verdächtigen Filter-f-Strings:
   - Zeilen mit `"filter"`, `params["filter"]`, `filter_expr`, `history_filter`
   - und f-String mit `{...}`
   - ohne `pb_quote` in derselben Zeile/kurzer Nähe.
2. Whitelist/Ignore für legitime Fälle:
   - statische Bool-Filter: `is_new=true`, `bounced=true`, `is_done!=true`, `has_attachments=true`, `folder="Sent"`.
   - zusammengesetzte Filter aus bereits gequoteten Teilen (`" && ".join(filters)`, `history_filter`, `filter_expr`) per Inline-Kommentar `# pb-filter-safe` oder Allowlist.
3. Exit-Code `1`, wenn neue verdächtige Treffer auftauchen.
4. Optional README/Plan-Hinweis: neue PB-Filter immer mit `pb_client.pb_quote(...)`; Script laufen lassen.

**Checks:**
```bash
python3 backend/tests/check_pb_filters.py   # oder scripts/check_pb_filters.py
rg -n 'filter.*f' backend --glob '*.py'
rg -n 'params\["filter"\]' backend --glob '*.py'
```

---

## Status (Stand 2026-05-22 abend)

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
- ✅ A11 Phase 3e — Audit-/Bulk-Cluster (bulk_sends/webhooks/webhook_logs): 3 PB-Rules + 8 User-Endpoints, Backend-Schreiber dokumentiert. **Damit ist A11 komplett** (Commit `76c1676`).
- ✅ Backlog A11-Nachzügler (2026-05-20) — `GET /smtp-servers` `fields=id,name,is_default`-Whitelist in `main.py`, FIXME im `_smtp_servers_schema` entfernt. `password` (und übrige Credentials) erreichen das Frontend nicht mehr.
- ✅ B14 Phase 1 (2026-05-20) — `_temp_uploads` mit TTL (30 min) + Gesamtlimit (200 MB) + Sweep-Coroutine im `lifespan`. Phase 2 (Disk-Spool) zurückgestellt, mit 200-MB-Cap nicht akut.
- ✅ B15 (2026-05-20) — Bulk-Jobs persistent: `next_attempt_at` + `job_id` pro Empfänger, `_bulk_worker_loop` im `lifespan`, `_bulk_restart_cleanup` für has_attachments-Bulks. `_do_bulk_send` raus.
- ✅ **C3 Phase 2** (2026-05-21, Commit `9e711da`) — `ImapService`-Klasse in `services/imap.py` bündelt alle 10 blocking-IMAP-Methoden, die vorher als `_imap_*_sync` in main.py lagen. main.py 3736 → 3560 Zeilen, Verhalten 1:1.
- ✅ **B9** (2026-05-21, Commit `a2c8eea`) — Anhang/Inline gezielt via BODYSTRUCTURE statt `BODY[]`. Walker DFS-kompatibel zu `email.message.walk()`, Fallback auf alten Pfad bei fehlender/unbrauchbarer Struktur. Side-Find: Inline-Bilder waren seit A11 stillschweigend kaputt (Doppel-`?` in Signed-URL) — separat in `b340a6f` gefixt.
- ✅ **C2 Phase 1 + Phase 2** (2026-05-21, Commits `ad0e942` + `868ef0a`) — 13 von 21 `data: dict`-Endpoints auf typisierte Pydantic-Request-Modelle umgestellt. Begleit-Handler für `RequestValidationError` flacht das Error-Array zu `{"detail": "..."}`, kompatibel zum bestehenden Frontend-Error-Handling.
- ✅ **C1 Phase 2 komplett** (2026-05-22) — alle Mail-Endpoints aus main.py rausgezogen. main.py **3723 → 281 Zeilen (−92 %)**, übrig bleibt reiner FastAPI-Bootstrap (lifespan, middleware, exception-handler, router-includes). 7 neue Router + 1 Service-Modul:
  - `routers/admin.py` (Commit `f4580f2`) — `/admin/*` (4 Endpoints)
  - `routers/templates.py` (Commit `8697b19`) — `/variables`, `/snippets`, `/templates`, `/templates/render` (17)
  - `routers/contacts.py` (Commit `1b2d178`) — `/contacts/*`, `/contact-groups/*`, `/contacts/import` (9)
  - `routers/bulk.py` (Commit `e47bba4`) — `/bulk-sends/*` CRUD (3)
  - `routers/system.py` (Commit `698cd30`) — Infrastruktur: health, sign, sync, events, accounts, smtp-servers, folders, xano (12)
  - `routers/ai.py` (Commit `7ae94f4`) — categories, ai/*, triage/example, response-patterns (7)
  - `services/mail.py` (Commit `2360747`) — Cross-cutting Helpers (state-dicts, send-pipeline, bulk-worker-loop, bounce-match, imap-aktions-helper) — Voraussetzung für 5c.2
  - `routers/mail.py` (Commit `960cacd`) — `/search`, `/emails/*`, `/attachments/*`, `/spam-rules/*` (26)
- ✅ **Schema-Migration** (2026-05-22, Commit `522bde6`) — `idx_emails_message_id (message_id)` UNIQUE → `idx_emails_account_folder_message_id (account, folder, message_id)` UNIQUE. Same-Mail darf jetzt in Sent (Sender) **und** INBOX (Empfänger via Alias) desselben Mailflow-Accounts liegen. Migration läuft idempotent beim Startup via `_swap_index`-Helper in `pb_setup.py`. Plus: `_fetch_and_save` schreibt bei `DuplicateRecordError` jetzt einen INFO-Log statt still zu schlucken (`imap_sync.py`).
- ✅ **Diagnose-Tab + Boundary-Filter** (2026-05-22, Commit `0fe03cb`) — neuer Tab in der Topbar zeigt letzte ~500 Sync-Skips/Fetch-Errors aus dem Backend-Ringpuffer (`GET /diagnostics/sync-skips`). IMAP-`N:*`-Quirk gefiltert: `_sync_folder` schmeißt UIDs ≤ `last_sync_uid` nach dem search raus → keine sinnlosen FETCHes + leeres Diagnose-Panel im Normalbetrieb.
- ✅ **SMTP-Recipient-Parser-Fix** (2026-05-22, Commit `479c82d`) — Komma im Display-Name (z.B. `Stefan Barres, HPA24 <addr>`) wurde durch naives `split(",")` zerrissen, erstes Fragment ohne `@` lief in 553 5.7.1. Fix via `email.utils.getaddresses()` + Filter auf `@`.
- ✅ **Per-(account, folder)-Lock im Sync** (2026-05-22, Commit `a469ddc`) — `_folder_sync_locks: dict[tuple[str, str], asyncio.Lock]` via `setdefault`, `_sync_folder`-Body in `async with`. Serialisiert Scheduler-Sync + IDLE-Sync auf demselben Ordner; die zuvor im Diagnose-Tab sichtbare Race ist weg.
- ✅ **C2 Phase 3 — alle 21 Endpoints typisiert** (2026-05-22, Commit `ca3230e`). Letzte 7 Endpoints auf Pydantic: `UpdateAccountRequest` (system.py), `SendEmailRequest`, `BulkSendRequest`, `CreateDraftRequest`, `UpdateDraftRequest` (mail.py), `ContactsImportRequest` (contacts.py), `TemplatesRenderRequest` (templates.py). Verhaltensänderung 400 → 422 bei Validierungsfehlern, Body bleibt `{"detail": "..."}` dank des `RequestValidationError`-Handlers. **Damit ist C2 komplett.**
- ✅ **fix(compose) — empty-To-Validierung sichtbar** (2026-05-22, Commit `c0195b2`). `saveDraft`-Erfolg schedulte einen unbedingten 2-s-Timer, der `#draft-status` blank machte — Validierungs-Meldungen direkt nach Auto-Save verschwanden 0–2 s später. Fix: nur löschen, wenn der Status noch „Entwurf gespeichert" lautet. Zusätzlich springt der Cursor jetzt ins leere Feld (An oder Betreff), nicht nur die Statuszeile aktualisiert.
- ✅ **C4 Phase 2 — 4 JS-Module aus inbox.js** (2026-05-22, Commits `999b28e` / `3fa9b2d` / `0e31409` / `1c17da4` / `606b1ae` / `58a8b51`). In 6 Schritten:
  - `sse.js` (41 Z.) — `startEventSource` (Realtime-Push für new-mail + send-result).
  - `spam.js` (150 Z.) — `spamEmail`, `setupSpamRules`, `loadSpamRulesCount`, `openSpamRulesModal`, `closeSpamRulesModal`, `renderSpamRules`.
  - `email_detail.js` (314 Z.) — `openEmail` (mit sandbox-Iframe-Rendering + CID-Inline + Resize-Observer), `updateReadToggle`, `linkify`, `showEmpty`.
  - `compose.js` (1351 Z.) in drei Sub-Commits: Phase A core (Send-Notifications, Toolbar, Confirm-Dialog, openCompose/closeCompose/saveDraft/scheduleDraftSave, btn-send-inline Listener), Phase B bulk (Massenversand-Pipeline + Test-Send + `mfComposeResend`), Phase C attachments + chip-fields (Drag&Drop + `makeAddressField` + `_toField`/`_ccField`).
  - Endstand: **`inbox.js`: 3825 → 2031 Zeilen (−47 %)**. Aufruf-Konvention: alles über shared global lexical environment, neue Module laden vor `inbox.js`.

**Offen — nächster Chat:**
- **D-Button (Domain-Block)** — dritter Spam-Button mit Provider-Schutzliste. Plan in `Wissen/81_ToDo/mailflow-spam-offen.md` Punkt 1.
- **B14 Phase 2** (optional) — Disk-Spool via `tempfile.NamedTemporaryFile` für Uploads >200 MB. Aktuell nicht akut. Bonus: würde Bulk-Resume *mit* Anhängen erlauben (siehe B15-Restriktion).
- **Mini-UX-Bug:** Adress-Chip Komma sollte beim offenen Autocomplete die markierte Adresse einsetzen (wie Return/Tab), nicht den getippten Rohtext (siehe `gleich-erledigen.md` Eintrag 2026-05-22).

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
- **3e — Audit-/Bulk-Collections ✅ (2026-05-20):** `bulk_sends`, `webhooks`, `webhook_logs`. 3 PB-Rules-Migrationen, 8 User-Endpoints (3 bulk_sends in main.py, 5 webhooks im Router). Backend-Pfade bleiben Admin und sind dokumentiert: `bulk_send_endpoint`-Audit-Write, `_bulk_record_recipient_result`, `_webhook_by_slug`, `_webhook_log`, `imap_sync._webhook_id_for_message`. Commit `76c1676`.

Am Ende: Admin-Token nur noch für IMAP-Sync, Bulk-Backend, Webhook-Send-Backend und ähnliche reine Backend-Operationen.

**A11 abgeschlossen (2026-05-20):** Alle 16 PocketBase-Collections via Code-Schema + `_ensure_rules` auf `@request.auth.id != ""` umgestellt. Frontend-Endpoints durchgehend im User-Token-Kontext (`pb_*_as`). Admin-Pfade (mit Begründung im Docstring): IMAP-Sync, Bulk-Send-Background, Webhook-Send mit X-Webhook-Key, Kontakt-Import mit X-Import-Key, Signed-URL-Endpoints, `/admin/*`.

### A13 — Filter-Escape konsistent

**Problem:** PocketBase-Filter werden teils per String-Interpolation gebaut. `_pb_safe()` existiert, wird aber nicht überall genutzt. Bei Sonderzeichen in Mail-Adressen/Subjects → kaputte Filter, im schlimmsten Fall Filter-Injection mit unerwarteten Treffern.

**Plan:** Alle `filter=`-Stellen auditieren (`grep -n 'filter.*"' backend/`). Eine zentrale `pb_filter(template, **kwargs)`-Funktion bauen, die Werte sicher escaped. Direkt-Interpolationen ersetzen.

---

## B. Robustheit

### B8 — Schema für `webhooks`/`webhook_logs` in `pb_setup.py`

**Problem:** `backend/main.py` nutzt die Collections, aber `pb_setup.py` legt sie nicht an. Neuinstallation/Recovery schlägt unvollständig fehl.

**Plan:** `_webhooks_schema()` und `_webhook_logs_schema()` ergänzen, inkl. unique-Indizes auf `slug` und `api_key`. Gegen frische PB-Instanz testen.

### B9 — Anhänge/Inline-Bilder via BODYSTRUCTURE, nicht ganze Mail ✅ (erledigt 2026-05-21, Commit `a2c8eea`)

**Umgesetzt:**
- `services/imap.py`: Helper `_walk_bodystructure` (DFS, kompatibel zu `email.message.walk()`), `_decode_part_body` (base64/quoted-printable), `_is_attachment_leaf` (Disposition=attachment ODER filename/name).
- `ImapService.fetch_attachment` und `fetch_inline` holen jetzt zuerst `BODYSTRUCTURE` (~1 KB), bestimmen die MIME-Part-ID des Ziels und fetchen gezielt `BODY[<part-id>]`. Decoding via Encoding-Feld aus der BODYSTRUCTURE.
- Fallback auf `BODY[]` bei: keine BODYSTRUCTURE, part_index außerhalb, CID nicht gefunden — Aufrufer verlieren nichts.
- Log-Zeile pro Fetch zeigt part_id + Rohbytes + Encoding → erlaubt Live-Beobachtung der Übertragungsmenge.

**Bekannte Restriktion:** Eingebettete `message/rfc822` (z.B. weitergeleitete Mails mit eigenen Anhängen) werden vom Walker als Leaf behandelt, nicht hineinrekursiert. Bei normalem Mailverkehr irrelevant; bei Bedarf später Rekursion ergänzen (Position 8 im message/rfc822-Leaf-Tupel).

**Side-Find während des manuellen Tests (separat in `b340a6f` gefixt):** Inline-Bilder waren seit der A11-Umstellung gar nicht sichtbar. Ursache: `_signUrl` in `frontend/js/api.js` hängte `?token=` immer mit `?` an — bei `inlineImageUrl` enthielt der Pfad bereits `?cid=`, das ergab `…/inline?cid=X?token=Y` (zwei `?`), Browser parste token als Teil des cid-Werts, Auth-Middleware antwortete 401. Fix: `_signUrl(path, ttl, extraParams)`, `inlineImageUrl` übergibt `cid` als Extra-Param.

### B12 — Schema-Setup als echtes Manifest, nicht historisch gewachsen

**Problem:** Einige Felder werden per `_add_missing_fields()` ergänzt, andere existieren nur historisch. „Läuft nur auf diesem einen PB-Stand."

**Plan:**
- Alle Schemas in `pb_setup.py` vollständig deklarieren (auch jene, die historisch existieren)
- Gegen frische PB-Instanz im Coolify-Setup testen — Dump des aktuellen Schemas als Vergleichsbasis
- Kombiniert mit B8 erledigt

### B14 — Temporäre Uploads (`_temp_uploads`) mit TTL

**Problem:** Anhänge bleiben in-memory, bis send/delete läuft. Browser-Crash oder Abbruch → Speicher belegt.

**Phase 1 ✅ (2026-05-20):** Hintergrund-Sweep-Coroutine im `lifespan` räumt Einträge älter als 30 min auf, globales 200-MB-Cap. Reicht im Alltag.

**Phase 2 (offen, optional):** Disk-Spool via `tempfile.NamedTemporaryFile` für sehr große Uploads, damit Bulk-Resume *mit* Anhängen funktioniert (siehe B15-Restriktion `backend_restart_with_attachments`). Aktuell mit 200-MB-Gesamtcap nicht akut.

### B15 — Bulk-Jobs persistent statt in-memory ✅ (erledigt 2026-05-20)

**Umgesetzt:**
- `bulk_sends.recipients[i]` um `next_attempt_at` (ISO-Datum) + `job_id` (UUID) erweitert — keine zweite Collection nötig.
- `bulk_sends` um `has_attachments` (bool) und `is_done` (bool) erweitert (Schema + Migration via `_add_missing_fields` in `pb_setup.py`).
- `_bulk_worker_loop()` läuft im `lifespan`, pollt alle 1 s `is_done!=true` und spawnt `_do_send_job` pro fälligem Empfänger (`status=queued && next_attempt_at <= now`). Lease via `next_attempt_at = now + 5 min` schützt vor Doppel-Pick.
- `_bulk_restart_cleanup()` markiert beim Start `queued`-Empfänger von Aussendungen mit Anhängen als `error: backend_restart_with_attachments` (Anhänge sind in-memory).
- `bulk_send_endpoint` setzt `next_attempt_at = start + idx*delay` pro Empfänger, `_do_bulk_send` ersatzlos entfernt.
- `_bulk_record_recipient_result` setzt `is_done=true` und gibt `_bulk_attachments_by_id[bulk_send_id]` frei, sobald alle terminal sind.
- `_send_jobs`-Hybrid: bleibt in-memory für SSE-Events; persistente Wahrheit ist `recipients[]`.

**Bekannte Restriktion:** Resume mit Anhängen fällt der `_bulk_restart_cleanup` zum Opfer. Lift via B14 Phase 2 (Disk-Spool) möglich.

---

## C. Architektur / Cleanup

### C1 — `backend/main.py` in Router/Services aufteilen

**Problem:** ~3500 Zeilen mit Auth, Mail, IMAP, AI, Webhooks, Templates, Bulk, Kontakte.

**Plan:**
- `routers/mail.py`, `routers/webhooks.py`, `routers/templates.py`, `routers/contacts.py`, `routers/bulk.py`, `routers/admin.py`
- `services/imap.py`, `services/send.py`, `services/pb.py`, `services/ai.py`
- Schrittweise: erst Webhook-Router rausziehen (kleiner Block, klare Grenze), dann Templates, zuletzt der dicke Mail/IMAP-Block

**Phase 1 ✅ (früher):** `routers/webhooks.py` rausgezogen.

**Phase 2 ✅ (2026-05-22):** Alle 5 ursprünglich geplanten Router rausgezogen — plus 2 zusätzliche aus der Zerlegung (System, AI) und 1 Service-Modul (services/mail.py als Zirkular-Import-Brecher für 5c.2). Commits: `f4580f2` (admin), `8697b19` (templates), `1b2d178` (contacts), `e47bba4` (bulk), `698cd30` (system / 5a), `7ae94f4` (ai / 5b), `2360747` (services/mail / 5c.1), `960cacd` (mail / 5c.2). main.py: 3723 → 281 Zeilen.

### C2 — Pydantic-Request-Modelle konsequent

**Problem:** Viele Endpoints nehmen `data: dict` und validieren manuell.

**Plan:** Pro Endpoint eigenes Request-Modell (`class SendEmailRequest(BaseModel): ...`). FastAPI generiert dann Validierung, Defaults, OpenAPI-Doku automatisch. Schrittweise — kein Big-Bang.

**Phase 1 ✅ (2026-05-21, Commit `ad0e942`):** 3 Endpoints (`set_category`, `move_email`, `variables_create`) + `RequestValidationError`-Handler, der das Error-Array zu `{"detail": "..."}` flacht (kompatibel zum `apiFetch`-Error-Handling im Frontend, das sonst `[object Object]` zeigen würde).

**Phase 2 ✅ (2026-05-21, Commit `868ef0a`):** 10 weitere Endpoints — `variables_update/rename`, `snippets_create/update/rename`, `templates_create/update`, `contact_groups_create/update`, `save_triage_example`. Update-Endpoints nutzen `Optional`-Felder + `model_dump(exclude_unset=True)` für PATCH-Semantik. Name-Normalisierung pro Collection in `_normalize_<x>_name`-Helpers konsolidiert.

**Phase 3 ✅ (2026-05-22, Commit `ca3230e`):** 7 komplexere Endpoints — `UpdateAccountRequest` (system.py), `SendEmailRequest`, `BulkSendRequest`, `CreateDraftRequest`, `UpdateDraftRequest` (mail.py), `ContactsImportRequest` (contacts.py), `TemplatesRenderRequest` (templates.py). Verhalten 400 → 422 bei Validierungsfehlern; Body bleibt `{"detail": "..."}` dank des `RequestValidationError`-Handlers. **Damit ist C2 komplett — alle 21 ehemaligen `data: dict`-Endpoints typisiert.**

### C3 — Zentraler IMAP-Service

**Phase 1 (erledigt früher):** `imap_session(acc)`-Context-Manager als zentrales Login. Genutzt von main.py, imap_sync.py, backfill.py, smtp_sender.py.

**Phase 2 ✅ (erledigt 2026-05-21, Commit `9e711da`):** `ImapService`-Klasse in `services/imap.py` bündelt alle blocking-IMAP-Methoden, die vorher als `_imap_*_sync` in `main.py` lagen:
- `append_draft`, `fetch_attachment`, `fetch_inline`, `set_read`, `set_answered`, `bulk_set_read`, `move_to_spam`, `move`, `trash`, `fetch_uids_with_msgids`
- Privater Helper `_search_by_msgid`
- Methoden sind blocking; main.py-Wrapper rufen `asyncio.to_thread(ImapService(acc).method, ...)`
- main.py 3736 → 3560 Zeilen (−176). Verhalten 1:1, kein Logik-Change.
- B9 als Nebeneffekt: BODYSTRUCTURE-Logik lebt jetzt zentral in `fetch_attachment` / `fetch_inline` (siehe oben).

Nicht migriert: `imap_session` selbst (weiter genutzt von imap_sync.py, backfill.py, smtp_sender.py — eigene Code-Pfade, out of scope).

### C4 — Frontend `inbox.js` weiter zerlegen

**Problem:** ~4000 Zeilen. Inbox, Compose, Webhooks, Spam, Bulk, KI, Attachments.

**Plan:** Reihenfolge nach Schnittstellen-Klarheit:
1. `webhooks.js` (separat, ohnehin eigenes Modal)
2. `compose.js` (Compose-Logik + Bulk + Templates)
3. `email_detail.js` (Detail-Pane inkl. iframe-Höhe, KI-Sidebar)
4. `spam.js` (Spam-Block-Flow)
5. `sse.js` (EventSource + Reconnect, kann von vielen Modulen genutzt werden)

**Phase 1 ✅ (früher):** `webhooks.js` rausgezogen.

**Phase 2 ✅ (2026-05-22, Commits `999b28e` / `3fa9b2d` / `0e31409` / `1c17da4` / `606b1ae` / `58a8b51`):**
- `sse.js` (41 Z.) — `startEventSource`
- `spam.js` (150 Z.) — `spamEmail` + Spam-Rules-Verwaltung
- `email_detail.js` (314 Z.) — `openEmail`, sandbox-Iframe, CID-Inline, `updateReadToggle`, `linkify`, `showEmpty`
- `compose.js` (1351 Z., 3 Sub-Commits A/B/C) — Toolbar, Send-Notifications, openCompose/closeCompose/saveDraft, Massenversand-Pipeline, Test-Send, Attachments, Drag&Drop, `makeAddressField`

inbox.js: 3825 → 2031 Zeilen (−47 %). Module laden vor `inbox.js`; Cross-Modul-Calls funktionieren über shared global lexical environment klassischer Script-Tags (kein ES-Module-Setup nötig).

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
- **Fix-forward statt Rollback:** Stefan arbeitet grundsätzlich vorwärts — bei Problemen Fehler suchen und korrigieren, nicht per `git revert` zurückrollen. Commits müssen daher nicht zwingend atomar pro Refactor-Schritt sein; saubere logische Schritte helfen aber beim Lesen der Historie und bei der Fehlersuche.
- Bei A1: erst PB-Token-Auth einbauen + per Feature-Flag toggleable machen, dann sukzessive Endpunkte umstellen, am Ende globalen API_KEY abschalten.
