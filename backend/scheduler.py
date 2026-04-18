import logging
from datetime import datetime, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from imap_sync import sync_all_accounts, set_last_sync

logger = logging.getLogger(__name__)

_scheduler: AsyncIOScheduler | None = None


async def _run_sync() -> None:
    logger.info("Scheduler: starting automatic sync")
    try:
        await sync_all_accounts()
        set_last_sync(datetime.now(timezone.utc))
        logger.info("Scheduler: sync completed")
    except Exception as e:
        logger.error(f"Scheduler: sync failed: {e}")


def start_scheduler() -> None:
    global _scheduler
    _scheduler = AsyncIOScheduler()
    _scheduler.add_job(
        _run_sync,
        trigger=IntervalTrigger(minutes=2),
        id="imap_sync",
        max_instances=1,
        coalesce=True,
    )
    _scheduler.start()
    logger.info("Scheduler started (interval: 2 min)")


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("Scheduler stopped")
