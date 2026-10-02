"""
Scraper for Anvay Maritime Institute (Navi Mumbai).

Anvay moved its online booking off APPEx (ami.appexonline.com now only holds a
"DND1 - Do not touch" placeholder course) to its own site,
https://www.anvayonline.com/booking, an ASP.NET Razor page with three public
JSON handlers that its booking form calls:

  GET /booking/Index?handler=Courses&type=<Basic Course|Advance Course|...>
      -> [{"courseId": 25, "courseName": "EFA"}, ...]
  GET /booking/Index?handler=Batches&courseId=<id>
      -> [{"cbId": 794, "cbNo": "28 Oct 2026 - 30 Oct 2026"}, ...]
  GET /booking/Index?handler=Fees&courseId=<id>&cbId=<cbId>
      -> {"actualFees": 7000.0, "sellingFees": 7000.0, ...}

Output matches ingest-marineims.php's batch schema (course_name, start_date,
end_date, total_fee) and posts to that endpoint, like simf_scraper.py.
Combo packages are skipped: they bundle several courses and don't map onto a
single course in the finder.
"""
import csv
import json
import os
import re
import sys
from datetime import datetime

from ingest import push, retry_session

INSTITUTE_SLUG = "anvay-maritime-institute"
INSTITUTE_NAME = "Anvay Maritime Institute"
BASE_URL = "https://www.anvayonline.com/booking/"
COURSE_TYPES = ["Basic Course", "Advance Course"]

WP_INGEST_URL = os.environ.get("IMCFI_INGEST_URL", "https://imariners.com/wp-json/imcfi/v1/ingest-marineims")
WORKER_TOKEN = os.environ.get("WORKER_TOKEN", "")


def to_iso(text):
    return datetime.strptime(text.strip(), "%d %b %Y").strftime("%Y-%m-%d")


def parse_range(label):
    """'28 Oct 2026 - 30 Oct 2026' (any dash) -> ('2026-10-28', '2026-10-30')"""
    parts = re.split(r"\s*[–—-]\s*", label.strip(), maxsplit=1)
    start = to_iso(parts[0])
    end = to_iso(parts[1]) if len(parts) > 1 and parts[1] else start
    return start, end


def main():
    if not WORKER_TOKEN:
        sys.exit("ERROR: WORKER_TOKEN env var not set")

    s = retry_session()
    s.headers["User-Agent"] = "Mozilla/5.0 (imariners-worker)"
    get = lambda handler, **params: s.get(BASE_URL + "Index", params={"handler": handler, **params}, timeout=30).json()

    batches = []
    for ctype in COURSE_TYPES:
        courses = get("Courses", type=ctype)
        print(f"[{INSTITUTE_SLUG}] {ctype}: {len(courses)} courses", flush=True)
        for c in courses:
            try:
                for b in get("Batches", courseId=c["courseId"]):
                    try:
                        start, end = parse_range(b["cbNo"])
                    except ValueError:
                        print(f"  skip unparseable batch label {b['cbNo']!r}", flush=True)
                        continue
                    fee = None
                    try:
                        f = get("Fees", courseId=c["courseId"], cbId=b["cbId"])
                        fee = f.get("sellingFees") or f.get("actualFees") or None
                    except Exception as e:
                        print(f"  fee lookup failed for {c['courseName']} {b['cbNo']}: {e}", flush=True)
                    batches.append({
                        "course_name": c["courseName"].strip(),
                        "course_code": str(c["courseId"]),
                        "batch_code": str(b["cbId"]),
                        "start_date": start,
                        "end_date": end,
                        "total_fee": fee,
                    })
            except Exception as e:
                print(f"[{INSTITUTE_SLUG}]   course {c.get('courseId')} ({c.get('courseName')}) FAILED: {e}", flush=True)

    print(f"[{INSTITUTE_SLUG}] {len(batches)} batch rows collected", flush=True)
    group = {
        "institute_slug": INSTITUTE_SLUG,
        "institute_name": INSTITUTE_NAME,
        "source_url": BASE_URL,
        "batches": batches,
    }
    with open("anvay_courses.json", "w", encoding="utf-8") as f:
        json.dump([group], f, indent=2, ensure_ascii=False)
    if batches:
        with open("anvay_courses.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=batches[0].keys())
            w.writeheader()
            w.writerows(batches)
    else:
        sys.exit("ERROR: no Anvay batches scraped -- failing the run.")

    push(WP_INGEST_URL, [group])


if __name__ == "__main__":
    main()
