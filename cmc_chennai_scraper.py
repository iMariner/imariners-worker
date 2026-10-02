"""
Scraper for CMC Maritime Academy, Chennai (Campus).

The academy's own booking page (https://www.cmcmaritimechennai.com/booking)
lists every course as a card and asks booking.php for each course's batches:

  POST /booking.php  type=getbatchData  category=<course name>
  -> "INR 6500#12-Oct-2026 (013/26),...#12-Oct-2026,...#6500,...#825,..."
      fee label # batch labels # start dates # fees # batch ids

Only start dates are published, so end_date is left empty, which the
ingest-appex-ui endpoint accepts ("no end date given"). Posts there in the same
group shape as appex_ui_scraper.py.
"""
import json
import os
import re
import sys
from datetime import datetime

from ingest import push, retry_session

INSTITUTE_SLUG = "cmc-maritime-academy-chennai-campus"
INSTITUTE_NAME = "CMC Maritime Academy, Chennai (Campus)"
BOOKING_URL = "https://www.cmcmaritimechennai.com/booking"
ENDPOINT = "https://www.cmcmaritimechennai.com/booking.php"
WP_INGEST_URL = os.environ.get("IMCFI_INGEST_URL", "https://imariners.com/wp-json/imcfi/v1/ingest-appex-ui")


def course_names(session):
    html = session.get(BOOKING_URL, timeout=30).text
    names = re.findall(r'class="course-card"\s+data-value="([^"]+)"', html)
    return list(dict.fromkeys(n.strip() for n in names if n.strip()))


def batches_for(session, course):
    raw = session.post(ENDPOINT, data={"type": "getbatchData", "category": course}, timeout=30).text
    parts = raw.split("#")
    if len(parts) < 4:
        return []
    labels, dates, fees = (p.split(",") for p in parts[1:4])
    out = []
    for i, d in enumerate(dates):
        d = d.strip()
        if not d:
            continue
        try:
            start = datetime.strptime(d, "%d-%b-%Y").strftime("%Y-%m-%d")
        except ValueError:
            print(f"  skip unparseable date {d!r} for {course}", flush=True)
            continue
        fee = None
        if i < len(fees) and fees[i].strip():
            try:
                fee = float(fees[i].replace(",", ""))
            except ValueError:
                pass
        out.append({
            "category": "",
            "mode": "Single Courses",
            "course": course,
            "batch_label": (labels[i].strip() if i < len(labels) else d),
            "commencement_date": start,
            "end_date": None,
            "seats_text": "",
            "fees": fee,
        })
    return out


def main():
    if not os.environ.get("WORKER_TOKEN"):
        sys.exit("ERROR: WORKER_TOKEN env var not set")
    s = retry_session()
    s.headers["User-Agent"] = "Mozilla/5.0 (imariners-worker)"
    names = course_names(s)
    print(f"[{INSTITUTE_SLUG}] {len(names)} courses on the booking page", flush=True)
    batches = []
    for n in names:
        try:
            batches.extend(batches_for(s, n))
        except Exception as e:
            print(f"[{INSTITUTE_SLUG}]   {n} FAILED: {e}", flush=True)
    print(f"[{INSTITUTE_SLUG}] {len(batches)} batch rows collected", flush=True)
    group = {
        "institute_slug": INSTITUTE_SLUG,
        "institute_name": INSTITUTE_NAME,
        "source_url": BOOKING_URL,
        "booking_url": BOOKING_URL,
        "batches": batches,
    }
    with open("cmc_chennai_courses.json", "w", encoding="utf-8") as f:
        json.dump([group], f, indent=2, ensure_ascii=False)
    if not batches:
        sys.exit("ERROR: no CMC Chennai batches scraped -- failing the run.")
    push(WP_INGEST_URL, [group])


if __name__ == "__main__":
    main()
