"""
Authentication helpers for Amazon Seller Central and Amazon Ads.

Login is done ONCE per browser session.
Account switching (between seller / ads profiles) is done per-account iteration.
"""

import json
import logging
from pathlib import Path
from typing import Optional

from playwright.async_api import Page, BrowserContext

from src.config import SESSIONS_DIR, DOWNLOADS_DIR, SC_SWITCHER_URL, SC_BASE_URL

log = logging.getLogger(__name__)


# ── Session persistence ───────────────────────────────────────────────────────

def _session_path(portal: str) -> Path:
    return SESSIONS_DIR / f"{portal}_session.json"


async def restore_session(context: BrowserContext, portal: str) -> bool:
    path = _session_path(portal)
    if not path.exists():
        return False
    cookies = json.loads(path.read_text())
    await context.add_cookies(cookies)
    log.info("Restored %s session from cache", portal)
    return True


async def save_session(context: BrowserContext, portal: str):
    SESSIONS_DIR.mkdir(exist_ok=True)
    cookies = await context.cookies()
    path = _session_path(portal)
    path.write_text(json.dumps(cookies))
    log.info("Saved %s session (%d cookies)", portal, len(cookies))


def clear_session(portal: str):
    path = _session_path(portal)
    if path.exists():
        path.unlink()
        log.info("Cleared %s session", portal)


# ── OTP / 2FA ─────────────────────────────────────────────────────────────────

async def _handle_otp(page: Page, label: str = ""):
    """
    OTP/2FA detected.

    Bot mode  → ask the user via Telegram, receive code, type it in.
    Manual mode → wait for the user to complete it in the visible browser.
    """
    # ── Bot mode: handle OTP via Telegram ────────────────────────────────────
    try:
        import src.bot_context as bot_ctx
        if bot_ctx.is_bot_mode():
            log.info("OTP required for %s — requesting via Telegram", label or "login")
            code = await bot_ctx.request_otp(label or "login")
            if code:
                # Find and fill the OTP input field
                otp_input = None
                for sel in [
                    'input[name="otpCode"]',
                    'input[id*="cvf"]',
                    'input[id*="otp"]',
                    'input[autocomplete="one-time-code"]',
                    'input[inputmode="numeric"]',
                    'input[type="tel"]',
                    'input[type="text"]',
                ]:
                    try:
                        await page.wait_for_selector(sel, timeout=3000, state="visible")
                        otp_input = sel
                        break
                    except Exception:
                        continue

                if otp_input:
                    await page.fill(otp_input, code)
                    # Submit the OTP form
                    submitted = False
                    for submit_sel in [
                        'input[type="submit"]',
                        'button[type="submit"]',
                        'button:has-text("Sign in")',
                        'button:has-text("Submit")',
                        'button:has-text("Verify")',
                        'button:has-text("Continue")',
                    ]:
                        try:
                            btn = await page.query_selector(submit_sel)
                            if btn:
                                await btn.click()
                                submitted = True
                                break
                        except Exception:
                            continue
                    if not submitted:
                        await page.keyboard.press("Enter")

                    try:
                        await page.wait_for_load_state("networkidle", timeout=20000)
                    except Exception:
                        pass
                    log.info("OTP submitted via Telegram — continuing")
                    await bot_ctx.send(f"✅ OTP entered for `{label}` — continuing...")
                else:
                    log.warning("OTP input field not found after receiving code")
                    await bot_ctx.send(
                        f"⚠️ Could not find OTP input for `{label}`. "
                        "Please log in manually at your Mac."
                    )
            return
    except (ImportError, AttributeError):
        pass

    # ── Manual mode: wait for user to complete in visible browser ────────────
    log.info(
        "⚠️  OTP / 2FA required%s — please complete it in the browser window. "
        "Waiting up to 3 minutes...",
        f" for {label}" if label else "",
    )
    try:
        await page.wait_for_url(
            lambda url: not any(
                x in url
                for x in ("ap/cvf", "ap/mfa", "ap/signin", "ap/twostep", "ap/challenge")
            ),
            timeout=180_000,
        )
        await page.wait_for_load_state("networkidle", timeout=15000)
        log.info("OTP completed — continuing.")
    except Exception as exc:
        log.error("OTP wait timed out or failed: %s", exc)


def _is_auth_page(url: str) -> bool:
    return any(x in url for x in ("ap/signin", "ap/cvf", "ap/mfa", "ap/twostep", "ap/challenge"))


# ── Seller Central login ──────────────────────────────────────────────────────

async def login_seller_central(page: Page, email: str, password: str, base_url: str = None) -> bool:
    if not base_url:
        base_url = SC_BASE_URL
    log.info("Logging into Seller Central (%s)...", base_url)
    await page.goto(f"{base_url}/gp/homepage.html", wait_until="domcontentloaded", timeout=30000)
    try:
        await page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass

    # Already past login → on dashboard
    if "sellercentral.amazon" in page.url and not _is_auth_page(page.url) and "account-switcher" not in page.url:
        if await page.query_selector("#sc-navbar-container, #merchant-header"):
            log.info("Already logged in to Seller Central")
            return True

    if not _is_auth_page(page.url) and "signin" not in page.url.lower():
        log.info("SC login page not detected — may already be logged in")
        return True

    try:
        DOWNLOADS_DIR.mkdir(exist_ok=True)
        await page.screenshot(path=str(DOWNLOADS_DIR / "debug_sc_login_page.png"), full_page=True)
        log.info("SC login page URL: %s", page.url)

        # ── Step 1: Email field ───────────────────────────────────────────────────
        # Use page.fill (re-queries the selector each time — avoids stale element errors)
        email_selector = None
        for sel in ["#ap_email", 'input[type="email"]', 'input[name="email"]', 'input[id*="email"]']:
            try:
                await page.wait_for_selector(sel, timeout=8000, state="visible")
                email_selector = sel
                break
            except Exception:
                continue

        if not email_selector:
            # No email field found — give the user up to 3 minutes to log in manually
            log.warning("SC login: email field not found — waiting up to 3 min for manual login")
            await page.screenshot(path=str(DOWNLOADS_DIR / "debug_sc_login_manual.png"), full_page=True)
            try:
                await page.wait_for_url(
                    lambda url: "sellercentral.amazon" in url and not _is_auth_page(url),
                    timeout=180_000,
                )
                log.info("SC manual login completed")
                return True
            except Exception:
                log.error("SC manual login timed out")
                return False

        await page.fill(email_selector, email)
        log.info("SC login: filled email field (%s)", email_selector)

        # Click Continue if present
        try:
            await page.wait_for_selector("#continue", timeout=3000, state="visible")
            await page.click("#continue")
            try:
                await page.wait_for_load_state("networkidle", timeout=12000)
            except Exception:
                pass
        except Exception:
            # No Continue button — might submit on Enter or go straight to password
            await page.keyboard.press("Enter")
            try:
                await page.wait_for_load_state("networkidle", timeout=12000)
            except Exception:
                pass

        # ── Step 2: Password field ────────────────────────────────────────────────
        try:
            await page.wait_for_selector("#ap_password", timeout=15000, state="visible")
            await page.fill("#ap_password", password)
            await page.click("#signInSubmit")
            try:
                await page.wait_for_load_state("networkidle", timeout=20000)
            except Exception:
                pass
        except Exception:
            log.warning("SC login: password field not found — may be on OTP page")

        # ── Step 3: Handle OTP / 2FA ──────────────────────────────────────────────
        if _is_auth_page(page.url):
            await _handle_otp(page, "Seller Central")

        if "sellercentral.amazon" in page.url:
            log.info("Seller Central login successful")
            return True

        log.error("SC login failed — landed on: %s", page.url)
        return False

    except Exception as exc:
        log.error("SC login error: %s", exc)
        return False


# ── SC account switching ──────────────────────────────────────────────────────

_SC_MARKETPLACE_TERMS = {
    "IN":  ["amazon.in",  "india",         "(in)",  " in "],
    "US":  ["amazon.com", "united states",  "(us)",  " us "],
    "CA":  ["amazon.ca",  "canada",         "(ca)",  " ca "],
    "UK":  ["amazon.co.uk","united kingdom","(gb)",  " gb "],
    "DE":  ["amazon.de",  "germany",        "(de)",  " de "],
    "FR":  ["amazon.fr",  "france",         "(fr)",  " fr "],
    "JP":  ["amazon.co.jp","japan",         "(jp)",  " jp "],
    "AU":  ["amazon.com.au","australia",    "(au)",  " au "],
    "AE":  ["amazon.ae",  "united arab",    "(ae)",  " ae "],
    "MX":  ["amazon.com.mx","mexico",       "(mx)",  " mx "],
    "SG":  ["amazon.sg",  "singapore",      "(sg)",  " sg "],
}


async def switch_sc_account(
    page: Page,
    sc_account_name: str,
    sc_parent: Optional[str],
    brand_name: str,
    marketplace: str = "IN",
    sc_merchant_id: Optional[str] = None,
    sc_marketplace_id: Optional[str] = None,
    sc_paid_id: Optional[str] = None,
    sc_dc: Optional[str] = None,
):
    """
    Switch to the given seller account.

    Fast path: if sc_merchant_id + sc_marketplace_id + sc_paid_id are provided
    (captured once via `python main.py --capture-ids`), navigate DIRECTLY to
    the homepage URL with those IDs as query params — Amazon honours this and
    activates the account without going through the buggy switcher UI.

    Slow path (fallback when no IDs are cached): use Amazon's account-switcher
    UI. This works for some accounts but silently no-ops for others; the
    contamination guard in main.py catches the no-ops.

    Returns the merchant ID (mons_sel_dir_mcid) on success, or None on failure.
    """
    from src.config import SC_BASE_URL_US
    sc_base = SC_BASE_URL_US if marketplace.upper() == "US" else SC_BASE_URL

    # ── FAST PATH: direct URL navigation with cached IDs ──────────────────────
    if sc_merchant_id and sc_marketplace_id and sc_paid_id:
        from urllib.parse import urlencode
        params = {
            "mons_sel_mkid":     sc_marketplace_id,
            "mons_sel_dir_mcid": sc_merchant_id,
            "mons_sel_dir_paid": sc_paid_id,
        }
        if sc_dc:
            params["mons_sel_dc"] = sc_dc
        params["ignore_selection_changed"] = "true"
        url = f"{sc_base}/gp/homepage.html?" + urlencode(params)
        log.info("[%s] SC fast-path: direct navigation with cached IDs", brand_name)
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_load_state("networkidle", timeout=20000)
            await page.wait_for_timeout(1500)
        except Exception as exc:
            log.warning("[%s] Fast-path nav failed: %s — falling back to UI flow", brand_name, exc)
        else:
            final = page.url
            log.info("[%s] Fast-path final URL: %s", brand_name, final)
            if "account-switcher" not in final and not _is_auth_page(final):
                # Confirm the active mcid matches what we asked for
                import re as _re
                m = _re.search(r"mons_sel_dir_mcid=([^&]+)", final)
                active = m.group(1) if m else None
                if active == sc_merchant_id:
                    log.info("[%s] SC account active (fast-path) — mcid=%s", brand_name, active)
                    return active
                else:
                    log.warning("[%s] Fast-path landed on wrong mcid (got %s, expected %s) — falling back to UI",
                                brand_name, active, sc_merchant_id)
            else:
                log.warning("[%s] Fast-path bounced to %s — falling back to UI", brand_name, final)

    log.info("[%s] Switching SC account to '%s' (SC portal: %s)...", brand_name, sc_account_name, sc_base)

    # Navigate to SC homepage — typically redirects to account selector if multiple accounts
    await page.goto(f"{sc_base}/gp/homepage.html", wait_until="domcontentloaded", timeout=30000)
    await page.wait_for_load_state("networkidle", timeout=20000)
    await page.wait_for_timeout(2000)

    # If not on the switcher, navigate directly to the known switcher URL
    if "account-switcher" not in page.url:
        log.info("[%s] Not on switcher — navigating directly", brand_name)
        await page.goto(
            f"{sc_base}/account-switcher/default/merchantMarketplace?returnTo=%2Fgp%2Fhomepage.html",
            wait_until="domcontentloaded", timeout=20000
        )
        await page.wait_for_load_state("networkidle", timeout=15000)
        await page.wait_for_timeout(2000)

    # Take debug screenshot
    DOWNLOADS_DIR.mkdir(exist_ok=True)
    await page.screenshot(path=str(DOWNLOADS_DIR / f"debug_{brand_name}_switcher.png"), full_page=True)
    log.info("[%s] Switcher URL: %s", brand_name, page.url)

    # Wait for the React account list to render (it loads async via API call)
    # The switcher shows a search field + radio list once accounts are fetched
    try:
        await page.wait_for_selector(
            'input[type="radio"], input[type="search"], input[type="text"], '
            '[class*="account"], [class*="merchant"], [data-testid*="account"]',
            timeout=15000,
        )
        log.info("[%s] Account list elements appeared", brand_name)
    except Exception:
        log.warning("[%s] Account list selector timed out — page may be empty or in iframe", brand_name)
        # Dump first 3000 chars of HTML for diagnosis
        html_snip = await page.content()
        log.info("[%s] Page HTML (first 3000): %s", brand_name, html_snip[:3000])
        # Check for iframes
        frames = page.frames
        log.info("[%s] Frames on page: %d — URLs: %s",
                 brand_name, len(frames), [f.url for f in frames])

    await page.wait_for_timeout(2000)

    # If there's a search field, type the account name to filter the list
    search_input = await page.query_selector(
        'input[type="search"], input[placeholder*="search" i], '
        'input[placeholder*="account" i], input[placeholder*="merchant" i]'
    )
    if search_input:
        log.info("[%s] Found search input — typing '%s'", brand_name, sc_account_name)
        await search_input.click()
        await search_input.type(sc_account_name, delay=60)  # type char-by-char to trigger React filter
        await page.wait_for_timeout(3000)  # wait for filtered results to render
        # Log what visible text is showing (exclude script/style tags and copyright noise)
        search_result_text = await page.evaluate("""
            () => {
                const skip = ['SCRIPT','STYLE','NOSCRIPT','META','LINK','HEAD'];
                const noise = ['©', 'amazon.com', 'affiliates', 'var ', 'window.', 'function('];
                return Array.from(document.querySelectorAll('*'))
                    .filter(el =>
                        !skip.includes(el.tagName) &&
                        el.children.length === 0 &&
                        el.textContent.trim().length > 1 &&
                        el.textContent.trim().length < 100 &&
                        !noise.some(n => el.textContent.trim().toLowerCase().includes(n))
                    )
                    .map(el => el.textContent.trim())
                    .filter((v, i, a) => a.indexOf(v) === i)  // unique
                    .slice(0, 30)
                    .join(' || ');
            }
        """)
        log.info("[%s] Search results for '%s': %s", brand_name, sc_account_name, search_result_text[:400])

    # If parent needed (SPN sub-accounts), expand the parent row first
    if sc_parent:
        log.info("[%s] Expanding parent account '%s'...", brand_name, sc_parent)
        parent_handle = await page.evaluate_handle("""
            (parentName) => {
                const all = Array.from(document.querySelectorAll('*'));
                return all.find(el =>
                    el.children.length === 0 &&
                    el.textContent.trim().toLowerCase().includes(parentName.toLowerCase())
                ) || null;
            }
        """, sc_parent)
        parent_el = parent_handle.as_element()
        if parent_el:
            # Walk up to find a clickable row/button
            expand_handle = await parent_el.evaluate_handle("""
                el => {
                    let cur = el;
                    for (let i = 0; i < 8; i++) {
                        if (!cur || !cur.parentElement) break;
                        cur = cur.parentElement;
                        const btn = cur.querySelector('button, [class*="expand"], [class*="toggle"]');
                        if (btn) return btn;
                        if (['LI', 'TR'].includes(cur.tagName)) return cur;
                    }
                    return null;
                }
            """)
            expand_el = expand_handle.as_element()
            if expand_el:
                await expand_el.click(force=True)
                await page.wait_for_timeout(1500)

    # ── Step 1: Click the account ROW (leaf text element) ─────────────────────
    #
    # Confirmed workflow (per account owner):
    #   1. Click the account row once  → it highlights AND reveals a
    #      marketplace sub-list (Amazon.in, Germany, France, ...).
    #   2. Pick the "Amazon.in" / India marketplace from that sub-list.
    #   3. Click the "Select account" button to confirm.
    #
    # Clicking the row's *leaf text node* (not a wrapping <button>) keeps the
    # "Select account" button on the page; clicking the <button> navigated away.

    async def _click_account_row(term: str) -> bool:
        # Find the leaf text node, then walk UP to a row container that has a
        # real radio input / role=radio / clickable row — click THAT, not the
        # leaf <span>. Clicking the dead label leaves the prior selection intact,
        # which causes silent "stuck on previous account" bugs.
        handle = await page.evaluate_handle("""
            (term) => {
                const skip = ['SCRIPT','STYLE','NOSCRIPT'];
                const isVis = el => {
                    const r = el.getBoundingClientRect();
                    return r.width > 0 && r.height > 0;
                };
                const all = Array.from(document.querySelectorAll('*'))
                    .filter(el => !skip.includes(el.tagName) && isVis(el));

                // Find the leaf text element
                let leaf = all.find(e =>
                    e.children.length === 0 && e.textContent.trim() === term
                );
                if (!leaf) {
                    leaf = all.find(e =>
                        e.children.length === 0 && e.textContent.trim().includes(term)
                    );
                }
                if (!leaf) return null;

                // Walk up to find a row container with a clickable radio/option
                let row = leaf;
                for (let depth = 0; depth < 10 && row.parentElement; depth++) {
                    row = row.parentElement;
                    // Prefer an unchecked radio inside this container
                    const radios = Array.from(row.querySelectorAll(
                        'input[type="radio"], [role="radio"]'
                    )).filter(isVis);
                    if (radios.length === 1) {
                        return radios[0];
                    }
                    // Container is itself a row/option
                    if (row.matches('li, [role="option"], [role="row"], [role="listitem"]')) {
                        return row;
                    }
                }
                return leaf;
            }
        """, term)
        el = handle.as_element()
        if not el:
            return False
        try:
            await el.scroll_into_view_if_needed(timeout=5000)
        except Exception:
            pass
        try:
            await el.click(force=True)
        except Exception:
            await el.evaluate("el => el.click()")
        await page.wait_for_timeout(2000)
        return True

    clicked_row = False
    for search_term in [sc_account_name, brand_name]:
        if search_term == brand_name and search_term != sc_account_name and search_input:
            log.info("[%s] Retrying with brand name '%s'", brand_name, brand_name)
            await search_input.fill("")
            await search_input.click()
            await search_input.type(brand_name, delay=60)
            await page.wait_for_timeout(2000)
        if await _click_account_row(search_term):
            log.info("[%s] Clicked account row for '%s'", brand_name, search_term)
            clicked_row = True
            break

    if not clicked_row:
        log.error("[%s] Could not find account row for '%s'", brand_name, sc_account_name)
        all_btns = await page.evaluate("""
            () => Array.from(document.querySelectorAll('button'))
                       .map(b => b.textContent.trim().slice(0, 50))
                       .filter(t => t.length > 1).slice(0, 25).join(' || ')
        """)
        log.info("[%s] Available buttons: %s", brand_name, all_btns[:500])
        return False

    # ── Step 2: Pick the correct marketplace (if a sub-list appeared) ────────────
    # After clicking the row, a marketplace list may appear with options like
    # "Amazon.in (A20WE5N42SW9UE)" or "United States (amazon.com)".
    # For single-marketplace accounts (e.g. US-only Charlotte Home on .com portal),
    # the button may already be enabled — skip the picker in that case.
    await page.wait_for_timeout(1500)  # let any sub-list animate in

    select_btn_check = page.locator(
        'button:has-text("Select account"), button:has-text("Select Account"), '
        'input[type="submit"][value*="Select"]'
    )
    already_enabled = False
    if await select_btn_check.count() > 0:
        already_enabled = not await select_btn_check.first.is_disabled()
        log.info("[%s] 'Select account' enabled state (pre-pick): %s", brand_name, already_enabled)

    marketplace_picked = None
    # Always log what options are visible after the row click — useful for debug
    visible_options = await page.evaluate("""
        () => {
            const isVis = el => {
                const r = el.getBoundingClientRect();
                return r.width > 0 && r.height > 0;
            };
            return Array.from(document.querySelectorAll(
                'button, li, [role="option"], [role="menuitem"]'
            )).filter(isVis)
              .map(e => e.textContent.trim().slice(0, 60))
              .filter(t => t.length > 1 && t.length < 60)
              .filter((v, i, a) => a.indexOf(v) === i)
              .slice(0, 30)
              .join(' || ');
        }
    """)
    log.info("[%s] Visible options after row click: %s", brand_name, visible_options[:400])

    mp_terms = _SC_MARKETPLACE_TERMS.get(marketplace.upper(), _SC_MARKETPLACE_TERMS["IN"])
    # ALWAYS run the marketplace picker (even when "Select" already enabled).
    # The previously-active account's marketplace radio remains checked across
    # switches, so "Select enabled" is meaningless. We must explicitly click the
    # target marketplace under the NEW merchant, skipping any "(current)"-marked
    # row (that's the previously-selected one we want to replace).
    marketplace_picked = await page.evaluate("""
    ([merchantId, want]) => {
        const isVis = el => {
            const r = el.getBoundingClientRect();
            const s = window.getComputedStyle(el);
            return r.width > 0 && r.height > 0 &&
                   s.visibility !== 'hidden' && s.display !== 'none';
        };
        // Reject noise: copyright, footers, long blocks of text
        const BAD = ['©', 'inc.', 'affiliates', 'trademark', 'reserved', 'privacy', 'cookie'];

        const els = Array.from(document.querySelectorAll(
            'button, a, [role="button"], [role="option"], li, label, span'
        )).filter(el => {
            if (!isVis(el)) return false;
            const t = el.textContent.trim();
            // Must be a short label (real marketplace options are < 60 chars)
            if (t.length > 60) return false;
            // Reject the currently-selected entries (these belong to the previous account)
            if (t.includes('(current)')) return false;
            // Reject pending-registration / inactive marketplaces
            if (t.toLowerCase().includes('pending registration')) return false;
            // Reject footer / copyright noise
            const tl = t.toLowerCase();
            if (BAD.some(b => tl.includes(b))) return false;
            return true;
        });

        // Prefer element matching target marketplace AND merchant id
        let target = els.find(e => {
            const t = e.textContent.trim().toLowerCase();
            return want.some(w => t.includes(w)) &&
                   (merchantId ? t.includes(merchantId.toLowerCase()) : true);
        });
        // Fallback: any short visible element matching the marketplace term
        if (!target) {
            target = els.find(e => {
                const t = e.textContent.trim().toLowerCase();
                return want.some(w => t.includes(w));
            });
        }
        if (target) {
            target.scrollIntoView({block: 'center'});
            target.click();
            target.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true}));
            return target.textContent.trim().slice(0, 60);
            }
            return null;
        }
    """, [sc_account_name, mp_terms])

    if marketplace_picked:
        log.info("[%s] Picked marketplace: '%s'", brand_name, marketplace_picked)
        await page.wait_for_timeout(2000)
    else:
        log.info("[%s] No marketplace sub-list found — account likely opens directly", brand_name)

    # ── Step 2b: If Select button is still disabled, fall back to clicking the
    #            first available radio / option (covers single-marketplace
    #            accounts like US-only where the row has no marketplace label).
    if not already_enabled:
        still_disabled = False
        if await select_btn_check.count() > 0:
            still_disabled = await select_btn_check.first.is_disabled()
        if still_disabled:
            picked_fallback = await page.evaluate("""
                () => {
                    const isVis = el => {
                        const r = el.getBoundingClientRect();
                        const s = window.getComputedStyle(el);
                        return r.width > 0 && r.height > 0 &&
                               s.visibility !== 'hidden' && s.display !== 'none';
                    };
                    // Try unchecked radio inputs first (most reliable for marketplace pickers)
                    const radios = Array.from(document.querySelectorAll(
                        'input[type="radio"], [role="radio"]'
                    )).filter(isVis).filter(r => {
                        if (r.tagName === 'INPUT') return !r.checked;
                        return r.getAttribute('aria-checked') !== 'true';
                    });
                    if (radios.length > 0) {
                        radios[0].scrollIntoView({block: 'center'});
                        radios[0].click();
                        return 'radio:' + (radios[0].id || radios[0].name || radios[0].value || 'unnamed');
                    }
                    return null;
                }
            """)
            if picked_fallback:
                log.info("[%s] Fallback picked: %s", brand_name, picked_fallback)
                await page.wait_for_timeout(1500)
            else:
                log.warning("[%s] 'Select account' disabled but no radio/option found to click", brand_name)

    # ── Step 2c: Verify the currently-checked radio belongs to the target. ────
    # When the previous account in the same run is still the active selection,
    # "Select account" can already be enabled with the WRONG radio checked. In
    # that case clicking it just re-confirms the previous account.
    selection_text = await page.evaluate("""
        (term) => {
            const isVis = el => {
                const r = el.getBoundingClientRect();
                return r.width > 0 && r.height > 0;
            };
            // Find the currently-checked radio
            const checked = Array.from(document.querySelectorAll(
                'input[type="radio"]:checked, [role="radio"][aria-checked="true"]'
            )).filter(isVis);
            if (checked.length === 0) return null;

            // Walk up from the radio to find its row label text
            for (const r of checked) {
                let row = r;
                for (let d = 0; d < 8 && row.parentElement; d++) {
                    row = row.parentElement;
                    const t = (row.innerText || row.textContent || '').trim();
                    if (t.length > 0 && t.length < 200) {
                        return t.slice(0, 120);
                    }
                }
            }
            return null;
        }
    """, sc_account_name)
    log.info("[%s] Currently checked row: %s", brand_name, selection_text)

    target_lower = sc_account_name.lower()
    if selection_text and target_lower not in selection_text.lower():
        log.warning("[%s] Checked radio does not contain '%s' — clicking target radio explicitly",
                    brand_name, sc_account_name)
        clicked_target = await page.evaluate("""
            (term) => {
                const isVis = el => {
                    const r = el.getBoundingClientRect();
                    return r.width > 0 && r.height > 0;
                };
                // Find a row whose visible text contains the target name
                const all = Array.from(document.querySelectorAll('li, [role="option"], [role="row"], [role="listitem"], div, label')).filter(isVis);
                const row = all.find(e => {
                    const t = (e.innerText || '').trim();
                    return t.includes(term) && t.length < 200;
                });
                if (!row) return null;
                // Click the radio inside that row
                const radio = row.querySelector('input[type="radio"], [role="radio"]');
                if (radio) {
                    radio.scrollIntoView({block: 'center'});
                    radio.click();
                    return 'radio:' + (radio.id || 'unnamed');
                }
                // Fallback: click the row itself
                row.scrollIntoView({block: 'center'});
                row.click();
                return 'row';
            }
        """, sc_account_name)
        if clicked_target:
            log.info("[%s] Clicked target radio (%s)", brand_name, clicked_target)
            await page.wait_for_timeout(1500)
        else:
            log.error("[%s] Could not find target radio for '%s'", brand_name, sc_account_name)

    # ── Step 3: Click the "Select account" button ─────────────────────────────
    select_btn = page.locator(
        'button:has-text("Select account"), button:has-text("Select Account"), '
        'input[type="submit"][value*="Select"]'
    )
    count = await select_btn.count()
    log.info("[%s] 'Select account' button count: %d", brand_name, count)

    if count > 0:
        is_disabled = await select_btn.first.is_disabled()
        log.info("[%s] 'Select account' button disabled: %s", brand_name, is_disabled)
        try:
            await select_btn.first.click(force=True)
            log.info("[%s] Clicked 'Select account'", brand_name)
        except Exception as exc:
            log.warning("[%s] Button click exception: %s — using JS click", brand_name, exc)
            await page.evaluate("""
                () => {
                    const all = Array.from(document.querySelectorAll('button'));
                    const btn = all.find(b => b.textContent.trim().toLowerCase().includes('select account'));
                    if (btn) btn.click();
                }
            """)
        await page.wait_for_load_state("networkidle", timeout=20000)
        await page.wait_for_timeout(2000)
        await page.screenshot(path=str(DOWNLOADS_DIR / f"debug_{brand_name}_after_select.png"))
    else:
        log.warning("[%s] 'Select account' button not found", brand_name)

    # ── Step 4: Verify the switch completed ───────────────────────────────────
    # For US accounts, a successful switch lands on sellercentral.amazon.com.
    # For India accounts it stays on sellercentral.amazon.in.
    from src.config import SC_BASE_URL_US
    sc_home = SC_BASE_URL_US if marketplace.upper() == "US" else SC_BASE_URL

    current = page.url
    log.info("[%s] After 'Select account': %s", brand_name, current)
    if "account-switcher" in current:
        await page.goto(f"{sc_home}/gp/homepage.html", wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_load_state("networkidle", timeout=20000)
        await page.wait_for_timeout(2000)

    final = page.url
    log.info("[%s] Final SC URL: %s", brand_name, final)
    if "account-switcher" in final or "Select an account" in (await page.title()) or _is_auth_page(final):
        reason = "still on selector" if "account-switcher" in final else \
                 "requires separate login (session not shared)" if _is_auth_page(final) else \
                 "title shows 'Select an account'"
        log.error("[%s] Account switch did not complete — %s", brand_name, reason)
        return None

    # Extract merchant ID for cross-contamination tracking
    import re as _re
    mcid_match = _re.search(r"mons_sel_dir_mcid=([^&]+)", final)
    mcid = mcid_match.group(1) if mcid_match else None
    log.info("[%s] SC account active — mcid=%s", brand_name, mcid)
    return mcid or "UNKNOWN"


# ── Amazon Ads login ──────────────────────────────────────────────────────────

async def login_ads(page: Page, email: str, password: str, start_url: str = None) -> bool:
    from src.config import ADS_CAMPAIGN_MANAGER

    if not start_url:
        start_url = ADS_CAMPAIGN_MANAGER

    log.info("Logging into Amazon Ads (%s)...", start_url)
    await page.goto(start_url, wait_until="domcontentloaded", timeout=30000)
    await page.wait_for_load_state("networkidle", timeout=20000)
    await page.wait_for_timeout(2000)

    # Already on Ads management console
    if "advertising.amazon" in page.url and not _is_auth_page(page.url):
        log.info("Already logged in to Amazon Ads")
        return True

    try:
        # Email step
        email_field = await page.query_selector("#ap_email")
        if email_field:
            await page.fill("#ap_email", email)
            continue_btn = await page.query_selector("#continue")
            if continue_btn:
                await page.click("#continue")
                await page.wait_for_load_state("networkidle", timeout=15000)

        # Password step
        pwd_field = await page.query_selector("#ap_password")
        if pwd_field:
            await page.fill("#ap_password", password)
            await page.click("#signInSubmit")
            await page.wait_for_load_state("networkidle", timeout=20000)

        # OTP
        if _is_auth_page(page.url):
            await _handle_otp(page, "Amazon Ads")

        if "advertising.amazon" in page.url:
            log.info("Amazon Ads login successful — url: %s", page.url)
            return True

        log.error("Ads login failed — landed on: %s", page.url)
        return False

    except Exception as exc:
        log.error("Ads login error: %s", exc)
        return False


# ── Ads account / profile switching ──────────────────────────────────────────

async def switch_ads_account(
    page: Page,
    ads_account_name: str,
    brand_name: str,
    campaign_manager_url: str = None,
) -> bool:
    """
    Switch to the correct advertiser profile in the Amazon Ads portal.
    The Ads portal shows a profile/account selector — find and click the right one.
    Returns True when on the correct profile.

    campaign_manager_url: override the default India URL for US/other portal pages.
    """
    from src.config import ADS_CAMPAIGN_MANAGER, ADS_BASE_URL

    if not campaign_manager_url:
        campaign_manager_url = ADS_CAMPAIGN_MANAGER

    log.info("[%s] Switching Ads account to '%s'...", brand_name, ads_account_name)

    # Navigate to campaign manager (may show profile selector)
    try:
        await page.goto(campaign_manager_url, wait_until="commit", timeout=30000)
        await page.wait_for_load_state("networkidle", timeout=20000)
        await page.wait_for_timeout(2000)
    except Exception as exc:
        log.warning("[%s] Ads nav failed: %s", brand_name, exc)

    if _is_auth_page(page.url):
        log.warning("[%s] Ads session expired mid-run — re-logging in", brand_name)
        await login_ads(page, "", "", start_url=campaign_manager_url)

    # Take screenshot to see current state
    DOWNLOADS_DIR.mkdir(exist_ok=True)
    await page.screenshot(path=str(DOWNLOADS_DIR / f"debug_{brand_name}_ads_switch.png"), full_page=True)
    log.info("[%s] Ads page: %s", brand_name, page.url)

    # The Ads campaign manager shows the current entity/brand name in the top-right header.
    # The entity switcher button typically says "<Brand> Sponsored ads, India".
    # We identify it via JS to be precise about which header button to click.
    SKIP_WORDS = {
        "help", "notification", "setting", "logout", "sign", "remove", "set",
        "home", "campaign", "homepage", "feedback", "search", "menu", "close",
    }

    clicked_switcher = await page.evaluate("""
        (skipWords) => {
            const isVisible = el => {
                const r = el.getBoundingClientRect();
                const s = window.getComputedStyle(el);
                return r.width > 0 && r.height > 0 &&
                       s.visibility !== 'hidden' && s.display !== 'none';
            };
            const allBtns = Array.from(document.querySelectorAll(
                'button, [role="button"], [class*="AccountSwitcher"], [class*="EntitySwitcher"],' +
                '[data-testid*="entity"], [data-testid*="account-switcher"]'
            )).filter(isVisible);

            // Priority 1: contains "Sponsored ads" — this is the entity name button
            const sponsoredBtn = allBtns.find(b => {
                const t = (b.innerText || '').toLowerCase();
                return t.includes('sponsored ads');
            });
            if (sponsoredBtn) {
                sponsoredBtn.click();
                return (sponsoredBtn.innerText || '').trim().slice(0, 80);
            }

            // Priority 2: header button NOT in skip list, with short text (< 60 chars)
            const headerArea = document.querySelector('header') || document.body;
            const headerBtns = Array.from(headerArea.querySelectorAll(
                'button, [role="button"]'
            )).filter(isVisible);
            const safe = headerBtns.find(b => {
                const t = (b.innerText || '').trim().toLowerCase();
                if (!t || t.length > 60) return false;
                return !skipWords.some(w => t.includes(w));
            });
            if (safe) {
                safe.click();
                return (safe.innerText || '').trim().slice(0, 80);
            }
            return null;
        }
    """, list(SKIP_WORDS))

    if clicked_switcher:
        log.info("[%s] Clicked entity switcher: '%s'", brand_name, clicked_switcher)
    else:
        log.warning("[%s] Entity switcher button not found", brand_name)

    await page.wait_for_timeout(2500)   # wait for dropdown to render

    # Screenshot the open dropdown for debugging
    await page.screenshot(
        path=str(DOWNLOADS_DIR / f"debug_{brand_name}_entity_dropdown.png"), full_page=False
    )

    # ── Search for the target entity ────────────────────────────────────────────
    # The entity list panel opens on the RIGHT side of the viewport.
    # It may be very long (100+ entries, alphabetical).
    # All entity interaction must be SCOPED to the right-side panel to avoid
    # accidentally clicking campaign table rows or other page elements.
    #
    # Strategy:
    #   1. Locate the entity panel: a container on the right side that holds the
    #      visible entity list items.
    #   2. If the panel has a search input, type in it using Playwright's
    #      click + keyboard (native events for React compatibility).
    #   3. Scan panel items for the target entity, scrolling the panel up to 40×.

    # Step 1: identify the entity panel and its search input
    # The entity panel opens on the RIGHT side of the viewport.
    # The search input may be in a SIBLING of the entity list container
    # (not necessarily inside it), so we scan the full right side of the page.
    panel_search_info = await page.evaluate("""
        (targetName) => {
            const isVisible = el => {
                const r = el.getBoundingClientRect();
                return r.width > 0 && r.height > 0;
            };
            const vw = window.innerWidth;
            const RIGHT_THRESHOLD = vw * 0.35;   // items in right 65% of screen

            // Count entity items in the right-side panel
            const entityItems = Array.from(document.querySelectorAll(
                'li, [role="option"], [role="menuitem"],' +
                '[class*="entity-item"], [class*="profile-item"]'
            )).filter(el => {
                if (!isVisible(el)) return false;
                const r = el.getBoundingClientRect();
                return r.left > RIGHT_THRESHOLD;
            });

            // Look for a search/filter input anywhere on the RIGHT side of the screen
            // (not just inside the entity list container — it may be a sibling above the list)
            const rightInputs = Array.from(document.querySelectorAll(
                'input[type="text"], input[type="search"], input:not([type="hidden"])'
            )).filter(i => {
                const r = i.getBoundingClientRect();
                return r.width > 0 && r.height > 0 && r.left > RIGHT_THRESHOLD;
            });

            let searchInput = rightInputs[0] || null;
            if (searchInput) {
                searchInput.scrollIntoView({block: 'center'});
                searchInput.click();
                searchInput.focus();
            }

            return {
                panelFound: entityItems.length > 0,
                entityCount: entityItems.length,
                hasSearchInput: !!searchInput,
                searchInputPlaceholder: searchInput
                    ? (searchInput.placeholder || searchInput.getAttribute('aria-label') || '')
                    : '',
                rightInputCount: rightInputs.length,
            };
        }
    """, ads_account_name)

    log.info("[%s] Entity panel info: %s", brand_name, panel_search_info)

    # Step 2: if a search box was focused, type the entity name via keyboard
    search_typed = False
    if panel_search_info.get("hasSearchInput"):
        try:
            await page.keyboard.type(ads_account_name, delay=80)
            search_typed = True
            log.info("[%s] Typed '%s' via keyboard in entity search box", brand_name, ads_account_name)
            await page.wait_for_timeout(4000)  # wait for search results to filter
        except Exception as exc:
            log.warning("[%s] Keyboard type failed: %s", brand_name, exc)

    # Even without a dedicated search box, the panel may accept keyboard input if focused
    if not search_typed:
        log.info("[%s] No entity search input found — trying keyboard type directly", brand_name)
        try:
            await page.keyboard.type(ads_account_name, delay=80)
            await page.wait_for_timeout(1500)
        except Exception:
            pass

    # Step 3: click the entity using page.mouse (real pointer events that React handles).
    # JS element.click() fires a synthetic event that React's delegated handler may ignore;
    # page.mouse.click dispatches real browser pointer/mouse events that always work.
    found_entity = None
    for scroll_attempt in range(40):   # up to 40 × 500px = 20 000px
        bbox_result = await page.evaluate("""
            (targetName) => {
                const isVisible = el => {
                    const r = el.getBoundingClientRect();
                    return r.width > 0 && r.height > 0;
                };
                const vw = window.innerWidth;

                // Scope to right-side panel items ONLY
                const rows = Array.from(document.querySelectorAll(
                    'li, option, [role="option"], [role="menuitem"],' +
                    '[class*="entity-item"], [class*="profile-item"]'
                )).filter(el => {
                    if (!isVisible(el)) return false;
                    const r = el.getBoundingClientRect();
                    return r.left > vw * 0.35;
                });

                const match = rows.find(r =>
                    (r.innerText || '').toLowerCase().includes(targetName.toLowerCase())
                );
                if (match) {
                    match.scrollIntoView({block: 'center'});
                    const r = match.getBoundingClientRect();
                    return {
                        x: r.left + r.width / 2,
                        y: r.top + r.height / 2,
                        text: (match.innerText || '').trim().slice(0, 60),
                    };
                }
                return null;
            }
        """, ads_account_name)

        if bbox_result:
            log.info("[%s] Clicking entity '%s' via mouse at (%.0f, %.0f)",
                     brand_name, bbox_result["text"][:40],
                     bbox_result["x"], bbox_result["y"])
            await page.mouse.click(bbox_result["x"], bbox_result["y"])
            found_entity = bbox_result["text"]
            break

        # Scroll the entity panel container
        reached_bottom = await page.evaluate("""
            () => {
                const vw = window.innerWidth;
                const containers = Array.from(document.querySelectorAll('*')).filter(el => {
                    const r = el.getBoundingClientRect();
                    return r.width > 0 && r.height > 0 &&
                           r.left > vw * 0.3 &&
                           el.scrollHeight > el.clientHeight + 10;
                }).sort((a, b) => b.scrollHeight - a.scrollHeight);

                if (containers.length) {
                    const c = containers[0];
                    const before = c.scrollTop;
                    c.scrollBy(0, 500);
                    return c.scrollTop === before;
                }
                return true;
            }
        """)
        await page.wait_for_timeout(300)

        if reached_bottom:
            log.info("[%s] Entity panel reached bottom after %d scroll attempts",
                     brand_name, scroll_attempt + 1)
            break

    if found_entity:
        log.info("[%s] Found + clicked ads entity '%s': '%s'", brand_name, ads_account_name, found_entity)
        # Wait for SPA to update after entity switch (may be instant or slow)
        await page.wait_for_timeout(3000)
        # Take a screenshot to verify the switch
        await page.screenshot(
            path=str(DOWNLOADS_DIR / f"debug_{brand_name}_after_entity_switch.png"), full_page=False
        )
        log.info("[%s] Ads entity switched — url: %s", brand_name, page.url)
    else:
        visible_rows = await page.evaluate("""
            () => {
                const isVisible = el => {
                    const r = el.getBoundingClientRect();
                    return r.width > 0 && r.height > 0;
                };
                return Array.from(document.querySelectorAll(
                    'li, [role="option"], [role="menuitem"]'
                )).filter(isVisible).map(e => (e.innerText || '').trim().slice(0, 50)).filter(Boolean).slice(0, 20);
            }
        """)
        log.error("[%s] Ads entity '%s' NOT FOUND — visible rows: %s",
                  brand_name, ads_account_name, visible_rows)
        log.error("[%s] Aborting ads switch — cross-contamination risk", brand_name)
        return None

    # ── Verification: extract entityId from URL + check header brand text ───────
    # The URL after a successful switch contains `entityId=ENTITY...`.
    # We also read the entity-switcher button text (which shows the current brand)
    # and require it to contain part of the target name.
    import re as _re
    entity_match = _re.search(r"entityId=([A-Z0-9]+)", page.url)
    entity_id = entity_match.group(1) if entity_match else None

    header_text = await page.evaluate("""
        () => {
            const isVis = el => {
                const r = el.getBoundingClientRect();
                const s = window.getComputedStyle(el);
                return r.width > 0 && r.height > 0 &&
                       s.visibility !== 'hidden' && s.display !== 'none';
            };
            const allBtns = Array.from(document.querySelectorAll('button, [role="button"]'))
                .filter(isVis);
            const switcher = allBtns.find(b => {
                const t = (b.innerText || '').toLowerCase();
                return t.includes('sponsored ads');
            });
            return switcher ? (switcher.innerText || '').trim() : '';
        }
    """)

    log.info("[%s] Post-switch verify: entityId=%s, header='%s'",
             brand_name, entity_id, header_text[:80])

    # The entityId in the URL is the authoritative identifier for the Ads
    # profile. The displayed header brand name is often DIFFERENT from the
    # ads_account_name (e.g. Shri Vinayak Services → header shows "GreenBrrew"),
    # so we do NOT reject on header text mismatch. Cross-contamination is
    # caught at the main.py level via entityId collision tracking.
    return entity_id or "UNKNOWN"
