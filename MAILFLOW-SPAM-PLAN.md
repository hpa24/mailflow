# Mailflow — Spam-Lernsystem (Plan, Stand 2026-05-07)

## Zielsetzung

Stefan markiert eingehende Mails als Spam. Künftige Mails werden auf zwei Wegen automatisch erkannt:

1. **Absender-Blocklist (Ebene 1, hart)** — exakte Adresse oder Domain → sofort nach Spam.
2. **Semantische Ähnlichkeit (Ebene 2, weich)** — Cosine-Similarity gegen Spam-Vektoren ≥ 0.82 → Badge in Inbox + 1-Klick-Bestätigung. Kein Auto-Move ohne Bestätigung.

## Festgelegte Defaults (aus Abstimmung 2026-05-07)

| Parameter | Wert |
|---|---|
| Auto-Modus | Ebene 1 sofort verschieben, Ebene 2 nur Vorschlag |
| Cosine-Schwellwert | 0.82 (in Settings änderbar) |
| UI-Geste | Zwei Buttons: „Spam" und „Spam + Absender blocken" |

## Architektur-Entscheidungen

### Eigene Qdrant-Collection für Spam-Samples

**Nicht** die bestehende `mailflow_emails`-Collection erweitern (Thread-Granularität, für Antwortvorschläge). Stattdessen:

- Neue Collection `mailflow_spam_samples`
- Granularität: **eine Mail = ein Punkt** (Spam ist meist Einzelmail, kein Thread)
- Vektor: Embedding aus `subject + body_plain[:3500]` (analog zu `embed_text`)
- Dimension: 1536 (OpenAI `text-embedding-3-small`, schon verwendet)
- Distance: Cosine
- Point-ID: `uuid5(SPAM_NS, email_id)` — deterministisch, idempotent

### Trennung Blocklist vs. Spam-Vektoren

- **Blocklist** in PocketBase (relationale Daten, oft gelesen, klein) — Collection `spam_rules`
- **Vektoren** in Qdrant (existierende Infrastruktur)

### Idempotenz und Re-Learn

- Spam-Markierung darf mehrfach passieren ohne Schaden (upsert + idempotente Point-ID).
- Entfernen aus Spam (= Mail aus Spam-Folder zurück) → Blocklist-Eintrag bleibt (Stefan löscht ihn manuell), Spam-Vektor wird gelöscht (`delete_points`).

## Datenmodell-Änderungen

### Neue PocketBase-Collection: `spam_rules`

| Feld | Typ | Beschreibung |
|---|---|---|
| `id` | text (auto) | PocketBase-Standard |
| `account` | relation→accounts | wessen Regel — pro Account separat |
| `match_type` | select(`email`, `domain`) | exakte Adresse oder ganze Domain |
| `pattern` | text, lowercase | z. B. `noreply@spammer.com` oder `spammer.com` |
| `created` | autodate | wann geblockt |
| `hits` | number, default 0 | wie oft hat die Regel schon ausgelöst |
| `last_hit` | date, optional | wann zuletzt ausgelöst |

**Index:** unique auf `(account, match_type, pattern)`.

### Neue Qdrant-Collection: `mailflow_spam_samples`

Payload pro Punkt:

```json
{
  "email_id": "pb-record-id",
  "account_id": "pb-account-id",
  "from_email": "absender@x.de",
  "subject": "...",
  "marked_at_ts": 1715000000
}
```

### PocketBase `emails`-Collection — neue Felder (additiv)

| Feld | Typ | Zweck |
|---|---|---|
| `spam_score` | number, optional | letzter Cosine-Score gegen `mailflow_spam_samples` (für UI-Badge + Tuning) |
| `spam_suggested` | bool, default false | Ebene-2-Vorschlag offen |
| `spam_rule_match` | text, optional | Doku, welche Regel/Sample ausgelöst hat (z. B. `domain:spammer.com` oder `vector:0.87`) |

Hinzufügen via `_add_missing_fields` in `pb_setup.py` — kein Migrationsschritt nötig.

### Settings (`backend/config.py`)

| Setting | Default |
|---|---|
| `SPAM_SIMILARITY_THRESHOLD` | `0.82` |
| `SPAM_AUTO_MOVE_ON_VECTOR_HIT` | `False` (Vorschlag-Modus) |

## Komponenten — Datei-für-Datei

### 1. `backend/spam_filter.py` (neu)

Zentrale Spam-Logik. Keine HTTP-Endpoints, nur Funktionen.

```python
# Öffentliche API
async def ensure_spam_collection() -> None
async def add_spam_sample(email: dict) -> None
async def remove_spam_sample(email_id: str) -> None
async def add_blocklist_entry(account_id: str, match_type: str, pattern: str) -> None
async def check_blocklist(account_id: str, from_email: str) -> dict | None
async def check_similarity(email: dict) -> dict | None
    # returns {"score": float, "matched_sample_id": str} or None
async def classify_incoming(email: dict) -> dict
    # returns {"action": "move"|"suggest"|"none", "reason": str, "score": float|None}
```

- `check_blocklist` macht zwei Lookups (exakter Match auf `email`, Domain-Match auf `domain`) — ein einziger PocketBase-Filter-Call mit `||`.
- `check_similarity` embedded den Subject+Body, ruft Qdrant mit `score_threshold=settings.SPAM_SIMILARITY_THRESHOLD`.
- `classify_incoming` ruft erst Blocklist, dann Similarity. Reihenfolge wichtig: Blocklist hart, Similarity weich.

### 2. `backend/vector_store.py` (erweitern)

Neue Konstanten und Funktionen — keine Änderungen an bestehenden Funktionen:

```python
SPAM_COLLECTION = "mailflow_spam_samples"
_SPAM_NS = uuid.UUID("...")  # neue UUID generieren

async def ensure_spam_collection() -> None
async def upsert_spam_sample(email_id: str, vector: list[float], payload: dict) -> None
async def delete_spam_sample(email_id: str) -> None
async def search_similar_spam(text: str, limit: int = 3) -> list[dict]
```

### 3. `backend/pb_setup.py` (erweitern)

- Funktion `_spam_rules_schema(accounts_id)` hinzufügen.
- In `setup_pocketbase_schema` aufrufen: `await _ensure_collection(client, headers, existing, _spam_rules_schema(accounts_id))`.
- Drei neue Felder in `_emails_schema` ergänzen oder via `_add_missing_fields` (additiv, sicher für Bestand).

### 4. `backend/main.py` (erweitern)

#### Bestehender Endpoint anpassen:

```python
@app.post("/emails/{email_id}/spam")
async def move_to_spam(
    email_id: str,
    block_sender: bool = False,        # NEU
    block_domain: bool = False,        # NEU (für Zukunft, jetzt false)
):
    # 1. wie bisher: IMAP-Move + PocketBase-Patch
    # 2. NEU: spam_filter.add_spam_sample(email)  → Qdrant
    # 3. NEU: wenn block_sender → spam_filter.add_blocklist_entry(...)
    # 4. NEU: wenn diese Mail vorher als spam_suggested markiert war → suggested-Flag löschen
```

#### Neue Endpoints:

```python
@app.post("/emails/{email_id}/unspam")
# Stefan hat fälschlich Spam markiert / Mail manuell aus Spam zurück
# → spam_filter.remove_spam_sample, suggested-Flag löschen, IMAP-Move zurück nach INBOX

@app.post("/emails/{email_id}/spam-suggestion/dismiss")
# „Doch kein Spam" auf einem Vorschlag-Badge → spam_suggested=false

@app.post("/emails/{email_id}/spam-suggestion/confirm")
# „Ja, Spam" auf einem Vorschlag-Badge → ruft intern move_to_spam mit block_sender=False

@app.get("/spam-rules")
@app.delete("/spam-rules/{rule_id}")
# Verwaltung der Blocklist (Settings-Seite)
```

#### Auto-Klassifikation einhängen:

- Hook in `imap_sync.py` direkt nach `pb_client.pb_post(...emails)` (Zeile ~248).
- Ablauf:
  1. Nur für neue eingehende Mails in INBOX (nicht für Sync von Spam/Trash/Sent).
  2. `result = await spam_filter.classify_incoming(record_with_id)`
  3. `result["action"] == "move"` → sofort `_imap_move_to_spam` + `pb_patch(folder=Spam, spam_rule_match=...)`
  4. `result["action"] == "suggest"` → `pb_patch(spam_suggested=true, spam_score=..., spam_rule_match=...)`
  5. `result["action"] == "none"` → nichts tun.

### 5. `backend/imap_sync.py` (kleine Ergänzung)

Nach erfolgreichem `pb_post` und `fts_insert` (~Zeile 258), für Mails in INBOX:

```python
# Spam-Auto-Klassifikation (best-effort, nicht blockierend bei Fehler)
if record.get("folder") == "INBOX":
    try:
        from spam_filter import classify_incoming
        await classify_incoming({**record, "id": email_id})
    except Exception as e:
        logger.warning(f"spam classify failed for {email_id}: {e}")
```

### 6. `frontend/js/api.js` (erweitern)

```js
spamEmail(id, opts = {}) {
  const qs = opts.blockSender ? '?block_sender=true' : '';
  return apiFetch(`/emails/${id}/spam${qs}`, { method: 'POST' });
},
unspamEmail(id) { return apiFetch(`/emails/${id}/unspam`, { method: 'POST' }); },
spamSuggestionConfirm(id) { return apiFetch(`/emails/${id}/spam-suggestion/confirm`, { method: 'POST' }); },
spamSuggestionDismiss(id) { return apiFetch(`/emails/${id}/spam-suggestion/dismiss`, { method: 'POST' }); },
spamRulesList()  { return apiFetch('/spam-rules'); },
spamRulesDelete(id) { return apiFetch(`/spam-rules/${id}`, { method: 'DELETE' }); },
```

### 7. `frontend/js/inbox.js` (erweitern)

#### Spam-Detail-Panel: zweiter Button

In `index.html` (~Zeile 112) zweiten Button daneben:

```html
<button class="action-btn danger" id="btn-spam">Spam</button>
<button class="action-btn danger" id="btn-spam-block">+ Absender blocken</button>
```

In `inbox.js` (~Zeile 1578) zweiten Handler:

```js
document.getElementById('btn-spam-block').onclick = () => spamEmail(email, itemEl, { blockSender: true });
```

`spamEmail(email, itemEl, opts = {})` so erweitern, dass `opts.blockSender` an `api.spamEmail(id, opts)` durchgereicht wird.

#### Inline-Spam-Vorschlag-Badge

Beim Rendern eines Listeneintrags (`renderEmailListItem` o. ä.) prüfen: wenn `email.spam_suggested === true`, gelben Banner einfügen:

```html
<div class="spam-suggestion-bar">
  Mögliches Spam (Score: 0.84). Ähnlich zu: "Betreff X"
  <button class="ssb-confirm">Ja, Spam</button>
  <button class="ssb-dismiss">Behalten</button>
</div>
```

Klick → `api.spamSuggestionConfirm(id)` bzw. `dismiss(id)`. Bei Confirm: gleiche Optimistic-UI-Logik wie `spamEmail`. Bei Dismiss: nur Badge entfernen.

#### Spam-Quick-Action im List-Item

`.qa-spam` bekommt zusätzlich Long-Press oder Rechtsklick-Kontextmenü mit „+ Absender blocken". Optional, nicht MVP — Detail-Panel reicht zunächst.

### 8. `frontend/index.html` + Settings-Seite

Neue Settings-Sektion „Spam-Regeln":

- Liste aller `spam_rules` (Pattern, Match-Type, Hits, Letzter Treffer)
- Pro Eintrag: Lösch-Button → `api.spamRulesDelete(id)`
- Optional MVP+1: manuelles Hinzufügen einer Regel

## Reihenfolge der Umsetzung

1. **Schema** — `pb_setup.py` (`spam_rules` + neue Felder in `emails`), `vector_store.py` (Spam-Collection-Funktionen). → Container neu starten, Schema migriert sich.
2. **Backend-Logik** — `spam_filter.py` neu, Endpoints in `main.py` erweitern.
3. **Auto-Klassifikation einhängen** — Hook in `imap_sync.py`. **Anfangs hinter Feature-Flag** (`SPAM_AUTO_CLASSIFY=False`), erst nach manuellem Smoke-Test scharf schalten.
4. **Frontend-API-Wrapper** — `api.js`.
5. **Detail-Panel-UI** — zweiter Button + Handler.
6. **Vorschlag-Badge-UI** — Inline-Bar in Listeneinträgen.
7. **Settings-Seite Spam-Regeln** — Verwaltung der Blocklist.
8. **Backfill (optional)** — bestehende Mails im IMAP-Spam-Folder als Spam-Samples einlernen (`backfill_spam.py` nach Vorbild `backfill_reply_to.py`). Bringt Lernsystem direkt mit Erfahrungswerten.

## Risiken und Tradeoffs

| Risiko | Maßnahme |
|---|---|
| False positives bei Domain-Block (z. B. `gmail.com`) | UI muss vor Domain-Block warnen; Default = nur exakter Adress-Block. Domain-Block erst per Settings-Seite manuell. |
| Falsch gelernte Spam-Samples (Stefan markiert versehentlich) | `unspam`-Endpoint muss Sample sicher löschen. UI: Mail im Spam-Folder hat „Doch kein Spam"-Button. |
| Embedding-Kosten | Pro neuer Mail ein Embedding-Call ohnehin geplant für Antwortvorschläge — gleicher Embed-Call kann für beide genutzt werden, wenn wir den `embed_text`-Output cachen. **MVP: zwei separate Calls, später optimieren.** |
| Qdrant nicht erreichbar | `classify_incoming` muss bei Qdrant-Fehler still scheitern → Mail bleibt in Inbox, Blocklist greift trotzdem (PocketBase-only). |
| Alte Mails ohne Embedding | Kein Problem für Vorwärts-Klassifikation. Backfill optional (Punkt 8). |
| Schwellwert 0.82 zu locker/streng | Settings-Wert; nach 1–2 Wochen Beobachtung von `spam_score` in PocketBase justieren. |

## Out-of-Scope (jetzt)

- Bayes-Filter / klassisches Token-Lernen — Embedding deckt das semantisch ab.
- LLM-basierte Spam-Klassifikation (Claude entscheidet) — zu teuer pro Mail, Embedding+Vektor ist 100× günstiger.
- Subject-Line-Regex-Regeln — übliche Spam-Floskeln werden vom Embedding mit erfasst.
- Bulk-Markierung (mehrere Mails als Spam markieren) — separate Aufgabe.

## Offene Punkte für nächste Session

- Konkrete UUID für `_SPAM_NS` festlegen (einmalig in `vector_store.py` hardcoden).
- CSS für `.spam-suggestion-bar` (gelb, dezent, ähnlich `#ci-replyto-warning`).
- Soll Domain-Block bei „+ Absender blocken" zusätzlich zur Auswahl stehen, oder strikt nur exakte Adresse? (Empfehlung: nur exakt, Domain-Block in Settings.)

---

## Beobachtungspunkte nach Aktivierung von SPAM_AUTO_CLASSIFY

**Notiert 2026-05-07.** Sobald das Feature-Flag scharf ist, beobachten:

### False-Positive-Häufung

Wenn legitime Mails wiederholt in der gelben Vorschlag-Bar auftauchen oder per Blocklist sofort verschoben werden, müssen wir die **Quelle des Treffers** identifizieren und entfernen können.

Zwei mögliche Ursachen:

1. **Falsch gelerntes Spam-Sample** — irgendeine frühere als Spam markierte Mail hat semantisch zu viel mit den legitimen Mails gemeinsam (z. B. fälschlicherweise eine Newsletter-Variante als Spam markiert, die der gewünschten Newsletter-Form sehr ähnelt).
2. **Zu weit gefasste Blocklist-Regel** — z. B. eine Domain-Regel, die sowohl Spam-Versender als auch legitime Absender abdeckt.

### Diagnose-Workflow (manuell, bis UI in Phase 2/3)

**Bei Vektor-Treffer (gelbe Bar):**
- In PocketBase die Mail öffnen → Feld `spam_rule_match` enthält den Hash/Score und die `email_id` des auslösenden Samples (Format: `vector:0.873:abc123def456`).
- Im Qdrant (`mailflow_spam_samples`-Collection) nach diesem `email_id` im Payload suchen → Sample identifizieren und löschen.
- Alternativ: `spam_filter.search_similar_spam(verdächtige_mail)` per Python-Konsole im Container aufrufen → Top-Treffer ist das Problem-Sample.

**Bei Blocklist-Treffer (Auto-Move):**
- `spam_rule_match` enthält `email:absender@x.de` oder `domain:x.de`.
- In PocketBase `spam_rules`-Collection den Eintrag finden und löschen.
- Mail per Drag aus Spam zurück in INBOX (nicht via Unspam, weil wir Sample-Cleanup separat brauchen).

### Konsequenz für Phase 2/3

Wenn diese Diagnose **mehr als ein paar Mal pro Woche** nötig wird, lohnt sich:

- **UI-Erweiterung in Phase 2:** Spam-Regeln-Verwaltung soll auch Spam-Samples anzeigen (Subject + Date + Score gegen jüngste Treffer) und ein Lösch-Button pro Sample.
- **„Anti-Spam"-Lernsignal beim „Behalten":** Aktuell wird der „Behalten"-Klick nur als Dismiss behandelt. Erweiterung: zusätzliche Qdrant-Collection `mailflow_ham_samples` als Negativ-Signal — wenn neue Mail Vektor-Treffer hat, prüfe vorher ob sie nicht Ham-Sample näher steht. Verhindert wiederkehrende False-Positives ohne manuellen Eingriff.

Bei moderater Häufung (1-2 Fehler pro Woche) reicht der manuelle Workflow erstmal.

---

## Phase 2 — Spam-Regeln-Verwaltung in der Hauptleiste

**Notiert 2026-05-07.** Ersetzt Punkt 7 der ursprünglichen Reihenfolge (Settings-Sektion) — Stefan möchte die Verwaltung direkt erreichbar in der Top-Toolbar, nicht in einem Settings-Untermenü.

### Anforderungen

- Neuer Button in der Top-Funktionsleiste (oben), z. B. mit Schloss- oder Verbots-Icon und Counter-Badge mit der Anzahl aktiver Regeln.
- Klick öffnet Modal/Drawer mit Liste aller `spam_rules` für den aktuellen Account.
- Pro Eintrag sichtbar:
  - Pattern (z. B. `noreply@spammer.com`)
  - Match-Type (`E-Mail` / `Domain`)
  - Hits-Counter und letzter Treffer
  - **Entblocken-Button** → `api.spamRulesDelete(id)` → Eintrag verschwindet aus Liste, Bestätigungs-Toast „Absender wieder erlaubt".
- Suchfeld zum Filtern, falls Liste lang wird (sortiert nach `last_hit DESC`).

### Datei-Änderungen

| Datei | Änderung |
|---|---|
| `frontend/index.html` | Neuer Toolbar-Button `#btn-spam-rules` mit Counter-Badge `<span id="spam-rules-count">`. Modal-Container `#spam-rules-modal`. |
| `frontend/js/inbox.js` | Neue Funktion `openSpamRulesModal()`, Render-Logik, Lösch-Handler. Counter aktualisieren in `init()` und nach jedem Block/Unblock. |
| `frontend/css/main.css` | Styling für Modal + Listeneinträge. |

Backend ist durch Phase 1 (`/spam-rules` GET + DELETE Endpoints) bereits abgedeckt.

### Nice-to-have (innerhalb Phase 2)

- Kontextmenü auf Listeneintrag: „Domain auch blocken" → fügt zweite Regel mit `match_type=domain` hinzu.
- Anzeige der zuletzt durch diese Regel geblockten Mails (Klappe ausklappbar).

---

## Phase 3 — Erweiterte Quick-Actions im Listeneintrag

**Notiert 2026-05-07.** Aktuell hat jeder Listeneintrag bei Hover zwei Quick-Actions: Löschen (`×`) und Spam (`!`). Das soll umgebaut werden.

### Zielzustand

```
┌─────────┐ ┌──┐
│         │ │S1│   S1 = "Spam (nur diese)"
│   ×     │ ├──┤
│ (groß)  │ │S2│   S2 = "Spam + Absender blocken"
│         │ └──┘
└─────────┘
```

- **Löschen-Button:** in der Höhe verdoppelt (über volle Listenitem-Höhe), Position links wie bisher.
- **Spam-Buttons:** zwei kleine, vertikal gestapelte Buttons rechts neben Löschen.
  - Oberer: bisheriger Spam-Button-Effekt (`api.spamEmail(id)` ohne `block_sender`).
  - Unterer: neuer Effekt (`api.spamEmail(id, { blockSender: true })`).
- Tooltips deutlich, weil Icons knapp.

### Datei-Änderungen

| Datei | Änderung |
|---|---|
| `frontend/js/inbox.js` (~Zeile 1148) | `email-quick-actions`-HTML-Template umbauen: ein `.qa-delete-tall` + Container `.qa-spam-stack` mit `.qa-spam-only` und `.qa-spam-block`. Drei Click-Handler statt zwei. |
| `frontend/css/main.css` | Layout via Flex/Grid: Löschen full-height, Spam-Stack 2×0.5-height. Hover-States, Farben (Spam+Block vielleicht etwas dunkler/wärmer als Spam-Only zur Unterscheidung). |

### Risiken

- Mehr Hover-Targets → versehentliches Klicken. Gegenmaßnahme: deutliche Trennung durch Abstand und unterschiedliche Farb-Akzente.
- Bei sehr schmaler Spaltenbreite kann der Stack klemmen. Gegenmaßnahme: Min-Width des Stack-Containers, ggf. unter Schwellwert auf alten Single-Spam-Button zurückfallen (Block-Variante dann nur via Detail-Panel-Button aus Phase 1).

### Reihenfolge

Phase 3 kann unabhängig von Phase 2 implementiert werden, beide bauen aber auf Phase 1 auf (Endpoints + `block_sender`-Parameter müssen existieren).
