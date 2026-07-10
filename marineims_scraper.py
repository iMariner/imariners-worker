"""
Scraper for institutes running the "IMS X7" platform (by 49webstreet),
e.g. cmet.marineims.com. This is a Laravel-based multi-tenant SaaS -- each
institute gets its own subdomain on marineims.com. Unlike BP Marine or the
newer AppEx generation, this platform needs NO browser automation at all:
it's a clean, stateless JSON API once you have a CSRF token + session cookie.

Config-driven like appex_ui_scraper.py: add another dict to INSTITUTE_CONFIGS
to onboard a new marineims.com institute -- no new script needed.

Flow per institute:
  1. GET  /register                       -> scrape csrf-token meta tag, keep cookies
  2. POST /getcourses/typeoptionwise       (type=All) -> full course catalog incl. fees
  3. POST /frontendbooking/getbatchlist    (course=<id>) for each course -> batch rows

Output: one JSON array of "groups" (one dict per institute) posted to
/wp-json/imcfi/v1/ingest-marineims, matching ingest-marineims.php's expected
shape. Also writes marineims_courses.json / .csv locally for debugging,
mirroring bpmarine_scraper.py's output convention.
"""

import json
import csv
import os
import re
import sys
import requests

INSTITUTE_CONFIGS = [
    {
        # slug MUST match the existing DG-approved institute's slug in
        # wp_imcfi_institutes exactly, so the ingest endpoint updates that
        # record in place instead of creating a duplicate. This institute
        # was manually curated from the DG Shipping approved list -- do not
        # change this slug without also renaming the institute in wp-admin.
        "slug": "centre-for-maritime-education-and-training",
        "name": "Centre for Maritime Education And Training",
        "base_url": "https://cmet.marineims.com",
        "source_url": "https://cmet.marineims.com",
    },
    # Add more marineims.com institutes here as they're onboarded. Always
    # check wp-admin's Institutes page first for an existing DG-approved
    # entry and reuse its slug -- never invent a new slug for an institute
    # that's already manually curated there.
]

WP_INGEST_URL = os.environ.get("IMCFI_INGEST_URL", "https://imariners.com/wp-json/imcfi/v1/ingest-marineims")
WORKER_TOKEN = os.environ.get("WORKER_TOKEN", "")


def get_csrf_and_session(session, base_url):
    r = session.get(f"{base_url}/register", timeout=30)
    r.raise_for_status()
    m = re.search(r'name="csrf-token" content="([^"]+)"', r.text)
    if not m:
        raise RuntimeError(f"Could not find csrf-token on {base_url}/register")
    return m.group(1)


def post_form(session, base_url, path, token, fields):
    data = dict(fields)
    data["_token"] = token
    r = session.post(f"{base_url}{path}", data=data, headers={"X-Requested-With": "XMLHttpRequest"}, timeout=30)
    r.raise_for_status()
    return r.json()


def scrape_institute(cfg):
    session = requests.Session()
    base_url = cfg["base_url"]
    print(f"[{cfg['slug']}] fetching csrf token...", flush=True)
    token = get_csrf_and_session(session, base_url)

    print(f"[{cfg['slug']}] fetching course catalog...", flush=True)
    courses_resp = post_form(session, base_url, "/getcourses/typeoptionwise", token, {"type": "All"})
    courses = courses_resp.get("data", [])
    print(f"[{cfg['slug']}] {len(courses)} courses found", flush=True)

    batches = []
    for c in courses:
        course_id = c.get("id")
        if not course_id:
            continue
        try:
            batch_resp = post_form(session, base_url, "/frontendbooking/getbatchlist", token, {"course": str(course_id)})
        except Exception as e:
            print(f"[{cfg['slug']}]   course {course_id} ({c.get('name')}) FAILED: {e}", flush=True)
            continue

        for b in batch_resp.get("data", []):
            batches.append({
                "course_name": c.get("name"),
                "course_code": c.get("code"),
                "duration_days": c.get("duration"),
                "total_fee": c.get("fees"),
                "minimum_fee": c.get("minimum_booking_fees"),
                "course_status": c.get("status"),
                "batch_code": b.get("batch_code"),
                "start_date": b.get("start_date"),
                "end_date": b.get("end_date"),
                "cut_off_date": b.get("cut_off_date"),
                "batch_status": b.get("status"),
                "used_seats": b.get("used_seats"),
                "max_seats": c.get("max_no_of_students_allowed"),
            })

    print(f"[{cfg['slug']}] {len(batches)} batch rows collected", flush=True)
    return {
        "institute_slug": cfg["slug"],
        "institute_name": cfg["name"],
        "source_url": cfg["source_url"],
        "batches": batches,
    }


def main():
    if not WORKER_TOKEN:
        print("ERROR: WORKER_TOKEN env var not set", flush=True)
        sys.exit(1)

    groups = []
    for cfg in INSTITUTE_CONFIGS:
        try:
            groups.append(scrape_institute(cfg))
        except Exception as e:
            print(f"[{cfg['slug']}] SCRAPE FAILED: {e}", flush=True)

    with open("marineims_courses.json", "w", encoding="utf-8") as f:
        json.dump(groups, f, indent=2, ensure_ascii=False)

    all_rows = [dict(b, institute=g["institute_name"]) for g in groups for b in g["batches"]]
    if all_rows:
        with open("marineims_courses.csv", "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=all_rows[0].keys())
            writer.writeheader()
            writer.writerows(all_rows)

    print(f"Posting {sum(len(g['batches']) for g in groups)} total batch rows to {WP_INGEST_URL}", flush=True)
    resp = requests.post(
        WP_INGEST_URL,
        json=groups,
        headers={"X-IMCFI-Token": WORKER_TOKEN, "Content-Type": "application/json"},
        timeout=120,
    )
    print(f"Ingest response: {resp.status_code} {resp.text[:1000]}", flush=True)
    resp.raise_for_status()


if __name__ == "__main__":
    main()
