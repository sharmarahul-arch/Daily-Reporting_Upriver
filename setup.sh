#!/bin/bash
set -e

echo "Setting up Amazon Daily Reporting Tool..."

# Create virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install --upgrade pip
pip install -r requirements.txt

# Install Playwright browsers
playwright install chromium

echo ""
echo "================================================================"
echo "  Setup complete! Follow these steps before your first run:"
echo "================================================================"
echo ""
echo "STEP 1 — Fill in your credentials:"
echo "  Edit config/credentials.json and set:"
echo "    • amazon_email          — your Amazon login email"
echo "    • amazon_password       — your Amazon password"
echo "    • sheets_service_account_path — full path to your Google"
echo "                                    service account .json key"
echo ""
echo "STEP 2 — Add your accounts:"
echo "  Edit config/accounts.json — one entry per brand."
echo "  Key fields: name, sc_account_name, ads_account_name,"
echo "              marketplace (IN/US), google_sheet_id"
echo "  See README.md for a full field guide."
echo ""
echo "STEP 3 — Share each Google Sheet with your service account email"
echo "  (give it Editor access)."
echo ""
echo "STEP 4 — Test the Sheets connection:"
echo "  source venv/bin/activate"
echo "  python main.py --test-sheets YOUR_SHEET_ID"
echo ""
echo "STEP 5 — Run:"
echo "  python main.py                           # all accounts"
echo "  python main.py --account 'Brand Name'   # one account"
echo ""
echo "See README.md for full instructions and troubleshooting."
echo ""
