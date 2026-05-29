"""
Daily scheduler — runs the reporting tool automatically at a configured time.

Usage:
    python scheduler.py              # Start scheduler (runs at 7:00 AM daily)
    python scheduler.py --time 06:30 # Use a custom time (HH:MM, 24-hour)
    python scheduler.py --run-now    # Run immediately once, then keep scheduling

Keep this process running (e.g. with nohup or launchd on macOS).
"""

import argparse
import asyncio
import logging
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from src.utils import setup_logging
from main import run

log = logging.getLogger(__name__)

DEFAULT_HOUR = 7
DEFAULT_MINUTE = 0


async def scheduled_job():
    log.info("Scheduler triggered at %s", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    await run()


async def start_scheduler(hour: int, minute: int, run_now: bool):
    scheduler = AsyncIOScheduler()
    trigger = CronTrigger(hour=hour, minute=minute)
    scheduler.add_job(scheduled_job, trigger, id="daily_report")
    scheduler.start()
    log.info("Scheduler started — reports will run daily at %02d:%02d", hour, minute)

    if run_now:
        log.info("Running immediately as requested...")
        await run()

    try:
        while True:
            await asyncio.sleep(60)
    except (KeyboardInterrupt, SystemExit):
        log.info("Scheduler stopped.")
        scheduler.shutdown()


def main():
    setup_logging()

    parser = argparse.ArgumentParser(description="Amazon Report Scheduler")
    parser.add_argument("--time", default=f"{DEFAULT_HOUR:02d}:{DEFAULT_MINUTE:02d}",
                        help="Daily run time in HH:MM format (24-hour). Default: 07:00")
    parser.add_argument("--run-now", action="store_true",
                        help="Also run immediately on startup")
    args = parser.parse_args()

    try:
        hour, minute = map(int, args.time.split(":"))
    except ValueError:
        log.error("Invalid time format '%s'. Use HH:MM.", args.time)
        return

    asyncio.run(start_scheduler(hour, minute, args.run_now))


if __name__ == "__main__":
    main()
