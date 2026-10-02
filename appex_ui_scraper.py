"""
AppEx UI-driven scraper -- ONE engine, config-driven per institute
-------------------------------------------------------------------
For institutes on the NEWER AppEx generation, whose booking flow can't be
hit as a simple stateless GET (the kind worker.js/sync.php use). TS Rahaman
runs this newer generation: a single generic POST /Appex/FillDropDownData
that takes the ENTIRE form-field state as a fields[n][...] array and returns
whatever the next dropdown should hold. We don't reconstruct that fragile
payload by hand -- we drive the real rendered dropdowns and let the page
build the payloads. Onboard a new institute of this kind by adding a config
block below, NOT by writing a new script.

VERIFIED LIVE (TS Rahaman, booking.tsrahaman.org) on 2026-06-23:
- Category select : #ddl_929_CourseCategory (POST SEA=1, Value Added=4)
- Type select     : #ddl_930_CourseType (Single Courses / Package Courses)
- Course select   : #ddl_931_CourseId (populates after type)
- Batch select    : #ddl_971_CourseBatchId_1 (injected after course pick)
- Fee input       : #txt_972_Fee_1 (readonly, e.g. "5500.00")
- Batch label fmt : "29 Jun - 03 Jul, Avl. Seats-19" (parser already matches)

VERIFIED LIVE (IMU Navi Mumbai Campus, imunavimumbai.ac.in) on 2026-07-11:
- No category level -- the form only has Course Type then Course.
- Type select   : #ddl_930_CourseType (only "Single Courses" -- same ids as
  ts-rahaman's; this is a fixed AppEx template field number, not
  institute-specific, so the same CSS works verbatim).
- Course select : #ddl_931_CourseId
- Batch select  : #ddl_971_CourseBatchId_1
- Fee input     : #txt_972_Fee_1
- Batch label fmt is DIFFERENT here -- a single date, not a range:
  "13 Jul 2026, Total Seats-24, Avl. Seats-24". parse_batch_label() below
  now tries the range format first and falls back to this single-date
  format (end_date is left null in that case).

SCOPE: Single Courses only. Package Courses are intentionally skipped -- a
package explodes into N component courses each with its own batch+fee that
the candidate composes individually, so it has no single schedule/fee row.
Edit the 'include_only' filter on the 'mode' level when a package model is
decided.

Run:
  python appex_ui_scraper.py
  python appex_ui_scraper.py --debug          # headed + screenshots
  python appex_ui_scraper.py --only ts-rahaman
"""

import asyncio
import json
import re
import sys
from datetime import datetime
from playwright.async_api import async_playwright

DEBUG = '--debug' in sys.argv
ONLY = sys.argv[sys.argv.index('--only') + 1] if '--only' in sys.argv else None

PLACEHOLDER_RE = re.compile(r'^\s*(--|select|choose)', re.I)
MONTHS = {'jan': 1, 'feb': 2, 'mar': 3, 'apr': 4, 'may': 5, 'jun': 6,
          'jul': 7, 'aug': 8, 'sep': 9, 'oct': 10, 'nov': 11, 'dec': 12}

# ─────────────────────────────────────────────────────────────────────────
# INSTITUTE CONFIGS -- add institutes here, not new code.
#   level fields:
#     name           logical level name (last level is always the batch level)
#     css            stable selector for the <select>
#     include_only   optional list of option TEXTS to keep (others skipped)
# ─────────────────────────────────────────────────────────────────────────
INSTITUTE_CONFIGS = [
    {
        'slug': 'ts-rahaman',
        'name': 'T S Rahaman Maritime Training Institute',
        'source_url': 'https://booking.tsrahaman.org',
        'entry_url': 'https://booking.tsrahaman.org/Login/Register?mkey=ose',
        'enabled': True,
        'wait_ms': 2500,
        'levels': [
            {'name': 'category', 'css': '#ddl_929_CourseCategory'},
            {'name': 'mode', 'css': '#ddl_930_CourseType',
             'include_only': ['Single Courses']},  # Packages skipped for now
            {'name': 'course', 'css': '#ddl_931_CourseId'},
            {'name': 'batch', 'css': '#ddl_971_CourseBatchId_1'},
        ],
        'fee_selector': '#txt_972_Fee_1',
    },
    {
        # Matches wp-admin institute id 60, slug "imu-navi-mumbai-campus".
        # This institute's form has no category level -- Course Type only
        # ever offers "Single Courses" (no packages at all here), so the
        # cascade is just type -> course -> batch.
        'slug': 'imu-navi-mumbai-campus',
        'name': 'IMU, Navi Mumbai Campus',
        'source_url': 'https://www.imunavimumbai.ac.in',
        'entry_url': 'https://www.imunavimumbai.ac.in/Booking/login/register?mkey=ose',
        'enabled': True,
        'wait_ms': 2500,
        'levels': [
            {'name': 'mode', 'css': '#ddl_930_CourseType',
             'include_only': ['Single Courses']},
            {'name': 'course', 'css': '#ddl_931_CourseId'},
            {'name': 'batch', 'css': '#ddl_971_CourseBatchId_1'},
        ],
        'fee_selector': '#txt_972_Fee_1',
    },
    {
        # Matches wp-admin institute id 66. Same eMTI booking app as the IMU
        # Navi Mumbai campus above, self-hosted on the campus domain.
        'slug': 'indian-maritime-university-mumbai-port-campus-lbs-camsar-meri',
        'name': 'IMU, Mumbai Port Campus',
        'source_url': 'https://imumumbaiport.ac.in',
        'entry_url': 'https://imumumbaiport.ac.in/BOOKING/Login/Register?mkey=ose',
        'enabled': True,
        'wait_ms': 2500,
        'levels': [
            {'name': 'mode', 'css': '#ddl_930_CourseType',
             'include_only': ['Single Courses']},
            {'name': 'course', 'css': '#ddl_931_CourseId'},
            {'name': 'batch', 'css': '#ddl_971_CourseBatchId_1'},
        ],
        'fee_selector': '#txt_972_Fee_1',
    },
]

# ─────────────────────────────────────────────────────────────────────────
# Generic engine -- institute-agnostic.
# ─────────────────────────────────────────────────────────────────────────

async def options_of(page, css):
    handle = await page.query_selector(css)
    if not handle:
        return []
    out = []
    for o in await handle.query_selector_all('option'):
        value = await o.get_attribute('value')
        text = (await o.text_content() or '').strip()
        if value in (None, '', '0') or PLACEHOLDER_RE.search(text):
            continue
        out.append((value, text))
    return out

async def set_value_and_fire(page, css, value):
    """Set the underlying <select>'s value directly via JS and dispatch the
    events the page's delegated jQuery handlers listen for, instead of using
    Playwright's select_option(). Confirmed live (real browser): AppEx's
    cascading selects can carry a literal disabled="disabled" attribute
    while still being the correct, live target for the next AJAX step --
    select_option() enforces an "enabled" actionability check before it'll
    touch an element and just times out on this, which is what failed in
    production (CI's headless run hit it; a normal desktop browser didn't
    visibly stay disabled long enough to notice). Setting .value via script
    is not blocked by the disabled attribute, so this sidesteps the check
    entirely rather than trying to out-guess why CI's timing differs."""
    return await page.evaluate(
        """([sel, val]) => {
            const el = document.querySelector(sel);
            if (!el) return false;
            el.disabled = false;
            el.value = val;
            el.dispatchEvent(new Event('input', {bubbles:true}));
            el.dispatchEvent(new Event('change', {bubbles:true}));
            if (window.jQuery) window.jQuery(el).trigger('change');
            return true;
        }""",
        [css, value],
    )

async def wait_until_ready(page, css, timeout_ms=15000, poll_ms=300):
    """Poll until `css` exists and is either enabled or already has more than
    the placeholder option -- whichever signals the AJAX response landed.
    Replaces a blind fixed sleep, which is both slower in the common case and
    not generous enough in the slow case (CI's headless run needed more than
    a fixed 2.5s on at least one step)."""
    elapsed = 0
    while elapsed < timeout_ms:
        state = await page.evaluate(
            """(sel) => { const el = document.querySelector(sel);
                if (!el) return null;
                return {disabled: el.disabled, optCount: el.options.length}; }""",
            css,
        )
        if state and (not state['disabled'] or state['optCount'] > 1):
            return True
        await page.wait_for_timeout(poll_ms)
        elapsed += poll_ms
    return False

async def select_and_wait(page, css, value, next_css, wait_ms, retry=True):
    """Drive one cascade step: set the value, then wait for whatever comes
    next (another dropdown, or nothing for the last/batch level) to actually
    become ready, retrying the selection once if it doesn't show up in time."""
    await set_value_and_fire(page, css, value)
    if next_css:
        ready = await wait_until_ready(page, next_css, timeout_ms=max(wait_ms * 6, 15000))
        if not ready and retry:
            print(f"    [WARN] '{next_css}' not ready after selecting {value!r} on {css} "
                  f"-- retrying selection once")
            await set_value_and_fire(page, css, value)
            ready = await wait_until_ready(page, next_css, timeout_ms=max(wait_ms * 6, 15000))
        if not ready:
            print(f"    [WARN] '{next_css}' still not ready after retry -- proceeding anyway")
    else:
        # Last level: nothing to poll for a "ready" state on, just give the
        # fee field a moment to render after the batch pick.
        await page.wait_for_timeout(wait_ms)

def parse_batch_label(label):
    """Matches the date/seats convention already used in worker.js / sync.php.

    Two formats seen live across institutes on this same AppEx template:
      1. Range:  "29 Jun - 03 Jul, Avl. Seats-19"          (ts-rahaman)
      2. Single: "13 Jul 2026, Total Seats-24, Avl. Seats-24"  (IMU Navi Mumbai)

    Tries the range format first; if that doesn't match, falls back to a
    single date (commencement date only -- end_date stays null, which the
    ingest endpoint already treats as a valid "no end date given" batch)."""
    start_date = end_date = None
    m = re.search(
        r'(\d{1,2})\s+([A-Za-z]{3})\D*?(\d{1,2})\s+([A-Za-z]{3})(?:\s+(\d{4}))?',
        label)
    if m:
        year = int(m.group(5)) if m.group(5) else datetime.now().year
        mo1 = MONTHS.get(m.group(2).lower()[:3])
        mo2 = MONTHS.get(m.group(4).lower()[:3])
        if mo1 and mo2:
            start_date = f"{year}-{mo1:02d}-{int(m.group(1)):02d}"
            end_date = f"{year}-{mo2:02d}-{int(m.group(3)):02d}"

    if not start_date:
        # Fallback: single date like "13 Jul 2026"
        m2 = re.search(r'(\d{1,2})\s+([A-Za-z]{3})[A-Za-z]*\s+(\d{4})', label)
        if m2:
            mo = MONTHS.get(m2.group(2).lower()[:3])
            if mo:
                start_date = f"{int(m2.group(3))}-{mo:02d}-{int(m2.group(1)):02d}"
                end_date = None

    seats = re.search(r'(?:Avl\.?\s*Seats|Seats\s*Avl\.?)[-:\s]*(\d+)', label, re.I)
    seats_text = f"{seats.group(1)} seats left" if seats else ''
    return start_date, end_date, seats_text

async def extract_fee(page, config):
    sel = config.get('fee_selector')
    if sel:
        el = await page.query_selector(sel)
        if el:
            raw = await el.get_attribute('value')
            if raw is None:
                raw = await el.text_content()
            m = re.search(r'[\d,]+(?:\.\d{1,2})?', raw or '')
            if m:
                return float(m.group(0).replace(',', ''))
    body = await page.text_content('body') or ''
    m = re.search(r'(?:₹|Rs\.?|INR)\s?([\d,]+(?:\.\d{1,2})?)', body)
    return float(m.group(1).replace(',', '')) if m else None

async def debug_dump(page, tag):
    if not DEBUG:
        return
    try:
        await page.screenshot(path=f"debug_{tag}.png")
    except Exception:
        pass

def _checkpoint(slug, results):
    """Write whatever's been collected so far to a recoverable file. If the
    whole run later crashes outside the per-option try/except (browser
    crash, network drop), this is still on disk -- the previous version had
    no such safety net, so a crash near the end of a long run (this one took
    9m25s) lost everything collected on the way there."""
    try:
        with open(f'appex_ui_checkpoint_{slug}.json', 'w', encoding='utf-8') as f:
            json.dump(results, f, indent=2, ensure_ascii=False)
    except Exception:
        pass

async def walk(page, levels, config, path=None, results=None):
    """Recursively walk the cascade. Last level = batch level. Works for any
    number of levels; a different institute just needs a different `levels`
    list in its config, not different code."""
    path = path or {}
    results = results if results is not None else []
    if not levels:
        return results

    level, remaining = levels[0], levels[1:]
    is_last = not remaining

    opts = await options_of(page, level['css'])
    if 'include_only' in level:
        opts = [(v, t) for (v, t) in opts if t.strip() in level['include_only']]

    if not opts:
        if not is_last:
            print(f"  [WARN] no options at level '{level['name']}' (path={path})")
        return results

    for value, text in opts:
        try:
            next_css = remaining[0]['css'] if remaining else None
            await select_and_wait(page, level['css'], value, next_css, config.get('wait_ms', 2000))
            await debug_dump(page, f"{config['slug']}_{level['name']}_{re.sub(r'[^A-Za-z0-9]+','_',text)[:18]}")
            next_path = {**path, level['name']: text}

            if is_last:
                start_date, end_date, seats_text = parse_batch_label(text)
                if not start_date:
                    print(f"    [SKIP] unparseable batch label: {text!r}")
                    continue
                fee = await extract_fee(page, config)
                results.append({
                    'category': next_path.get('category', ''),
                    'mode': next_path.get('mode', ''),
                    'course': next_path.get('course', '').strip(),
                    'batch_label': text,
                    'commencement_date': start_date,
                    'end_date': end_date,
                    'seats_text': seats_text,
                    'fees': fee,
                })
                if len(results) % 10 == 0:
                    _checkpoint(config['slug'], results)
            else:
                await walk(page, remaining, config, next_path, results)
        except Exception as e:
            # Isolate the failure to this one option. Without this, one bad
            # selection deep in the tree would propagate up through every
            # parent recursion and discard every result already collected
            # for this whole institute -- which is exactly what happened in
            # production (a 9m25s run that still reported 0 batch rows).
            print(f"  [ERROR] level '{level['name']}'={text!r} (path={path}): {e}")
            continue

    return results

async def goto_and_wait_for_form(page, config, attempts=3):
    """Navigate and wait for the actual category dropdown to exist, instead
    of wait_until='networkidle' (confirmed broken on this site -- see
    set_value_and_fire's docstring history). Retries with a short backoff and
    writes a screenshot + page snapshot on every failed attempt, regardless
    of --debug, so a CI failure leaves real evidence instead of just a bare
    timeout message -- the previous version only captured screenshots in
    --debug mode, which scheduled runs never use."""
    category_css = config['levels'][0]['css']
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            await page.goto(config['entry_url'], wait_until='domcontentloaded', timeout=45000)
            await page.wait_for_selector(category_css, timeout=60000)
            await page.wait_for_timeout(config.get('initial_wait_ms', 1500))
            return
        except Exception as e:
            last_error = e
            print(f"  [WARN] goto/form-wait attempt {attempt} failed: {e}")
            try:
                await page.screenshot(path=f"failure_{config['slug']}_attempt{attempt}.png")
                html = await page.content()
                with open(f"failure_{config['slug']}_attempt{attempt}.html", 'w', encoding='utf-8') as f:
                    f.write(html[:200000])
                print(f"  current url: {page.url}")
            except Exception as diag_err:
                print(f"  [WARN] could not capture diagnostics: {diag_err}")
            if attempt < attempts:
                await page.wait_for_timeout(5000)
    raise last_error

async def scrape_institute(browser, config):
    print(f"\n=== {config['name']} ({config['slug']}) ===")
    context = await browser.new_context()
    page = await context.new_page()
    try:
        await goto_and_wait_for_form(page, config)
        await debug_dump(page, f"{config['slug']}_initial")
        results = await walk(page, config['levels'], config)
        print(f"  -> {len(results)} single-course batch row(s)")
        return results
    except Exception as e:
        print(f"  [ERROR] {config['name']}: {e}")
        try:
            with open(f"appex_ui_checkpoint_{config['slug']}.json", encoding='utf-8') as f:
                recovered = json.load(f)
            print(f"  [RECOVERED] {len(recovered)} row(s) from last checkpoint before the crash")
            return recovered
        except Exception:
            return []
    finally:
        await context.close()

async def main():
    print(f"AppEx UI scraper run @ {datetime.utcnow().isoformat()} UTC")
    configs = [c for c in INSTITUTE_CONFIGS if c.get('enabled', True)]
    if ONLY:
        configs = [c for c in configs if c['slug'] == ONLY]

    groups = []
    async with async_playwright() as p:
        # channel='chromium' opts out of Playwright's default "headless shell"
        # binary (a leaner, separately-downloaded browser used automatically
        # for headless=True on newer Playwright versions) in favour of full
        # Chromium running its native headless mode. The headless shell is
        # faster but less exercised against legacy/quirky sites; this
        # institute's page (jQuery 2.1.4, blocking sequential script tags,
        # a request that never resolves) is exactly the kind of site where
        # that gap could plausibly explain why CI's headless run can't even
        # reach 'domcontentloaded' while a real desktop browser loads fine.
        browser = await p.chromium.launch(headless=not DEBUG, channel='chromium')
        for config in configs:
            batches = await scrape_institute(browser, config)
            groups.append({
                'institute_slug': config['slug'],
                'institute_name': config['name'],
                'source_url': config['source_url'],
                'booking_url': config['entry_url'],
                'batches': batches,
            })
        await browser.close()

    with open('appex_ui_courses.json', 'w', encoding='utf-8') as f:
        json.dump(groups, f, indent=2, ensure_ascii=False)

    total = sum(len(g['batches']) for g in groups)
    print(f"\nDone. {total} batch row(s) across {len(groups)} institute(s).")
    if total == 0 and groups:
        # Both prior production runs "succeeded" with 0 rows because nothing
        # here raised -- the workflow showed a green check while silently
        # scraping nothing. Treat zero results as a real failure so CI marks
        # the run red and the failure-artifact upload step actually fires.
        print("[FATAL] 0 batch rows scraped across all institutes -- failing the run.")
        sys.exit(1)
    return groups

if __name__ == '__main__':
    asyncio.run(main())
