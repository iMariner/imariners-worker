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
  - Category select : #ddl_929_CourseCategory  (POST SEA=1, Value Added=4)
  - Type select     : #ddl_930_CourseType      (Single Courses / Package Courses)
  - Course select   : #ddl_931_CourseId        (populates after type)
  - Batch select    : #ddl_971_CourseBatchId_1  (injected after course pick)
  - Fee input       : #txt_972_Fee_1            (readonly, e.g. "5500.00")
  - Batch label fmt : "29 Jun - 03 Jul, Avl. Seats-19"  (parser already matches)

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
#     name         logical level name (last level is always the batch level)
#     css          stable selector for the <select>
#     include_only optional list of option TEXTS to keep (others skipped)
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
             'include_only': ['Single Courses']},   # Packages skipped for now
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


async def select_and_wait(page, css, value, wait_ms):
    """Select an option and trigger the change handlers the page relies on.
    Playwright's select_option dispatches bubbling input+change (caught by the
    site's delegated jQuery handlers); we also fire an explicit jQuery
    .trigger('change') as belt-and-braces for the FillDropDownData call, then
    wait for the async repaint to settle."""
    await page.select_option(css, value=value)
    await page.evaluate(
        """(sel) => { const el = document.querySelector(sel);
            if (el) { el.dispatchEvent(new Event('change', {bubbles:true}));
                      if (window.jQuery) window.jQuery(el).trigger('change'); } }""",
        css,
    )
    try:
        await page.wait_for_load_state('networkidle', timeout=wait_ms * 2)
    except Exception:
        pass
    await page.wait_for_timeout(wait_ms)


def parse_batch_label(label):
    """Matches the date/seats convention already used in worker.js / sync.php."""
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
            print(f"    [WARN] no options at level '{level['name']}' (path={path})")
        return results

    for value, text in opts:
        await select_and_wait(page, level['css'], value, config.get('wait_ms', 2000))
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
        else:
            await walk(page, remaining, config, next_path, results)

    return results


async def scrape_institute(browser, config):
    print(f"\n=== {config['name']} ({config['slug']}) ===")
    context = await browser.new_context()
    page = await context.new_page()
    try:
        await page.goto(config['entry_url'], wait_until='networkidle')
        await page.wait_for_timeout(config.get('initial_wait_ms', 2000))
        await debug_dump(page, f"{config['slug']}_initial")
        results = await walk(page, config['levels'], config)
        print(f"  -> {len(results)} single-course batch row(s)")
        return results
    except Exception as e:
        print(f"  [ERROR] {config['name']}: {e}")
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
        browser = await p.chromium.launch(headless=not DEBUG)
        for config in configs:
            batches = await scrape_institute(browser, config)
            groups.append({
                'institute_slug': config['slug'],
                'institute_name': config['name'],
                'source_url': config['source_url'],
                'batches': batches,
            })
        await browser.close()

    with open('appex_ui_courses.json', 'w', encoding='utf-8') as f:
        json.dump(groups, f, indent=2, ensure_ascii=False)

    total = sum(len(g['batches']) for g in groups)
    print(f"\nDone. {total} batch row(s) across {len(groups)} institute(s).")
    return groups


if __name__ == '__main__':
    asyncio.run(main())
