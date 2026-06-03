"""
Downloads two reports from Amazon Advertising Campaign Manager (India):
  1. Campaign Performance  — Campaigns tab → date picker → Export → Download (table data)
  2. Advertised Products   — Products tab → last page → Export → Download (table data)
"""

import asyncio
import logging
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd
from playwright.async_api import Page

from src.utils import wait_for_download, yesterday
from src.config import ADS_BASE_URL, ADS_CAMPAIGN_MANAGER, ADS_BASE_URL_US, ADS_CAMPAIGN_MANAGER_US, DOWNLOADS_DIR

log = logging.getLogger(__name__)


def _ads_urls(account: dict) -> tuple[str, str]:
    """Return (base_url, campaign_manager_url) for this account's marketplace."""
    if account.get("marketplace", "IN").upper() == "US":
        return ADS_BASE_URL_US, ADS_CAMPAIGN_MANAGER_US
    return ADS_BASE_URL, ADS_CAMPAIGN_MANAGER


# ── Shared helpers ─────────────────────────────────────────────────────────────

async def _debug_screenshot(page: Page, label: str):
    try:
        DOWNLOADS_DIR.mkdir(exist_ok=True)
        path = DOWNLOADS_DIR / f"debug_{label}.png"
        await page.screenshot(path=str(path), full_page=True)
        log.info("Debug screenshot: %s", path.name)
    except Exception:
        pass


async def _set_dashboard_date_yesterday(page: Page, account_name: str, target_date: "date" = None):
    """
    Set the Campaign Manager dashboard date picker.

    If target_date is yesterday (or None) the fast 'Yesterday' preset is used.
    Otherwise the preset is skipped and the calendar/date-input path selects the
    specific target_date (start == end == that day).
    """
    from src.utils import is_yesterday, resolve_report_date
    target_date = resolve_report_date(target_date)
    use_preset = is_yesterday(target_date)
    try:
        await page.wait_for_timeout(800)

        # ── Step 1: Scroll to top ───────────────────────────────────────────────
        await page.evaluate("window.scrollTo(0, 0)")
        await page.wait_for_timeout(300)

        # ── Step 2: Find and click the CORRECT date picker button ───────────────
        # The Campaign Manager page has TWO date pickers:
        #   A) Chart date picker (top performance chart) — "Date rangeMay 16 - May 22"
        #   B) Table date picker (in the toolbar next to Columns / Export) — "22 May, 2026"
        # We must click the TABLE date picker (B) — it controls the exported data.
        # Strategy: find the date button that lives in the SAME toolbar container as Export.

        opened = await page.evaluate("""
            () => {
                const isVis = el => {
                    const r = el.getBoundingClientRect();
                    const s = window.getComputedStyle(el);
                    return r.width > 0 && r.height > 0 &&
                           s.visibility !== 'hidden' && s.display !== 'none';
                };

                // A button is a "date picker" if it shows a formatted date OR a preset label
                const DATE_RANGE_LABELS = [
                    'lifetime', 'today', 'yesterday', 'last 7', 'last 14', 'last 30',
                    'this week', 'this month', 'last month', 'custom', 'date range'
                ];
                const isDateBtn = (el, exportBtn) => {
                    if (!isVis(el) || el === exportBtn) return false;
                    const t = (el.innerText || el.textContent || '').trim();
                    const tl = t.toLowerCase();
                    if (t.length === 0 || t.length > 60) return false;
                    // Formatted date: "22 May, 2026" / "May 22, 2026" / "May 16 - May 22, 2026"
                    if (/20\\d{2}/.test(t) && /[A-Za-z]/.test(t)) return true;
                    // Preset label: "Lifetime", "Last 7 days", etc.
                    if (DATE_RANGE_LABELS.some(l => tl.includes(l))) return true;
                    return false;
                };

                // Find the Export button first — we know it's in the table toolbar
                const exportBtn = Array.from(document.querySelectorAll(
                    'button, [role="button"], a'
                )).find(e => {
                    const t = (e.innerText || e.textContent || '').trim().toLowerCase();
                    return isVis(e) && t === 'export';
                });

                if (exportBtn) {
                    // Walk up to find the toolbar container, then search sideways for a date button
                    let container = exportBtn.parentElement;
                    for (let depth = 0; depth < 8 && container; depth++) {
                        const dateBtns = Array.from(container.querySelectorAll(
                            'button, [role="button"], [role="combobox"]'
                        )).filter(b => isDateBtn(b, exportBtn));
                        if (dateBtns.length > 0) {
                            // Click the one closest to the Export button (last in DOM order)
                            const target = dateBtns[dateBtns.length - 1];
                            target.scrollIntoView({block: 'center'});
                            target.click();
                            return (target.innerText || target.textContent || '').trim().slice(0, 60);
                        }
                        container = container.parentElement;
                    }
                }

                // Fallback: any visible date button/combobox near the right side of page
                const allBtns = Array.from(document.querySelectorAll(
                    'button, [role="button"], [role="combobox"]'
                ));
                const dateBtn = allBtns.find(e => isDateBtn(e, null));
                if (dateBtn) {
                    dateBtn.scrollIntoView({block: 'center'});
                    dateBtn.click();
                    return (dateBtn.innerText || dateBtn.textContent || '').trim().slice(0, 60);
                }
                return null;
            }
        """)

        if not opened:
            log.warning("[%s] Dashboard date picker button not found — skipping date filter", account_name)
            return

        log.info("[%s] Opened dashboard date picker (was: '%s')", account_name, opened)
        await page.wait_for_timeout(1500)  # wait for picker panel to animate open

        # ── Step 3: Click "Yesterday" preset (only when target IS yesterday) ────
        picked = False
        if use_preset:
            # Try Playwright locator first (handles visibility correctly)
            for yesterday_selector in [
                'text="Yesterday"',
                '[data-value="yesterday"], [data-preset="yesterday"]',
                'button:has-text("Yesterday"), li:has-text("Yesterday"), span:has-text("Yesterday")',
            ]:
                try:
                    yest_loc = page.locator(yesterday_selector).first
                    await yest_loc.wait_for(state="visible", timeout=2000)
                    await yest_loc.click()
                    picked = True
                    break
                except Exception:
                    continue

            if not picked:
                # JS fallback
                picked = await page.evaluate("""
                    () => {
                        const isVis = el => {
                            const r = el.getBoundingClientRect();
                            return r.width > 0 && r.height > 0 && el.offsetParent !== null;
                        };
                        const all = Array.from(document.querySelectorAll('*'));
                        const el = all.find(e => {
                            const t = (e.textContent || '').trim().toLowerCase();
                            return t === 'yesterday' && isVis(e);
                        });
                        if (el) { el.scrollIntoView({block: 'center'}); el.click(); return true; }
                        return false;
                    }
                """)
        else:
            log.info("[%s] Custom date %s — skipping 'Yesterday' preset, using calendar",
                     account_name, target_date.isoformat())

        if picked:
            log.info("[%s] Clicked 'Yesterday' preset button", account_name)
        else:
            # ── Custom date: navigate calendar to target month, click the day ────────
            # Strategy:
            #   1. Navigate backward/forward until the target month is visible
            #   2. Use Playwright locator with aria-label to click the cell (React-aware)
            #   3. Click the same cell again as the range end (same-day range)
            #   4. Confirm via keyboard Enter or scoped Apply button
            log.info("[%s] Custom date %s — navigating calendar", account_name, target_date.isoformat())
            await _debug_screenshot(page, f"{account_name}_datepicker_open")

            yest = target_date
            yest_day   = str(yest.day)        # "30"
            yest_month = yest.strftime("%B")  # "May"
            yest_year  = str(yest.year)       # "2026"

            # ── Step 3a: Navigate calendar to the right month ────────────────────────
            target_year_int  = yest.year
            target_month_num = yest.month
            import re as _re, calendar as _cal

            for nav_attempt in range(24):
                # Find visible calendar month headers (text exactly "Month YYYY" or
                # containing it — handles both exact-text elements and header buttons
                # that include arrow characters).
                cal_info = await page.evaluate(r"""
                    () => {
                        const isVis = el => {
                            const r = el.getBoundingClientRect();
                            return r.width > 0 && r.height > 0;
                        };
                        const headers = [];
                        const seen = new Set();
                        for (const el of Array.from(document.querySelectorAll('*'))) {
                            if (!isVis(el)) continue;
                            const t = (el.innerText || el.textContent || '').trim();
                            const m = t.match(/\b([A-Z][a-z]+)\s+(20\d{2})\b/);
                            if (m) {
                                const key = m[1] + ' ' + m[2];
                                if (!seen.has(key)) { seen.add(key); headers.push(key); }
                            }
                            if (headers.length >= 2) break;
                        }
                        return headers;
                    }
                """)
                if cal_info:
                    log.info("[%s] Calendar shows: %s", account_name, cal_info)
                    if any(yest_month.lower() in h.lower() and yest_year in h for h in cal_info):
                        log.info("[%s] Target month %s %s visible — proceeding to click",
                                 account_name, yest_month, yest_year)
                        break
                    # Determine direction from first visible header
                    m = _re.search(r'([A-Z][a-z]+)\s+(20\d{2})', cal_info[0])
                    if m:
                        try:
                            shown_month = list(_cal.month_name).index(m.group(1))
                            shown_total = int(m.group(2)) * 12 + shown_month
                            target_total = target_year_int * 12 + target_month_num
                            go_forward = target_total > shown_total
                        except Exception:
                            go_forward = True
                    else:
                        go_forward = True

                    direction = 'next' if go_forward else 'prev'
                    nav_sel = (
                        'button[aria-label*="Next"], button[aria-label*="next"], '
                        'button[aria-label*="Forward"], button[aria-label*="forward"]'
                        if go_forward else
                        'button[aria-label*="Prev"], button[aria-label*="prev"], '
                        'button[aria-label*="Back"], button[aria-label*="back"]'
                    )
                    # Try Playwright locator first; fall back to text-match JS
                    nav_btn = page.locator(nav_sel).first
                    try:
                        await nav_btn.wait_for(state="visible", timeout=1000)
                        await nav_btn.click()
                        await page.wait_for_timeout(400)
                        continue
                    except Exception:
                        pass
                    # JS fallback for arrow characters
                    nav_chars = ('>', '›', '→', '▶') if go_forward else ('<', '‹', '←', '◀')
                    navigated = await page.evaluate("""
                        (chars) => {
                            const isVis = el => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
                            const btn = Array.from(document.querySelectorAll('button,[role="button"]')).find(b => {
                                if (!isVis(b)) return false;
                                const t = (b.textContent || b.innerText || '').trim();
                                return chars.includes(t);
                            });
                            if (btn) { btn.scrollIntoView({block:'center'}); btn.click(); return true; }
                            return false;
                        }
                    """, list(nav_chars))
                    if not navigated:
                        log.warning("[%s] Could not navigate calendar %s", account_name, direction)
                        break
                    await page.wait_for_timeout(400)
                else:
                    # No headers visible — wait briefly and retry (picker still animating)
                    await page.wait_for_timeout(300)

            # ── Step 3b: Click the target day cell via aria-label ────────────────────
            # aria-label format is typically: "Saturday, May 30, 2026" or
            # "Choose Saturday, May 30, 2026 as your start date."
            # Using Playwright's locator fires real pointer events that React handles.
            day_label = f"{yest_month} {yest_day}, {yest_year}"  # "May 30, 2026"

            async def _click_day_cell() -> tuple:
                """
                Click the calendar cell for the target day.
                Returns (success: bool, bbox: dict|None).
                bbox is saved so the caller can re-use the coordinates for the end click.

                Selector order:
                  1. Full date in aria-label (products picker: 'Choose Saturday, May 30…')
                  2. Any element with full date (after start click — role may change)
                  3. Day+year fragment
                  4. Day-number-only aria-label (some pickers)
                  5. Exact text content 'DD' scoped to visible cells near the picker
                     (campaigns picker — cells have NO aria-label, just plain numbers)
                """
                for sel in [
                    f'[role="gridcell"][aria-label*="{day_label}"]',
                    f'button[aria-label*="{day_label}"]',
                    f'td[aria-label*="{day_label}"]',
                    f'[aria-label*="{day_label}"]',
                    f'[aria-label*="{yest_day}, {yest_year}"]',
                    f'[role="gridcell"][aria-label="{yest_day}"]',
                    f'button[aria-label="{yest_day}"]',
                    f'td[aria-label="{yest_day}"]',
                    f'[aria-label="{yest_day}"]',
                ]:
                    loc = page.locator(sel).first
                    try:
                        await loc.wait_for(state="visible", timeout=1000)
                        lbl = await loc.get_attribute("aria-label") or yest_day
                        bbox = await loc.bounding_box()
                        await loc.click()
                        log.info("[%s] Clicked day cell [aria]: %s", account_name, lbl[:60])
                        return True, bbox
                    except Exception:
                        pass

                # Text-content fallback for pickers with no aria-labels on cells
                # (campaigns picker). After navigation the target month is the LEFT
                # calendar → its cells appear FIRST in DOM order, so .first gives the
                # correct month's cell rather than the right calendar's same number.
                try:
                    # page.get_by_text finds elements whose FULL text is exactly yest_day
                    loc = page.get_by_text(yest_day, exact=True).first
                    await loc.wait_for(state="visible", timeout=2000)
                    bbox = await loc.bounding_box()
                    lbl = await loc.text_content() or yest_day
                    await loc.click()
                    log.info("[%s] Clicked day cell [text]: %s", account_name, lbl[:10])
                    return True, bbox
                except Exception:
                    pass

                return False, None

            # Start click — delegate entirely to _click_day_cell which now
            # returns (success, bbox). The bbox is reused for the end click.
            start_ok, start_bbox = await _click_day_cell()

            if start_ok:
                await page.wait_for_timeout(700)

                # End click at the SAME coordinates — bypasses post-click DOM changes
                if start_bbox:
                    end_x = start_bbox['x'] + start_bbox['width']  / 2
                    end_y = start_bbox['y'] + start_bbox['height'] / 2
                    await page.mouse.click(end_x, end_y)
                    log.info("[%s] Clicked cell end at (%.0f, %.0f)", account_name, end_x, end_y)
                    end_ok = True
                else:
                    end_ok, _ = await _click_day_cell()
                if end_ok:
                    log.info("[%s] Clicked cell end: %s", account_name, day_label)
                else:
                    log.warning("[%s] End-date click missed — will try Apply anyway", account_name)
                await page.wait_for_timeout(600)
                picked = True
            else:
                log.warning("[%s] Could not find calendar cell for %s %s — picker may not show calendar",
                            account_name, yest_month, yest_day)

        await page.wait_for_timeout(800)

        # ── Step 4: Apply the date selection ─────────────────────────────────────
        # Primary: keyboard Enter (no risk of hitting wrong button; works when
        # the picker has keyboard focus after cell clicks).
        # Fallback: click the Apply button that is NEAR a visible gridcell
        # (the picker's own Apply, not a search/filter Apply elsewhere on page).
        url_before = page.url
        applied = None
        if picked and not use_preset:
            # Primary: Cancel-sibling Apply (most targeted — finds the picker's own Apply)
            # NOTE: try this BEFORE Enter so Enter is only a last resort.
            # The Cancel-sibling approach is tried first; if it fails, fall through to Enter.
            # Strategy: find visible calendar cells, compute their bounding region,
            # then click the Apply button whose horizontal position overlaps that region.
            # This is robust: the picker panel always sits at a fixed location relative
            # to its calendar cells, and the Apply button is ALWAYS in the same panel.
            if not applied:
                apply_result = await page.evaluate("""
                    () => {
                        const isVis = el => {
                            const r = el.getBoundingClientRect();
                            const s = window.getComputedStyle(el);
                            return r.width > 0 && r.height > 0 &&
                                   s.visibility !== 'hidden' && s.display !== 'none';
                        };
                        // Strategy 1: find Apply as the sibling of Cancel.
                        // Cancel + Apply always appear together in the picker footer.
                        const allBtns = Array.from(document.querySelectorAll('button,[role="button"]')).filter(isVis);
                        const cancelBtn = allBtns.find(b => (b.textContent||'').trim().toLowerCase() === 'cancel');
                        if (cancelBtn) {
                            // Check siblings in same parent
                            const siblings = Array.from(cancelBtn.parentElement.querySelectorAll('button,[role="button"]')).filter(isVis);
                            const applyInParent = siblings.find(b => (b.textContent||'').trim().toLowerCase() === 'apply');
                            if (applyInParent) {
                                applyInParent.scrollIntoView({block:'center'});
                                applyInParent.click();
                                return 'Apply (cancel-sibling)';
                            }
                            // Try grandparent
                            const gpSiblings = Array.from(cancelBtn.parentElement.parentElement.querySelectorAll('button,[role="button"]')).filter(isVis);
                            const applyGp = gpSiblings.find(b => (b.textContent||'').trim().toLowerCase() === 'apply');
                            if (applyGp) {
                                applyGp.scrollIntoView({block:'center'});
                                applyGp.click();
                                return 'Apply (cancel-grandparent)';
                            }
                        }

                        // Strategy 2: region-scoped via calendar cells bounding box
                        const cells = Array.from(document.querySelectorAll(
                            '[role="gridcell"], td[aria-label], button[aria-label*="202"]'
                        )).filter(isVis);
                        if (cells.length > 0) {
                            const rects = cells.map(c => c.getBoundingClientRect());
                            const minX = Math.min(...rects.map(r => r.left));
                            const maxX = Math.max(...rects.map(r => r.right));
                            const applyBtns = allBtns.filter(b => {
                                if ((b.textContent||'').trim().toLowerCase() !== 'apply') return false;
                                const r = b.getBoundingClientRect();
                                return r.left >= minX - 50 && r.right <= maxX + 100;
                            });
                            if (applyBtns.length > 0) {
                                applyBtns[0].scrollIntoView({block:'center'});
                                applyBtns[0].click();
                                return 'Apply (region-scoped)';
                            }
                        }
                        return null;
                    }
                """)
                if apply_result:
                    applied = apply_result
                    await page.wait_for_timeout(1500)
                    # Safety guard
                    if "advertising.amazon" not in page.url:
                        log.warning("[%s] Apply navigated away — recovering", account_name)
                        await page.goto(url_before, wait_until="domcontentloaded", timeout=30000)
                        await page.wait_for_load_state("networkidle", timeout=15000)
                        applied = None

            # Last resort: keyboard Enter (if picker still open with focus)
            if not applied:
                try:
                    await page.keyboard.press("Enter")
                    await page.wait_for_timeout(1200)
                    if "advertising.amazon" in page.url:
                        applied = "Enter"
                        log.info("[%s] Applied date via Enter key", account_name)
                except Exception:
                    pass

        if applied:
            log.info("[%s] Applied date picker ('%s')", account_name, applied)
            await page.wait_for_timeout(2500)
        else:
            log.info("[%s] No Apply button found — date may auto-apply", account_name)
            await page.wait_for_timeout(1500)

        # ── Readback: what range does the date button show now? ─────────────────
        # Find the date button in the same toolbar as Export and read its label,
        # so we can confirm the export will reflect the intended single day.
        readback = await page.evaluate("""
            () => {
                const isVis = el => {
                    const r = el.getBoundingClientRect();
                    const s = window.getComputedStyle(el);
                    return r.width>0 && r.height>0 && s.visibility!=='hidden' && s.display!=='none';
                };
                const exportBtn = Array.from(document.querySelectorAll('button,[role="button"],a'))
                    .find(e => isVis(e) && (e.innerText||'').trim().toLowerCase()==='export');
                const isDateTxt = t => /20\\d{2}/.test(t) && /[A-Za-z]/.test(t) && t.length<60;
                if (exportBtn) {
                    let c = exportBtn.parentElement;
                    for (let d=0; d<8 && c; d++) {
                        const b = Array.from(c.querySelectorAll('button,[role="button"]'))
                            .find(x => isVis(x) && x!==exportBtn && isDateTxt((x.innerText||'').trim()));
                        if (b) return (b.innerText||'').trim().slice(0,60);
                        c = c.parentElement;
                    }
                }
                return null;
            }
        """)
        log.info("[%s] ADS DATE READBACK (table picker now shows): %s", account_name, readback)

    except Exception as exc:
        log.warning("[%s] Could not set dashboard date: %s", account_name, exc)


async def _click_export_and_download(page: Page, account_name: str, label: str) -> Optional[Path]:
    """
    Click the Export / Download button on the Campaign Manager dashboard/tab.

    Two modes:
      A) Button is labeled "Download" → direct download (no dropdown).
      B) Button is labeled "Export"   → opens dropdown; click "Download" inside it.

    The Export dropdown has two sections:
      • "Current table data" → "Download"   ← we want this
      • "Reporting"          → "Create report"  ← we do NOT want this

    Returns the downloaded file path, or None.
    """
    try:
        await page.wait_for_load_state("networkidle", timeout=20000)
    except Exception:
        log.info("[%s] Network not fully idle — proceeding with export for %s", account_name, label)
    await page.wait_for_timeout(2000)

    # Find the Export or Download button (visible, short label)
    export_btn = await page.evaluate_handle("""
        () => {
            const isVisible = el => {
                const r = el.getBoundingClientRect();
                const s = window.getComputedStyle(el);
                return r.width > 0 && r.height > 0 &&
                       s.visibility !== 'hidden' && s.display !== 'none';
            };
            return Array.from(document.querySelectorAll('button, a, [role="button"]')).find(e => {
                const t = (e.innerText || e.textContent || e.getAttribute('aria-label') || '')
                           .trim().toLowerCase();
                return isVisible(e) && (
                    t === 'export' || t === 'download' || t === 'bulk download' ||
                    (t.includes('export') && t.length < 25)
                );
            }) || null;
        }
    """)
    btn = export_btn.as_element()
    if not btn:
        log.warning("[%s] No Export/Download button found for %s", account_name, label)
        await _debug_screenshot(page, f"{account_name}_{label}_no_export_btn")
        return None

    btn_text = (await btn.inner_text()).strip()
    log.info("[%s] Found Export button '%s' — clicking for %s", account_name, btn_text, label)

    # ── Mode A: button IS the download trigger (labeled "Download") ──────────────
    # Some tabs (e.g. Products) show a plain "Download" button that directly downloads
    # the current table without opening any dropdown.
    if btn_text.lower() in ("download", "bulk download"):
        try:
            path = await wait_for_download(page, lambda: btn.click(force=True), timeout=60)
            return path
        except Exception as exc:
            log.warning("[%s] Direct download failed for %s: %s", account_name, label, exc)
            return None

    # ── Mode B: button labeled "Export" ──────────────────────────────────────────
    # On some tabs (Products), clicking Export directly downloads without a dropdown.
    # On others (Campaigns), it opens a dropdown where we pick "Download".
    # Strategy: set up download watcher FIRST, click Export, then handle either outcome.

    DROPDOWN_SELECTORS = (
        '[role="menuitem"]:has-text("Download"):not(:has-text("Create")),'
        'li:has-text("Download"):not(:has-text("Create")),'
        'a:has-text("Download"):not(:has-text("Create")),'
        'button:has-text("Download"):not(:has-text("Create"))'
    )

    # Attempt: wrap the Export click in a short-timeout download watcher.
    # If Export directly triggers a download → we catch it immediately.
    # If Export opens a dropdown → the watcher times out (3 s) and we fall through.
    try:
        path = await wait_for_download(page, lambda: btn.click(force=True), timeout=3)
        log.info("[%s] Export button triggered direct download for %s", account_name, label)
        return path
    except Exception:
        pass  # No direct download — button opened a dropdown (or click failed)

    # The Export click either opened a dropdown (which is now open) or didn't register.
    # Give the dropdown time to finish rendering.
    await page.wait_for_timeout(3000)
    await _debug_screenshot(page, f"{account_name}_{label}_after_export_click")

    # Try Playwright's text-based locator (fast path)
    dl_el = None
    try:
        dl_locator = page.locator(DROPDOWN_SELECTORS).first
        await dl_locator.wait_for(state="visible", timeout=6000)
        dl_el = dl_locator
    except Exception:
        pass

    # JS fallback — catches elements Playwright's CSS selectors miss
    if dl_el is None:
        dl_handle = await page.evaluate_handle("""
            () => {
                const isVisible = el => {
                    const r = el.getBoundingClientRect();
                    const s = window.getComputedStyle(el);
                    return r.width > 0 && r.height > 0 &&
                           s.visibility !== 'hidden' && s.display !== 'none';
                };
                const candidates = Array.from(document.querySelectorAll(
                    '[role="menuitem"], [role="option"], li, a, button, span, div'
                )).filter(isVisible);

                // Exact "Download" (not "Create report")
                const exact = candidates.find(e => {
                    const t = (e.innerText || e.textContent || '').trim().toLowerCase();
                    return t === 'download';
                });
                if (exact) return exact;

                // Partial match — contains "download", not "create", short text
                return candidates.find(e => {
                    const t = (e.innerText || e.textContent || '').trim().toLowerCase();
                    return t.includes('download') && !t.includes('create') && t.length < 30;
                }) || null;
            }
        """)
        raw_el = dl_handle.as_element()
        if raw_el:
            dl_el = raw_el

    if dl_el is None:
        # If dropdown may have not reopened after the first failed-direct-download click,
        # try clicking Export one more time to reopen it
        log.info("[%s] Retrying Export click to open dropdown for %s", account_name, label)
        try:
            await btn.click(force=True)
        except Exception:
            await btn.evaluate("el => el.click()")
        await page.wait_for_timeout(3500)

        try:
            dl_locator2 = page.locator(DROPDOWN_SELECTORS).first
            await dl_locator2.wait_for(state="visible", timeout=6000)
            dl_el = dl_locator2
        except Exception:
            pass

    if dl_el is None:
        visible_items = await page.evaluate("""
            () => {
                const isVisible = el => {
                    const r = el.getBoundingClientRect();
                    return r.width > 0 && r.height > 0;
                };
                return Array.from(document.querySelectorAll(
                    '[role="menuitem"], [role="option"], [role="menu"] *, [class*="dropdown"] *'
                )).filter(isVisible).map(e =>
                    (e.innerText || e.textContent || '').trim().slice(0, 60)
                ).filter(t => t.length > 0);
            }
        """)
        log.warning("[%s] 'Download' option not found in dropdown for %s — visible: %s",
                    account_name, label, visible_items)
        return None

    # Click the Download item
    try:
        dl_text_raw = await dl_el.inner_text()
    except Exception:
        dl_text_raw = "Download"
    log.info("[%s] Clicking dropdown item '%s' for %s", account_name, dl_text_raw.strip(), label)
    try:
        path = await wait_for_download(page, lambda d=dl_el: d.click(force=True), timeout=60)
        return path
    except Exception as exc:
        log.warning("[%s] Download click error for %s: %s", account_name, label, exc)
        return None


async def _get_entity_id(page: Page) -> Optional[str]:
    """Extract entityId from the current page URL or any link on the page."""
    # Check page URL first
    if "entityId=" in page.url:
        return page.url.split("entityId=")[1].split("&")[0]
    # Scan all links on the page for entityId
    entity_id = await page.evaluate("""
        () => {
            for (const a of document.querySelectorAll('a')) {
                const m = (a.href || '').match(/entityId=([A-Z0-9]+)/);
                if (m) return m[1];
            }
            // Also check inline scripts / data attributes
            const m2 = document.body.innerHTML.match(/entityId[=:]["']?([A-Z0-9]{15,})/);
            if (m2) return m2[1];
            return null;
        }
    """)
    return entity_id


async def _navigate_to_products_tab(
    page: Page,
    account_name: str,
    entity_id: Optional[str] = None,
    base_url: str = None,
) -> bool:
    """
    Navigate to the Advertised Products tab in Campaign Manager.

    Strategy (in order):
    1. Direct URL navigation using entityId — fastest and most reliable.
    2. Click the Products nav item in the sidebar — fallback for SPA navigation.
    """
    if not base_url:
        base_url = ADS_BASE_URL

    try:
        # ── Strategy 1: Direct URL with entityId ─────────────────────────────────
        if entity_id:
            products_url = f"{base_url}/cm/products?entityId={entity_id}&view=advertised"
            log.info("[%s] Navigating directly to Products URL (entityId=%s)", account_name, entity_id)
            await page.goto(products_url, wait_until="domcontentloaded", timeout=30000)
            try:
                await page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass  # Continue even if background calls never settle
            await page.wait_for_timeout(3000)
            log.info("[%s] Products URL after navigation: %s", account_name, page.url)
            if "advertising.amazon" in page.url and "404" not in page.url:
                return True

        # ── Strategy 2: Click the Products nav element ────────────────────────────
        log.info("[%s] Falling back to Products nav click", account_name)
        result = await page.evaluate("""
            () => {
                const all = Array.from(document.querySelectorAll('*'));
                const productsEls = all.filter(e => {
                    const t = (e.innerText || '').trim();
                    const r = e.getBoundingClientRect();
                    return t === 'Products' && r.width > 0 && r.height > 0;
                });
                if (!productsEls.length) return {found: false};
                const innermost = productsEls.find(e =>
                    !productsEls.some(other => other !== e && e.contains(other))
                ) || productsEls[productsEls.length - 1];
                let clickTarget = innermost;
                let cur = innermost.parentElement;
                const CLICKABLE_TAGS = new Set(['A', 'BUTTON']);
                const CLICKABLE_ROLES = new Set(['button', 'link', 'menuitem', 'tab']);
                while (cur && cur !== document.body) {
                    if (CLICKABLE_TAGS.has(cur.tagName) ||
                        CLICKABLE_ROLES.has((cur.getAttribute('role') || '').toLowerCase())) {
                        clickTarget = cur; break;
                    }
                    cur = cur.parentElement;
                }
                clickTarget.scrollIntoView({block: 'center'});
                clickTarget.click();
                return {
                    found: true,
                    clicked: clickTarget.tagName + '|' + (clickTarget.className || '').slice(0, 50),
                    href: clickTarget.href || clickTarget.getAttribute('href') || '',
                };
            }
        """)

        if result.get("found"):
            log.info("[%s] Clicked Products element: %s (href: %s)",
                     account_name, result.get("clicked", "?"), result.get("href", ""))
            try:
                await page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass
            await page.wait_for_timeout(3000)
            log.info("[%s] Products URL after click: %s", account_name, page.url)
            if "advertising.amazon" in page.url and "404" not in page.url:
                return True

        log.warning("[%s] Products tab navigation failed", account_name)
        return False

    except Exception as exc:
        log.error("[%s] Products navigation error: %s", account_name, exc)
        return False


_PAG_TEXT_JS = """
    () => {
        const els = Array.from(document.querySelectorAll('*'));
        for (const el of els) {
            const t = (el.innerText || '').trim();
            if (/\\d+[-–]\\d+\\s+of\\s+\\d+/i.test(t) && el.children.length === 0)
                return t;
        }
        return null;
    }
"""

_CLICK_NEXT_JS = """
    () => {
        const isEnabled = b =>
            !b.disabled &&
            b.getAttribute('aria-disabled') !== 'true' &&
            !b.classList.toString().toLowerCase().includes('disabled') &&
            b.getBoundingClientRect().width > 0;

        // ── Guard: stop if already on the last page ─────────────────────────
        const countEl = Array.from(document.querySelectorAll('*')).find(e => {
            const t = (e.innerText || '').trim();
            return /\\d+[-–]\\d+\\s+of\\s+\\d+/i.test(t) && e.children.length === 0;
        });
        if (countEl) {
            const m = (countEl.innerText || '').match(/(\\d+)[-–](\\d+)\\s+of\\s+(\\d+)/i);
            if (m && parseInt(m[2]) >= parseInt(m[3])) return null;
        }

        // ── Strategy 1: aria-label "Next" / "Next page" ──────────────────────
        const byLabel = Array.from(document.querySelectorAll(
            'button, a, [role="button"]'
        )).find(b => {
            const lbl = (b.getAttribute('aria-label') || '').toLowerCase();
            return isEnabled(b) && (lbl === 'next' || lbl === 'next page' ||
                   lbl.includes('go to next'));
        });
        if (byLabel) { byLabel.click(); return 'aria-label'; }

        // ── Strategy 2: rightmost enabled button near the "X-Y of Z" text ───
        if (countEl) {
            let parent = countEl.parentElement;
            for (let i = 0; i < 6 && parent; i++) {
                const btns = Array.from(parent.querySelectorAll(
                    'button, a, [role="button"]'
                )).filter(b => {
                    if (!isEnabled(b)) return false;
                    const t = (b.getAttribute('aria-label') || b.innerText || '').trim().toLowerCase();
                    return !t.includes('per page') && !t.includes('previous') && !t.includes('prev');
                });
                if (btns.length) { btns[btns.length - 1].click(); return 'rightmost-sibling'; }
                parent = parent.parentElement;
            }
        }
        return null;
    }
"""


async def _read_csv_raw(path: "Path") -> "Optional[pd.DataFrame]":
    """Read a CSV/Excel file into a DataFrame without adding Account/Date columns."""
    try:
        df = pd.read_csv(path, encoding="utf-8-sig")
    except Exception:
        try:
            df = pd.read_excel(path)
        except Exception:
            return None
    df.columns = df.columns.str.strip()
    return df if not df.empty else None


async def _download_all_pages(
    page: Page, account_name: str, label: str
) -> "list[pd.DataFrame]":
    """
    Export every page of the current table and return a list of DataFrames.

    The Campaign Manager export is page-limited (returns only the visible page).
    This function downloads page 1, navigates to page 2, downloads, … until the
    last page, then returns all DataFrames for concatenation.
    """
    import re as _re
    import shutil as _shutil

    all_dfs: list = []
    page_num = 0

    while True:
        page_num += 1

        # Grab current pagination summary
        pag_text = await page.evaluate(_PAG_TEXT_JS)
        log.info("[%s] %s page %d: %s", account_name, label, page_num, pag_text)

        # Download this page
        path = await _click_export_and_download(page, account_name, label)
        if path:
            df_page = await _read_csv_raw(path)
            if df_page is not None:
                all_dfs.append(df_page)
            # Rename so the next page download doesn't overwrite
            try:
                _shutil.copy2(str(path), str(path.parent / f"__p{page_num}_{path.name}"))
            except Exception:
                pass

        # Check if this was the last page
        is_last = True
        if pag_text:
            m = _re.search(r"(\d+)[-–](\d+)\s+of\s+(\d+)", pag_text)
            if m:
                end_row, total = int(m.group(2)), int(m.group(3))
                is_last = end_row >= total

        if is_last:
            log.info("[%s] %s: All pages downloaded (%d pages)", account_name, label, page_num)
            break

        if page_num >= 15:
            log.warning("[%s] %s: Safety cap at 15 pages", account_name, label)
            break

        # Navigate to the next page
        next_clicked = await page.evaluate(_CLICK_NEXT_JS)
        if not next_clicked:
            log.info("[%s] %s: Next button not found — stopping", account_name, label)
            break

        log.info("[%s] %s: → page %d (%s)", account_name, label, page_num + 1, next_clicked)
        try:
            await page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass
        await page.wait_for_timeout(1500)

    return all_dfs


# ── Campaign Report ─────────────────────────────────────────────────────────────

async def _sort_by_spends(page: Page, account_name: str):
    """Click the Spends / Spend column header to sort by highest to lowest."""
    try:
        await page.wait_for_timeout(800)
        # Scroll right to ensure all columns are reachable, then find the Spend column
        clicked = await page.evaluate("""
            () => {
                const isVis = el => {
                    const r = el.getBoundingClientRect();
                    return r.width > 0 && r.height > 0;
                };
                // Match th / [role=columnheader] / button whose TEXT is exactly or contains "Spend"
                const headers = Array.from(document.querySelectorAll(
                    'th, [role="columnheader"], thead td'
                ));
                let target = null;
                for (const h of headers) {
                    const t = (h.innerText || h.textContent || '').trim().toLowerCase();
                    if (t === 'spend' || t === 'spends' || t === 'total spend' ||
                        t === 'spend (inr)' || t === 'spend (usd)') {
                        target = h;
                        break;
                    }
                }
                // Broader match: contains "spend" but not "spend goal" etc
                if (!target) {
                    target = headers.find(h => {
                        const t = (h.innerText || h.textContent || '').trim().toLowerCase();
                        return t.includes('spend') && t.length < 20;
                    });
                }
                // Also try buttons inside headers (sortable column buttons)
                if (!target) {
                    const btns = Array.from(document.querySelectorAll(
                        'th button, [role="columnheader"] button'
                    ));
                    target = btns.find(b => {
                        const t = (b.innerText || b.textContent || '').trim().toLowerCase();
                        return t.includes('spend') && t.length < 20;
                    });
                }
                if (!target) return null;
                target.scrollIntoView({block: 'center', inline: 'center'});
                target.click();
                return (target.innerText || target.textContent || '').trim().slice(0, 30);
            }
        """)
        if clicked:
            await page.wait_for_timeout(1200)
            log.info("[%s] Clicked Spends column header ('%s') to sort", account_name, clicked)
        else:
            log.warning("[%s] Could not find Spends column header", account_name)
    except Exception as exc:
        log.warning("[%s] Error sorting by Spends: %s", account_name, exc)


async def _set_max_results_per_page(page: Page, account_name: str):
    """Find and click the 'Results per page' dropdown and select the highest number."""
    try:
        await page.wait_for_timeout(800)
        # Use JS to find a VISIBLE results-per-page dropdown only
        clicked = await page.evaluate("""
            () => {
                const isVis = el => {
                    const r = el.getBoundingClientRect();
                    const s = window.getComputedStyle(el);
                    return r.width > 0 && r.height > 0 &&
                           s.visibility !== 'hidden' && s.display !== 'none';
                };
                // Look for buttons/selects near "per page" or "results per page" text
                const all = Array.from(document.querySelectorAll('button, select, [role="combobox"], [role="listbox"]'));
                const rpp = all.find(e => {
                    const t = (e.textContent || e.innerText || '').toLowerCase();
                    return isVis(e) && (t.includes('per page') || t.includes('results per page'));
                });
                if (rpp) { rpp.scrollIntoView({block: 'center'}); rpp.click(); return true; }
                return false;
            }
        """)
        if not clicked:
            log.warning("[%s] Could not find 'Results per page' dropdown", account_name)
            return

        await page.wait_for_timeout(800)
        # Click the last (highest) option
        option_clicked = await page.evaluate("""
            () => {
                const isVis = el => {
                    const r = el.getBoundingClientRect();
                    return r.width > 0 && r.height > 0;
                };
                const opts = Array.from(document.querySelectorAll(
                    'option, [role="option"], [role="menuitem"], li'
                )).filter(isVis);
                if (opts.length) {
                    // Find the option with the largest number
                    let best = null, bestNum = -1;
                    for (const o of opts) {
                        const t = (o.textContent || o.innerText || '').trim();
                        const n = parseInt(t, 10);
                        if (!isNaN(n) && n > bestNum) { bestNum = n; best = o; }
                    }
                    if (best) { best.click(); return bestNum; }
                    opts[opts.length - 1].click();
                    return true;
                }
                return false;
            }
        """)
        if option_clicked:
            await page.wait_for_timeout(1000)
            log.info("[%s] Set max results per page (%s)", account_name, option_clicked)
        else:
            log.warning("[%s] Could not find page size options", account_name)
    except Exception as exc:
        log.warning("[%s] Error setting max results per page: %s", account_name, exc)


_MARKETPLACE_COUNTRY = {
    "US": ["united states", "usa"],
    "CA": ["canada"],
    "IN": ["india"],
    "UK": ["united kingdom"],
    "DE": ["germany"],
    "FR": ["france"],
    "JP": ["japan"],
    "AU": ["australia"],
    "AE": ["united arab emirates"],
    "MX": ["mexico"],
    "SG": ["singapore"],
}


async def _handle_choose_account_page(page: Page, account_name: str, marketplace: str):
    """
    If Amazon shows a 'Choose an account' / marketplace picker after switching entities
    (URL contains 'choose-account'), click the country matching the account's marketplace.
    """
    if "choose-account" not in page.url:
        return

    log.info("[%s] 'Choose an account' page detected — selecting marketplace '%s'", account_name, marketplace)
    targets = _MARKETPLACE_COUNTRY.get(marketplace.upper(), [marketplace.lower()])

    clicked = await page.evaluate(f"""
        () => {{
            const targets = {targets};
            const isVis = el => {{
                const r = el.getBoundingClientRect();
                return r.width > 0 && r.height > 0;
            }};
            // Find any clickable element whose text matches the target country
            const all = Array.from(document.querySelectorAll('a, button, li, td, [role="row"], [role="option"], div, span'));
            for (const t of targets) {{
                const el = all.find(e => {{
                    if (!isVis(e)) return false;
                    const txt = (e.innerText || e.textContent || '').trim().toLowerCase();
                    return txt === t;
                }});
                if (el) {{
                    el.scrollIntoView({{block: 'center'}});
                    el.click();
                    return (el.innerText || el.textContent || '').trim();
                }}
            }}
            return null;
        }}
    """)

    if clicked:
        log.info("[%s] Clicked marketplace: '%s'", account_name, clicked)
        try:
            await page.wait_for_url("**/campaign-manager**", timeout=15000)
        except Exception:
            pass
        try:
            await page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass
        await page.wait_for_timeout(2000)
    else:
        log.warning("[%s] Could not find marketplace '%s' on choose-account page", account_name, marketplace)


async def download_campaign_report(page: Page, account: dict, account_name: str,
                                   target_date: "date" = None) -> Optional[pd.DataFrame]:
    """
    Campaigns tab → navigate to entityId URL → set date → Export → Download.
    target_date defaults to yesterday when None.
    """
    from src.utils import resolve_report_date
    report_date = resolve_report_date(target_date)
    log.info("[%s] Fetching Campaign Performance report for %s...", account_name, report_date.isoformat())
    try:
        marketplace = account.get("marketplace", "IN")
        base_url, campaign_manager_url = _ads_urls(account)

        # ── Get entityId so we can navigate to the right URL ─────────────────────
        entity_id = await _get_entity_id(page)
        log.info("[%s] entityId for Campaign: %s", account_name, entity_id)

        # ── Navigate to the entityId-specific campaigns URL ───────────────────────
        if entity_id:
            campaign_url = f"{base_url}/cm/campaigns?entityId={entity_id}"
            log.info("[%s] Navigating to entity-specific campaign URL", account_name)
            await page.goto(campaign_url, wait_until="domcontentloaded", timeout=30000)
        else:
            current = page.url
            if "advertising.amazon" not in current:
                await page.goto(campaign_manager_url, wait_until="domcontentloaded", timeout=30000)

        # Wait for page to settle — catch networkidle timeout gracefully
        try:
            await page.wait_for_load_state("networkidle", timeout=20000)
        except Exception:
            log.info("[%s] Network not idle — continuing with campaign report", account_name)
        await page.wait_for_timeout(3000)

        # ── Handle marketplace chooser if we somehow end up on the picker ────────
        await _handle_choose_account_page(page, account_name, marketplace)

        await _debug_screenshot(page, f"{account_name}_campaign_dashboard")

        # ── Set the report date ───────────────────────────────────────────────────
        await _set_dashboard_date_yesterday(page, account_name, report_date)
        await _debug_screenshot(page, f"{account_name}_campaign_after_date")

        # ── Sort by Spends and set max results per page ───────────────────────────
        await _sort_by_spends(page, account_name)
        await _set_max_results_per_page(page, account_name)

        # ── Export all pages → combine ────────────────────────────────────────────
        all_dfs = await _download_all_pages(page, account_name, "Campaign")
        if all_dfs:
            combined = pd.concat(all_dfs, ignore_index=True).drop_duplicates()
            combined.insert(0, "Account", account_name)
            combined.insert(1, "Report Date", report_date.isoformat())
            log.info("[%s] Campaign rows (all pages): %d", account_name, len(combined))
            return combined

        log.warning("[%s] Campaign report could not be downloaded", account_name)
        return None

    except Exception as exc:
        log.error("[%s] Campaign report error: %s", account_name, exc)
        return None


# ── Advertised Products Report ──────────────────────────────────────────────────

async def download_advertised_products_report(page: Page, account: dict, account_name: str,
                                              target_date: "date" = None) -> Optional[pd.DataFrame]:
    """
    Products tab → set date → go to last page → Export → Download.
    target_date defaults to yesterday when None.
    """
    from src.utils import resolve_report_date
    report_date = resolve_report_date(target_date)
    log.info("[%s] Fetching Advertised Products report for %s...", account_name, report_date.isoformat())
    try:
        marketplace = account.get("marketplace", "IN")
        base_url, campaign_manager_url = _ads_urls(account)

        # Stay on the current Campaign Manager page to preserve the active entity.
        # Re-navigating to /campaign-manager would reset the entity back to the default.
        # Only navigate if we've somehow left the Ads domain entirely.
        current = page.url
        if "advertising.amazon" not in current:
            await page.goto(campaign_manager_url, wait_until="domcontentloaded", timeout=30000)
            try:
                await page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass
            await page.wait_for_timeout(3000)
        else:
            log.info("[%s] Staying on current Ads page: %s", account_name, page.url)

        # ── Handle marketplace chooser if still on choose-account page ───────────
        await _handle_choose_account_page(page, account_name, marketplace)

        # Let the page load its nav links (they appear after auth/entity resolution)
        entity_id = await _get_entity_id(page)
        log.info("[%s] entityId extracted for Products: %s", account_name, entity_id)

        # Navigate to Products tab (passes entityId for direct URL navigation)
        reached = await _navigate_to_products_tab(page, account_name, entity_id=entity_id, base_url=base_url)
        if not reached:
            log.warning("[%s] Could not reach Products tab", account_name)
            return None

        await _debug_screenshot(page, f"{account_name}_products_tab")

        # Clear any active search/filter on the Products table
        # (a leftover filter from a previous entity's session could limit exported rows)
        # Strategy: find visible search inputs with values and clear them via keyboard + × button
        search_inputs = await page.query_selector_all(
            'input[type="text"], input[type="search"], input:not([type])'
        )
        cleared_filter = 0
        for si in search_inputs:
            try:
                bb = await si.bounding_box()
                if not bb or bb["width"] == 0:
                    continue
                val = await si.input_value()
                if not val.strip():
                    continue
                # First try clicking the × (clear) button next to the input
                cleared_via_btn = await page.evaluate("""
                    (inp) => {
                        const isVis = el => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
                        // Search up to 5 levels of ancestors for a clear button
                        let parent = inp.parentElement;
                        for (let i = 0; i < 5 && parent; i++) {
                            const btn = Array.from(parent.querySelectorAll('button, [role="button"], span, svg'))
                                .find(b => {
                                    if (!isVis(b)) return false;
                                    const t = (b.innerText || b.textContent || b.getAttribute('aria-label') || '').trim();
                                    return t === '×' || t === '✕' || t === 'x' || t === 'X' ||
                                           t.toLowerCase() === 'clear' ||
                                           (b.getAttribute('aria-label') || '').toLowerCase().includes('clear');
                                });
                            if (btn) { btn.click(); return true; }
                            parent = parent.parentElement;
                        }
                        return false;
                    }
                """, si)
                if cleared_via_btn:
                    cleared_filter += 1
                    await page.wait_for_timeout(800)
                    continue
                # Fallback: use keyboard to select-all + delete
                await si.click()
                await page.keyboard.press("Control+a")
                await page.keyboard.press("Backspace")
                await page.wait_for_timeout(500)
                await page.keyboard.press("Escape")
                cleared_filter += 1
                await page.wait_for_timeout(800)
            except Exception:
                pass
        if cleared_filter:
            log.info("[%s] Cleared %d active search filter(s) on Products page", account_name, cleared_filter)
            await page.wait_for_timeout(2000)  # wait for table to re-render with all products

        # Set the report date
        await _set_dashboard_date_yesterday(page, account_name, report_date)
        await _debug_screenshot(page, f"{account_name}_products_after_date")

        # Sort by Spends (highest to lowest)
        await _sort_by_spends(page, account_name)

        # Set max results per page
        await _set_max_results_per_page(page, account_name)
        await page.wait_for_timeout(1000)
        await _debug_screenshot(page, f"{account_name}_products_ready")

        # ── Export all pages → combine ────────────────────────────────────────────
        all_dfs = await _download_all_pages(page, account_name, "Products")
        if all_dfs:
            combined = pd.concat(all_dfs, ignore_index=True).drop_duplicates()
            combined.insert(0, "Account", account_name)
            combined.insert(1, "Report Date", report_date.isoformat())
            log.info("[%s] Advertised Products rows (all pages): %d", account_name, len(combined))
            return combined

        log.warning("[%s] Advertised Products report could not be downloaded", account_name)
        return None

    except Exception as exc:
        log.error("[%s] Advertised Products report error: %s", account_name, exc)
        return None


# ── CSV parser ─────────────────────────────────────────────────────────────────

def _parse_ads_csv(path: Path, account_name: str, label: str) -> pd.DataFrame:
    try:
        df = pd.read_csv(path, encoding="utf-8-sig")
    except Exception:
        try:
            df = pd.read_excel(path)
        except Exception as exc:
            log.error("[%s] Could not parse %s file %s: %s", account_name, label, path.name, exc)
            return pd.DataFrame()
    df.columns = df.columns.str.strip()
    df.insert(0, "Account", account_name)
    df.insert(1, "Report Date", yesterday())
    log.info("[%s] %s rows: %d", account_name, label, len(df))
    return df
