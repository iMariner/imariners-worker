"""
Scraper for institutes running the "IMS X7" platform (by 49webstreet),
e.g. cmet.marineims.com. This is a Laravel-based multi-tenant SaaS -- each
institute gets its own subdomain on marineims.com. Unlike BP Marine or the
newer AppEx generation, this platform needs NO browser automation at all:
it's a clean, stateless JSON API once you have a CSRF token + session cookie.

Config-driven like appex_ui_scraper.py: add another dict to INSTITUTE_CONFIGS
to onboard a new marineims.com institute -- no new script needed.

Flow per institute:
  1. GET  {base_url}{register_path}         -> scrape csrf-token meta tag, keep cookies
  2. POST /getcourses/typeoptionwise        (type=All) -> full course catalog incl. fees
  3. POST /frontendbooking/getbatchlist     (course=<id>) for each course -> batch rows

register_path defaults to "/register" but some institutes on this platform
use "/course/register" instead -- override per-config as needed.

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

from ingest import push

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
    {
        # Matches wp-admin institute id 88, "MTI" (Shipping Corporation of
        # India's Maritime Training Institute -- site itself is branded
        # "Shipping Corporation of India Land and Assets Ltd").
        "slug": "mti",
        "name": "MTI",
        "base_url": "https://sci.marineims.com",
        "register_path": "/register",
        "source_url": "https://sci.marineims.com/register",
    },
    {
        # Matches wp-admin institute id 142, "Seven Islands Maritime
        # Training Institute".
        "slug": "seven-islands-maritime-training-institute",
        "name": "Seven Islands Maritime Training Institute",
        "base_url": "https://sis.marineims.com",
        "register_path": "/register/",
        "source_url": "https://sis.marineims.com/register/",
    },
    {
        # Matches wp-admin institute id 156, "The Institute of Marine
        # Engineers(India)" -- Mumbai centre. Uses /course/register instead
        # of /register for the CSRF-bearing page.
        "slug": "the-institute-of-marine-engineers-india",
        "name": "The Institute of Marine Engineers(India)",
        "base_url": "https://imeimum.marineims.com",
        "register_path": "/course/register",
        "source_url": "https://imeimum.marineims.com/course/register",
    },
    {
        # Matches wp-admin institute id 146, "Sriram Institute of Marine
        # Studies".
        "slug": "sriram-institute-of-marine-studies",
        "name": "Sriram Institute of Marine Studies",
        "base_url": "https://sims.marineims.com",
        "register_path": "/register",
        "source_url": "https://sims.marineims.com/register",
    },
    {
        # Matches wp-admin institute id 71, "Institute of Marine
        # Engineers(India) Cochin" -- Kochi centre. Also uses /course/register.
        "slug": "institute-of-marine-engineers-india-cochin",
        "name": "Institute of Marine Engineers(India) Cochin",
        "base_url": "https://imeikochi.marineims.com",
        "register_path": "/course/register",
        "source_url": "https://imeikochi.marineims.com/course/register",
    },
    {
        # Matches wp-admin institute id 171, "Zasha Institute of Maritime
        # Studies" (site itself is branded "Zasha Institute of Maritime
        # Studies (ZIMS) - Dehradun"). Note: a second, separately-curated
        # institute row "ZASHA MARITIME EDUCATION AND RESEARCH" (id 170)
        # also exists in wp-admin with the same contact email -- that is a
        # different record (likely the trust/legal name) and is NOT the one
        # this scraper should update.
        "slug": "zasha-institute-of-maritime-studies",
        "name": "Zasha Institute of Maritime Studies",
        "base_url": "https://zasha.marineims.com",
        "register_path": "/register",
        "source_url": "https://zasha.marineims.com/register",
    },
    # Add more marineims.com institutes here as they're onboarded. Always
    # check wp-admin's Institutes page first for an existing DG-approved
    # entry and reuse its slug -- never invent a new slug for an institute
    # that's already manually curated there.
]

WP_INGEST_URL = os.environ.get("IMCFI_INGEST_URL", "https://imariners.com/wp-json/imcfi/v1/ingest-marineims")
WORKER_TOKEN = os.environ.get("WORKER_TOKEN", "")


def get_csrf_and_session(session, base_url, register_path):
    r = session.get(f"{base_url}{register_path}", timeout=30)
    r.raise_for_status()
    m = re.search(r'name="csrf-token" content="([^"]+)"', r.text)
    if not m:
        raise RuntimeError(f"Could not find csrf-token on {base_url}{register_path}")
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
    register_path = cfg.get("register_path", "/register")
    print(f"[{cfg['slug']}] fetching csrf token...", flush=True)
    token = get_csrf_and_session(session, base_url, register_path)

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

    push(WP_INGEST_URL, groups)


if __name__ == "__main__":
    main()
