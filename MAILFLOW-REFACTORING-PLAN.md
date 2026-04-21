# Mailflow – Refactoring-Plan

Erstellt: 2026-04-21  
Basis: Codex-Review des Gesamtprojekts

Dieser Plan enthält zwei gezielte Verbesserungen, die auf Basis eines externen Code-Reviews als relevant eingestuft wurden. Alle anderen Review-Punkte wurden bewusst als „nicht relevant für dieses Projekt" abgelegt (Begründungen unten).

---

## Schritt 1 — API-Key aus dem Frontend-Code entfernen

### Problem

In `frontend/js/api.js` Zeile 6 steht der Produktions-API-Key als Klartext-Fallback:

```js
const API_KEY = window.MAILFLOW_API_KEY || 'rWuVH8m3VguMHRFxGMrRcK-QOKYDut_un2yElm_VMaY';
```

Wer die JS-Datei lädt (mailflow.barres.de ist per Browser erreichbar), hat den Key und kann direkt die Backend-API unter mailflow-api.barres.de aufrufen — ohne Umweg über die Frontend-Auth. Der Key taucht damit sowohl im Git-Repo als auch in der ausgelieferten JS-Datei auf.

### Lösung

Nginx liefert eine kleine `/config.js`-Datei aus, die `window.MAILFLOW_API_KEY` setzt. Der Wert kommt aus einer Coolify-Umgebungsvariable und ist nie im Quellcode.

### Konkrete Umsetzungsschritte

1. **`nginx.conf`**: Neuen Location-Block für `/config.js` hinzufügen, der den Key aus einer Umgebungsvariable injiziert. Da nginx keine nativen Env-Vars in `return`-Direktiven unterstützt, wird beim Container-Start ein kleines Shell-Script (`docker-entrypoint.sh`) die Datei `/usr/share/nginx/html/config.js` aus der Env-Variable generieren.

2. **`index.html`**: `<script src="/config.js"></script>` als erstes Script im `<head>` einfügen, vor `api.js`.

3. **`api.js`**: Fallback-Key entfernen:
   ```js
   const API_KEY = window.MAILFLOW_API_KEY || '';
   ```

4. **`docker-compose.yml`**: Dem nginx-Service die Env-Variable `MAILFLOW_API_KEY` zugänglich machen.

5. **Coolify**: `MAILFLOW_API_KEY` als Env-Variable für den Frontend-Container setzen (analog zu `API_KEY` beim Backend-Container — den gleichen Wert verwenden).

6. **Wissensdatei aktualisieren**: `Wissens-Dateien/HPA24/20_Apps/mailflow/README.md` Security-Abschnitt anpassen.

**Aufwand:** ~1 Stunde  
**Risiko:** Niedrig — nur Nginx-Konfiguration und eine Zeile JS-Änderung

---

## Schritt 2 — Blockierende IMAP-Operationen in Executor auslagern

### Problem

Vier Funktionen in `backend/main.py` führen synchrone IMAP-I/O direkt im asyncio-Event-Loop aus — ohne `run_in_executor`. Das blockiert FastAPI während der gesamten IMAP-Verbindungszeit (1–5 Sekunden je nach Mailserver) und verhindert, dass andere Requests parallel bearbeitet werden:

| Funktion | Zeile | Ausgelöst durch |
|---|---|---|
| `_imap_move_to_spam` | ~1045 | Spam-Button |
| `_imap_move` | ~1112 | Ordner-Verschieben |
| `_imap_trash` | ~1170 | Löschen |
| `_imap_set_read` | ~1578 | Gelesen/Ungelesen-Toggle |

In anderen Teilen des Codes (Draft-Append, Bulk-Flag-Sync) wird `run_in_executor` bereits korrekt genutzt — das ist inkonsistent.

### Lösung

Das blockierende `with IMAPClient(...): ...`-Innere jeder Funktion wird in eine synchrone Hilfsfunktion extrahiert und per `await loop.run_in_executor(None, _sync_fn, ...)` aufgerufen. Das ist dasselbe Muster, das in `imap_sync.py` und beim IMAP-Append bereits eingesetzt wird.

### Muster (Beispiel `_imap_set_read`)

**Vorher:**
```python
async def _imap_set_read(email: dict, is_read: bool) -> None:
    ...
    with IMAPClient(acc["imap_host"], ...) as srv:
        srv.login(acc["imap_user"], acc["imap_pass"])
        srv.select_folder(folder)
        if is_read:
            srv.set_flags([imap_uid], [b"\\Seen"])
        else:
            srv.remove_flags([imap_uid], [b"\\Seen"])
```

**Nachher:**
```python
def _imap_set_read_sync(acc: dict, imap_uid: int, folder: str, is_read: bool) -> None:
    with IMAPClient(acc["imap_host"], ...) as srv:
        srv.login(acc["imap_user"], acc["imap_pass"])
        srv.select_folder(folder)
        if is_read:
            srv.set_flags([imap_uid], [b"\\Seen"])
        else:
            srv.remove_flags([imap_uid], [b"\\Seen"])

async def _imap_set_read(email: dict, is_read: bool) -> None:
    ...
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _imap_set_read_sync, acc, imap_uid, folder, is_read)
```

Gleiches Muster für `_imap_move_to_spam`, `_imap_move` und `_imap_trash`. Bei Funktionen mit Rückgabewert (neue IMAP-UID) wird dieser aus dem Executor-Ergebnis durchgereicht.

### Konkrete Umsetzungsschritte

1. `_imap_set_read_sync` extrahieren, `_imap_set_read` auf Executor umstellen
2. `_imap_trash_sync` extrahieren, `_imap_trash` auf Executor umstellen
3. `_imap_move_sync` extrahieren, `_imap_move` auf Executor umstellen (Rückgabe: neue UID)
4. `_imap_move_to_spam_sync` extrahieren, `_imap_move_to_spam` auf Executor umstellen (Rückgabe: spam_folder + neue UID)
5. Nach Umbau: Manuell testen — Spam, Löschen, Verschieben, Gelesen/Ungelesen

**Aufwand:** ~1–2 Stunden  
**Risiko:** Niedrig — rein mechanische Umstrukturierung, keine Logikänderung

---

## Bewusst nicht umgesetzt — Begründungen

### Monolithische Dateien (`main.py` 1624 Zeilen, `inbox.js` 2480 Zeilen)

Die Logik ist bereits gut in separate Module ausgelagert (`imap_sync.py`, `smtp_sender.py`, `fts.py`, `pb_client.py` etc.). Ein weiteres Aufsplitten von `inbox.js` würde ein ES-Module-Setup erfordern, das aktuell nicht konfiguriert ist. Hoher Umbauaufwand, hohes Regressionsrisiko, kein konkreter Nutzwert für ein Single-Developer-Projekt.

### In-Memory-State (`_temp_uploads`, `_import_status`)

Bewusste Designentscheidung (dokumentiert). Temporäre Uploads im RAM (max. 25 MB, bei Neustart verloren) ist für Single-User/Single-Instance korrekt und ausreichend. Eine Umstellung auf persistente Storage würde Infrastruktur-Aufwand ohne echten Mehrwert bedeuten.

### Test-Suite

Für ein persönliches Produktivwerkzeug wäre eine formale Test-Suite Over-Engineering. Die vorhandenen Skripte (`test_imap.py`, `reset_emails.py` etc.) sind für diesen Projekttyp angemessen.

---

## Reihenfolge

1. **Schritt 1** zuerst (Security-relevanter)
2. **Schritt 2** danach (unabhängig von Schritt 1)

Beide Schritte können im selben Chat abgearbeitet werden.
