"""IMAP-Hilfen — kapselt wiederholtes Login/Connect-Boilerplate.

Phase 1 von C3: zentraler `imap_session(acc)`-Context-Manager. Alle Aufrufer in
main.py rufen die gleiche Funktion statt jeweils eigenes IMAPClient + login.

Phase 2 (TODO): vollständige ImapService-Klasse mit move/trash/set_read/
fetch_attachment-Methoden, async-Wrapper (asyncio.to_thread), BODYSTRUCTURE
statt BODY[] (B9). Vorerst nicht — würde diese Session sprengen.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from typing import Iterator

from imapclient import IMAPClient


@contextmanager
def imap_session(acc: dict) -> Iterator[IMAPClient]:
    """Öffnet eine IMAP-Verbindung für ein Account-Dict (PB-Record).
    Login + automatische Cleanup via with-Statement.

    Beispiel:
        with imap_session(acc) as srv:
            srv.select_folder("INBOX")
            ...
    """
    host = acc["imap_host"]
    port = int(acc.get("imap_port") or 993)
    user = acc["imap_user"]
    password = acc["imap_pass"]
    with IMAPClient(host, port=port, ssl=True) as srv:
        srv.login(user, password)
        yield srv


async def run_blocking(fn, *args, **kwargs):
    """Wrapper um asyncio.to_thread für blocking IMAP-Operationen.
    Bestehende Aufrufer nutzen oft loop.run_in_executor — diese Funktion
    kapselt das einheitlich.
    """
    return await asyncio.to_thread(fn, *args, **kwargs)
