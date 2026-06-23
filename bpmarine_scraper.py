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

async def scrape_category(page, category_id, category_name):
    """Scrape all courses and batches for a single category."""
    results = []
    
    # Step 1: Select category
    await page.select_option('#ContentPlaceHolder1_ContentPlaceHolder2_ddlCategory', category_id)
    await page.wait_for_timeout(2500)  # Wait for UpdatePanel
    
    # Step 2: Get all course options
    course_options = await page.query_selector_all(
        '#ContentPlaceHolder1_ContentPlaceHolder2_ddlCourseName option'
    )
    
    for opt in course_options[1:]:  # Skip "-- Select --"
        course_id = await opt.get_attribute('value')
        course_name = await opt.text_content()
        if not course_id or not course_name:
            continue
        
        # Select the course
        await page.select_option(
            '#ContentPlaceHolder1_ContentPlaceHolder2_ddlCourseName', 
            course_id
        )
        await page.wait_for_timeout(2500)
        
        # Step 3: Get all date options
        date_options = await page.query_selector_all(
            '#ContentPlaceHolder1_ContentPlaceHolder2_ddlCommencementDate option'
        )
        
        for date_opt in date_options[1:]:
            batch_id = await date_opt.get_attribute('value')
            date_text = await date_opt.text_content()
            if not batch_id or not date_text:
                continue
            
            # Step 4: Select the date to reveal fees & duration
            await page.select_option(
                '#ContentPlaceHolder1_ContentPlaceHolder2_ddlCommencementDate',
                batch_id
            )
            await page.wait_for_timeout(2000)
            
            # Step 5: Extract the data
            duration = await page.text_content(
                '#ContentPlaceHolder1_ContentPlaceHolder2_lbl_duration'
            )
            fees = await page.text_content(
                '#ContentPlaceHolder1_ContentPlaceHolder2_lblcourse_fee'
            )
            
            results.append({
                'category_id': category_id,
                'category_name': category_name,
                'course_id': course_id,
                'course_name': course_name.strip(),
                'batch_id': batch_id,
                'commencement_date': date_text.strip(),
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
                # Fresh page for each category (prevents stale ViewState)
                await page.goto(BASE_URL)
                await page.wait_for_load_state('networkidle')

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
