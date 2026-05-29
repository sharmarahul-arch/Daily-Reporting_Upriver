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
    DOWNLOADS_DIR.mkdir(exist_ok=True)
    async with page.expect_download(timeout=timeout * 1000) as dl_info:
        await trigger_fn()
    download = await dl_info.value
    dest = DOWNLOADS_DIR / download.suggested_filename
    await download.save_as(dest)
    log.info("Downloaded: %s", dest.name)
    return dest


def clean_downloads():
    """Remove files older than 7 days from downloads folder."""
    cutoff = time.time() - 7 * 86400
    for f in DOWNLOADS_DIR.glob("*"):
        if f.stat().st_mtime < cutoff:
            f.unlink()
