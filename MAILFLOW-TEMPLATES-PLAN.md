# Mailflow — Vorlagen, Snippets, Variablen, Gruppenversand (Plan)

**Stand:** 2026-05-18
**Ziel:** Ablöse des FileMaker-Versandtools. Vorlagen + wiederverwendbare Snippets + globale Variablen + Kontaktgruppen + Gruppenversand mit per-Empfänger gerendertem `{{name}}`. Conditional Sections via HTML-Marker.
**Limit-Check:** mailbox.org 10.000 Mails/Tag, Kursgruppen 20–200 Empfänger, 5 s Delay → 200er-Gruppe ≈ 17 min. Passt.

---

## Umsetzungsstand 2026-05-18

### Erledigt (Phase 1 + Phase 2a)

- ✅ **Variablen**: Collection `email_variables` (name unique, value), CRUD-Endpoints, UI-Tabelle mit Inline-Edit, Präfix-Filter-Buttons
- ✅ **Snippets**: Collection `email_snippets`, CRUD, UI mit Liste + Editor + Live-Preview, Copy-Buttons für Referenz/HTML, Variable-Einfügen-Dropdown, Outlook-kompatibles Default-Skelett
- ✅ **Templates**: Collection `email_templates` (prefix, name unique-Combo), CRUD, UI mit Präfix-Filter + Suche + Gruppen-Liste + Editor mit Subject/HTML + Live-Preview, Variable- und Snippet-Einfügen-Dropdowns (Snippet hat Referenz- und Code-Modus)
- ✅ **Render-Pipeline** `backend/rendering.py`: `render_phase1` (Sections + Snippets + globale Vars), `render_phase2` (Kontakt-Vars), `render_full`, `find_unresolved`, `strip_unresolved`. Section-Regex akzeptiert `if=role:X` als no-op (Rollen-Vorbereitung).
- ✅ **Endpoint** `POST /templates/render` für Live-Preview im Editor und für Compose-„Aus Vorlage"
- ✅ **Wiederverwendbare Editor-Dropdown-Komponente** `js/editor_dropdowns.js`
- ✅ **Topbar-Tabs** (Inbox / Vorlagen / Kontakte) mit `localStorage`-Memo
- ✅ **Compose „Aus Vorlage"**: Action-Bar-Button, Modal mit Vorlagen-Auswahl, Banner mit offenen Platzhaltern, beim Senden Phase-2-Auto-Render + `strip_unresolved`
- ✅ **Schema-Migration**: Collection `contact_groups`, `contacts.groups` (multi-relation) + `contacts.unsubscribed` (bool)
- ✅ **CRUD-Endpoints** für Gruppen + `GET /contact-groups/{id}/members`
- ✅ **Import-Endpoint** `POST /contacts/import` mit Format `email,name,gruppen`, Modi `add`/`remove`, Auto-Anlegen unbekannter Gruppen, Name-Auto-Normalisierung. Auth via globalem `API_KEY` ODER neuem optionalem `IMPORT_API_KEY` per `X-Import-Key`-Header (für externe Quellen wie FileMaker).
- ✅ **Live-Test der Import-Endpoint-Logik** per Node-Fetch aus dem Terminal — alle Modi und Edge Cases bestätigt. Test-Daten (4 Kontakte + 6 Gruppen) bleiben in der DB für UI-Tests.

### Test-Daten in der Production-DB

Kontakte: `test@example.com` (Tester Neu), `anna@example.com` (Anna), `bob@example.com` (Bob), `good@x.de` (Good).
Gruppen: `dritte_gruppe`, `gross_geschrieben`, `kurs_a`, `kurs_b`, `test_gruppe` (leer), `zweite_gruppe`.

### Als nächstes — Phase 2b: Gruppen-UI

- **Untermenü-Eintrag „Gruppen"** im Vorlagen-Tab aktivieren (aktuell `(folgt)`)
- **Layout**: analog zur Templates-Section, 320px Liste + Detail-Pane
  - Liste links: Suchfeld + „+ Neu" + Gruppen-Liste (Name + Mitglieder-Count)
  - Detail rechts: Gruppen-Name editierbar, Beschreibung, Mitglieder-Tabelle, Multiline-Import-Feld unten („email,name,gruppen"), Vor-Bestätigung-Preview mit Counts pro Eintrag (`anlegen` / `bereits in Gruppe` / `in anderer Gruppe` / `invalid`)
  - Bulk-Aktionen: Mitglieder entfernen, Gruppe löschen mit Confirm
- **Datei-Stubs**: neues `js/groups.js` analog zu `js/templates.js`, `template_subnav.js` muss `groups` in `KNOWN` aufnehmen
- **Backend-Stand**: alle Endpoints existieren bereits (`/contact-groups` CRUD + members + `/contacts/import`). Keine Backend-Änderungen nötig für 2b.

### Danach — Phase 2c: Gruppen-Versand

- **Bulk-Send-Template-Endpoint** `POST /emails/bulk-send-template` mit Body `{template_id, group_id, active_sections, delay_seconds, from_account_id?}`. Lädt Gruppen-Mitglieder (filter `unsubscribed=false`), rendert pro Empfänger via `render_full(html, snippets, variables, active_sections, contact)`, erzeugt Sub-Jobs in `_send_jobs` mit gerendertem Inhalt (self-contained, kein Re-Render im Versandzyklus), sequenzieller Versand mit `asyncio.sleep(delay_seconds)` wie bei `_do_bulk_send`.
- **Compose-Integration**: zweiter Action-Bar-Button (alternativ eigener Menüpunkt im Vorlagen-Tab) „Gruppen-Versand": Vorlage wählen → Section-Checkboxen → Gruppe wählen → Vorschau (gerenderte Mail pro Empfänger durchklickbar) → Senden mit Status-Panel.

### Phase 3 (später)

- Sections-UI (Checkbox-Auswahl-Modal vor dem Versand)
- Unsubscribe-Link mit signiertem Token + Endpoint
- Bounce-Erkennung im IMAP-Sync
- Tagesversand-Counter gegen das 10K-mailbox.org-Limit
- Rollenbasierte Conditional Sections (`if=role:X` ist schon syntaktisch erlaubt, Auswertung fehlt; Kontakt-Feld `roles` als JSON-Array)

### Wichtige Konventionen für Folge-Sessions

- **Push-Strategie**: direkt nach `main`, kein Staging. Pro Push eine kohärente Einheit, niemals broken halfway. Backend-Schema-Migrationen sind idempotent (via `_ensure_collection` + `_add_missing_fields`).
- **Cachebust** für JS/CSS: Versions-Param `?v=YYYYMMDD-tplN` in `index.html` bei jedem Frontend-Push hochzählen.
- **Auth lokal**: `~/Syncthing/Claude/_Web-Apps/mailflow/.env` enthält `API_KEY` als `API_KEY=...` für Curl/Node-Tests aus dem Terminal (`.env` ist `.gitignore`d).
- **Tab-Pane-Layout**: alle Top-Level-Container brauchen `grid-row: 3` im `#layout`-Grid, sonst kollabieren sie im Auto-Placement.

---

## Datenmodell (PocketBase)

### `email_templates` (neu)
| Feld | Typ | Hinweis |
|---|---|---|
| `prefix` | text | Filter-/Sortierschlüssel, z. B. „HPA24", „Intensivkurs", „Autoresponder" |
| `name` | text | freier Name, z. B. „Einladung Online-Workshop" |
| `subject` | text | darf Variablen enthalten |
| `html_body` | text | volles HTML (darf `{{var}}`, `{{> snippet}}`, `<!-- @section x -->` enthalten) |
| `text_body` | text | optional; bei leer aus HTML generiert |
| `from_account` | relation → `accounts` | Default-Absender; pro Versand überschreibbar |
| `detected_vars` | json | auto beim Save: Liste aller `{{…}}` minus `{{name}}`, `{{email}}` |
| `detected_sections` | json | auto: Liste aller `<!-- @section x -->`-IDs |
| `detected_snippets` | json | auto: Liste aller `{{> snippet}}`-Referenzen |
| `created`/`updated` | auto | |

**Indexes:** `(prefix, name)` für Sortierung.

### `email_snippets` (neu)
| Feld | Typ | Hinweis |
|---|---|---|
| `name` | text, unique | wird als `{{> name}}` referenziert |
| `description` | text | Bedienhilfe in der Liste |
| `html` | text | darf selbst Variablen enthalten (`{{firma_name}}`) — aber **keine** weiteren Snippet-Includes (vermeidet Rekursion) |
| `created`/`updated` | auto | |

### `email_variables` (neu)
| Feld | Typ | Hinweis |
|---|---|---|
| `name` | text, unique | wird als `{{name}}` referenziert — Achtung: `{{name}}` und `{{email}}` sind reserviert für Kontaktdaten |
| `value` | text | aktueller Wert (z. B. „24. Juni 2026") |
| `description` | text | Hinweis was diese Variable bedeutet |
| `updated` | auto | sichtbar in der UI um veraltete Werte zu erkennen |

**Bewusst flach:** keine Default- vs. Override-Logik. Wert ändern = überall geändert. Wenn ein Kurs vorbei ist, setzt Stefan den nächsten Termin rein.

### `contact_groups` (neu)
| Feld | Typ |
|---|---|
| `name` | text |
| `description` | text |
| `created`/`updated` | auto |

### `contacts` (existiert — wir erweitern)
**Bestehende Felder:** `email` (unique), `name`, `email_count`, `last_contact`, `notes`
**Neu:**
| Feld | Typ | Hinweis |
|---|---|---|
| `groups` | relation → `contact_groups`, multi | Many-to-Many; Kontakt ist in 0..n Gruppen |
| `unsubscribed` | bool | global, gilt für alle Gruppen |

**Wichtig:** Wir behalten die existierende Collection. Vorteil: jede E-Mail-Adresse, mit der Stefan je geschrieben hat, ist schon angelegt (vom IMAP-Sync) — Stefan ordnet sie nur noch Gruppen zu. Kein Daten-Reimport nötig.

---

## Rendering-Pipeline — zwei Phasen

Rendering läuft in **zwei klar getrennten Phasen**, gesteuert durch einen `phase`-Parameter:

### Phase 1 — Pre-Compose / Pre-Send-Vorbereitung („Aus Vorlage")

Wird ausgeführt wenn der User „Aus Vorlage" wählt → Subject+HTML wird in den Compose-Editor geladen. Schritte:

1. **Sections strippen** — `<!-- @section ID --> ... <!-- @end -->`, deren ID nicht in der aktiven Liste steht, entfernen (Regex, non-greedy).
2. **Snippets auflösen** — `{{> snippet_name}}` → `email_snippets.html` (einmaliger Pass).
3. **Globale Variablen ersetzen** — `{{key}}` aufgelöst gegen `email_variables`.
4. **Kontakt-Variablen NICHT anfassen** — `{{name}}`, `{{email}}` bleiben als Platzhalter.

Ergebnis: HTML mit fertigem Inhalt, aber Kontakt-Personalisierung steht noch aus.

### Phase 2 — Pre-Send (eigentlicher Versand)

Wird ausgeführt unmittelbar vor jedem Mail-Versand, pro Empfänger:

1. **Sections strippen** (idempotent — schon in Phase 1 erledigt, aber Schutz falls User im Compose-Editor Section-Marker reingeschrieben hat)
2. **Snippets auflösen** (idempotent — schon in Phase 1 erledigt)
3. **Globale Variablen ersetzen** (idempotent — schon in Phase 1 erledigt)
4. **Kontakt-Variablen ersetzen** — `{{name}}` → Empfänger-Name, `{{email}}` → Empfänger-E-Mail.

### Beim „Direkt versenden"-Modus

Phasen 1 und 2 laufen am Stück nacheinander pro Empfänger; Stefan sieht nur die Vorschau, kein Compose-Editor dazwischen.

### Strict mode

Unbekannte Variable in Phase 2 → Sub-Job wird mit `status: "failed"`, Reason „Variable `{{xyz}}` nicht definiert" markiert. Keine kaputten Mails raus. In Phase 1 ist Strict weicher: globale Variable fehlt → bleibt Platzhalter, User sieht's im Compose und kann reagieren.

### Regex

`re.sub(r'\{\{\s*(>?\s*[\w.]+)\s*\}\}', resolver, body)` — Prefix `>` markiert Snippet-Includes.

---

## Backend (FastAPI)

### Neue Endpoints
| Route | Zweck |
|---|---|
| `GET/POST/PATCH/DELETE /templates` | CRUD; Filter `prefix=` und `search=` für die Liste |
| `POST /templates/{id}/preview` | body: `{contact_id?, active_sections?: [string]}` → `{subject, html, text}` |
| `GET/POST/PATCH/DELETE /snippets` | CRUD |
| `GET/POST/PATCH/DELETE /variables` | CRUD; einfache Tabelle |
| `GET/POST/PATCH/DELETE /contact-groups` | CRUD |
| `GET /contact-groups/{id}/members` | Kontakte der Gruppe |
| `POST /contact-groups/{id}/import` | body: `{lines: string}` — siehe „Import-Verhalten" unten |
| `POST /contacts/{id}/unsubscribe` | setzt Flag |
| `POST /emails/bulk-send-template` | body: `{template_id, group_id, active_sections, delay_seconds, from_account_id?}` |

### Import-Verhalten (`/contact-groups/{id}/import`)

E-Mail-Adresse ist case-insensitive und wird normalisiert (lowercase, trim). Dedup auf zwei Ebenen:

1. **Innerhalb des Inputs:** doppelte Zeilen einmal verarbeiten.
2. **Gegen DB:**
   - E-Mail existiert + ist schon in dieser Gruppe → übersprungen
   - E-Mail existiert + ist in einer anderen Gruppe → Gruppe additiv hinzufügen, `name` nicht überschreiben
   - E-Mail neu → Kontakt anlegen + Gruppe zuweisen

Response gibt vier Zahlen zurück: `{added: n, already_in_group: n, new_contact: n, invalid: n}` plus Liste der ungültigen Zeilen. Im UI als Bestätigungs-Toast nach dem Import.

### Bulk-Pipeline
Neue Funktion `_do_bulk_send_template`:
1. Lade Kontakte der Gruppe, filter `unsubscribed=false`
2. Pro Kontakt: `render(template, contact, active_sections)` → fertiges HTML/Subject/Text
3. Lege Sub-Job in `_send_jobs` mit **gerendertem** Inhalt (self-contained, kein Re-Render im Versandzyklus)
4. Sequenzieller Versand wie bisher (`asyncio.sleep(delay_seconds)` zwischen Sub-Jobs)
5. SSE-Events, Status-Panel, Retry funktionieren unverändert

---

## Frontend (Vanilla JS)

### Navigations-Pattern: Topbar-Tabs

Mailflow bekommt auf oberster Ebene drei Tabs in der Topbar: **Inbox** (bestehende Ansicht), **Vorlagen**, **Kontakte**. Klick wechselt den Hauptbereich (alles unterhalb der Topbar) komplett aus.

```
┌─────────────────────────────────────────────────────────────────────┐
│ Mailflow │ [Inbox] [Vorlagen] [Kontakte]    🔍 Suche           ⚙   │
├─────────────────────────────────────────────────────────────────────┤
│   ... Hauptbereich passend zum aktiven Tab ...                      │
└─────────────────────────────────────────────────────────────────────┘
```

Implementierungs-Hinweis: ein einfacher View-Switcher im Frontend, kein Router-Library. Aktiver Tab wird in `localStorage` gemerkt, Default ist Inbox. Compose-Mode bleibt wie heute Teil des Inbox-Tabs.

### Tab „Inbox" — zwei Vorlagen-Modi

Unverändert zum heutigen Zustand. Neu sind zwei Compose-Action-Bar-Buttons mit **klar getrennten Workflows**:

#### Modus 1: „Aus Vorlage" — flexibel, editierbar
- Modal mit Präfix-Filter + Liste → Auswahl → Section-Checkboxen → **Phase-1-Rendering serverseitig** (Sections strippen, Snippets auflösen, globale Variablen ersetzen — `{{name}}`/`{{email}}` bleiben Platzhalter) → Subject+HTML in Compose laden
- Banner „Vorlage: HPA24-Einladung" mit Hinweis welche Kontakt-Variablen noch offen sind
- Stefan editiert frei — Begrüßung anpassen, Block ergänzen
- Senden via Send-/Bulk-Send-Endpoint. Backend macht **Phase 2** pro Mail (Kontakt-Vars ersetzen).
- Use Case: persönliche Mails, einzelne Antworten, individuelle Anpassungen

#### Modus 2: „Gruppen-Versand" — Template 1:1, kein Editor
- Modal:
  1. Vorlage wählen + Section-Checkboxen
  2. Gruppe wählen
  3. Vorschau (gerenderte Mail pro Empfänger durchklickbar)
  4. Senden
- Keine Edit-Stufe — Template-HTML geht serverseitig durch Phase 1+2 und raus
- Bestehende Bulk-Send-Pipeline (5 s Delay, SSE, Status-Panel) bleibt unverändert
- Use Case: Massenversand an Kursgruppe, alles wo Stefan sicher sein will dass exakt das rausgeht was im Template steht

### Tab „Vorlagen" — drei-spaltiger Editor mit Untermenü

```
┌─Untermenü─┐ ┌─Liste───────────┐ ┌─Editor + Preview────────────────┐
│ Vorlagen  │ │ ┌─Präfix-Filter│ │ Präfix:  [HPA24]                 │
│ Snippets  │ │ │ Alle ▾       │ │ Name:    [Einladung Online-WS]   │
│ Variablen │ │ │ 🔍 Suche...  │ │ Subject: [...]                   │
│ Gruppen   │ │ ├──────────────┤ │ ┌────────────────┬─────────────┐ │
│ Kontakte  │ │ │ HPA24-Einla. │ │ │ Toolbar:       │             │ │
│           │ │ │ HPA24-Erinn. │ │ │ [Box][Button]  │ Live-Preview│ │
│           │ │ │ Intens-Ang.  │ │ │ [Trenner]      │  (iframe    │ │
│           │ │ │ Crash-Live   │ │ │ [Snippet▾]     │   srcdoc)   │ │
│           │ │ │ Auto-Folge   │ │ │ [Section]      │             │ │
│           │ │ │ + neu        │ │ │ ─────────────  │             │ │
│           │ │ └──────────────┘ │ │ HTML-Textarea  │             │ │
│           │ │                  │ │                │             │ │
│           │ │                  │ │ Erkannt: vars, snippets, sec │ │
│           │ │                  │ └────────────────┴─────────────┘ │
└───────────┘ └──────────────────┘ └──────────────────────────────────┘
```

**Untermenü links** schaltet zwischen den fünf Editier-Bereichen des Vorlagen-Tabs. Mittlere und rechte Spalte ändern sich pro Bereich:

#### Untermenü-Eintrag „Vorlagen"
- **Liste:** Präfix-Filter-Dropdown + Suchfeld + scrollbare Liste, gruppiert nach Präfix mit ein-/ausklappbaren Sektionen
- **Editor:** Felder Präfix, Name, Subject; daneben Toolbar + HTML-Textarea links, Live-Preview-iframe rechts
- **Toolbar über der Textarea:**
  - `[Box einfügen]` öffnet Mini-Modal (BG-Farbe, Border-Farbe+Breite, Padding, Width) → fügt Inline-CSS-HTML an Cursor-Position
  - `[Button einfügen]`, `[Trenner einfügen]` (analog mit eigenen Mini-Modals)
  - `[Snippet einfügen ▾]` → Dropdown der `email_snippets`, mit **zwei Einfüge-Modi pro Snippet:**
    - **Als Referenz** → fügt `{{> snippet_name}}` ein. Dynamisch — Snippet-Änderung wirkt überall. Use Case: Header, Footer, Standard-Hinweise.
    - **Code kopieren** → HTML-Inhalt wird inline ins Template kopiert. Statisch — Verbindung gekappt, im Template anpassbar. Use Case: Box-/Layout-Gerüst als Startpunkt für individuelle Anpassung.
  - `[Section einfügen]` → fragt ID-Name ab → fügt `<!-- @section ID -->\n\n<!-- @end -->` ein
- **Unter dem Editor „Erkannt":** Variablen, Snippet-Referenzen, Section-IDs (auto-extrahiert), klickbar → springt zur Zeile im Editor

#### Untermenü-Eintrag „Snippets"
- Liste + Editor analog zu Vorlagen, aber ohne Präfix/Subject; nur Name, Description, HTML-Body
- Toolbar identisch (Box/Button/Trenner-Generator), aber **kein** Snippet-im-Snippet-Button (Rekursion vermeiden)

#### Untermenü-Eintrag „Variablen"
- Schlichte Tabelle (kein Editor-Pane nötig): Name, Wert, Beschreibung, „Geändert am". Inline-Edit auf Wert per Doppelklick.

#### Untermenü-Eintrag „Gruppen"
- **Liste** der Gruppen; Klick → Detailansicht im Editor-Pane
- **Detail:** Kontakte-Tabelle der Gruppe + Multiline-Import-Feld unten
- **Multiline-Import:** Textarea + Button „Hinzufügen" → parst Adressen (eine pro Zeile, `Name <email>` erlaubt, Komma/Semikolon-getrennt erlaubt) → ruft `/contact-groups/{id}/import`. Vor Bestätigung: Live-Vorschau der erkannten Adressen mit Status (neu / schon in Gruppe / in anderer Gruppe / ungültig) — analog zum bestehenden Bulk-Modal von 2026-05-13

#### Untermenü-Eintrag „Kontakte"
- Tabelle mit Suche, Spalten: Email, Name, Gruppen, last_contact, unsubscribed
- Klick auf Zeile → kleines Edit-Modal: Name, Gruppen-Zuordnung (Multi-Select), unsubscribed-Toggle, Notes
- Multi-Select + Bulk-Aktion „zu Gruppe hinzufügen"

### Tab „Kontakte"

Identisch zum Untermenü-Eintrag „Kontakte" im Vorlagen-Tab — als eigener Top-Level-Tab erreichbar weil Stefan Adressbuch-Arbeit oft ohne Vorlagen-Kontext macht. Untermenü ist hier ausgeblendet (oder der Untermenü-Eintrag „Kontakte" entfällt im Vorlagen-Tab — entscheiden wir beim Bau, was redundanzfrei wirkt).

---

## Phasen

### Phase 1 — Variablen + Snippets + Vorlagen (ohne Gruppenversand)
- Collections + CRUD-Endpoints für `email_templates`, `email_snippets`, `email_variables`
- Rendering-Pipeline (Schritte 2–5)
- Vorlagen-/Snippets-/Variablen-Views im Frontend mit Toolbar-Generator
- Compose: „Aus Vorlage" + Section-Checkboxen + serverseitiges Rendering (ein Empfänger = wie heute manueller Versand)
- **Schon hier:** alle FileMaker-Vorlagen können rüber, Versand läuft via existierende Wege

### Phase 2 — Kontakte-Erweiterung + Gruppen + Gruppenversand
- `contact_groups`-Collection + UI
- `contacts` um `groups`, `unsubscribed` erweitern
- Multiline-Import-Endpoint + UI
- `/emails/bulk-send-template`-Endpoint
- Compose: „Gruppen-Versand"-Banner, Vorschau-Modal, Status-Panel-Anbindung

### Phase 3 — Hygiene
- Unsubscribe-Endpoint + Link-Generator-Snippet (mit signiertem Token)
- Bounce-Erkennung: Mailer-Daemon-Mails im IMAP-Sync → `unsubscribed=true`
- Tagesversand-Counter (Schutz gegen 10K-Limit)
- Optional: `bulk_sends`-Historie-Collection für Audit

---

## Bewusst nicht jetzt

### Rollen-basierte Sections (geplant für später)

**Idee:** Kontakte bekommen Feld `roles` (z. B. `["kursteilnehmer", "interessent"]`). In Templates kann eine Section abhängig von der Rolle ein- oder ausgeblendet werden:

```html
<!-- @section angebot_kurs if=role:kursteilnehmer -->
{{> snippet_kurs}}
<!-- @end -->

<!-- @section angebot_neu if=role:interessent -->
{{> snippet_interessent}}
<!-- @end -->
```

**Warum jetzt schon mitdenken — und was ändert das am Plan?**

- Die heutige Rendering-Pipeline läuft **pro Empfänger** (Schritte 2–4). Das Section-Stripping in Schritt 2 ist also schon pro-Kontakt-fähig — Erweiterung um `if=role:X` ist additiv, keine Umstrukturierung.
- Snippet-Includes (`{{> name}}`) sind schon dynamisch. Snippet-pro-Rolle wird automatisch unterstützt, sobald die Section um sie herum bedingt aktiv ist.
- **Datenmodell-Vorbereitung:** keine zwingenden Schema-Änderungen jetzt. Wenn Rollen kommen, eine Migration: Feld `roles` (json array of strings) auf `contacts`. Optional eine `contact_roles`-Collection als Lookup gegen Tippfehler.
- **Globale Section-Auswahl bleibt:** der Versand-Dialog mit Section-Checkboxen wird ergänzt, nicht ersetzt — globale Deaktivierung gewinnt vor Rollen-Bedingung („wenn deaktiviert, dann immer raus, egal welche Rolle").

**Konsequenz für Phase 1/2:** keine. Section-Marker-Syntax wird so geparst, dass `if=…` heute optional ist und ignoriert wird, wenn vorhanden. Damit funktionieren später erstellte Templates mit Rollen-Bedingung in der heutigen Version stillschweigend „immer aktiv" — kein Crash.

### Weiteres

- **Pro-Kontakt-Variablen** (`{{vars.anrede_formell}}` etc.) — Stefan klärte: alle Werte sind globale Konstanten. Bei Bedarf später nachrüsten (Feld `vars` JSON auf Kontakt, Resolver erweitern).
- **Verschachtelte Snippets** (Snippet referenziert Snippet) — vermeidet Rekursionsproblem; Schritt 3 läuft genau einmal.
- **Versand-spezifische Variablen-Overrides** — wenn nötig, später als optionales Feld am `/bulk-send-template`-Call.
- **Visueller HTML-Builder mit Boxen-Tabelle wie FileMaker** — Toolbar-Generator deckt die häufigen Fälle ab.
- **WYSIWYG-Editor** — Textarea + Live-iframe reicht; HTML-Kontrolle bleibt bei Stefan.
- **A/B-Tests, Open/Click-Tracking** — DSGVO-Aufwand zu hoch.
- **Geplanter/zeitversetzter Versand (Cron)** — später.

---

## Offene Punkte

| Punkt | Status |
|---|---|
| **Snippets dynamisch:** Templates speichern nur `{{> snippet_name}}`-Referenz. Beim Versand wird Snippet-HTML live aus Collection gezogen. Snippet-Änderung wirkt sofort auf alle Templates. | bestätigt 2026-05-17 |
| Reserved Names — `name`, `email` als Kontakt-Felder dürfen nicht in `email_variables` definiert werden; validieren beim Anlegen | implementieren |
| Section-Marker-Syntax `<!-- @section X -->` ↔ E-Mail-Clients: HTML-Kommentare werden von allen Clients ignoriert, das ist sicher | OK |
| Migration der FileMaker-Variablen-Werte → `email_variables` | manuell, einmaliger Aufwand |
| Snippet-Löschung-Schutz: bevor ein Snippet gelöscht wird, prüfen ob es noch referenziert ist (Volltextsuche über `email_templates.html_body`); Warnung mit Treffer-Liste | implementieren in Phase 1 |

---

## Aufwand grob

| Phase | Tage |
|---|---|
| 1 (Templates+Snippets+Variablen+Rendering+Compose-Integration) | 2–3 |
| 2 (Gruppen+Import+Bulk-Endpoint) | 1–1,5 |
| 3 (Hygiene) | 1–2, asynchron |

MVP (Phase 1+2) ≈ 3,5–4,5 Tage.
