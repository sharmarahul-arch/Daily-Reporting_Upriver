#!/usr/bin/env python3
"""
Telegram bot for the Amazon Daily Reporting Tool.

Commands:
  /run              — Run reports for all 8 accounts
  /run AccountName  — Run for one specific account
  /status           — Check if a run is currently in progress
  /sessions         — Show which login sessions are cached
  /help             — List all commands

OTP handling:
  When a login requires OTP, the bot sends you a message.
  Simply reply with the 6-digit code — it enters it automatically.

Start the bot:
  python3 telegram_bot.py
"""

import asyncio
import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ── Project path ───────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent
sys.path.insert(0, str(BASE_DIR))

import src.bot_context as bot_ctx
from src.utils import setup_logging

log = logging.getLogger(__name__)


# ── Load credentials ───────────────────────────────────────────────────────────
def _load_config() -> tuple[str, str]:
    path = BASE_DIR / "config" / "credentials.json"
    if not path.exists():
        sys.exit("❌ config/credentials.json not found")
    creds = json.loads(path.read_text())
    token   = creds.get("telegram_bot_token", "")
    chat_id = str(creds.get("telegram_chat_id", ""))
    if not token or not chat_id or chat_id == "0":
        sys.exit("❌ Set telegram_bot_token and telegram_chat_id in config/credentials.json")
    return token, chat_id


BOT_TOKEN, ALLOWED_CHAT_ID = _load_config()


# ── Auth guard ─────────────────────────────────────────────────────────────────
def _allowed(update: Update) -> bool:
    return str(update.effective_chat.id) == ALLOWED_CHAT_ID


# ── Run state ──────────────────────────────────────────────────────────────────
_run_task: Optional[asyncio.Task] = None
_run_start: Optional[datetime]    = None


# ── /help ─────────────────────────────────────────────────────────────────────
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _allowed(update):
        return
    await update.message.reply_text(
        "📊 *Amazon Daily Reports Bot*\n\n"
        "*/run* — All 3 reports for all accounts\n"
        "*/run* `Account Name` — All 3 reports for one account\n"
        "*/run sc* — Sales report only (all accounts)\n"
        "*/run ads* — Ads reports only (all accounts)\n"
        "*/run* `Account Name` `sc` — Sales only for one account\n"
        "*/run* `Account Name` `ads` — Ads only for one account\n"
        "*/status* — Is a run in progress?\n"
        "*/sessions* — Show cached login sessions\n"
        "*/help* — This message\n\n"
        "_When OTP is needed, just reply with the 6-digit code._",
        parse_mode="Markdown",
    )


# ── /status ───────────────────────────────────────────────────────────────────
async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _allowed(update):
        return
    if _run_task and not _run_task.done():
        elapsed = ""
        if _run_start:
            secs = int((datetime.now() - _run_start).total_seconds())
            elapsed = f" ({secs // 60}m {secs % 60}s elapsed)"
        if bot_ctx.is_awaiting_otp():
            await update.message.reply_text(
                f"⏳ Run in progress{elapsed} — *waiting for your OTP code.*\n"
                "Reply with the 6-digit code to continue.",
                parse_mode="Markdown",
            )
        else:
            await update.message.reply_text(f"⏳ Run in progress{elapsed}...")
    else:
        await update.message.reply_text("✅ Idle. Send /run to start a report run.")


# ── /sessions ─────────────────────────────────────────────────────────────────
async def cmd_sessions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _allowed(update):
        return
    sessions_dir = BASE_DIR / "sessions"
    if not sessions_dir.exists() or not list(sessions_dir.glob("*.json")):
        await update.message.reply_text(
            "⚠️ No cached sessions found.\n"
            "The next run will require full login + OTP for each portal."
        )
        return

    _LABELS = {
        "sc":     "Seller Central (India)",
        "sc_us":  "Seller Central (US)",
        "ads":    "Amazon Ads (India)",
        "ads_us": "Amazon Ads (US)",
    }
    lines = []
    for f in sorted(sessions_dir.glob("*.json")):
        label = _LABELS.get(f.stem, f.stem)
        size  = f.stat().st_size
        mtime = datetime.fromtimestamp(f.stat().st_mtime).strftime("%d %b %H:%M")
        lines.append(f"✅ {label}\n   _{size} bytes · saved {mtime}_")

    await update.message.reply_text(
        "🔑 *Cached sessions:*\n\n" + "\n\n".join(lines),
        parse_mode="Markdown",
    )


# ── Background run ─────────────────────────────────────────────────────────────
async def _do_run(bot, account_filter: Optional[str], sc_only: bool = False, ads_only: bool = False):
    """Run the report script and stream results to Telegram."""
    from main import run as run_reports

    try:
        await run_reports(
            filter_account=account_filter,
            headless=True,
            sc_only=sc_only,
            ads_only=ads_only,
        )
        elapsed = ""
        if _run_start:
            secs = int((datetime.now() - _run_start).total_seconds())
            elapsed = f" in {secs // 60}m {secs % 60}s"
        await bot.send_message(
            chat_id=ALLOWED_CHAT_ID,
            text=f"🏁 *All done{elapsed}!* Google Sheets are up to date.",
            parse_mode="Markdown",
        )
    except Exception as exc:
        log.exception("Run failed: %s", exc)
        await bot.send_message(
            chat_id=ALLOWED_CHAT_ID,
            text=f"❌ *Run failed:*\n`{exc}`",
            parse_mode="Markdown",
        )
    finally:
        bot_ctx.set_sender(None)


# ── /run ──────────────────────────────────────────────────────────────────────
async def cmd_run(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global _run_task, _run_start
    if not _allowed(update):
        return

    if _run_task and not _run_task.done():
        await update.message.reply_text(
            "⏳ A run is already in progress. Send /status to check."
        )
        return

    # Parse args:
    #   /run                        → all reports, all accounts
    #   /run AccountName            → all reports, one account
    #   /run sc                     → Sales only, all accounts
    #   /run ads                    → Ads (campaigns+products) only, all accounts
    #   /run AccountName sc         → Sales only, one account
    #   /run AccountName ads        → Ads only, one account
    args = list(context.args) if context.args else []
    sc_only  = False
    ads_only = False

    # Check if last arg is a mode flag
    if args and args[-1].lower() == "sc":
        sc_only = True
        args = args[:-1]
    elif args and args[-1].lower() == "ads":
        ads_only = True
        args = args[:-1]

    account_filter = " ".join(args).strip() or None

    # Build a human-readable label for the confirmation message
    report_label = "Sales only" if sc_only else "Ads only (campaigns + products)" if ads_only else "all 3 reports (Sales + Campaigns + Products)"
    acct_label   = f"*{account_filter}*" if account_filter else "all accounts"

    await update.message.reply_text(
        f"🚀 Running *{report_label}* for {acct_label}...\n"
        "I'll send a message for each account as it completes.",
        parse_mode="Markdown",
    )

    # Wire up Telegram sender so the script can post progress
    async def send_to_chat(text: str):
        try:
            await context.bot.send_message(
                chat_id=ALLOWED_CHAT_ID,
                text=text,
                parse_mode="Markdown",
            )
        except Exception:
            pass

    bot_ctx.set_sender(send_to_chat)
    _run_start = datetime.now()

    # Run in background so the bot stays responsive for OTP replies
    _run_task = asyncio.create_task(_do_run(context.bot, account_filter, sc_only=sc_only, ads_only=ads_only))


# ── OTP / plain message handler ────────────────────────────────────────────────
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _allowed(update):
        return

    text = (update.message.text or "").strip()

    if bot_ctx.is_awaiting_otp():
        # Validate: OTP should be 6–8 digits
        if text.isdigit() and 4 <= len(text) <= 8:
            await update.message.reply_text(
                f"🔑 Got it — entering OTP `{text}`...",
                parse_mode="Markdown",
            )
            await bot_ctx.deliver_otp(text)
        else:
            await update.message.reply_text(
                "⚠️ That doesn't look like an OTP code.\n"
                "Please reply with the 6-digit number from your authenticator."
            )
    else:
        await update.message.reply_text(
            "Send /run to start a report run, or /help for all commands."
        )


# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    setup_logging()
    log.info("Telegram bot starting (allowed chat: %s)", ALLOWED_CHAT_ID)

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start",   cmd_help))
    app.add_handler(CommandHandler("help",    cmd_help))
    app.add_handler(CommandHandler("run",     cmd_run))
    app.add_handler(CommandHandler("status",  cmd_status))
    app.add_handler(CommandHandler("sessions", cmd_sessions))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("✅ Bot is running. Send /run in Telegram to start a report.")
    print(f"   Chat ID: {ALLOWED_CHAT_ID}")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
