# Amazon Daily Reporting Tool

Automatically pulls **yesterday's** Seller Central + Amazon Ads data for every account you manage, and uploads it to that account's Google Sheet. Built for agencies running multiple seller accounts under one Amazon login.

**What it collects per account:**

| Tab written to | Source | One row per |
|---|---|---|
| `Sales` | Seller Central → Business Reports → Detail Page Sales and Traffic by Child Item | ASIN |
| `Ad Campaigns` | Amazon Ads → Campaign Manager | campaign |
| `Advertised Products` | Amazon Ads → Products tab | product |

**How it works (short version):** Playwright opens Amazon in a real Chromium browser, logs into Seller Central + Ads, switches to each account in turn, downloads the reports, parses them, and writes to Google Sheets via the gspread API. The Amazon switcher UI is unreliable across multi-account orgs, so the tool uses **direct-URL navigation** with pre-captured merchant IDs — fast and bulletproof. A **contamination guard** in `main.py` aborts uploads if a switch silently lands on the wrong account, so wrong-data-correct-label bugs can't happen.

---

## Quick start (first-time setup)

### 1. Clone and install

```bash
git clone <your-fork-url> "Daily Reporting"
cd "Daily Reporting"
bash setup.sh
```

`setup.sh` creates a Python venv, installs all packages from `requirements.txt`, and installs Playwright's Chromium browser.

### 2. Set up Google Sheets access (one-time per Google project)

1. In [Google Cloud Console](https://console.cloud.google.com/) create a project (or reuse one).
2. Enable the **Google Sheets API** and **Google Drive API**.
3. **IAM & Admin → Service Accounts** → create a service account → create a **JSON key** → download it.
4. Save the JSON file somewhere stable (e.g. `~/.config/gcp/sheet-bridge.json`).
5. For every Google Sheet the tool will write to, click **Share** and give the service account's email (`xxx@your-project.iam.gserviceaccount.com`) **Editor** access.

### 3. Configure credentials

```bash
cp config/credentials.example.json config/credentials.json
```

Edit `config/credentials.json`:

```json
{
  "amazon_email": "your-seller-central-email@example.com",
  "amazon_password": "your-amazon-password",
  "sheets_service_account_path": "/Users/you/.config/gcp/sheet-bridge.json",
  "telegram_bot_token": "",
  "telegram_chat_id": 0
}
```

- `amazon_email` / `amazon_password` — the Amazon login that has access to every Seller Central account you want to report on.
- `sheets_service_account_path` — absolute path to the JSON key from step 2.
- Telegram fields are optional; leave empty to skip Telegram alerts.

> 🔒 `config/credentials.json` is gitignored. Never commit it.

### 4. Configure accounts

```bash
cp config/accounts.example.xlsx config/accounts.xlsx
```

Open `config/accounts.xlsx` in Excel / Numbers / LibreOffice and fill one row per account. The example file has column-header tooltips explaining each field:

| Column | What to put |
|---|---|
| `Account Name` | Friendly label (used in logs, Telegram, sheet rows) |
| `SC Account Name` | **Exact** text shown in Seller Central's account switcher (case-sensitive) |
| `SC Parent` | If your account is a child of a parent merchant group, put the parent's name/ID; otherwise leave blank |
| `Ads Account Name` | **Exact** text in Amazon Ads' entity switcher (often a brand name, may differ from SC) |
| `Marketplace` | Two-letter code: `IN`, `US`, `UK`, `DE`, `FR`, `JP`, `AU`, `AE`, `MX`, `SG`, `CA` |
| `Google Sheet ID` | The long ID from the sheet URL (`docs.google.com/spreadsheets/d/`**`THIS_PART`**`/edit`) |
| `Amazon Email` / `Amazon Password` | Optional per-account override |
| `Active` | `Yes` or `No` |
| `SC Merchant ID` / `SC Marketplace ID` / `SC Paid ID` / `SC DC` | **Leave blank** — populated automatically in step 5 |

> 🔒 `config/accounts.xlsx` is also gitignored — it contains your sheet IDs and account names.

### 5. Capture Seller Central IDs (one-time, ~5 min)

Amazon's account switcher UI is unreliable for many multi-account organisations — clicks silently no-op. The tool sidesteps this by navigating directly to a URL containing the account's merchant ID. You need to capture these IDs once per account.

```bash
source venv/bin/activate
python main.py --capture-ids
```

What happens:
1. A Chromium window opens. If asked, complete the Amazon OTP/2FA.
2. For each account, the terminal prints `Press ENTER to open the switcher →`.
3. Press Enter. The browser loads the SC account switcher.
4. **In the browser:** click the target account → pick the marketplace (e.g., India) → click the green **Select account** button.
5. The terminal automatically captures the IDs from the URL and prints `✓ Captured`, then moves to the next account.
6. After the last account, IDs are saved into `config/accounts.xlsx`.

Re-run any time to add new accounts — already-captured accounts are skipped. Use `--recapture-ids` to force a re-capture (e.g. after rotating accounts).

### 6. First real run

```bash
python main.py                           # all active accounts
python main.py --account "HEM Agarbatti" # one account only
python main.py --sc-only                 # Sales reports only
python main.py --ads-only                # Ads reports only
```

First run will trigger Amazon OTP/2FA (once per region — IN, US). Complete it in the browser; the session is saved to `sessions/` and reused on every subsequent run.

### 7. Schedule daily runs

Two options, depending on your OS:

**macOS — launchd (recommended).** Survives reboots, runs even when you're not logged in. Example plist at `~/Library/LaunchAgents/com.yourname.daily-reporting.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
    <key>Label</key><string>com.yourname.daily-reporting</string>
    <key>ProgramArguments</key>
    <array>
        <string>/absolute/path/to/Daily Reporting/venv/bin/python</string>
        <string>/absolute/path/to/Daily Reporting/main.py</string>
    </array>
    <key>WorkingDirectory</key><string>/absolute/path/to/Daily Reporting</string>
    <key>StartCalendarInterval</key><dict>
        <key>Hour</key><integer>7</integer>
        <key>Minute</key><integer>0</integer>
    </dict>
    <key>StandardOutPath</key><string>/absolute/path/to/Daily Reporting/logs/scheduled.out</string>
    <key>StandardErrorPath</key><string>/absolute/path/to/Daily Reporting/logs/scheduled.err</string>
</dict></plist>
```

Load it:

```bash
launchctl load -w ~/Library/LaunchAgents/com.yourname.daily-reporting.plist
```

To run a specific account at a specific time (e.g. a US account at 13:30 IST), use a separate plist with `--account "Charlotte Home"` in `ProgramArguments` and a different `StartCalendarInterval`.

**Cross-platform — built-in `scheduler.py`.** Keeps a Python process alive that fires on a cron schedule:

```bash
python scheduler.py --time 07:00
```

Use `nohup` / `screen` / `tmux` to keep it running.

---

## CLI reference

```bash
python main.py                           # run all active accounts (SC + Ads)
python main.py --account "Name"          # run one account only
python main.py --sc-only                 # Seller Central reports only
python main.py --ads-only                # Ads reports only
python main.py --clear-sessions          # forget saved logins (forces fresh OTP)
python main.py --test-sheets SHEET_ID    # verify Google Sheets connection
python main.py --capture-ids             # interactive: capture SC IDs for accounts that don't have them
python main.py --recapture-ids           # force re-capture for ALL accounts
```

---

## Project structure

```
Daily Reporting/
├── config/
│   ├── accounts.xlsx              ← YOUR account list (gitignored)
│   ├── accounts.example.xlsx      ← template, committed
│   ├── credentials.json           ← YOUR Amazon + Google creds (gitignored)
│   └── credentials.example.json   ← template, committed
├── src/
│   ├── config.py                  — loads accounts.xlsx + credentials.json
│   ├── auth.py                    — Amazon login, SC + Ads account switching (with fast-path + contamination guard)
│   ├── seller_reports.py          — Seller Central Business Reports download
│   ├── ads_reports.py             — Amazon Ads campaign + product report download (handles pagination)
│   ├── sheets.py                  — Google Sheets upload via gspread
│   ├── sc_id_discovery.py         — interactive --capture-ids flow
│   ├── bot_context.py             — Telegram alert helper (optional)
│   └── utils.py                   — logging, date helpers
├── logs/                          — daily run logs (gitignored)
├── downloads/                     — temp CSVs + debug screenshots (gitignored, auto-cleaned)
├── sessions/                      — saved Amazon login cookies (gitignored)
├── main.py                        — CLI entry point
├── scheduler.py                   — cron-style daily runner
├── telegram_bot.py                — optional Telegram bot for run-trigger / status
├── requirements.txt
└── setup.sh                       — first-time install script
```

---

## How the bulletproofing works

A previous version of this tool silently uploaded HEM's data into Acuro's / Cambridge's / Ayantara's / Shri Vinayak's / SWASHAA's sheets because Amazon's account switcher click was a no-op for multi-account orgs. Two changes prevent that now:

1. **Fast-path direct URL navigation.** When an account's IDs are cached (`SC Merchant ID` etc. in `accounts.xlsx`), `switch_sc_account` skips the buggy UI entirely and navigates to `…/gp/homepage.html?mons_sel_dir_mcid=…&mons_sel_mkid=…&mons_sel_dir_paid=…`. Amazon honours this URL and activates the account directly. Takes ~6s vs ~25s for the UI flow.

2. **Contamination guard.** `main.py` tracks the merchant ID returned by every switch in a `seen_ids` dict. If the same ID is returned for two different accounts in the same run, the second one's upload is aborted with `❌ contamination guard` in the Telegram summary. The same guard exists for the Ads `entityId`.

The UI flow stays as a fallback for accounts without cached IDs, so the tool degrades gracefully.

---

## Common issues

| Problem | Fix |
|---|---|
| OTP prompt on every run | Complete it once in the headed browser — session saves to `sessions/`. If it keeps asking, run `python main.py --clear-sessions` and retry. |
| "Account switch failed" / contamination guard fires | The account doesn't have IDs cached. Run `python main.py --capture-ids` and select that account. |
| "Account not found" in SC switcher | Check `SC Account Name` exactly matches the text in Amazon's switcher (case + punctuation). |
| "Entity not found" in Ads | Same — check `Ads Account Name` matches exactly. The Ads switcher entity is often a brand name, not the legal entity. |
| Wrong date in downloaded report | The tool always pulls *yesterday's* data. Run after midnight in your local timezone. |
| Google Sheets `PERMISSION_DENIED` | Share the sheet with the service account email (Editor access). |
| `TargetClosedError` mid-run | Don't close the Chromium window while a run is in progress. |
| Chrome already open | Playwright launches its own Chromium; close personal Chrome only if you see profile-lock errors. |

---

## Contributing / sharing

`.gitignore` excludes everything user-specific: `accounts.xlsx`, `credentials.json`, `sessions/`, `downloads/`, `logs/`, the Playwright venv, and any `*service-account*.json`. The `*.example` files in `config/` are templates anyone can copy.

If you're forking for your own org, the only files you'll touch are `config/accounts.xlsx` and `config/credentials.json`. Everything else (code, scheduler, README) can be pulled fresh from upstream.
