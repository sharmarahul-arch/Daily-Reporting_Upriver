# Colleague Setup Guide

Get the Amazon Daily Reporting Tool running on your own machine, with your own
accounts and your own Google service account. Takes ~20–30 minutes the first
time (most of it is the one-time Google setup).

If you use **Claude Code**, you can ask it to run any of the terminal steps for
you (e.g. *"clone this repo and run setup"*, *"run the doctor check"*).

---

## What you need before you start

- A **Mac or Linux** machine with **Python 3.9+** and **git** installed
- Your **Amazon Seller Central** email + password
- A **Google account** (to create your own service account — steps below)
- The **Google Sheet IDs** for the sheets you want reports written to
  (one sheet per account)

---

## Step 1 — Get the code

```bash
git clone https://github.com/sharmarahul-arch/Daily-Reporting_Upriver.git
cd Daily-Reporting_Upriver
```

To get future updates later, just run `git pull` in this folder.

---

## Step 2 — Install everything

```bash
bash setup.sh
```

This creates the Python environment, installs all dependencies + the Chromium
browser, and copies the config templates. Safe to re-run anytime.

---

## Step 3 — Create your own Google service account (one-time)

This is the account the tool uses to write into your Google Sheets. **Each
person makes their own** so access stays isolated.

1. Go to **https://console.cloud.google.com/** and sign in.
2. **Create a project:** top bar → project dropdown → **New Project** →
   name it (e.g. `daily-reporting`) → **Create**. Select it.
3. **Enable the two APIs the tool needs:**
   - Search **"Google Sheets API"** → **Enable**
   - Search **"Google Drive API"** → **Enable**
4. **Create the service account:**
   **IAM & Admin → Service Accounts → + Create service account**
   - Name: `sheet-writer` (anything) → **Create and continue**
   - Skip the optional roles → **Done**
5. **Create a key file:**
   - Click your new service account → **Keys** tab → **Add key → Create new key**
   - Choose **JSON** → **Create** → a `.json` file downloads
6. **Copy the service-account email** — it looks like
   `sheet-writer@your-project.iam.gserviceaccount.com`. You'll need it in Step 6.

---

## Step 4 — Put your key into the project

Move the `.json` file you just downloaded into the **`config/`** folder of the
project. That's it — the tool auto-detects it (any filename works, no path to
edit).

```bash
# example
mv ~/Downloads/your-project-abc123.json config/
```

> Your key never gets committed to GitHub — `config/` is git-ignored.

---

## Step 5 — Fill in your details

**`config/credentials.json`** — open it and set:

```json
{
  "amazon_email": "your-seller-central-email@example.com",
  "amazon_password": "your-amazon-password"
}
```

(Leave `sheets_service_account_path` empty — the file in `config/` is found
automatically.)

**`config/accounts.xlsx`** — open in Excel / Numbers / LibreOffice. One row per
account. Column tooltips explain each field. Fill in:

| Column | What to put |
|---|---|
| Account Name | A friendly label |
| SC Account Name | The **exact** name shown in Seller Central's account switcher |
| Ads Account Name | The **exact** name shown in Amazon Ads' account switcher |
| Marketplace | `IN`, `US`, `UK`, `DE`, … |
| Google Sheet ID | The long ID from the sheet URL |
| Active | `Yes` |

Leave the **SC Merchant ID / Marketplace ID / Paid ID** columns blank — Step 7
fills them.

---

## Step 6 — Share your sheets with your service account

For **each** Google Sheet listed in your accounts file: open it → **Share** →
paste your service-account email (from Step 3.6) → give it **Editor** → send.

Without this, the tool can't write to the sheet.

---

## Step 7 — Check your setup

```bash
source venv/bin/activate
python main.py --doctor
```

This verifies everything and lists exactly what's still missing (a green ✓ for
each item means you're good). Fix any ✗ items and re-run it.

---

## Step 8 — Capture your Seller Central IDs (one-time, ~5 min)

```bash
python main.py --capture-ids
```

A browser opens. For each account, click the account → pick its marketplace →
**Select account**. The tool captures the IDs automatically. If Amazon asks for
an **OTP code**, type it in the browser.

---

## Step 9 — Run

```bash
python main.py                          # all accounts, yesterday's data
python main.py --date 2026-05-30        # a specific past day (e.g. a missed Sunday)
python main.py --account "Brand Name"   # just one account
python main.py --exclude "Charlotte Home"  # everything except these
```

The **first run** may ask for an Amazon OTP once — type it in the browser. After
that the login is saved and future runs are silent.

---

## Troubleshooting

| Problem | Fix |
|---|---|
| `--doctor` says credentials missing | Copy `config/credentials.example.json` to `config/credentials.json` and fill it in |
| `--doctor` says no service-account key | Make sure your `.json` key from Step 3 is in the `config/` folder |
| "SHEET NOT ACCESSIBLE" | Share that sheet with your service-account email (Step 6) |
| "needs --capture-ids" | Run `python main.py --capture-ids` |
| Asked for OTP every run | Complete it once; if it keeps asking, run `python main.py --clear-sessions` and retry |

Full details and command reference are in **README.md**.
