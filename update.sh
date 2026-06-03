#!/bin/bash
# Pull the latest version of the tool and apply any new dependencies.
# Your personal files (credentials, accounts, service-account key, sessions)
# are git-ignored and are NEVER touched by this.
set -e
cd "$(dirname "$0")"

echo "→ Fetching latest code from GitHub..."
git pull --ff-only origin main || {
    echo ""
    echo "⚠️  Could not fast-forward. You may have local edits to tracked files."
    echo "    Your config/ files are safe (git-ignored). If you didn't change any"
    echo "    code on purpose, run:  git stash && bash update.sh"
    exit 1
}

echo "→ Updating dependencies (in case the update added any)..."
source venv/bin/activate
pip install -q -r requirements.txt
playwright install chromium >/dev/null 2>&1 || true

echo ""
echo "✓ Update complete. Your accounts and credentials are unchanged."
echo "  Running a quick health check..."
echo ""
python main.py --doctor || true
