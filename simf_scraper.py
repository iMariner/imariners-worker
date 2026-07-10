"""
Scraper for Sakshi Institute of Marine Foundation (SIMF), simfedu.com.

This is a custom Laravel booking site (not on the shared MarineIMS or AppEx
SaaS platforms), but the flow is just as simple as MarineIMS -- no browser
automation needed at all:

1. GET  /booking            -> scrape the Laravel CSRF token
                                (input[name="_token"]) AND the full course
                                list from the "Select Course" dropdown
                                (<select name="courses[0]">) -- every course
                                across every course type is already present
                                in this single select on the static page, so
                                there's no need to walk course types first.
2. POST /getbatches          (form field "standard_type" = course_id, plus
                               _token, X-Requested-With: XMLHttpRequest) for
                               each course id -> JSON array of batches:
                               [{"id":.., "batch_name":.., "batch_start_date":..,
                                 "batch_end_date":.., "batch_fee":..}, ...]

Dates already come back as "YYYY-MM-DD" and there's a single flat fee field
per batch (no separate min/full or female-discount split like FOSMA), so the
output shape matches ingest-marineims.php's expected batch schema (verified
directly against that file's source): required fields are course_name,
start_date, end_date (all three must be non-empty -- SIMF's /getbatches
always returns both dates, so this is satisfied), and the fee column reads
$item['total_fee'] specifically (NOT 'fees' -- that's a different ingest
route's field name). This script posts to that SAME existing
ingest-marineims endpoint rather than needing a new PHP ingest route.
"""

import json
import csv
import os
import sys
import requests
from bs4 import BeautifulSoup

INSTITUTE_SLUG = "sakshi-institute-of-maritime-foundation"
INSTITUTE_NAME = "Sakshi Institute of Maritime Foundation"
BASE_URL = "https://www.simfedu.com"
BOOKING_URL = f"{BASE_URL}/booking"

WP_INGEST_URL = os.environ.get("IMCFI_INGEST_URL", "https://imariners.com/wp-json/imcfi/v1/ingest-marineims")
WORKER_TOKEN = os.environ.get("WORKER_TOKEN", "")


def get_token_and_courses(session):
    r = session.get(BOOKING_URL, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    token_input = soup.find("input", {"name": "_token"})
    if not token_input or not token_input.get("value"):
        raise RuntimeError("Could not find CSRF token on booking page")
    token = token_input["value"]

    course_select = soup.find("select", {"name": "courses[0]"})
    if not course_select:
        raise RuntimeError('Could not find <select name="courses[0]"> on booking page')

    courses = []
    for opt in course_select.find_all("option"):
        val = opt.get("value")
        if not val:
            continue
        courses.append({"id": val, "name": opt.get_text(strip=True)})
    return token, courses


def get_batches(session, token, course_id):
    r = session.post(
        f"{BASE_URL}/getbatches",
        data={"_token": token, "standard_type": course_id},
        headers={"X-Requested-With": "XMLHttpRequest"},
        timeout=30,
    )
    r.raise_for_status()
    try:
        return r.json()
    except ValueError:
        return []


def main():
    if not WORKER_TOKEN:
        print("ERROR: WORKER_TOKEN env var not set", flush=True)
        sys.exit(1)

    session = requests.Session()
    print(f"[{INSTITUTE_SLUG}] fetching csrf token + course list...", flush=True)
    token, courses = get_token_and_courses(session)
    print(f"[{INSTITUTE_SLUG}] {len(courses)} courses found", flush=True)

    batches = []
    for c in courses:
        try:
            rows = get_batches(session, token, c["id"])
        except Exception as e:
            print(f"[{INSTITUTE_SLUG}]   course {c['id']} ({c['name']}) FAILED: {e}", flush=True)
            continue

        for b in rows:
            batches.append({
                "course_name": c["name"],
                "batch_code": b.get("batch_name"),
                "start_date": b.get("batch_start_date"),
                "end_date": b.get("batch_end_date"),
                "total_fee": b.get("batch_fee"),
            })

    print(f"[{INSTITUTE_SLUG}] {len(batches)} batch rows collected", flush=True)

    group = {
        "institute_slug": INSTITUTE_SLUG,
        "institute_name": INSTITUTE_NAME,
        "source_url": BOOKING_URL,
        "batches": batches,
    }

    with open("simf_courses.json", "w", encoding="utf-8") as f:
        json.dump([group], f, indent=2, ensure_ascii=False)

    if batches:
        with open("simf_courses.csv", "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=batches[0].keys())
            writer.writeheader()
            writer.writerows(batches)

    print(f"Posting {len(batches)} total batch rows to {WP_INGEST_URL}", flush=True)
    resp = requests.post(
        WP_INGEST_URL,
        json=[group],
        headers={"X-IMCFI-Token": WORKER_TOKEN, "Content-Type": "application/json"},
        timeout=120,
    )
    print(f"Ingest response: {resp.status_code} {resp.text[:1000]}", flush=True)
    resp.raise_for_status()


if __name__ == "__main__":
    main()
