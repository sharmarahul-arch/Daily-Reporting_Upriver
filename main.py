"""
Amazon Daily Reporting Tool
===========================
One login → iterate 8 seller accounts → download SC + Ads reports → upload to Google Sheets.

Usage:
    python main.py                        # Run all active accounts
    python main.py --account "Ayantara"   # Run one account by name
    python main.py --clear-sessions       # Force re-login next run
    python main.py --test-sheets SHEET_ID # Quick Sheets connection test
"""

import argparse
import asyncio
import logging
import sys
from typing import Optional

from playwright.async_api import async_playwright

from src.config import (
    load_accounts, DOWNLOADS_DIR, SESSIONS_DIR, AMAZON_EMAIL, AMAZON_PASSWORD,
    ADS_CAMPAIGN_MANAGER, ADS_CAMPAIGN_MANAGER_US,
    SC_BASE_URL, SC_BASE_URL_US,
)
from src.utils import setup_logging, yesterday, clean_downloads
from src.auth import (
    restore_session, save_session, clear_session,
    login_seller_central, login_ads,
    switch_sc_account, switch_ads_account,
)
from src.seller_reports import download_sales_report
from src.ads_reports import download_campaign_report, download_advertised_products_report
from src.sheets import upload_dataframe, test_connection
import src.bot_context as bot_ctx

log = logging.getLogger(__name__)


async def run(filter_account: Optional[str] = None, headless: bool = False, sc_only: bool = False, ads_only: bool = False):
    accounts = load_accounts()
    if filter_account:
        accounts = [a for a in accounts if a["name"].lower() == filter_account.lower()]
        if not accounts:
            log.error("Account '%s' not found in config", filter_account)
            await bot_ctx.send(f"❌ Account `{filter_account}` not found in config.")
            return

    email    = AMAZON_EMAIL
    password = AMAZON_PASSWORD

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=headless,
            downloads_path=str(DOWNLOADS_DIR),
            args=["--no-sandbox"],
        )

        # ── India SC browser context (sellercentral.amazon.in) ───────────────
        # Skipped entirely when ads_only=True to avoid touching SC sessions
        sc_context = sc_page = None
        sc_logged_in = False

        if not ads_only:
            sc_context = await browser.new_context(accept_downloads=True, viewport={"width": 1920, "height": 1080})
            sc_page    = await sc_context.new_page()

            sc_logged_in = await restore_session(sc_context, "sc")
            if sc_logged_in:
                try:
                    await sc_page.goto(f"{SC_BASE_URL}/gp/homepage.html", wait_until="domcontentloaded", timeout=15000)
                    # Only treat a hard sign-in redirect as failure.
                    # "account-switcher/switcher" means we ARE logged in — switch_sc_account handles it.
                    if "ap/signin" in sc_page.url:
                        sc_logged_in = False
                except Exception:
                    sc_logged_in = False

            if not sc_logged_in:
                clear_session("sc")
                sc_logged_in = await login_seller_central(sc_page, email, password, base_url=SC_BASE_URL)
                if sc_logged_in:
                    await save_session(sc_context, "sc")
                else:
                    await bot_ctx.send(
                        "⚠️ *SC India login failed* — Sales reports will be skipped.\n"
                        "Run `python3 main.py --sc-only` (headed) to refresh the session."
                    )

        # ── US SC browser context (sellercentral.amazon.com) ─────────────────
        sc_us_context = sc_us_page = None
        sc_us_logged_in = False

        if not ads_only:
            sc_us_context = await browser.new_context(accept_downloads=True, viewport={"width": 1920, "height": 1080})
            sc_us_page    = await sc_us_context.new_page()

            sc_us_logged_in = await restore_session(sc_us_context, "sc_us")
            if sc_us_logged_in:
                try:
                    await sc_us_page.goto(f"{SC_BASE_URL_US}/gp/homepage.html", wait_until="domcontentloaded", timeout=15000)
                    if "ap/signin" in sc_us_page.url:
                        sc_us_logged_in = False
                except Exception:
                    sc_us_logged_in = False

            if not sc_us_logged_in:
                clear_session("sc_us")
                sc_us_logged_in = await login_seller_central(sc_us_page, email, password, base_url=SC_BASE_URL_US)
                if sc_us_logged_in:
                    await save_session(sc_us_context, "sc_us")
                else:
                    await bot_ctx.send(
                        "⚠️ *SC US login failed* — US Sales reports will be skipped.\n"
                        "Run `python3 main.py --sc-only` (headed) to refresh the session."
                    )

        # ── India Ads browser context (advertising.amazon.in) ────────────────
        # Skipped entirely when sc_only=True to avoid touching Ads sessions
        ads_context = ads_page = None
        ads_logged_in = False

        if not sc_only:
            ads_context = await browser.new_context(accept_downloads=True, viewport={"width": 1920, "height": 1080})
            ads_page    = await ads_context.new_page()

            ads_logged_in = await restore_session(ads_context, "ads")
            if ads_logged_in:
                try:
                    await ads_page.goto(ADS_CAMPAIGN_MANAGER, wait_until="domcontentloaded", timeout=15000)
                    if "ap/signin" in ads_page.url:
                        ads_logged_in = False
                except Exception:
                    ads_logged_in = False

            if not ads_logged_in:
                clear_session("ads")
                ads_logged_in = await login_ads(ads_page, email, password, start_url=ADS_CAMPAIGN_MANAGER)
                if ads_logged_in:
                    await save_session(ads_context, "ads")

        # ── US Ads browser context (advertising.amazon.com) ──────────────────
        ads_us_context = ads_us_page = None
        ads_us_logged_in = False

        if not sc_only:
            ads_us_context = await browser.new_context(accept_downloads=True, viewport={"width": 1920, "height": 1080})
            ads_us_page    = await ads_us_context.new_page()

            ads_us_logged_in = await restore_session(ads_us_context, "ads_us")
            if ads_us_logged_in:
                try:
                    await ads_us_page.goto(ADS_CAMPAIGN_MANAGER_US, wait_until="domcontentloaded", timeout=15000)
                    if "ap/signin" in ads_us_page.url:
                        ads_us_logged_in = False
                except Exception:
                    ads_us_logged_in = False

            if not ads_us_logged_in:
                clear_session("ads_us")
                ads_us_logged_in = await login_ads(ads_us_page, email, password, start_url=ADS_CAMPAIGN_MANAGER_US)
                if ads_us_logged_in:
                    await save_session(ads_us_context, "ads_us")

        # Track merchant/entity IDs across accounts to catch silent switch no-ops.
        # If switching to a new account returns an ID we've already seen for a
        # different brand, the switch silently failed and uploading would
        # cross-contaminate (e.g. Acuro labelled rows with HEM campaign data).
        sc_seen_ids: dict[str, str] = {}    # mcid -> brand_name that owns it
        ads_seen_ids: dict[str, str] = {}   # entityId -> brand_name that owns it

        # ── Process each account ──────────────────────────────────────────────
        for account in accounts:
            name        = account["name"]
            sc_account  = account.get("sc_account_name", name)
            sc_parent   = account.get("sc_parent")
            ads_account = account.get("ads_account_name", name)
            sheet_id    = account["google_sheet_id"]
            marketplace = account.get("marketplace", "IN").upper()
            # Per-account credentials (fall back to global if not set in xlsx)
            acct_email    = account.get("email") or email
            acct_password = account.get("password") or password

            # Route US accounts to their own SC/Ads contexts; all others use India
            is_us = marketplace == "US"
            active_sc_page        = sc_us_page        if is_us else sc_page
            active_sc_logged_in   = sc_us_logged_in   if is_us else sc_logged_in
            active_ads_page       = ads_us_page        if is_us else ads_page
            active_ads_logged_in  = ads_us_logged_in  if is_us else ads_logged_in
            active_cm_url         = ADS_CAMPAIGN_MANAGER_US if is_us else ADS_CAMPAIGN_MANAGER

            log.info("=" * 60)
            log.info("Processing: %s  (date: %s)", name, yesterday())
            log.info("=" * 60)
            await bot_ctx.send(f"📍 *{name}* — starting...")

            account_summary = []

            # ── Seller Central reports ────────────────────────────────────────
            if not ads_only and active_sc_logged_in:
                try:
                    mcid = await switch_sc_account(
                        active_sc_page, sc_account, sc_parent, name,
                        marketplace=marketplace,
                        sc_merchant_id=account.get("sc_merchant_id"),
                        sc_marketplace_id=account.get("sc_marketplace_id"),
                        sc_paid_id=account.get("sc_paid_id"),
                        sc_dc=account.get("sc_dc"),
                    )
                    if mcid:
                        prev_owner = sc_seen_ids.get(mcid)
                        if prev_owner and prev_owner != name:
                            log.error(
                                "[%s] CONTAMINATION GUARD: SC mcid %s already belongs to '%s' — "
                                "aborting Sales download for %s", name, mcid, prev_owner, name
                            )
                            account_summary.append(f"Sales: ❌ contamination guard (mcid matches {prev_owner})")
                        else:
                            sc_seen_ids[mcid] = name
                            sales_df = await download_sales_report(active_sc_page, name, marketplace=marketplace)
                            rows = upload_dataframe(sales_df, sheet_id, "sales")
                            if rows:
                                account_summary.append(f"Sales: {rows} rows")
                    else:
                        log.error("[%s] Skipping SC reports — account switch failed", name)
                        account_summary.append("Sales: ❌ switch failed")
                except Exception as exc:
                    log.exception("[%s] SC error: %s", name, exc)
                    account_summary.append(f"Sales: ❌ error")
            elif not ads_only:
                log.error("[%s] Skipping SC reports — not logged in to %s SC portal",
                          name, "US" if is_us else "India")

            # ── Ads reports ───────────────────────────────────────────────────
            if not sc_only and active_ads_logged_in:
                try:
                    entity_id = await switch_ads_account(
                        active_ads_page, ads_account, name,
                        campaign_manager_url=active_cm_url,
                    )
                    if not entity_id:
                        log.error("[%s] Skipping Ads reports — entity switch failed", name)
                        account_summary.append("Ads: ❌ switch failed")
                    else:
                        prev_owner = ads_seen_ids.get(entity_id)
                        if prev_owner and prev_owner != name and entity_id != "UNKNOWN":
                            log.error(
                                "[%s] CONTAMINATION GUARD: Ads entityId %s already belongs to '%s' — "
                                "aborting Ads download for %s", name, entity_id, prev_owner, name
                            )
                            account_summary.append(f"Ads: ❌ contamination guard (entityId matches {prev_owner})")
                        else:
                            if entity_id != "UNKNOWN":
                                ads_seen_ids[entity_id] = name

                            campaign_df = await download_campaign_report(active_ads_page, account, name)
                            rows = upload_dataframe(campaign_df, sheet_id, "campaigns")
                            if rows:
                                account_summary.append(f"Campaigns: {rows} rows")

                            products_df = await download_advertised_products_report(active_ads_page, account, name)
                            rows = upload_dataframe(products_df, sheet_id, "advertised_products")
                            if rows:
                                account_summary.append(f"Products: {rows} rows")

                except Exception as exc:
                    log.exception("[%s] Ads error: %s", name, exc)
                    account_summary.append("Ads: ❌ error")
            elif not sc_only:
                log.error("[%s] Skipping Ads reports — not logged in to %s Ads portal",
                          name, "US" if is_us else "India")

            # Send per-account summary to Telegram
            if account_summary:
                await bot_ctx.send(f"✅ *{name}*\n" + "\n".join(f"  • {s}" for s in account_summary))
            else:
                await bot_ctx.send(f"⚠️ *{name}* — no data uploaded")

        if sc_context:     await sc_context.close()
        if sc_us_context:  await sc_us_context.close()
        if ads_context:    await ads_context.close()
        if ads_us_context: await ads_us_context.close()
        await browser.close()

    clean_downloads()
    log.info("All accounts processed.")


def main():
    setup_logging()

    parser = argparse.ArgumentParser(description="Amazon Daily Reporting Tool")
    parser.add_argument("--account", help="Run for a single account by name")
    parser.add_argument(
        "--clear-sessions", action="store_true",
        help="Delete all saved sessions (force re-login next run)",
    )
    parser.add_argument("--test-sheets", help="Test Google Sheets connection for given sheet ID")
    parser.add_argument("--sc-only", action="store_true", help="Run only Seller Central (business) reports")
    parser.add_argument("--ads-only", action="store_true", help="Run only Ads reports")
    parser.add_argument("--capture-ids", action="store_true",
                        help="Interactive: capture SC mcid/mkid/paid IDs for each account into accounts.xlsx")
    parser.add_argument("--recapture-ids", action="store_true",
                        help="Same as --capture-ids but re-captures ALL accounts (overwriting existing IDs)")
    args = parser.parse_args()

    if args.clear_sessions:
        for f in SESSIONS_DIR.glob("*.json"):
            f.unlink()
        print("All sessions cleared.")
        return

    if args.test_sheets:
        test_connection(args.test_sheets)
        return

    if args.capture_ids or args.recapture_ids:
        from src.sc_id_discovery import capture_all_ids
        asyncio.run(capture_all_ids(force=args.recapture_ids))
        return

    asyncio.run(run(
        filter_account=args.account,
        sc_only=args.sc_only,
        ads_only=args.ads_only,
    ))


if __name__ == "__main__":
    main()
