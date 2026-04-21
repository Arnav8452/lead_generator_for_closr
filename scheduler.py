"""
Closr — APScheduler Cron Scheduler
Runs the master orchestration loop on a daily schedule:
  - 06:00 IST (00:30 UTC) — morning batch before 9am app open
  - 13:00 IST (07:30 UTC) — afternoon refresh

Uses APScheduler's AsyncIOScheduler for non-blocking execution.
"""

import logging
from datetime import datetime

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz

from config import validate_config, TIMEZONE, LOG_LEVEL
from main import master_orchestration_loop

# ─────────────────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────────────────
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("closr.scheduler")


def scheduled_run():
    """Wrapper for the scheduled pipeline execution with error isolation."""
    try:
        logger.info(
            f"Scheduled pipeline run starting at "
            f"{datetime.now(pytz.timezone(TIMEZONE)).strftime('%Y-%m-%d %H:%M:%S %Z')}"
        )
        metrics = master_orchestration_loop()
        logger.info(
            f"Scheduled run complete — injected {metrics['injected']} leads"
        )
    except Exception as e:
        logger.critical(f"Scheduled pipeline run failed: {e}", exc_info=True)


def start_scheduler():
    """
    Initialize and start the APScheduler with two daily triggers:
    - 06:00 IST — pre-morning batch
    - 13:00 IST — afternoon refresh
    """
    validate_config()

    scheduler = BlockingScheduler(timezone=TIMEZONE)

    # Morning run at 06:00 IST (gives 3 hours before 9am app open)
    scheduler.add_job(
        scheduled_run,
        CronTrigger(hour=6, minute=0, timezone=TIMEZONE),
        id="morning_pipeline",
        name="Morning Pipeline Run (06:00 IST)",
        misfire_grace_time=3600,  # Allow 1hr late execution
        coalesce=True,           # Skip missed runs, only run latest
    )

    # Afternoon run at 13:00 IST (pool refresh)
    scheduler.add_job(
        scheduled_run,
        CronTrigger(hour=13, minute=0, timezone=TIMEZONE),
        id="afternoon_pipeline",
        name="Afternoon Pipeline Run (13:00 IST)",
        misfire_grace_time=3600,
        coalesce=True,
    )

    logger.info(
        f"Scheduler started with {len(scheduler.get_jobs())} jobs:\n"
        f"  1. Morning Pipeline  — 06:00 {TIMEZONE}\n"
        f"  2. Afternoon Pipeline — 13:00 {TIMEZONE}"
    )

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Scheduler shutting down gracefully…")
        scheduler.shutdown(wait=False)


if __name__ == "__main__":
    start_scheduler()
