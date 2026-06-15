import logging
import os
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

from src.config import DOWNLOADS_DIR

log = logging.getLogger(__name__)


def yesterday() -> str:
    return (date.today() - timedelta(days=1)).strftime("%Y-%m-%d")


def today_str() -> str:
    return date.today().strftime("%Y-%m-%d")


def parse_date_arg(s: str) -> date:
    """Parse a YYYY-MM-DD string into a date (used by the --date CLI flag)."""
    from datetime import datetime
    return datetime.strptime(s.strip(), "%Y-%m-%d").date()


def resolve_report_date(target_date: Optional[date] = None) -> date:
    """Return the target report date, defaulting to yesterday when unset."""
    return target_date if target_date is not None else (date.today() - timedelta(days=1))


def is_yesterday(d: date) -> bool:
    """True when d is exactly yesterday (lets callers use the fast 'Yesterday' preset)."""
    return d == (date.today() - timedelta(days=1))


def setup_logging():
    from src.config import LOGS_DIR
    LOGS_DIR.mkdir(exist_ok=True)
    log_file = LOGS_DIR / f"{today_str()}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(message)s",
        datefmt="%H:%M:%S",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(log_file),
        ],
    )


async def wait_for_download(page, trigger_fn, timeout: int = 60) -> Optional[Path]:
    """Trigger a download, wait for it, return the saved file path."""
    import uuid
    DOWNLOADS_DIR.mkdir(exist_ok=True)
    async with page.expect_download(timeout=timeout * 1000) as dl_info:
        await trigger_fn()
    download = await dl_info.value
    # Unique prefix: accounts run in parallel and Amazon suggests the same
    # filename (e.g. Campaign_Jun_12_2026.csv) for every account — a shared
    # name would let one account's file overwrite another's mid-parse.
    dest = DOWNLOADS_DIR / f"{uuid.uuid4().hex[:8]}_{download.suggested_filename}"
    await download.save_as(dest)
    log.info("Downloaded: %s", dest.name)
    return dest


def clean_downloads():
    """Remove files older than 7 days from downloads folder."""
    cutoff = time.time() - 7 * 86400
    for f in DOWNLOADS_DIR.glob("*"):
        if f.stat().st_mtime < cutoff:
            f.unlink()
