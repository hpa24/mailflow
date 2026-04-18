"""IMAP IDLE Manager — überwacht INBOX pro Account auf neue Nachrichten."""
import asyncio
import logging

from imapclient import IMAPClient

logger = logging.getLogger(__name__)

# SSE-Queues: eine pro verbundenem Frontend-Client
_sse_queues: list[asyncio.Queue] = []


def get_sse_queues() -> list[asyncio.Queue]:
    return _sse_queues


async def notify_new_mail() -> None:
    """Benachrichtigt alle verbundenen SSE-Clients über neue Mail."""
    for q in list(_sse_queues):
        try:
            q.put_nowait({"type": "new-mail"})
        except asyncio.QueueFull:
            pass


class IdleManager:
    def __init__(self) -> None:
        self._tasks: dict[str, asyncio.Task] = {}
        self._running = False

    async def start(self) -> None:
        self._running = True
        await self._launch_all()

    async def stop(self) -> None:
        self._running = False
        for task in self._tasks.values():
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        self._tasks.clear()

    async def _launch_all(self) -> None:
        import pb_client
        result = await pb_client.pb_get(
            "/api/collections/accounts/records",
            params={"perPage": 100},
        )
        for acc in result.get("items", []):
            self._launch_account(acc)

    def _launch_account(self, acc: dict) -> None:
        account_id = acc["id"]
        existing = self._tasks.get(account_id)
        if existing and not existing.done():
            return
        task = asyncio.create_task(
            self._idle_loop(acc),
            name=f"idle-{acc.get('imap_user', account_id)}",
        )
        task.add_done_callback(
            lambda t: t.exception() and logger.error(
                "IDLE-Task für %s unerwartet beendet: %s",
                acc.get("imap_user"), t.exception(),
            )
        )
        self._tasks[account_id] = task

    async def _idle_loop(self, acc: dict) -> None:
        backoff = 5
        while self._running:
            try:
                loop = asyncio.get_running_loop()
                has_change = await loop.run_in_executor(
                    None,
                    _blocking_idle,
                    acc["imap_host"],
                    int(acc.get("imap_port") or 993),
                    acc["imap_user"],
                    acc["imap_pass"],
                )
                if has_change:
                    logger.info("IDLE: Änderung erkannt für %s — starte Sync", acc["imap_user"])
                    from imap_sync import sync_account
                    await sync_account(acc, full_import=False)
                    await notify_new_mail()
                backoff = 5  # Backoff nach Erfolg zurücksetzen
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(
                    "IDLE-Fehler für %s: %s — Retry in %ds",
                    acc.get("imap_user"), e, backoff,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 300)  # Max. 5 Minuten


def _blocking_idle(host: str, port: int, user: str, password: str) -> bool:
    """Blockierend: baut IMAP-Verbindung auf, wartet via IDLE auf Änderungen.

    Gibt True zurück wenn der Server eine Änderung gemeldet hat (neue Mail,
    gelöschte Mail etc.). Gibt False zurück bei regulärem 28-Minuten-Timeout.
    """
    with IMAPClient(host, port=port, ssl=True) as srv:
        srv.login(user, password)
        srv.select_folder("INBOX", readonly=True)
        srv.idle()
        responses = srv.idle_check(timeout=28 * 60)  # 28 min — unter dem RFC-Limit von 29
        srv.idle_done()
        return bool(responses)


idle_manager = IdleManager()
