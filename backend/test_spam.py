"""
test_spam.py — Smoke-Test für das Spam-Lernsystem (Phase 1).

Ausführung im Backend-Container:
    docker compose exec backend python3 /app/test_spam.py

Was geprüft wird:
- Schema (PocketBase-Collections + neue emails-Felder + Qdrant-Collection)
- Spam-Sample lernen und wiederfinden
- Blocklist-Eintrag anlegen, prüfen
- classify_incoming durchläuft alle drei Pfade: suggest → move → none
- Aufräumen am Ende: Sample + Regel werden wieder gelöscht

Die Test-Mail im INBOX wird NICHT verschoben oder verändert.
Nur Qdrant-Samples und PocketBase-Regeln werden temporär angelegt.
"""
import asyncio
import sys

sys.path.insert(0, "/app")

import pb_client
import spam_filter
import vector_store
from config import settings


def step(title: str) -> None:
    print(f"\n=== {title} ===")


def ok(msg: str) -> None:
    print(f"  ✓ {msg}")


def fail(msg: str) -> None:
    print(f"  ✗ {msg}")


def info(msg: str) -> None:
    print(f"  · {msg}")


async def check_schema() -> bool:
    step("1. Schema-Verifikation")
    all_ok = True

    try:
        coll = await pb_client.pb_get("/api/collections/spam_rules")
        ok(f"PocketBase-Collection 'spam_rules' existiert (id: {coll['id']})")
    except Exception as e:
        fail(f"PocketBase 'spam_rules' fehlt: {e}")
        return False

    coll = await pb_client.pb_get("/api/collections/emails")
    field_names = {f["name"] for f in coll.get("fields", [])}
    for f in ("spam_score", "spam_suggested", "spam_rule_match"):
        if f in field_names:
            ok(f"emails.{f} existiert")
        else:
            fail(f"emails.{f} fehlt")
            all_ok = False

    if not settings.QDRANT_URL:
        fail("QDRANT_URL nicht konfiguriert — Vektor-Tests nicht möglich")
        return False

    try:
        client = vector_store._get_client()
        cols = await client.get_collections()
        names = {c.name for c in cols.collections}
        if vector_store.SPAM_COLLECTION in names:
            ok(f"Qdrant-Collection '{vector_store.SPAM_COLLECTION}' existiert")
        else:
            fail(f"Qdrant '{vector_store.SPAM_COLLECTION}' fehlt (wird beim nächsten Backend-Start angelegt)")
            all_ok = False
    except Exception as e:
        fail(f"Qdrant nicht erreichbar: {e}")
        return False

    return all_ok


async def pick_test_email() -> dict | None:
    step("2. Test-Mail aus INBOX auswählen")
    result = await pb_client.pb_get(
        "/api/collections/emails/records",
        params={
            "filter": 'folder="INBOX" && body_plain != ""',
            "perPage": 1,
            "sort": "-date_sent",
        },
    )
    items = result.get("items", [])
    if not items:
        fail("Keine INBOX-Mail mit Body gefunden")
        return None
    email = items[0]
    info(f"Mail-ID:   {email['id']}")
    info(f"Account:   {email['account']}")
    info(f"Absender:  {email.get('from_email')}")
    info(f"Betreff:   {(email.get('subject') or '')[:80]}")
    info(f"Body-Len:  {len(email.get('body_plain') or '')} Zeichen")
    ok("Mail gefunden")
    return email


async def test_sample_learn(email: dict) -> bool:
    step("3. Spam-Sample lernen (add_spam_sample)")
    await spam_filter.add_spam_sample(email)
    ok("add_spam_sample aufgerufen")
    hits = await vector_store.search_similar_spam(email, limit=3)
    if hits and hits[0]["score"] > 0.99:
        ok(f"Sample in Qdrant gefunden: Score {hits[0]['score']:.4f}")
        return True
    fail(f"Sample nicht oder mit zu niedrigem Score in Qdrant (hits={len(hits)})")
    return False


async def test_classify_suggest(email: dict) -> bool:
    step("4. classify_incoming → 'suggest' (Vektor-Treffer, keine Blocklist)")
    cls = await spam_filter.classify_incoming(email)
    info(f"Result: {cls}")
    if cls["action"] == "suggest":
        ok(f"action='suggest', score={cls['score']:.3f}")
        return True
    fail(f"Erwartet 'suggest', bekommen '{cls['action']}'")
    return False


async def test_blocklist_add(email: dict) -> dict | None:
    step("5. Blocklist-Eintrag hinzufügen")
    rule = await spam_filter.add_blocklist_entry(
        email["account"], email["from_email"], block_domain=False,
    )
    if rule:
        ok(f"Regel angelegt: id={rule['id']}, pattern={rule['pattern']}")
        return rule
    fail("add_blocklist_entry hat None geliefert (möglicherweise Duplikat aus früherem Lauf — prüfe spam_rules in PocketBase)")
    return None


async def test_blocklist_check(email: dict) -> bool:
    step("6. check_blocklist (Treffer + Nicht-Treffer)")
    hit = await spam_filter.check_blocklist(email["account"], email["from_email"])
    if hit:
        ok(f"Treffer für eigenen Absender: {hit['match_type']}={hit['pattern']}")
    else:
        fail("Kein Treffer für eigenen Absender")
        return False
    miss = await spam_filter.check_blocklist(
        email["account"], "definitely-not-blocked@example.invalid"
    )
    if miss is None:
        ok("Kein Treffer für unbekannte Fake-Adresse")
        return True
    fail(f"Falsch-positiver Treffer: {miss}")
    return False


async def test_classify_move(email: dict) -> bool:
    step("7. classify_incoming → 'move' (Blocklist gewinnt vor Vektor)")
    cls = await spam_filter.classify_incoming(email)
    info(f"Result: {cls}")
    if cls["action"] == "move":
        ok(f"action='move', reason={cls['reason']}, rule_match={cls['rule_match']}")
        return True
    fail(f"Erwartet 'move', bekommen '{cls['action']}'")
    return False


async def cleanup(email: dict, rule: dict | None) -> None:
    step("8. Aufräumen")
    if rule:
        try:
            await pb_client.pb_delete(f"/api/collections/spam_rules/records/{rule['id']}")
            ok(f"Blocklist-Eintrag {rule['id']} gelöscht")
        except Exception as e:
            fail(f"Blocklist-Lösch-Fehler: {e}")

    await spam_filter.remove_spam_sample(email["id"])
    ok("Spam-Sample entfernt")

    miss = await spam_filter.check_blocklist(email["account"], email["from_email"])
    if miss is None:
        ok("Blocklist-Check nach Cleanup: leer")
    else:
        fail(f"Blocklist-Eintrag noch da: {miss}")

    cls = await spam_filter.classify_incoming(email)
    if cls["action"] == "none":
        ok("classify_incoming → 'none' (sauberer Zustand)")
    else:
        fail(f"classify_incoming liefert noch '{cls['action']}': {cls}")


async def main() -> None:
    print("Mailflow Spam-Smoke-Test (Phase 1)")
    print("=" * 50)

    await pb_client.authenticate()

    if not await check_schema():
        print("\nABBRUCH: Schema unvollständig. Backend neu starten und Logs prüfen.")
        return

    email = await pick_test_email()
    if not email:
        return

    rule = None
    try:
        await test_sample_learn(email)
        await test_classify_suggest(email)
        rule = await test_blocklist_add(email)
        if rule:
            await test_blocklist_check(email)
            await test_classify_move(email)
    finally:
        await cleanup(email, rule)

    print("\n" + "=" * 50)
    print("Fertig. Alle ✓ = Phase 1 grün; mind. ein ✗ = bitte zurückmelden.")


if __name__ == "__main__":
    asyncio.run(main())
