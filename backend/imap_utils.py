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
