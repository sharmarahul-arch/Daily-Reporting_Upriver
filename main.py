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

# ── --doctor short-circuit ───────────────────────────────────────────────────
# Run the setup health check BEFORE importing config/playwright, so it works
# even when the setup is incomplete (missing credentials, deps, etc.) — which
# is exactly when someone runs --doctor.
if "--doctor" in sys.argv:
    from src.doctor import run_doctor
    raise SystemExit(run_doctor())

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


async def run(filter_account: Optional[str] = None, headless: bool = False, sc_only: bool = False,
              ads_only: bool = False, target_date=None, exclude_accounts=None,
              backfill_days: int = 2, parallel: int = 3):
    accounts = load_accounts()

    # ── Attribution backfill ──────────────────────────────────────────────
    # Amazon keeps re-attributing ad sales for several days after the fact,
    # so a normal (yesterday) run also re-pulls the N days before yesterday
    # and REPLACES those rows in Sheets (see upload_dataframe replace_date).
    # An explicit --date run pulls only that one day (still replace-on-upload).
    from datetime import date as _date, timedelta as _td
    if target_date is not None:
        report_dates = [target_date]
    else:
        yest = _date.today() - _td(days=1)
        report_dates = [yest - _td(days=n) for n in range(backfill_days, 0, -1)] + [yest]
    if filter_account:
        accounts = [a for a in accounts if a["name"].lower() == filter_account.lower()]
        if not accounts:
            log.error("Account '%s' not found in config", filter_account)
            await bot_ctx.send(f"❌ Account `{filter_account}` not found in config.")
            return
    if exclude_accounts:
        excl = {e.strip().lower() for e in exclude_accounts}
        before = len(accounts)
        accounts = [a for a in accounts if a["name"].lower() not in excl]
        log.info("Excluded %d account(s): %s", before - len(accounts), ", ".join(sorted(excl)))

    email    = AMAZON_EMAIL
    password = AMAZON_PASSWORD

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=headless,
            downloads_path=str(DOWNLOADS_DIR),
            args=["--no-sandbox"],
        )

        # ── Phase 1: ensure portal sessions (sequential — may need manual OTP) ──
        # Each needed portal gets a short-lived bootstrap context: restore
        # cookies or log in fresh, save cookies to sessions/<portal>.json,
        # close. The parallel per-account contexts in Phase 2 then seed
        # themselves from those saved session files.
        has_in = any(a.get("marketplace", "IN").upper() != "US" for a in accounts)
        has_us = any(a.get("marketplace", "IN").upper() == "US" for a in accounts)
        need = {
            "sc":     (not ads_only) and has_in,
            "sc_us":  (not ads_only) and has_us,
            "ads":    (not sc_only) and has_in,
            "ads_us": (not sc_only) and has_us,
        }

        async def _ensure_portal_session(portal: str, check_url: str, login_fn) -> bool:
            ctx = await browser.new_context(accept_downloads=True,
                                            viewport={"width": 1920, "height": 1080})
            try:
                page = await ctx.new_page()
                ok = await restore_session(ctx, portal)
                if ok:
                    try:
                        await page.goto(check_url, wait_until="domcontentloaded", timeout=15000)
                        # Only treat a hard sign-in redirect as failure.
                        # "account-switcher/switcher" means we ARE logged in.
                        if "ap/signin" in page.url:
                            ok = False
                    except Exception:
                        ok = False
                if not ok:
                    clear_session(portal)
                    ok = await login_fn(page)
                    if ok:
                        await save_session(ctx, portal)
                return ok
            finally:
                await ctx.close()

        portal_ok = {p: False for p in need}
        if need["sc"]:
            portal_ok["sc"] = await _ensure_portal_session(
                "sc", f"{SC_BASE_URL}/gp/homepage.html",
                lambda page: login_seller_central(page, email, password, base_url=SC_BASE_URL))
            if not portal_ok["sc"]:
                await bot_ctx.send(
                    "⚠️ *SC India login failed* — Sales reports will be skipped.\n"
                    "Run `python3 main.py --sc-only` (headed) to refresh the session."
                )
        if need["sc_us"]:
            portal_ok["sc_us"] = await _ensure_portal_session(
                "sc_us", f"{SC_BASE_URL_US}/gp/homepage.html",
                lambda page: login_seller_central(page, email, password, base_url=SC_BASE_URL_US))
            if not portal_ok["sc_us"]:
                await bot_ctx.send(
                    "⚠️ *SC US login failed* — US Sales reports will be skipped.\n"
                    "Run `python3 main.py --sc-only` (headed) to refresh the session."
                )
        if need["ads"]:
            portal_ok["ads"] = await _ensure_portal_session(
                "ads", ADS_CAMPAIGN_MANAGER,
                lambda page: login_ads(page, email, password, start_url=ADS_CAMPAIGN_MANAGER))
        if need["ads_us"]:
            portal_ok["ads_us"] = await _ensure_portal_session(
                "ads_us", ADS_CAMPAIGN_MANAGER_US,
                lambda page: login_ads(page, email, password, start_url=ADS_CAMPAIGN_MANAGER_US))

        # Track merchant/entity IDs across accounts to catch silent switch no-ops.
        # If switching to a new account returns an ID we've already seen for a
        # different brand, the switch silently failed and uploading would
        # cross-contaminate (e.g. Acuro labelled rows with HEM campaign data).
        sc_seen_ids: dict[str, str] = {}    # mcid -> brand_name that owns it
        ads_seen_ids: dict[str, str] = {}   # entityId -> brand_name that owns it

        # ── Phase 2: process all accounts IN PARALLEL ─────────────────────────
        # Every account gets its OWN SC context and Ads context, each seeded
        # from the saved portal cookies. A context holds exactly one account
        # selection for its whole life, so accounts can run side by side
        # without racing each other's switcher state. `parallel` caps how
        # many accounts run at once (each takes up to 2 contexts). Waits stay
        # generous — while one account waits on a slow table, the others keep
        # working, so patience no longer costs wall-clock time.
        sem = asyncio.Semaphore(max(1, parallel))

        async def _new_seeded_context(portal: str):
            ctx = await browser.new_context(accept_downloads=True,
                                            viewport={"width": 1920, "height": 1080})
            await restore_session(ctx, portal)
            return ctx

        async def _account_sc(account, name, summary):
            if ads_only:
                return
            marketplace = account.get("marketplace", "IN").upper()
            is_us = marketplace == "US"
            portal = "sc_us" if is_us else "sc"
            if not portal_ok.get(portal):
                log.error("[%s] Skipping SC reports — not logged in to %s SC portal",
                          name, "US" if is_us else "India")
                return
            sheet_id   = account["google_sheet_id"]
            sc_account = account.get("sc_account_name", name)
            sc_parent  = account.get("sc_parent")
            ctx = await _new_seeded_context(portal)
            try:
                page = await ctx.new_page()
                mcid = await switch_sc_account(
                    page, sc_account, sc_parent, name,
                    marketplace=marketplace,
                    sc_merchant_id=account.get("sc_merchant_id"),
                    sc_marketplace_id=account.get("sc_marketplace_id"),
                    sc_paid_id=account.get("sc_paid_id"),
                    sc_dc=account.get("sc_dc"),
                )
                if not mcid:
                    log.error("[%s] Skipping SC reports — account switch failed", name)
                    summary.append("Sales: ❌ switch failed")
                    return
                prev_owner = sc_seen_ids.get(mcid)
                if prev_owner and prev_owner != name and mcid != "UNKNOWN":
                    log.error(
                        "[%s] CONTAMINATION GUARD: SC mcid %s already belongs to '%s' — "
                        "aborting Sales download for %s", name, mcid, prev_owner, name
                    )
                    summary.append(f"Sales: ❌ contamination guard (mcid matches {prev_owner})")
                    return
                if mcid != "UNKNOWN":
                    sc_seen_ids[mcid] = name
                need_nav = True
                for rd in report_dates:
                    sales_df = await download_sales_report(
                        page, name, marketplace=marketplace,
                        target_date=rd, navigate=need_nav)
                    # Reuse the open report page only after a clean pull
                    need_nav = sales_df is None
                    rows = upload_dataframe(sales_df, sheet_id, "sales", replace_date=rd)
                    if rows:
                        summary.append(f"Sales {rd.isoformat()}: {rows} rows")
            except Exception as exc:
                log.exception("[%s] SC error: %s", name, exc)
                summary.append("Sales: ❌ error")
            finally:
                await ctx.close()

        async def _account_ads(account, name, summary):
            if sc_only:
                return
            marketplace = account.get("marketplace", "IN").upper()
            is_us = marketplace == "US"
            portal = "ads_us" if is_us else "ads"
            if not portal_ok.get(portal):
                log.error("[%s] Skipping Ads reports — not logged in to %s Ads portal",
                          name, "US" if is_us else "India")
                return
            sheet_id    = account["google_sheet_id"]
            ads_account = account.get("ads_account_name", name)
            cm_url      = ADS_CAMPAIGN_MANAGER_US if is_us else ADS_CAMPAIGN_MANAGER
            ctx = await _new_seeded_context(portal)
            try:
                page = await ctx.new_page()
                entity_id = await switch_ads_account(
                    page, ads_account, name,
                    campaign_manager_url=cm_url,
                )
                if not entity_id:
                    log.error("[%s] Skipping Ads reports — entity switch failed", name)
                    summary.append("Ads: ❌ switch failed")
                    return
                prev_owner = ads_seen_ids.get(entity_id)
                if prev_owner and prev_owner != name and entity_id != "UNKNOWN":
                    log.error(
                        "[%s] CONTAMINATION GUARD: Ads entityId %s already belongs to '%s' — "
                        "aborting Ads download for %s", name, entity_id, prev_owner, name
                    )
                    summary.append(f"Ads: ❌ contamination guard (entityId matches {prev_owner})")
                    return
                if entity_id != "UNKNOWN":
                    ads_seen_ids[entity_id] = name

                need_nav = True
                for rd in report_dates:
                    campaign_df = await download_campaign_report(
                        page, account, name, target_date=rd, navigate=need_nav)
                    need_nav = campaign_df is None
                    rows = upload_dataframe(campaign_df, sheet_id, "campaigns", replace_date=rd)
                    if rows:
                        summary.append(f"Campaigns {rd.isoformat()}: {rows} rows")

                need_nav = True
                for rd in report_dates:
                    products_df = await download_advertised_products_report(
                        page, account, name, target_date=rd, navigate=need_nav)
                    need_nav = products_df is None
                    rows = upload_dataframe(products_df, sheet_id, "advertised_products", replace_date=rd)
                    if rows:
                        summary.append(f"Products {rd.isoformat()}: {rows} rows")
            except Exception as exc:
                log.exception("[%s] Ads error: %s", name, exc)
                summary.append("Ads: ❌ error")
            finally:
                await ctx.close()

        async def _process_account(account):
            name = account["name"]
            async with sem:
                log.info("=" * 60)
                log.info("Processing: %s  (dates: %s)", name,
                         ", ".join(d.isoformat() for d in report_dates))
                log.info("=" * 60)
                summary = []
                # SC and Ads for one account use separate contexts — run together
                await asyncio.gather(_account_sc(account, name, summary),
                                     _account_ads(account, name, summary))
                if summary:
                    await bot_ctx.send(f"✅ *{name}*\n" + "\n".join(f"  • {s}" for s in summary))
                else:
                    await bot_ctx.send(f"⚠️ *{name}* — no data uploaded")

        await asyncio.gather(*(_process_account(a) for a in accounts))

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
    parser.add_argument("--date", help="Pull a specific day (YYYY-MM-DD) instead of yesterday")
    parser.add_argument("--exclude", help="Comma-separated account names to skip (e.g. \"Charlotte Home\")")
    parser.add_argument("--backfill-days", type=int, default=2,
                        help="On a default (yesterday) run, also re-pull and replace this many "
                             "days before yesterday to absorb Amazon attribution updates "
                             "(default: 2, use 0 to disable)")
    parser.add_argument("--parallel", type=int, default=3,
                        help="How many accounts to process at the same time, each in its own "
                             "browser context (default: 3; raise carefully — each account "
                             "opens up to 2 browser windows)")
    args = parser.parse_args()

    target_date = None
    if args.date:
        from src.utils import parse_date_arg
        from datetime import date as _date
        try:
            target_date = parse_date_arg(args.date)
        except ValueError:
            print(f"❌ Invalid --date '{args.date}'. Use YYYY-MM-DD.")
            return
        if target_date >= _date.today():
            print(f"❌ --date {target_date} is not in the past. Amazon only has complete data for past days.")
            return

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

    exclude_accounts = [e for e in (args.exclude or "").split(",") if e.strip()] or None

    asyncio.run(run(
        filter_account=args.account,
        sc_only=args.sc_only,
        ads_only=args.ads_only,
        target_date=target_date,
        exclude_accounts=exclude_accounts,
        backfill_days=max(0, args.backfill_days),
        parallel=max(1, args.parallel),
    ))


if __name__ == "__main__":
    main()
