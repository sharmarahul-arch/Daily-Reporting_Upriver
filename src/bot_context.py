"""
Shared state between the Telegram bot and the running report script.

The bot sets `_send_fn` before each run so the script can post progress
messages. The OTP queue lets the bot pass a code to the waiting script.
"""

import asyncio
from typing import Optional, Callable, Awaitable

# Set by the bot before a run; cleared after
_send_fn: Optional[Callable[[str], Awaitable[None]]] = None

# asyncio.Queue for OTP codes: bot puts code here, _handle_otp reads it.
#
# IMPORTANT: do NOT create the Queue at import time. On Python 3.9 an
# asyncio.Queue binds to the event loop that is current when it is first
# awaited. telegram_bot.py creates a fresh loop inside app.run_polling(),
# so an import-time queue ends up bound to the wrong loop and raises
# "got Future attached to a different loop" when the user replies with the
# code — silently breaking OTP delivery. We create it lazily instead, so it
# binds to whichever loop request_otp/deliver_otp actually run in.
_otp_queue: Optional["asyncio.Queue[str]"] = None
_awaiting_otp: bool = False


def _get_otp_queue() -> "asyncio.Queue[str]":
    global _otp_queue
    if _otp_queue is None:
        _otp_queue = asyncio.Queue()
    return _otp_queue


def set_sender(fn: Optional[Callable[[str], Awaitable[None]]]):
    """Wire up (or remove) the Telegram message sender."""
    global _send_fn
    _send_fn = fn


def is_bot_mode() -> bool:
    """True when the script is being driven by the Telegram bot."""
    return _send_fn is not None


async def send(text: str):
    """Send a message to Telegram. No-op when running manually."""
    if _send_fn:
        try:
            await _send_fn(text)
        except Exception:
            pass


async def request_otp(label: str) -> str:
    """
    Notify the user via Telegram that OTP is needed, then wait up to
    3 minutes for them to reply with the code. Returns "" on timeout.
    """
    global _awaiting_otp
    queue = _get_otp_queue()
    # Drain any stale code left over from a previous (timed-out) attempt so we
    # never enter a fresh, valid code that was queued late.
    while not queue.empty():
        try:
            queue.get_nowait()
        except asyncio.QueueEmpty:
            break
    _awaiting_otp = True
    await send(
        f"⚠️ *OTP / 2FA needed* for `{label}`\n\n"
        "Check your Amazon authenticator app or email and "
        "*reply here with the 6-digit code*:"
    )
    try:
        code = await asyncio.wait_for(queue.get(), timeout=180)
        return code.strip()
    except asyncio.TimeoutError:
        await send(f"⏱ OTP timed out for `{label}` — skipping this login.")
        return ""
    finally:
        _awaiting_otp = False


def is_awaiting_otp() -> bool:
    return _awaiting_otp


async def deliver_otp(code: str):
    """Called by the bot when the user replies with a code."""
    await _get_otp_queue().put(code)
