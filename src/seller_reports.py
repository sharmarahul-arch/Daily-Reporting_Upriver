"""
Downloads two reports from Amazon Seller Central (India):
  1. Business Report – Sales & Traffic by ASIN (yesterday)
  2. FBA Inventory – current snapshot
"""

import logging
import re
from pathlib import Path
from datetime import date, timedelta
from typing import Optional

import pandas as pd
from playwright.async_api import Page

from src.utils import wait_for_download, yesterday
from src.config import SC_BASE_URL, SC_BASE_URL_US

log = logging.getLogger(__name__)

_YESTERDAY = (date.today() - timedelta(days=1)).strftime("%m/%d/%Y")


# ── Sales & Traffic ───────────────────────────────────────────────────────────

async def download_sales_report(page: Page, account_name: str, marketplace: str = "IN",
                                target_date: Optional["date"] = None) -> Optional[pd.DataFrame]:
    """
    Downloads the "Detail Page Sales and Traffic By Child Item" Business Report.
    Flow: open Business Reports → click that report in the left nav →
          set date (yesterday by default, or target_date) → Generate → download CSV.
    marketplace: "IN" for India SC, "US" (or any non-IN) for US SC.
    target_date: specific day to pull; defaults to yesterday when None.
    """
    from src.utils import resolve_report_date
    report_date = resolve_report_date(target_date)
    sc_base = SC_BASE_URL_US if marketplace.upper() == "US" else SC_BASE_URL
    log.info("[%s] Fetching Sales & Traffic report for %s (SC: %s)...",
             account_name, report_date.isoformat(), sc_base)
    try:
        from src.config import DOWNLOADS_DIR
        DOWNLOADS_DIR.mkdir(exist_ok=True)

        await page.goto(
            f"{sc_base}/business-reports/ref=xx_bizrpt_dnav_xx#/dashboard",
            wait_until="domcontentloaded",
            timeout=30000,
        )
        await page.wait_for_load_state("networkidle", timeout=20000)
        await page.wait_for_timeout(2500)

        # ── Step 1: Click "Detail Page Sales and Traffic By Child Item" in nav ──
        nav_clicked = await page.evaluate("""
            () => {
                const els = Array.from(document.querySelectorAll(
                    'a, button, [role="link"], [role="menuitem"], li, span, div'
                ));
                const norm = s => s.trim().toLowerCase().replace(/\\s+/g, ' ');
                // Exact match first
                let t = els.find(e =>
                    e.children.length === 0 &&
                    norm(e.textContent) === 'detail page sales and traffic by child item'
                );
                // Partial fallback
                if (!t) {
                    t = els.find(e =>
                        e.children.length === 0 &&
                        norm(e.textContent).includes('sales and traffic by child')
                    );
                }
                if (t) {
                    t.scrollIntoView({block: 'center'});
                    t.click();
                    return norm(t.textContent);
                }
                return null;
            }
        """)
        if nav_clicked:
            log.info("[%s] Opened report: '%s'", account_name, nav_clicked)
            await page.wait_for_load_state("networkidle", timeout=15000)
            await page.wait_for_timeout(3000)
        else:
            log.warning("[%s] Could not find 'Detail Page Sales and Traffic By Child Item' nav link",
                        account_name)

        await page.screenshot(path=str(DOWNLOADS_DIR / f"debug_{account_name}_sales_report_page.png"),
                              full_page=True)

        # ── Step 2: Set the date range to yesterday ───────────────────────────
        await _set_date_to_yesterday(page, account_name, report_date)
        await page.wait_for_timeout(1500)

        # ── Step 3: Click "Generate" / "Request report" ───────────────────────
        gen_clicked = await page.evaluate("""
            () => {
                const els = Array.from(document.querySelectorAll(
                    'button, a, input[type="submit"], [role="button"]'
                ));
                const b = els.find(e => {
                    const t = (e.textContent || e.value || '').trim().toLowerCase();
                    return t === 'generate' || t === 'generate report' ||
                           t === 'request report' || t === 'request' ||
                           t.includes('generate');
                });
                if (b) { b.click(); return (b.textContent || b.value || '').trim(); }
                return null;
            }
        """)
        if gen_clicked:
            log.info("[%s] Clicked generate/request: '%s'", account_name, gen_clicked)
            await page.wait_for_timeout(4000)
        else:
            log.info("[%s] No Generate button — report may render inline", account_name)

        # ── Step 4: Poll for the Download control, then download ──────────────
        for attempt in range(20):  # 20 × 15s = 5 min max
            # The report row, once ready, exposes a Download button/link
            dl = await page.query_selector(
                'a[href*="download"], a[download], '
                'a:has-text("Download"), button:has-text("Download")'
            )
            if dl:
                log.info("[%s] Download control found (attempt %d) — downloading",
                         account_name, attempt + 1)
                path = await wait_for_download(page, lambda d=dl: d.click(), timeout=90)
                if path:
                    return _parse_sales_csv(path, account_name, report_date)
                # The Download button may open a CSV submenu — try that
                await page.wait_for_timeout(1500)
                csv_el = await page.query_selector(
                    'a:has-text("CSV"), button:has-text("CSV"), [role="menuitem"]:has-text("CSV")'
                )
                if csv_el:
                    log.info("[%s] Clicking CSV submenu item", account_name)
                    path = await wait_for_download(page, lambda c=csv_el: c.click(), timeout=90)
                    if path:
                        return _parse_sales_csv(path, account_name, report_date)

            log.info("[%s] Waiting for sales report... attempt %d/20", account_name, attempt + 1)
            await page.wait_for_timeout(15000)
            try:
                await page.reload()
                await page.wait_for_load_state("networkidle", timeout=15000)
                await page.wait_for_timeout(2000)
            except Exception:
                pass

        ss_path = DOWNLOADS_DIR / f"debug_{account_name}_sales.png"
        await page.screenshot(path=str(ss_path), full_page=True)
        log.info("[%s] Debug screenshot saved: %s", account_name, ss_path.name)
        log.warning("[%s] Sales report download did not produce a file", account_name)
        return None

    except Exception as exc:
        log.error("[%s] Sales report error: %s", account_name, exc)
        return None


async def _set_date_to_yesterday(page: Page, account_name: str, report_date: "date" = None):
    """
    Set the Business Reports date filter.

    If report_date is yesterday (or None), use the fast 'Yesterday' preset.
    Otherwise select the 'Custom' range and set start == end == report_date,
    so the report covers exactly that single day.
    """
    from src.utils import is_yesterday, resolve_report_date
    report_date = resolve_report_date(report_date)
    use_preset = is_yesterday(report_date)
    try:
        await page.wait_for_timeout(800)

        # ── Step 1: Scroll to top to see the date controls ──────────────────────
        await page.evaluate("window.scrollTo(0, 0)")
        await page.wait_for_timeout(500)

        # ── Step 2: Click the Date dropdown using data-testid ──────────────────
        # The dropdown is: <kat-dropdown value="YESTERDAY" data-testid="selection-box-dropdown">
        date_dropdown = await page.query_selector('[data-testid="selection-box-dropdown"]')

        if date_dropdown:
            log.info("[%s] Found Date dropdown — clicking to open", account_name)
            await date_dropdown.click()
            await page.wait_for_timeout(1200)

            current_value = await date_dropdown.evaluate("el => el.getAttribute('value')")
            log.info("[%s] Current date value: %s", account_name, current_value)

            if use_preset:
                if current_value and current_value.upper() == "YESTERDAY":
                    log.info("[%s] Date already set to 'Yesterday' — no change needed", account_name)
                else:
                    yesterday_option = await page.query_selector('kat-option[value="YESTERDAY"]')
                    if yesterday_option:
                        log.info("[%s] Found 'Yesterday' kat-option — clicking it", account_name)
                        await yesterday_option.click()
                        await page.wait_for_timeout(800)
                    else:
                        log.warning("[%s] Could not find kat-option[value='YESTERDAY']", account_name)
            else:
                # ── Custom single day: select CUSTOM, then set From == To == day ──
                log.info("[%s] Selecting CUSTOM range for %s", account_name, report_date.isoformat())
                custom_option = await page.query_selector('kat-option[value="CUSTOM"]')
                if custom_option:
                    await custom_option.click()
                    await page.wait_for_timeout(1500)
                else:
                    log.warning("[%s] Could not find kat-option[value='CUSTOM']", account_name)

                # The picker exposes two text inputs with placeholder "DD/MM/YYYY"
                # (From, To). Format is locale-dependent, so we READ each input's
                # placeholder and format the target date to match (handles IN
                # dd/mm/yyyy and US mm/dd/yyyy). Playwright's .fill() pierces the
                # kat-input shadow DOM and fires the native events Katal listens to.
                def _fmt_for(placeholder: str) -> str:
                    p = (placeholder or "").upper()
                    if p.startswith("MM/DD") or p.startswith("MM-DD"):
                        return report_date.strftime("%m/%d/%Y")
                    # default / DD/MM/YYYY
                    return report_date.strftime("%d/%m/%Y")

                date_inputs = page.locator('input[placeholder*="/"]')
                n = await date_inputs.count()
                log.info("[%s] Found %d input(s) matching placeholder*='/'", account_name, n)
                set_count = 0
                for i in range(n):
                    inp = date_inputs.nth(i)
                    try:
                        vis = await inp.is_visible()
                        ph = (await inp.get_attribute("placeholder")) or ""
                        if not vis:
                            log.info("[%s] date input %d (ph='%s') not visible — skip", account_name, i, ph)
                            continue
                        if "YYYY" not in ph.upper():
                            log.info("[%s] date input %d (ph='%s') no YYYY — skip", account_name, i, ph)
                            continue
                        val = _fmt_for(ph)
                        await inp.click()
                        await inp.fill("")
                        await inp.type(val, delay=40)
                        # From and To are SEPARATE kat-date-pickers; each opens a
                        # calendar popover that overlays the other input. Press
                        # Escape to dismiss this picker's calendar so the next
                        # input becomes visible/clickable for the following pass.
                        await inp.press("Escape")
                        await page.wait_for_timeout(600)
                        set_count += 1
                        log.info("[%s] Set date input %d (ph='%s') = %s", account_name, i, ph, val)
                        if set_count >= 2:
                            break
                    except Exception as exc:
                        log.warning("[%s] Could not set date input %d: %s", account_name, i, exc)
                if set_count == 0:
                    log.warning("[%s] No DD/MM/YYYY date inputs found for custom range", account_name)
                await page.wait_for_timeout(800)
        else:
            log.warning("[%s] Could not find Date dropdown with data-testid", account_name)

        # ── Step 3: Click the "Apply" button using data-testid ─────────────────
        # The Apply button is: <kat-button data-testid="ASINS_SEARCH_APPLY" label="Apply">
        await page.wait_for_timeout(500)
        apply_btn = await page.query_selector('[data-testid="ASINS_SEARCH_APPLY"]')

        if apply_btn:
            log.info("[%s] Found Apply button — clicking it", account_name)
            await apply_btn.click()
            await page.wait_for_timeout(2500)
            log.info("[%s] Date filter applied successfully", account_name)
            if not use_preset:
                # Read back the applied range so we can confirm start == end == target
                applied_vals = await page.evaluate("""
                    () => {
                        const collect = (root, out) => {
                            root.querySelectorAll('input').forEach(i => {
                                const r = i.getBoundingClientRect();
                                if (r.width > 0 && r.height > 0 && i.type !== 'hidden' && (i.value||'').trim())
                                    out.push((i.value||'').trim());
                            });
                            root.querySelectorAll('*').forEach(e => { if (e.shadowRoot) collect(e.shadowRoot, out); });
                        };
                        const out = []; collect(document, out);
                        return out.slice(0, 6);
                    }
                """)
                log.info("[%s] APPLIED DATE READBACK: %s", account_name, applied_vals)
        else:
            log.warning("[%s] Could not find Apply button with data-testid", account_name)

    except Exception as exc:
        log.warning("[%s] Error setting date filter: %s", account_name, exc)


def _parse_sales_csv(path: Path, account_name: str, report_date: "date" = None) -> pd.DataFrame:
    df = pd.read_csv(path, encoding="utf-8-sig")
    df.columns = df.columns.str.strip()
    df.insert(0, "Account", account_name)
    df.insert(1, "Report Date", report_date.isoformat() if report_date else yesterday())
    log.info("[%s] Sales rows: %d", account_name, len(df))
    return df


# ── FBA Inventory ─────────────────────────────────────────────────────────────

async def download_inventory_report(page: Page, account_name: str, marketplace: str = "IN") -> Optional[pd.DataFrame]:
    """
    Downloads FBA inventory using the Seller Central 'Manage Inventory' export.
    Falls back to the Reports Centre if the direct export isn't available.
    marketplace: "IN" for India SC, "US" (or any non-IN) for US SC.
    """
    sc_base = SC_BASE_URL_US if marketplace.upper() == "US" else SC_BASE_URL
    log.info("[%s] Fetching FBA Inventory report (SC: %s)...", account_name, sc_base)
    try:
        # Primary: Manage FBA Inventory — has a direct "Download" button
        await page.goto(
            f"{sc_base}/hz/inventory",
            wait_until="domcontentloaded",
            timeout=30000,
        )
        await page.wait_for_load_state("networkidle", timeout=20000)
        await page.wait_for_timeout(2000)

        # Look for export / download button on Manage Inventory
        for sel in [
            'button:has-text("Download")',
            'a:has-text("Download Inventory")',
            'button:has-text("Export")',
            'a:has-text("Export")',
            '[id*="download"]',
        ]:
            el = await page.query_selector(sel)
            if el:
                log.info("[%s] Inventory export button found: %s", account_name, sel)
                path = await wait_for_download(page, lambda e=el: e.click(), timeout=90)
                if path:
                    return _parse_inventory_csv(path, account_name)
                break

        # Fallback: Reports Centre → FBA Inventory report
        log.info("[%s] Trying Reports Centre for inventory...", account_name)
        await page.goto(
            f"{sc_base}/gp/ssof/reports.html",
            wait_until="domcontentloaded",
            timeout=30000,
        )
        await page.wait_for_load_state("networkidle", timeout=15000)
        await page.wait_for_timeout(2000)

        # Request a new FBA inventory report
        request_btn = await page.query_selector(
            'button:has-text("Request"), input[value*="Request report"]'
        )
        if request_btn:
            await request_btn.click()
            await page.wait_for_timeout(3000)

        for attempt in range(10):
            download_link = await page.query_selector(
                'a:has-text("Download"), a[href*="download"], td a[href*=".txt"], td a[href*=".csv"]'
            )
            if download_link:
                path = await wait_for_download(
                    page,
                    lambda e=download_link: e.click(),
                    timeout=90,
                )
                if path:
                    return _parse_inventory_csv(path, account_name)
            log.info("[%s] Waiting for inventory report... (attempt %d/10)", account_name, attempt + 1)
            await page.wait_for_timeout(5000)
            await page.reload()
            await page.wait_for_load_state("networkidle", timeout=10000)

        log.warning("[%s] Inventory report not ready after waiting", account_name)
        return None

    except Exception as exc:
        log.error("[%s] Inventory report error: %s", account_name, exc)
        return None


def _parse_inventory_csv(path: Path, account_name: str) -> pd.DataFrame:
    df = pd.read_csv(path, sep="\t", encoding="utf-8-sig")
    df.columns = df.columns.str.strip()
    df.insert(0, "Account", account_name)
    df.insert(1, "Report Date", yesterday())
    log.info("[%s] Inventory rows: %d", account_name, len(df))
    return df
