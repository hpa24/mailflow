"""Gemeinsame IMAP-Hilfsfunktionen für main.py und smtp_sender.py."""


def find_imap_folder(srv, flags: list[bytes], fallbacks: list[str]) -> str | None:
    """Findet einen IMAP-Ordner per Flag oder bekannten Fallback-Namen."""
    for folder_flags, _delim, name in srv.list_folders():
        if any(f in folder_flags for f in flags):
            return name
    for candidate in fallbacks:
        try:
            srv.select_folder(candidate, readonly=True)
            return candidate
        except Exception:
            continue
    return None


def resolve_imap_path(srv, name: str) -> str:
    """Übersetzt einen normierten UI-Ordnernamen (Spam/Trash/Drafts/Sent) in den echten IMAP-Pfad.

    Hintergrund: PocketBase emails.folder enthält den normierten Namen (siehe
    imap_sync._IMAP_FLAG_TO_STANDARD). IMAP-Operationen brauchen aber den echten Server-Pfad
    (z. B. 'Junk' statt 'Spam'). Andere Werte (inkl. INBOX) bleiben unverändert.
    """
    if not name:
        return name
    n = name.lower()
    if n == "spam":
        return find_imap_folder(srv, [b"\\Junk", b"\\Spam"],
                                 ["Spam", "Junk", "Junk E-Mail", "INBOX.Spam", "INBOX.Junk"]) or name
    if n == "trash":
        return find_imap_folder(srv, [b"\\Trash", b"\\Deleted"],
                                 ["Trash", "Papierkorb", "Deleted", "Deleted Items", "INBOX.Trash"]) or name
    if n == "drafts":
        return find_imap_folder(srv, [b"\\Drafts", b"\\Draft"],
                                 ["Drafts", "Draft", "Entwürfe", "INBOX.Drafts"]) or name
    if n == "sent":
        return find_imap_folder(srv, [b"\\Sent"],
                                 ["Sent", "Gesendet", "Gesendete Objekte", "INBOX.Sent"]) or name
    return name
