"""
Google Sheets integration.
Each report type goes to a dedicated tab in the master spreadsheet.
Rows are appended daily (headers written once on first use).
When a replace_date is given, existing rows for that Report Date are
deleted first so re-pulls overwrite instead of duplicating.
"""

import logging
import re
from datetime import date, datetime, timedelta
from typing import Optional

import gspread
import pandas as pd
from google.oauth2.service_account import Credentials

from src.config import SHEETS_CREDS_PATH, SHEET_TABS

log = logging.getLogger(__name__)

# Currency symbols to strip from cell values before writing to Sheets
_CURRENCY_RE = re.compile(r"[₹$£€¥₩₦₫₪₡₱₲₵₴₾₺₼₸₽¢฿₿]")


def _strip_currency(df: pd.DataFrame) -> pd.DataFrame:
    """Return a copy of df with all currency symbols removed from string columns."""
    df = df.copy()
    for col in df.columns:
        if df[col].dtype == object:
            df[col] = df[col].astype(str).str.replace(_CURRENCY_RE, "", regex=True).str.strip()
    return df

_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

_client: Optional[gspread.Client] = None


def _get_client() -> gspread.Client:
    global _client
    if _client is None:
        creds = Credentials.from_service_account_file(SHEETS_CREDS_PATH, scopes=_SCOPES)
        _client = gspread.authorize(creds)
    return _client


def _get_or_create_worksheet(spreadsheet: gspread.Spreadsheet, tab_name: str) -> gspread.Worksheet:
    try:
        return spreadsheet.worksheet(tab_name)
    except gspread.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=tab_name, rows=500, cols=50)
        log.info("Created new tab: %s", tab_name)
        return ws


# Google Sheets stores dates as serial numbers counted from this epoch
_SHEETS_EPOCH = datetime(1899, 12, 30)

_DATE_FORMATS = (
    "%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y", "%Y/%m/%d",
    "%d %b %Y", "%b %d, %Y",
)


def _cell_to_date(val) -> Optional[date]:
    """Best-effort conversion of a sheet cell (serial number or text) to a date."""
    if val is None or val == "":
        return None
    try:
        return (_SHEETS_EPOCH + timedelta(days=float(val))).date()
    except (TypeError, ValueError):
        pass
    s = str(val).strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _delete_rows_for_date(ws: gspread.Worksheet, replace_date: date) -> int:
    """
    Delete all data rows whose 'Report Date' cell equals replace_date.
    Returns the number of rows deleted. No-op when the tab has no
    'Report Date' column or no matching rows.
    """
    values = ws.get_values(
        value_render_option=gspread.utils.ValueRenderOption.unformatted
    )
    if not values:
        return 0
    headers = [str(h).strip() for h in values[0]]
    try:
        col = headers.index("Report Date")
    except ValueError:
        return 0

    # 1-based sheet rows of matches (values[i] is sheet row i+1)
    matches = [
        i + 1
        for i, row in enumerate(values[1:], start=1)
        if col < len(row) and _cell_to_date(row[col]) == replace_date
    ]
    if not matches:
        return 0

    # Group into contiguous ranges, delete bottom-up so indices stay valid
    ranges = []
    start = prev = matches[0]
    for r in matches[1:]:
        if r == prev + 1:
            prev = r
        else:
            ranges.append((start, prev))
            start = prev = r
    ranges.append((start, prev))
    for s, e in reversed(ranges):
        ws.delete_rows(s, e)

    log.info("Replaced %d existing rows for %s in '%s' tab",
             len(matches), replace_date.isoformat(), ws.title)
    return len(matches)


def clear_and_upload(df: pd.DataFrame, sheet_id: str, tab_name: str):
    """
    Clear the named tab completely, then write headers + data fresh.
    Creates the tab if it doesn't exist.
    """
    client = _get_client()
    spreadsheet = client.open_by_key(sheet_id)
    ws = _get_or_create_worksheet(spreadsheet, tab_name)
    ws.clear()

    if df is None or df.empty:
        return

    df = _strip_currency(df)
    rows = [df.columns.tolist()] + df.fillna("").astype(str).values.tolist()
    batch_size = 500
    for i in range(0, len(rows), batch_size):
        ws.append_rows(rows[i : i + batch_size], value_input_option="USER_ENTERED")

    log.info("Wrote %d rows to '%s' tab", len(df), tab_name)


def upload_dataframe(df: pd.DataFrame, sheet_id: str, report_key: str, account_name: str = None,
                     replace_date: Optional[date] = None) -> int:
    """
    Write df to the account's own Google Sheet.
    Each account has its own sheet_id — tab names are just the report type
    (e.g. "Sales", "Ad Campaigns") with no account prefix.
    replace_date: delete existing rows for this Report Date before appending,
    so re-pulls (attribution backfill) overwrite instead of duplicating.
    Returns the number of rows uploaded (0 if skipped).
    """
    if df is None or df.empty:
        log.warning("Skipping upload — empty dataframe for %s", report_key)
        return 0

    tab_name = SHEET_TABS[report_key]   # e.g. "Sales", "Ad Campaigns"

    df = _strip_currency(df)

    client = _get_client()
    spreadsheet = client.open_by_key(sheet_id)
    ws = _get_or_create_worksheet(spreadsheet, tab_name)

    if replace_date is not None:
        try:
            _delete_rows_for_date(ws, replace_date)
        except Exception as exc:
            log.warning("Could not delete old rows for %s in '%s': %s — appending anyway",
                        replace_date.isoformat(), tab_name, exc)

    # Write headers if they are not already in row 1.
    # Sheets pads every row with trailing '' to match the widest row, so strip
    # those before comparing — otherwise a column-count change causes a new
    # header row to be inserted on every run, stacking up duplicates.
    existing = ws.get_all_values()
    expected_headers = df.columns.tolist()
    existing_row1 = [c.strip() for c in existing[0]] if existing else []
    while existing_row1 and existing_row1[-1] == "":
        existing_row1.pop()
    row1_is_header = existing_row1 and existing_row1[0] == expected_headers[0]
    has_correct_headers = existing_row1 == expected_headers
    if not has_correct_headers:
        if not existing:
            ws.append_row(expected_headers, value_input_option="USER_ENTERED")
            log.info("Wrote header row to '%s' tab", tab_name)
        elif row1_is_header:
            # Row 1 is already a header but columns changed — update in place
            # instead of inserting (which would stack duplicate header rows).
            ws.update("A1", [expected_headers], value_input_option="USER_ENTERED")
            log.info("Updated header row in '%s' tab", tab_name)
        else:
            # Row 1 is data (no header at all) — insert one at the top
            ws.insert_row(expected_headers, index=1, value_input_option="USER_ENTERED")
            log.info("Inserted header row into '%s' tab", tab_name)

    # Append data rows in batches to avoid API limits
    rows = df.fillna("").astype(str).values.tolist()
    batch_size = 500
    for i in range(0, len(rows), batch_size):
        ws.append_rows(rows[i : i + batch_size], value_input_option="USER_ENTERED")

    log.info("Uploaded %d rows to '%s' tab", len(rows), tab_name)
    return len(rows)


def test_connection(sheet_id: str) -> bool:
    """Quick check that credentials and sheet access work."""
    try:
        client = _get_client()
        sheet = client.open_by_key(sheet_id)
        log.info("Sheet access OK: %s", sheet.title)
        return True
    except Exception as exc:
        log.error("Sheet connection failed: %s", exc)
        return False
