"""
Google Sheets integration.
Each report type goes to a dedicated tab in the master spreadsheet.
Rows are appended daily (headers written once on first use).
"""

import logging
import re
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


def upload_dataframe(df: pd.DataFrame, sheet_id: str, report_key: str, account_name: str = None) -> int:
    """
    Write df to the account's own Google Sheet.
    Each account has its own sheet_id — tab names are just the report type
    (e.g. "Sales", "Ad Campaigns") with no account prefix.
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

    # Write headers if they are not already in row 1
    existing = ws.get_all_values()
    expected_headers = df.columns.tolist()
    has_headers = existing and existing[0] == expected_headers
    if not has_headers:
        if existing:
            # Sheet has data but wrong/missing headers — insert header row at top
            ws.insert_row(expected_headers, index=1, value_input_option="USER_ENTERED")
            log.info("Inserted header row into '%s' tab", tab_name)
        else:
            ws.append_row(expected_headers, value_input_option="USER_ENTERED")
            log.info("Wrote header row to '%s' tab", tab_name)

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
