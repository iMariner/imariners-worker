import asyncio
import json
import csv
import sys
from datetime import datetime, timezone
from playwright.async_api import async_playwright

CATEGORIES = {
    '32': 'Pre-sea Courses',
    '45': 'Basic Courses',
    '46': 'Advance Courses',
    '47': 'Simulator Courses',
    '48': 'MCA (UK) Approved Courses',
    '49': 'Refresher Courses',
    '51': 'Value Added Courses',
    '52': 'IGF CODE',
    '55': 'Electrical Courses',
    '56': 'Boiler Courses',
    '61': 'Documents Renewal and Applications',
}

BASE_URL = 'https://bpmarine.in/booking/GenericScreen/BookSelectedCourse.aspx'

SEL = '#ContentPlaceHolder1_ContentPlaceHolder2_'


async def settle(page, ms=1500):
    """Wait for the ASP.NET UpdatePanel postback to finish, then a short pause."""
    try:
        await page.wait_for_load_state('networkidle', timeout=15000)
    except Exception:
        pass
    await page.wait_for_timeout(ms)


async def open_course(page, category_id, course_id=None):
    """(Re)load the booking form and select category (and course)."""
    await page.goto(BASE_URL)
    await page.wait_for_load_state('networkidle')
    await page.select_option(SEL + 'ddlCategory', category_id, timeout=20000)
    await settle(page, 2500)
    if course_id:
        await page.select_option(SEL + 'ddlCourseName', course_id, timeout=20000)
        await settle(page, 2500)


async def scrape_category(page, category_id, category_name):
    """Scrape all courses and batches for a single category.

    Each course and each batch date is handled on its own: a dropdown that
    stalls (the Refresher category used to time out on every run and lose all
    its batches) now skips just that date, reloads the form and carries on.
    """
    results = []
    await open_course(page, category_id)
    courses = [(await o.get_attribute('value'), (await o.text_content() or '').strip())
               for o in (await page.query_selector_all(SEL + 'ddlCourseName option'))[1:]]

    for course_id, course_name in courses:
        if not course_id or not course_name:
            continue
        try:
            await page.select_option(SEL + 'ddlCourseName', course_id, timeout=20000)
            await settle(page, 2500)
        except Exception as exc:
            print(f"    course {course_name}: reselect after error ({str(exc)[:80]})")
            try:
                await open_course(page, category_id, course_id)
            except Exception as exc2:
                print(f"    SKIP course {course_name}: {str(exc2)[:120]}")
                continue

        dates = [(await o.get_attribute('value'), (await o.text_content() or '').strip())
                 for o in (await page.query_selector_all(SEL + 'ddlCommencementDate option'))[1:]]
        for batch_id, date_text in dates:
            if not batch_id or not date_text:
                continue
            try:
                await page.select_option(SEL + 'ddlCommencementDate', batch_id, timeout=20000)
                await settle(page, 1500)
                duration = await page.text_content(SEL + 'lbl_duration', timeout=10000)
                fees = await page.text_content(SEL + 'lblcourse_fee', timeout=10000)
            except Exception as exc:
                print(f"    skip {course_name} {date_text}: {str(exc)[:100]}")
                try:
                    await open_course(page, category_id, course_id)
                except Exception:
                    break
                continue
            results.append({
                'category_id': category_id,
                'category_name': category_name,
                'course_id': course_id,
                'course_name': course_name,
                'batch_id': batch_id,
                'commencement_date': date_text,
                'has_offer': '/OFFER' in date_text,
                'duration': duration.strip() if duration else '',
                'course_fees': fees.strip() if fees else '',
            })

    return results

async def main():
    print(f"Starting BP Marine scraper at {datetime.now(timezone.utc).isoformat()} UTC")
    all_data = []
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()
        
        for cat_id, cat_name in CATEGORIES.items():
            print(f"Scraping category: {cat_name} ({cat_id})")

            try:
                results = await scrape_category(page, cat_id, cat_name)
                all_data.extend(results)
                print(f"  Found {len(results)} batches for {cat_name}")
            except Exception as exc:
                # Don't let one broken category (e.g. a selector that no
                # longer matches because the site changed) take down every
                # other category's results with it.
                print(f"  ERROR scraping {cat_name} ({cat_id}): {exc}")
                continue
        
        await browser.close()
    
    # Output JSON
    with open('bpmarine_courses.json', 'w', encoding='utf-8') as f:
        json.dump(all_data, f, indent=2, ensure_ascii=False)
    
    # Output CSV (optional, but useful for debugging)
    if all_data:
        with open('bpmarine_courses.csv', 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=all_data[0].keys())
            writer.writeheader()
            writer.writerows(all_data)
    
    print(f"Scrape complete. Total batches: {len(all_data)}")

    if not all_data:
        # Every category failed, or the site's markup changed enough that no
        # selector matched anything. Exit non-zero so the GitHub Actions step
        # shows red instead of silently "succeeding" with zero data and
        # posting an empty array to the ingest endpoint.
        print("ERROR: no batches scraped from any category — failing the run.")
        sys.exit(1)

    return all_data

if __name__ == '__main__':
    asyncio.run(main())
