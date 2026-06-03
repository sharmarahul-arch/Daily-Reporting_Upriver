"""
Setup health check:  python main.py --doctor

Verifies a fresh install end-to-end and tells the user EXACTLY what's missing,
so a colleague can get from `git clone` to a working run without guesswork.

Written defensively: every check is isolated so a missing dependency or broken
config can't crash the report — that's the whole point of running it.
"""

import json
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = BASE_DIR / "config"
SESSIONS_DIR = BASE_DIR / "sessions"

# ── pretty output ───────────────────────────────────────────────────────────
_GREEN, _YEL, _RED, _RST = "\033[32m", "\033[33m", "\033[31m", "\033[0m"


def _ok(m):   print(f"  {_GREEN}✓{_RST} {m}")
def _warn(m): print(f"  {_YEL}!{_RST} {m}")
def _fail(m): print(f"  {_RED}✗{_RST} {m}")


def _find_service_account() -> Path:
    """Mirror config._resolve_service_account_path, but defensively (no raise)."""
    # 1. explicit path in credentials.json
    cred = CONFIG_DIR / "credentials.json"
    if cred.exists():
        try:
            raw = (json.loads(cred.read_text()).get("sheets_service_account_path") or "").strip()
            if raw:
                p = Path(raw)
                if not p.is_absolute():
                    p = BASE_DIR / p
                if p.exists():
                    return p
        except Exception:
            pass
    # 2. auto-detect a service-account json dropped into config/
    for pattern in ("*service-account*.json", "*service_account*.json",
                    "*sheet*bridge*.json", "*.iam.gserviceaccount.json"):
        hits = sorted(CONFIG_DIR.glob(pattern))
        if hits:
            return hits[0]
    for jf in sorted(CONFIG_DIR.glob("*.json")):
        if jf.name in ("credentials.json", "credentials.example.json", "accounts.json"):
            continue
        try:
            if '"type": "service_account"' in jf.read_text():
                return jf
        except Exception:
            pass
    return None


def run_doctor() -> int:
    """Run all checks. Returns 0 if ready to run, 1 if blocking problems found."""
    print("\n=== Daily Reporting — setup check ===\n")
    problems = 0
    sa_email = None

    # ── 1. Python dependencies ───────────────────────────────────────────────
    print("Dependencies:")
    missing_deps = []
    for mod, label in [("playwright", "playwright"), ("gspread", "gspread"),
                       ("pandas", "pandas"), ("openpyxl", "openpyxl"),
                       ("google.oauth2", "google-auth")]:
        try:
            __import__(mod)
            _ok(label)
        except Exception:
            _fail(f"{label} not installed")
            missing_deps.append(label)
            problems += 1
    if missing_deps:
        _warn("Run: bash setup.sh   (installs all dependencies)")

    # Chromium browser
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            exe = pw.chromium.executable_path
            if exe and Path(exe).exists():
                _ok("Chromium browser installed")
            else:
                _fail("Chromium not installed — run: playwright install chromium")
                problems += 1
    except Exception:
        _warn("Could not verify Chromium (playwright not ready) — run: playwright install chromium")

    # ── 2. credentials.json ──────────────────────────────────────────────────
    print("\nCredentials:")
    cred_path = CONFIG_DIR / "credentials.json"
    creds = {}
    if not cred_path.exists():
        _fail("config/credentials.json missing — copy config/credentials.example.json to config/credentials.json")
        problems += 1
    else:
        try:
            creds = json.loads(cred_path.read_text())
            email = (creds.get("amazon_email") or "").strip()
            pw = (creds.get("amazon_password") or "").strip()
            if email and pw and "example.com" not in email and "your-" not in email:
                _ok(f"Amazon login set ({email})")
            else:
                _fail("amazon_email / amazon_password not filled in (still example values?)")
                problems += 1
        except Exception as e:
            _fail(f"credentials.json is not valid JSON: {e}")
            problems += 1

    # ── 3. Google service account ────────────────────────────────────────────
    print("\nGoogle Sheets access:")
    sa_path = _find_service_account()
    if not sa_path:
        _fail("No Google service-account .json found — drop your key file into the config/ folder")
        problems += 1
    else:
        _ok(f"Service-account key found: config/{sa_path.name}")
        # Try to authenticate
        try:
            from google.oauth2.service_account import Credentials
            import gspread
            sa_info = json.loads(sa_path.read_text())
            sa_email = sa_info.get("client_email")
            scopes = ["https://www.googleapis.com/auth/spreadsheets",
                      "https://www.googleapis.com/auth/drive"]
            creds_obj = Credentials.from_service_account_file(str(sa_path), scopes=scopes)
            gc = gspread.authorize(creds_obj)
            _ok(f"Authenticated to Google as {sa_email}")
            _warn(f"Share each Google Sheet with this email (Editor): {sa_email}")
        except Exception as e:
            _fail(f"Service-account key present but auth failed: {e}")
            problems += 1
            gc = None

    # ── 4. accounts.xlsx + per-sheet reachability ────────────────────────────
    print("\nAccounts:")
    acct_path = CONFIG_DIR / "accounts.xlsx"
    accounts = []
    if not acct_path.exists():
        _fail("config/accounts.xlsx missing — copy config/accounts.example.xlsx to config/accounts.xlsx")
        problems += 1
    else:
        try:
            from src.config import load_accounts
            accounts = load_accounts()
            if accounts:
                _ok(f"{len(accounts)} active account(s) configured")
            else:
                _fail("accounts.xlsx has no active accounts (set Active = Yes)")
                problems += 1
        except Exception as e:
            _fail(f"Could not read accounts.xlsx: {e}")
            problems += 1

    # ── 5. Per-account: sheet access + captured IDs ──────────────────────────
    if accounts:
        gc = None
        try:
            from google.oauth2.service_account import Credentials
            import gspread
            if sa_path:
                creds_obj = Credentials.from_service_account_file(
                    str(sa_path),
                    scopes=["https://www.googleapis.com/auth/spreadsheets",
                            "https://www.googleapis.com/auth/drive"])
                gc = gspread.authorize(creds_obj)
        except Exception:
            gc = None

        print("\nPer-account readiness:")
        for a in accounts:
            name = a["name"]
            has_ids = bool(a.get("sc_merchant_id"))
            sheet_id = a.get("google_sheet_id", "")
            # Sheet reachability
            sheet_msg = ""
            if gc and sheet_id:
                try:
                    gc.open_by_key(sheet_id)
                    sheet_msg = "sheet OK"
                except Exception:
                    sheet_msg = "SHEET NOT ACCESSIBLE (share it with the service account)"
            elif not sheet_id:
                sheet_msg = "no Google Sheet ID"
            id_msg = "IDs captured" if has_ids else "needs --capture-ids"
            line = f"{name:<24} {sheet_msg:<42} {id_msg}"
            if "NOT ACCESSIBLE" in sheet_msg or "no Google Sheet" in sheet_msg:
                _fail(line); problems += 1
            elif not has_ids:
                _warn(line)
            else:
                _ok(line)
        if any(not a.get("sc_merchant_id") for a in accounts):
            _warn("Some accounts lack Seller Central IDs — run: python main.py --capture-ids")

    # ── 6. Sessions (informational) ──────────────────────────────────────────
    print("\nLogin sessions:")
    sess = list(SESSIONS_DIR.glob("*.json")) if SESSIONS_DIR.exists() else []
    if sess:
        _ok(f"{len(sess)} saved session(s) — first run won't need OTP")
    else:
        _warn("No saved sessions — first run will prompt for OTP (normal)")

    # ── Summary ──────────────────────────────────────────────────────────────
    print("\n" + "=" * 50)
    if problems == 0:
        print(f"{_GREEN}All checks passed — you're ready to run:{_RST}")
        print("    python main.py")
        return 0
    print(f"{_RED}{problems} blocking issue(s) found.{_RST} Fix the ✗ items above, then re-run:")
    print("    python main.py --doctor")
    return 1
