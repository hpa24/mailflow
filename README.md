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
- **`index.html`:** Action-Bar-Button `#btn-bulk`, Banner `#ci-bulk-banner` im An-Zeilen-Container (ersetzt `#ci-to-field` per `display:none`), zwei Modals `#bulk-modal-overlay` (Eingabe) und `#bulk-status-overlay` (Live-Status).
- **`inbox.js`:** State `_bulkRecipients` (aktive Liste) und `_bulkTracking = { byJobId, byAddr, compose }` (laufender Versand). `_parseBulkInput` splittet nach `\n`/`,`/`;`, validiert mit `_EMAIL_RE`, dedupliziert. Der bestehende `btn-send-inline`-Handler zweigt früh in `_sendBulk()` ab, wenn `_bulkRecipients.length > 0`. **SSE-Hook in `_handleSendResult`:** ist die `job_id` in `_bulkTracking.byJobId`, übernimmt das Status-Modal die Anzeige und die normale Send-Notif wird unterdrückt. `closeCompose()` ruft `_clearBulkMode()`, sodass Bulk-State nicht zwischen Compose-Sitzungen leakt.
- **Retry:** beim Klick auf „Fehlgeschlagene erneut versuchen“ werden die alten `job_id`s der fehlgeschlagenen Adressen aus `byJobId` entfernt (vermeidet Race mit verspäteten SSE-Events) und `_bulkStart(failed, snapshot)` neu aufgerufen — mit `draft_id: null` und `attachment_ids: []`, da beides beim ersten Lauf konsumiert wurde.

### Bewusst nicht gebaut

- **Platzhalter** (`{{name}}` etc.) — braucht zweispaltige Eingabe (Adresse + Daten), kommt später.
- **Backend-Persistenz** der Bulk-Jobs — `_send_jobs` ist in-memory. Bei Backend-Restart mitten im Bulk gehen offene Sub-Jobs verloren. Bei 5 s × N ist das Fenster klein; bei Bedarf später in PocketBase verlagern.
- **Progress-Bar** im Status-Modal — die Summary-Zeile reicht.
