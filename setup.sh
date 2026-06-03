#!/bin/bash
# One-command setup for the Amazon Daily Reporting Tool.
# Safe to re-run: it never overwrites your real config files.
set -e
cd "$(dirname "$0")"

echo "================================================================"
echo "  Amazon Daily Reporting Tool — setup"
echo "================================================================"

# ── 1. Python virtual environment ──────────────────────────────────
if [ ! -d venv ]; then
    echo "→ Creating virtual environment..."
    python3 -m venv venv
fi
source venv/bin/activate

# ── 2. Dependencies ────────────────────────────────────────────────
echo "→ Installing Python packages..."
pip install --upgrade pip >/dev/null
pip install -r requirements.txt

echo "→ Installing Chromium browser for Playwright..."
playwright install chromium

# ── 3. Copy config templates (only if missing — never overwrite) ───
mkdir -p config sessions downloads logs
if [ ! -f config/credentials.json ]; then
    cp config/credentials.example.json config/credentials.json
    echo "→ Created config/credentials.json (from template)"
fi
if [ ! -f config/accounts.xlsx ]; then
    cp config/accounts.example.xlsx config/accounts.xlsx
    echo "→ Created config/accounts.xlsx (from template)"
fi

echo ""
echo "================================================================"
echo "  Setup complete. Three things to do before your first run:"
echo "================================================================"
echo ""
echo "1) config/credentials.json"
echo "     - amazon_email / amazon_password   (your Seller Central login)"
echo ""
echo "2) Google Sheets access"
echo "     - Put your Google service-account .json key in the config/ folder."
echo "       (The tool auto-detects it — no path editing needed.)"
echo "     - Share each Google Sheet with the service-account email (Editor)."
echo ""
echo "3) config/accounts.xlsx"
echo "     - One row per account. Column tooltips explain each field."
echo "     - Leave the SC Merchant ID / Marketplace ID / Paid ID columns blank."
echo ""
echo "Then capture each account's Seller Central IDs (one-time, ~5 min):"
echo "     source venv/bin/activate"
echo "     python main.py --capture-ids"
echo ""
echo "And run:"
echo "     python main.py                          # all accounts, yesterday"
echo "     python main.py --date 2026-05-30        # a specific past day"
echo "     python main.py --exclude 'Charlotte Home'"
echo ""
echo "See README.md for the full guide."
echo ""
