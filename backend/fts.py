import sqlite3
import logging

logger = logging.getLogger(__name__)


def fts_setup(db_path: str) -> None:
    """Create FTS5 virtual table if it doesn't exist. Called once at startup."""
    conn = sqlite3.connect(db_path, timeout=10)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS fts_emails
            USING fts5(
                email_id,
                subject,
                body_plain,
                from_email,
                from_name,
                content='',
                tokenize='unicode61'
            )
        """)
        conn.commit()
        logger.info("FTS5 table ready")
    except Exception as e:
        logger.error(f"FTS5 setup failed: {e}")
        raise
    finally:
        conn.close()


def fts_insert(db_path: str, email_id: str, subject: str, body: str,
               from_email: str, from_name: str) -> None:
    """Insert email into FTS5 index. Call after every new email saved to PocketBase."""
    conn = sqlite3.connect(db_path, timeout=10)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            "INSERT INTO fts_emails(email_id, subject, body_plain, from_email, from_name) "
            "VALUES (?, ?, ?, ?, ?)",
            (email_id, subject, body, from_email, from_name)
        )
        conn.commit()
    finally:
        conn.close()


def fts_search(db_path: str, query: str) -> list[str]:
    """Search FTS5 index. Returns list of valid PocketBase email record IDs."""
    conn = sqlite3.connect(db_path, timeout=10)
    try:
        rows = conn.execute(
            "SELECT email_id FROM fts_emails WHERE fts_emails MATCH ? ORDER BY rank",
            (query,)
        ).fetchall()
        # Ungültige Einträge (z. B. "None") herausfiltern
        return [r[0] for r in rows if r[0] and r[0] != "None"]
    finally:
        conn.close()


def fts_delete(db_path: str, email_id: str) -> None:
    """Entfernt eine E-Mail aus dem FTS5-Index. Aufrufen wenn PocketBase-Record gelöscht wird."""
    conn = sqlite3.connect(db_path, timeout=10)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("DELETE FROM fts_emails WHERE email_id = ?", (email_id,))
        conn.commit()
    finally:
        conn.close()


def fts_rebuild(db_path: str, records: list[dict]) -> int:
    """FTS5-Index aus PocketBase-Datensätzen neu aufbauen. Gibt Anzahl ein­gefügter Einträge zurück."""
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("DELETE FROM fts_emails")
        conn.executemany(
            "INSERT INTO fts_emails(email_id, subject, body_plain, from_email, from_name) "
            "VALUES (?, ?, ?, ?, ?)",
            [(r["id"], r.get("subject", ""), r.get("body_plain", ""),
              r.get("from_email", ""), r.get("from_name", "")) for r in records]
        )
        conn.commit()
        return len(records)
    finally:
        conn.close()
