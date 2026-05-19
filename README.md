# Mailflow

E-Mail-Client auf Basis von FastAPI + PocketBase + Vanilla JS, deployed via Coolify.

**Dokumentation:** `~/Syncthing/Claude/Wissen/20_Apps/mailflow/`

| Datei | Inhalt |
|---|---|
| `README.md` | Architektur, Collections, Suche, Performance, Fallstricke, offene Punkte |
| `briefing.md` | Ursprüngliches Briefing, vollständiges PocketBase-Schema |
| `MAILFLOW-KIINTEGRATION-PLAN.md` (im Repo) | KI-Triage, Antwortvorschlag, Xano-Plan |

## Sicherheit

Auth-Pattern, PocketBase-Rules und n8n-Tokens folgen dem zentralen Modell in `~/Syncthing/Claude/Wissen/20_Apps/_shared/sicherheit.md`.

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

### Reply-To-Warnung im Compose

Wenn eine eingehende E-Mail einen `Reply-To`-Header hat, der sich von der `From`-Adresse unterscheidet (z. B. interne Routing-Adressen wie `Gerhard@smtp2.mailbox.org`), wird beim Öffnen der Antwort ein gelber Hinweisbalken eingeblendet:

> „Hinweis: Diese E-Mail wird an die Reply-To-Adresse gesendet (X), nicht an die Absenderadresse (Y)."

- **Datei:** `frontend/js/inbox.js` — `openCompose()` bekommt Parameter `replyToFromEmail`; Reply-Handler berechnet `replyToFromEmail = (full.reply_to && full.reply_to !== full.from_email) ? full.from_email : null`
- **HTML:** `<div id="ci-replyto-warning">` in `index.html` nach den Compose-Feldern
- **CSS:** `#ci-replyto-warning` in `main.css` (gelb, Border-left)

## Massenversand 2026-05-13

Dieselbe E-Mail einzeln an viele Empfänger versenden, mit 5 s Abstand pro Mail — jeder Empfänger sieht nur sich selbst im `To`-Header (keine CC/BCC-Vermischung).

### Bedienung

In der Compose-Action-Bar Button **„Massenversand“** → Modal mit Textarea (eine Adresse pro Zeile, `Name <addr>` erlaubt). Bei Übernahme ersetzt ein gelber Banner das normale „An“-Feld („Massenversand aktiv: N Empfänger“, mit „Bearbeiten“ und „✕“). Beim Klick auf „Senden“ öffnet sich ein Status-Modal mit Live-Updates pro Adresse (✓/✗), Summary-Zeile (`X gesendet · Y Fehler · Z ausstehend`), und am Ende den Buttons „Fehlgeschlagene kopieren“ (Clipboard), „Fehlgeschlagene erneut versuchen“, „Schließen“. Die Liste ist sortiert: Erfolge oben, Fehler unten — letztere lassen sich so direkt rauskopieren.

### Backend

Neuer Endpoint **`POST /emails/bulk-send`** akzeptiert `recipients: list[str]`, `delay_seconds` (default 5, hard cap 300) plus die üblichen Felder wie `/emails/send`. Adressen werden normalisiert, dedupliziert und per Regex validiert (400 bei ungültigen Einträgen). Für jeden Empfänger wird ein eigener Eintrag in `_send_jobs` mit `status: "queued"` und gemeinsamer `bulk_id` angelegt und `(job_id, to)` zurückgegeben. `_do_bulk_send` startet die Sub-Jobs sequentiell via `asyncio.create_task(_do_send_job(...))` mit `asyncio.sleep(delay_seconds)` dazwischen — keine neue SMTP- oder SSE-Logik, jeder Sub-Job feuert sein eigenes `send-result`-Event.

Details der Sub-Job-Erzeugung: nur der **erste** Sub-Job behält `draft_id` und `in_reply_to_email_id` (Entwurf wird einmal gelöscht, ein eventuelles Original einmal als beantwortet markiert). `attachment_ids` werden in allen Sub-Jobs auf `[]` gesetzt; die Bereinigung von `_temp_uploads` übernimmt `_do_bulk_send` einmal am Ende, sonst würde der erste Sub-Job die Datei-Refs der nachfolgenden zerstören. `cc` wird im Bulk-Modus serverseitig auf `""` gezwungen.

### Frontend

- **`api.js`:** `bulkSendEmail(data)` → `/emails/bulk-send`.
- **`index.html`:** Action-Bar-Button `#btn-bulk`, Banner `#ci-bulk-banner` im An-Zeilen-Container (ersetzt `#ci-to-field` per `display:none`), Eingabe-Modal `#bulk-modal-overlay` und nicht-blockierendes Floating-Panel `#bulk-status-panel` (unten rechts, durch Header-Klick einklappbar — `.minimized` blendet Body+Footer aus). Während der Bulk läuft, bleibt die übrige UI bedienbar.
- **`inbox.js`:** State `_bulkRecipients` (aktive Liste) und `_bulkTracking = { byJobId, byAddr, compose }` (laufender Versand). `_parseBulkInput` splittet nach `\n`/`,`/`;`, validiert mit `_EMAIL_RE`, dedupliziert. Der bestehende `btn-send-inline`-Handler zweigt früh in `_sendBulk()` ab, wenn `_bulkRecipients.length > 0`. **SSE-Hook in `_handleSendResult`:** ist die `job_id` in `_bulkTracking.byJobId`, übernimmt das Status-Panel die Anzeige und die normale Send-Notif wird unterdrückt. `closeCompose()` ruft `_clearBulkMode()`, sodass Bulk-State nicht zwischen Compose-Sitzungen leakt.
- **Retry:** beim Klick auf „Fehlgeschlagene erneut versuchen“ werden die alten `job_id`s der fehlgeschlagenen Adressen aus `byJobId` entfernt (vermeidet Race mit verspäteten SSE-Events) und `_bulkStart(failed, snapshot)` neu aufgerufen — mit `draft_id: null` und `attachment_ids: []`, da beides beim ersten Lauf konsumiert wurde.

### Bewusst nicht gebaut

- **Platzhalter** (`{{name}}` etc.) — braucht zweispaltige Eingabe (Adresse + Daten), kommt später.
- **Backend-Persistenz** der Bulk-Jobs — `_send_jobs` ist in-memory. Bei Backend-Restart mitten im Bulk gehen offene Sub-Jobs verloren. Bei 5 s × N ist das Fenster klein; bei Bedarf später in PocketBase verlagern.
- **Progress-Bar** im Status-Panel — die Summary-Zeile reicht.

## Webhooks (externer Mail-Versand) 2026-05-15

Externe Workflows (Xano, Webseiten-Kontaktformulare, Buchungssysteme) lösen den Versand über einen eigenen, pro Use-Case konfigurierten Endpoint aus — als Ablösung von Make. Eine Webhook-Konfig bündelt SMTP-Server, Absender-Account, optionale Default-Empfänger, Override-Berechtigungen und einen eigenen `api_key`. Versand läuft durch dieselbe `smtp_sender.send_email`-Pipeline wie die UI, daher landet jede Mail wie gewohnt im Sent-Ordner per IMAP APPEND.

### Endpoint

**`POST /webhooks/{slug}/send`** — von der globalen Frontend-API-Key-Middleware ausgenommen, validiert eigenen Key per `X-Webhook-Key`-Header (`secrets.compare_digest`). Payload-Felder: `to`, `subject`, `body` und/oder `body_html`, optional `reply_to`, `cc`. Override-Felder werden nur akzeptiert wenn der entsprechende Toggle im Webhook aktiv ist (`allow_to_override`, `allow_reply_to`, `allow_cc`) — sonst kommt der Wert aus der Webhook-Konfig (`default_to`) oder bleibt leer. `to` darf payload-seitig nur überschrieben werden wenn das Feld nicht leer ist, sonst greift `default_to`.

Bei `is_active=false` oder unbekanntem Slug wird bewusst `401 Unauthorized` zurückgegeben (kein 404), damit Slug-Existenz nicht durch Fehlercodes leakt.

### Collections

- **`webhooks`** (`pbc_3653375940`) — `name`, `slug` (unique, `^[a-z0-9-]+$`), `smtp_server` (rel), `from_account` (rel), `default_to`, `from_name_override`, `allow_to_override`/`allow_reply_to`/`allow_cc` (bool), `api_key` (unique, generiert als `whk_` + `secrets.token_urlsafe(32)`), `is_active`. Indexe: unique auf `slug` und `api_key`.
- **`webhook_logs`** (`pbc_305862465`) — `webhook` (rel, cascadeDelete), `ip`, `status` (`success`/`error`), `to`, `subject`, `error`, `message_id`, `email` (rel zur `emails`-Collection, optional). Jeder externer Aufruf — auch Validierungsfehler ohne Versand — wird hier protokolliert.

### Reply-To-Header

`smtp_sender.send_email` hat neuen Parameter `reply_to: str = ""`; wenn gesetzt wird `msg["Reply-To"]` gefügt. Use Case Kontaktformular: Absender = `zentrale@hpa24.de` (vom Mailflow-Account), Reply-To = User-Adresse — Klick auf „Antworten" landet direkt beim User.

### Absender-Anzeigename-Override

Pro Webhook optionales Feld `from_name_override`: überschreibt für diesen Webhook den `from_name` aus dem Account. So sieht der Empfänger im Postfach klar getrennt z.B. „Verwaltung, HPA24" statt dem persönlichen Namen aus dem Account. Implementierung: `send_email` bekommt Parameter `from_name_override`, beim Aufbau des `From`-Headers gilt `from_name = from_name_override or acc.get("from_name", "")`.

### Verwaltungs-UI

Topbar-Button **„Webhooks"** öffnet ein Modal mit drei Views (List / Edit / Logs). Anlegen: alle Felder im Modal, `api_key` wird beim Speichern serverseitig erzeugt und nach Anlage als read-only mit Copy-Button + Rotate-Button (`PATCH` mit `rotate_api_key: true`) angezeigt. Webhook-URL ebenfalls read-only kopierbar. Eingebaute Hilfe-Sektion (`<details>`) zeigt das erwartete JSON-Schema für Xano-Setup.

Logs-View pro Webhook: letzte 100 Einträge mit Status-Icon, Timestamp, IP, Empfänger, Betreff, Fehler und Message-ID — grüner Balken bei Success, roter bei Error.

### Drei Ebenen Mail-Historie

Bei Kunden-Reklamationen („Mail nicht angekommen") drei voneinander unabhängige Anhaltspunkte:

1. **Webhook-Trigger erfolgt?** → `webhook_logs` (deckt auch Aufrufe ab, die vor dem SMTP-Versand aus Validierungsgründen abbrechen)
2. **SMTP-Versand erfolgreich?** → Message-ID im Log-Eintrag
3. **Im Sent-Folder?** → IMAP APPEND wie bisher, nach nächstem Sync auch in der `emails`-Collection sichtbar

### Bewusst nicht gebaut

- **Templates / Platzhalter** im Webhook-Body — Xano liefert fertige Texte. Wenn später nötig, würde das in `webhooks` als `subject_template` / `body_html_template` mit Jinja-ähnlichem Rendering ergänzt.
- **From-Address-Override per Payload** — der Absender ist bewusst pro Webhook in der Config festgenagelt (Anti-Spoofing). Wenn ein Workflow mehrere Absender braucht: pro Absender ein eigener Webhook.
- **Rate-Limiting** im Endpoint — bisher kein Bedarf, der eigene API-Key pro Webhook + die externe Netcup-Firewall reichen. Würde sich bei Missbrauch trivial via fastapi-limiter ergänzen lassen.

## Vorlagen-System 2026-05-17

Ablöse des FileMaker-Versandtools. Globale Variablen, wiederverwendbare HTML-Snippets, Templates mit Live-Preview, Compose-Integration. Vollständiger Plan in `MAILFLOW-TEMPLATES-PLAN.md`.

### Collections

| Collection | Felder | Zweck |
|---|---|---|
| `email_variables` | name (unique), value | Globale Werte (`{{kurs_termin}}` etc.), beim Versand ersetzt |
| `email_snippets` | name (unique), html | Wiederverwendbare HTML-Blöcke, in Templates via `{{> name}}` |
| `email_templates` | prefix, name, subject, html_body, text_body | Volle Vorlagen mit (prefix, name) unique |
| `contact_groups` | name (unique), description | Sets von Kontakten für Gruppen-Versand |
| `contacts` (existierte) | + groups (multi-relation), unsubscribed | M:N mit `contact_groups` |

### Render-Pipeline (`backend/rendering.py`)

Zweiphasig, gesteuert durch optionale `contact`-Parameter:

1. **Phase 1 (Pre-Compose)** — Sections strippen (`<!-- @section X --> … <!-- @end -->` mit `active_sections`-Filter), Snippets auflösen (`{{> name}}`), globale Variablen ersetzen. `{{name}}`/`{{email}}` bleiben Platzhalter.
2. **Phase 2 (Pre-Send pro Empfänger)** — Kontakt-Variablen ersetzen, danach `strip_unresolved` für übrige Platzhalter.

Section-Regex akzeptiert bereits optionales `if=role:X`-Suffix als no-op — Vorbereitung für rollenbasierte Sections (kommt mit Phase 3).

### UI: Topbar-Tabs

Drei Top-Level-Tabs in der Topbar: **Inbox / Vorlagen / Kontakte**. Aktiver Tab in `localStorage`. Tab-Panes sitzen via `grid-row: 3` in der `1fr`-Row des `#layout`-Grids.

**Vorlagen-Tab** ist dreispaltig: Untermenü links (Variablen / Snippets / Vorlagen / Gruppen / Kontakte — letzte zwei noch `(folgt)`), Liste in der Mitte, Editor + Live-Preview rechts.

- **Variablen**: Inline-Tabelle mit Doppelklick-Edit auf Wert, Präfix-Filter-Buttons (Konvention `präfix_name`). Reserved Names: `name`, `email`.
- **Snippets**: Liste + Editor mit HTML-Textarea + Live-Preview-iframe. Default-HTML beim Neu = Outlook-kompatibles Tabellen-Skelett mit H2 + zwei P-Tags (Inline-Margins). Copy-Buttons für Referenz `{{> name}}` und HTML. Variable-Einfügen-Dropdown.
- **Vorlagen**: Liste mit Präfix-Filter + Suche + Gruppierung. Editor mit Präfix/Name/Subject + Textarea + Preview. „Erkannt"-Box zeigt Variablen, Snippets, Sections live. Variable- und Snippet-Einfügen-Dropdowns; Snippet hat zwei Action-Buttons: **Referenz** (`{{> name}}`, dynamisch) oder **Code** (HTML inline kopiert, statisch).

### Compose: „Aus Vorlage"

Action-Bar-Button öffnet Modal mit Vorlagen-Liste. Auswahl ruft `POST /templates/render` mit Template-HTML, schreibt Subject + Phase-1-gerendertes HTML in `#ci-subject` und `#ci-body`. Banner zeigt Vorlagenname und übrige Platzhalter. Stefan editiert manuell (persönliche Anpassungen), Phase 2 läuft automatisch beim Senden.

### Send-Endpoint mit Phase 2

`_do_send_job` ruft vor SMTP-Versand `_finalize_for_recipient` auf:
- Ein Empfänger im `to`-Feld → Kontakt-Lookup in DB, `{{name}}`/`{{email}}` ersetzen
- Mehrere Empfänger oder unbekannt → kein Auto-Replace
- Anschließend `strip_unresolved` auf Subject/Body/HTML

Idempotent für Mails ohne Platzhalter. Funktioniert auch bei Bulk-Send (jeder Sub-Job hat ein eigenes `to`).

### Kontakt-Import

`POST /contacts/import` mit Body `{lines, mode: "add" | "remove"}`. Format pro Zeile:

```
email,name,gruppen
```

- `email` erforderlich, `name` optional (leer = bestehenden Wert nicht überschreiben), `gruppen` optional mit `;` getrennt **oder** mehrfach pro Email in eigenen Zeilen
- Gruppen-Namen werden lowercase + whitespace_zu_underscore normalisiert
- **Add-Mode**: Kontakt upserten, Name überschreibend, Gruppen additiv mergen, unbekannte Gruppen werden automatisch angelegt
- **Remove-Mode**: nur angegebene Gruppen-Zuordnungen entfernen; Kontakt + andere Gruppen bleiben unverändert

**Auth**: globaler `API_KEY` (für die Mailflow-UI) **oder** optionaler separater `IMPORT_API_KEY` per `X-Import-Key`-Header — gedacht für externe Quellen wie FileMaker. `IMPORT_API_KEY` default leer = externer Zugang aus.

### Endpoints

| Route | Zweck |
|---|---|
| `GET/POST/PATCH/DELETE /variables` | CRUD |
| `GET /variables/{id}/usage` | Findet Templates + Snippets, die `{{name}}` referenzieren — für Lösch-Schutz |
| `GET/POST/PATCH/DELETE /snippets` | CRUD |
| `GET /snippets/{id}/usage` | Findet Templates, die `{{> name}}` referenzieren — für Lösch-Schutz |
| `GET/POST/PATCH/DELETE /templates` | CRUD (Filter `prefix=`, `search=`) |
| `POST /templates/render` | `{html, subject, active_sections?, contact_id?}` → `{html, subject, unresolved}` |
| `GET/POST/PATCH/DELETE /contact-groups` | CRUD |
| `GET /contact-groups/{id}/members` | Mitglieder einer Gruppe |
| `POST /contacts/import` | `{lines, mode}` → Counts + invalid-Report + auto_created_groups |

### Phase 2b: Gruppen-Tab + Lösch-Schutz 2026-05-19

**Gruppen-Tab** im Vorlagen-Bereich: Liste links (mit Mitglieder-Count-Badge) + Detail rechts (Name/Beschreibung editierbar, Mitglieder-Tabelle mit Einzel- und Bulk-Entfernen, Multiline-Import-Feld). Member-Entfernen läuft über `POST /contacts/import` mit `mode=remove` (kein extra Endpoint). Beim Gruppen-Delete räumt PocketBase die Relations automatisch auf (`cascadeDelete=False` auf `contacts.groups`).

**Lösch-Schutz** für Variablen + Snippets via `GET /{var|snippet}/{id}/usage`:
- Variable: scannt `email_templates.subject` + `html_body` + `email_snippets.html`
- Snippet: scannt nur Templates (Snippet-in-Snippet ist per Plan verboten)
- Frontend `js/delete_guard.js`: Modal mit Treffer-Liste, Option „Trotzdem löschen". Bei 0 Treffern fällt das Modal weg und es kommt nur ein einfaches `confirm()`.

**Snippet-Editor** hat einen `+ Snippet ▾`-Button — fügt ein anderes Snippet als HTML-Code inline ein (keine `{{> }}`-Referenz, weil Snippet-in-Snippet verboten). Aktiv editiertes Snippet wird im Dropdown ausgeblendet.

**Inline-Save-Button** am aktiven Listen-Eintrag (Templates + Snippets): rechts in der Liste neben dem Namen, gelb hervorgehoben bei Dirty-State. Verhindert dass der Editor-Header-Save-Button beim Scrollen aus dem Sichtfeld verschwindet.

### Phase 2c: Gruppen im Massenversand 2026-05-19

Statt eines separaten Gruppen-Versand-Workflows kommt eine Gruppen-Auswahl ins bestehende Bulk-Modal:
- Button **„＋ Gruppe ▾"** über der Textarea → `mfDropdown` mit allen `contact_groups`
- Auswahl lädt Mitglieder (filter `unsubscribed=false`) und hängt Emails an die Textarea (Dedup gegen bestehende Zeilen)
- Mehrfach klickbar für mehrere Gruppen kumulativ
- Status-Info nach jedem Klick: `X ergänzt · Y doppelt · Z unsubscribed`
- Bestehende `/emails/bulk-send`-Pipeline macht ohnehin Phase-2-Rendering pro Empfänger — kein zweiter Send-Pfad nötig

**Test-Versand-Button „✉ Test senden"** in der Compose-Action-Bar: sendet die aktuelle Mail mit Subject-Prefix `[TEST] ` an die Adresse des eingeloggten PocketBase-Users. `{{name}}` und `{{email}}` werden clientseitig mit den User-Daten gefüllt — Vorschau ist die fertig gerenderte Mail. Bestätigungs-Popup nach Erfolg.

### Bewusst nicht gebaut

- **WYSIWYG-Editor**: Textarea + Live-iframe reicht; E-Mail-HTML braucht ohnehin Inline-Styles und Tabellen-Layout.
- **CodeMirror / Syntax-Highlighting**: Plain Textarea + Monospace + 17px reicht aktuell. Nachrüstbar wenn Stefan das im Alltag vermisst.
- **Sections-UI**: Backend kann Sections strippen (Marker im HTML), Editor-UI und Compose-Section-Checkboxen kommen mit Phase 2b.
- **Pro-Kontakt-Variablen** (`{{vars.anrede}}` etc.): Stefan nutzt nur globale Werte. Bei Bedarf später nachrüstbar (Feld `vars` JSON auf Kontakt + Resolver-Erweiterung).
- **Rendered-Preview-Iframe im Compose**: Stefans Feststellung 2026-05-19 — der `contenteditable`-Div rendert das HTML bereits direkt, ein zusätzliches Iframe wäre redundant. Der Test-Versand-Button deckt den End-Empfänger-Check ab.
- **Phase 3**: Unsubscribe-Token-Link, Bounce-Erkennung, Tagesversand-Counter, rollenbasierte Conditional Sections.

## Webhook-Filter im Sent-Ordner 2026-05-19 #webhook #xano

Nachzug zum Webhook-System (s.o.): Sent-Mails, die per `/webhooks/{slug}/send` rausgingen, sind jetzt in der UI vom normalen Compose-Versand trennbar.

### Sync-Markierung

Neues Feld `emails.webhook` (relation → `webhooks`, optional, single) — per Migration in `pb_setup.py` über `_add_missing_fields()` ergänzt, greift nur wenn die `webhooks`-Collection schon existiert.

Befüllt wird das Feld im IMAP-Sync (`imap_sync._fetch_and_save`): für `folder == "Sent"` wird die `message_id` der eingehenden Mail in `webhook_logs` mit `status="success"` nachgeschlagen. Bei Treffer landet die Webhook-Record-ID im Feld, sonst bleibt es leer (= normaler Versand). Lookup-Helper: `_webhook_id_for_message()`. Bestehende Sent-Mails behalten ihr leeres Feld und erscheinen damit korrekt unter „Normal".

### Backend-Filter

`_email_filters()` in `main.py` versteht neuen Param `webhook="true"` / `webhook="false"` → PocketBase-Filter `webhook!=""` bzw. `webhook=""`. Greift in `/emails`, `/emails/threaded`, `/emails/by-sender`. Bewusst **nicht** in `/search` — dort ordnerübergreifend semantisch unklar.

### UI-Filter

Im Sent-Ordner zeigt die Filter-Leiste statt „Alle / Ungelesen / Gelesen" jetzt **„Alle / Webhook / Normal"** — gleiches Markup, gleicher Stil (`.read-filter-btn`). `renderReadFilterButtons()` in `inbox.js` rendert die passenden Buttons abhängig von `state.activeFolder`, Click-Handler läuft via Event-Delegation auf dem `.read-filter`-Container (weil die Buttons je nach Ordner neu gemounted werden). State: `state.sentFilter` parallel zu `state.readFilter`. Cache-Key in `_cacheKey()` zieht je nach aktivem Ordner den richtigen Filter.

## Aussendungs-Historie 2026-05-19 #aussendung #bouncetracking

Persistierung aller Massenversände als Audit-Records — Grundlage für Re-Send-Workflows und kommendes Bounce-Tracking (Phase 3b).

### Collection `bulk_sends`

Schema in `backend/pb_setup.py` → `_bulk_sends_schema(accounts_id)`. Felder: `subject`, `from_account` (rel), `from_account_email`, `smtp_server`, `body_html`/`body_text` (Snapshot), `sent_at`, `delay_seconds`, `recipients` (JSON-Array), Counts `total_count` / `sent_count` / `error_count` / `bounced_count`. Index auf `sent_at DESC`.

`recipients`-Schema pro Eintrag:
```json
{"email": "x@y.de", "name": "Max", "raw": "Max <x@y.de>",
 "status": "queued|sent|error|bounced",
 "message_id": "<...@host>", "error": null, "sent_at": null}
```

### Backend-Pipeline

`bulk_send_endpoint` legt **vor** dem Versand den `bulk_sends`-Record an. Pro Sub-Job:
- `_do_send_job` empfängt `_bulk_send_id` über `base_data` und reicht die von `smtp_send_email` zurückgegebene Message-ID weiter.
- `_bulk_record_recipient_result(bulk_send_id, recipient, status=, message_id=, error=)` patcht den eigenen Empfänger-Eintrag im JSON-Array.
- Race-Schutz: `_bulk_send_locks: dict[str, asyncio.Lock]` mit einem Lock pro Bulk-Send-ID, weil mehrere Sub-Jobs gleichzeitig dasselbe `recipients`-Array lesen + schreiben.
- Counts werden bei jedem Update neu summiert und mitgepatcht.

### Endpoints

| Route | Zweck |
|---|---|
| `GET /bulk-sends?limit=N` | Liste neueste zuerst, **ohne** `recipients`-Array (Performance) |
| `GET /bulk-sends/{id}` | Volldetail inkl. `recipients` |
| `DELETE /bulk-sends/{id}` | Audit-Eintrag löschen (gesendete Mails sind nicht betroffen) |

### Frontend `js/bulk_sends.js`

Neuer Subnav-Eintrag „Aussendungen" zwischen „Gruppen" und „Kontakte". Liste links (320px), Detail rechts mit Empfänger-Tabelle, Status-Filter-Chips (Alle/Erfolgreich/Fehler/Bounce/Ausstehend) und Selection-Hint. Vorschau-Modal mit iframe-srcdoc. Bouncte sind in der Tabelle default markiert.

### Re-Send-Workflow

Button „Auswahl als neuer Versand" → `window.mfComposeResend.open({subject, body_html, body_text, recipients, from_account, smtp_server})` (definiert in `inbox.js`):
1. `mfTabs.setActiveTab('inbox')` — zurück zum Inbox-Tab
2. `openCompose({subject, fromAccountId})` — Compose öffnet
3. `#ci-body.innerHTML = body_html` — HTML direkt setzen (statt Plain-`body` über `openCompose`)
4. `#ci-smtp-server.value = smtp_server` — SMTP-Vorauswahl, Stefan kann im Dropdown wechseln
5. `_bulkRecipients = [...]` + `_openBulkModal()` — Bulk-Modal sofort offen mit den vorgefüllten Adressen

Bulk-Send läuft danach durch die normale `/emails/bulk-send`-Pipeline und legt einen **neuen** `bulk_sends`-Record an.

### Bewusst nicht jetzt

- **Bounce-Erkennung** (Phase 3b, geplant): IMAP-Sync-Heuristik für Mailer-Daemon/DSN-Mails, Message-ID-Match gegen `bulk_sends.recipients[*].message_id`, `contacts.bounced`-Flag, UI-Badges. Vorbereitung dafür ist mit der Message-ID-Persistierung schon getan.
- **Tagesversand-Counter** ist bereits live (siehe „Tagesversand-Counter" unten / Plan-Eintrag).
