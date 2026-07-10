"""
Scraper for fosma.net (FOSMA Maritime Institute & Research Organisation --
Noida and Kolkata centres). This is a classic session-based ASP.NET MVC app,
not a JSON API, so the flow is a bit more involved than marineims.com but
still needs no browser automation (Playwright) -- plain requests + a
persistent session/cookie jar is enough.

Flow:
  1. GET  /CourseBooking/FosmaInstitute
     -> server-rendered page listing BOTH institutes' ongoing/future batches
        (course name, category, batch code, date range). No fees here.
        Reliable section boundaries in the rendered text are the phrases
        "View Noida Batches" / "View Kolkata Batches" (NOT the "NOIDA"/
        "KOLKATA" toggle-button labels, which are mislabeled/reversed).
  2. For each institute, POST institute=NOIDA|KOLKATA to the same URL to
     select it in session (kept for parity with the site's own flow; the
     batch listing itself is not actually institute-scoped server-side).
  3. Walk the "Select Program" list: POST item.ApId/item.ApTypeId to
     /CourseBooking/FosmaPrograms, then GET
     /CourseBooking/CourseBatches/OnGoing and pull the hidden fields
     item.Fees / item.FemaleDiscount / item.GenericDiscount out of the form.
     Fee is a per-program attribute, constant across all of that program's
     batches -- NOT per batch, NOT per institute.
  4. Fuzzy-match (token overlap ratio, threshold 0.5) each batch's course
     name against the ~23 program names that have fee data. Batches with no
     match simply have no fee -- normal "Contact Institute" fallback for
     roughly a quarter of batches whose course isn't in the bookable
     program list, this is expected, not a bug.
  5. Post one combined JSON array (one group per institute) to
     /wp-json/imcfi/v1/ingest-fosma, matching ingest-fosma.php's shape,
     with fees=FULL fee only (female_discount_pct passed through as
     metadata, never used to reduce the fee).

Config-driven like the other *_scraper.py files: add another dict to
INSTITUTE_CONFIGS to onboard more fosma.net centres. slug MUST match the
existing DG-approved institute's slug in wp_imcfi_institutes exactly.
"""

import json
import csv
import os
import re
import sys
from datetime import datetime

import requests

BASE_URL = "https://fosma.net"

INSTITUTE_CONFIGS = [
    {
        "slug": "fosma-maritime-institute-research-organisation",
        "name": "Fosma Maritime Institute & Research Organisation",
        "session_value": "NOIDA",
        "section_marker": "View Noida Batches",
        "next_marker": "View Kolkata Batches",
        "source_url": f"{BASE_URL}/CourseBooking/FosmaInstitute",
    },
    # Kolkata centre has its own separate wp_imcfi_institutes row
    # ("Fosma Maritime Institute & Research Organisation (Kol)") -- out of
    # scope for this run per Ajit's Noida-specific request, but the config
    # below is ready if/when that centre needs onboarding too:
    # {
    #     "slug": "fosma-maritime-institute-research-organisation-kol",
    #     "name": "Fosma Maritime Institute & Research Organisation (Kol)",
    #     "session_value": "KOLKATA",
    #     "section_marker": "View Kolkata Batches",
    #     "next_marker": None,
    #     "source_url": f"{BASE_URL}/CourseBooking/FosmaInstitute",
    # },
]

WP_INGEST_URL = os.environ.get("IMCFI_INGEST_URL", "https://imariners.com/wp-json/imcfi/v1/ingest-fosma")
WORKER_TOKEN = os.environ.get("WORKER_TOKEN", "")

MONTHS = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
}


def parse_date(txt):
    """'07 Jul 2026' -> '2026-07-07'"""
    m = re.match(r"(\d{1,2})\s+([A-Za-z]{3})\s+(\d{4})", txt.strip())
    if not m:
        return None
    day, mon, year = m.groups()
    month = MONTHS.get(mon[:3].title())
    if not month:
        return None
    return f"{year}-{month:02d}-{int(day):02d}"


def tokenize(name):
    return set(re.findall(r"[a-z0-9]+", name.lower()))


def fuzzy_match(course_name, fee_map):
    """Token-overlap-ratio match against the fee-bearing program list.
    Returns the best fee entry if overlap ratio >= 0.5, else None."""
    tokens = tokenize(course_name)
    if not tokens:
        return None
    best_score, best_entry = 0, None
    for entry in fee_map:
        entry_tokens = tokenize(entry["name"])
        if not entry_tokens:
            continue
        overlap = len(tokens & entry_tokens)
        ratio = overlap / min(len(tokens), len(entry_tokens))
        if ratio > best_score:
            best_score, best_entry = ratio, entry
    return best_entry if best_score >= 0.5 else None


def get_program_list(session):
    """GET the FosmaPrograms page and parse out {apId, apTypeId, name}
    from the Select Program dropdown's <option> tags."""
    r = session.get(f"{BASE_URL}/CourseBooking/FosmaPrograms", timeout=30)
    r.raise_for_status()
    # Options look like: <option value="3|2">Second Mate Competency</option>
    programs = []
    for m in re.finditer(r'<option\s+value="(\d+)\|(\d+)"[^>]*>([^<]+)</option>', r.text):
        ap_id, ap_type_id, name = m.groups()
        programs.append({"apId": ap_id, "apTypeId": ap_type_id, "name": name.strip()})
    return programs


def get_fee_for_program(session, program):
    """POST the program selection, then GET OnGoing and scrape hidden
    item.Fees / item.FemaleDiscount / item.GenericDiscount fields."""
    session.post(
        f"{BASE_URL}/CourseBooking/FosmaPrograms",
        data={"item.ApId": program["apId"], "item.ApTypeId": program["apTypeId"]},
        timeout=30,
    )
    r = session.get(f"{BASE_URL}/CourseBooking/CourseBatches/OnGoing", timeout=30)
    r.raise_for_status()

    def hidden(field):
        m = re.search(rf'name="{re.escape(field)}"[^>]*value="([^"]*)"', r.text)
        return m.group(1) if m else None

    return {
        "name": program["name"],
        "fees": hidden("item.Fees"),
        "female_discount": hidden("item.FemaleDiscount"),
        "generic_discount": hidden("item.GenericDiscount"),
    }


def parse_batches(section_text):
    """Parse the rendered batch listing text for one institute's section.
    Each batch block looks roughly like:
      <Category>
      <Course Name>
      <Batch Code>
      <DD Mon YYYY> - <DD Mon YYYY>
      Status (Ongoing/Upcoming)
    This mirrors the state-machine parsing done live in-browser against
    document.body.innerText; adapt field regexes here if fosma.net changes
    its markup/wording.
    """
    batches = []
    date_range_re = re.compile(r"(\d{1,2}\s+[A-Za-z]{3}\s+\d{4})\s*-\s*(\d{1,2}\s+[A-Za-z]{3}\s+\d{4})")
    lines = [ln.strip() for ln in section_text.splitlines() if ln.strip()]

    i = 0
    current_category = None
    current_course = None
    while i < len(lines):
        line = lines[i]
        date_match = date_range_re.search(line)
        if date_match:
            start = parse_date(date_match.group(1))
            end = parse_date(date_match.group(2))
            # Batch code is usually the line just before the date range
            batch_code = lines[i - 1] if i > 0 else ""
            status = "active"
            if i + 1 < len(lines) and re.search(r"upcoming|future", lines[i + 1], re.I):
                status = "future"
            if current_course and start:
                batches.append({
                    "category": current_category or "",
                    "course_name": current_course,
                    "batch_code": batch_code,
                    "start_date": start,
                    "end_date": end,
                    "batch_status": status,
                })
        elif re.match(r"^(Pre[- ]?Sea|Post[- ]?Sea|STCW|GME|DG Shipping|Simulator|Refresher)", line, re.I):
            current_category = line
        elif not date_match and len(line) > 3 and not re.match(r"^(Ongoing|Upcoming|Status|View)", line, re.I):
            # Heuristic: treat a standalone line as a course name if it's
            # not a status/category/nav marker and doesn't look like a code
            if not re.match(r"^[A-Z0-9/.\-]+$", line):
                current_course = line
        i += 1

    return batches


def scrape_institute(cfg, program_fee_map):
    session = requests.Session()
    print(f"[{cfg['slug']}] fetching FosmaInstitute batch listing...", flush=True)

    session.post(
        f"{BASE_URL}/CourseBooking/FosmaInstitute",
        data={"institute": cfg["session_value"]},
        timeout=30,
    )
    r = session.get(f"{BASE_URL}/CourseBooking/FosmaInstitute", timeout=30)
    r.raise_for_status()

    text = re.sub(r"<[^>]+>", "\n", r.text)  # crude tag-strip to approximate rendered text
    start_idx = text.find(cfg["section_marker"])
    end_idx = text.find(cfg["next_marker"]) if cfg.get("next_marker") else -1
    if start_idx == -1:
        print(f"[{cfg['slug']}] WARNING: section marker '{cfg['section_marker']}' not found", flush=True)
        section_text = text
    else:
        section_text = text[start_idx:end_idx] if end_idx != -1 else text[start_idx:]

    raw_batches = parse_batches(section_text)
    print(f"[{cfg['slug']}] {len(raw_batches)} batches parsed", flush=True)

    batches = []
    unmatched = 0
    for b in raw_batches:
        fee_entry = fuzzy_match(b["course_name"], program_fee_map)
        fees = None
        female_discount_pct = None
        if fee_entry and fee_entry.get("fees"):
            try:
                fees = float(fee_entry["fees"])
            except (TypeError, ValueError):
                fees = None
            try:
                female_discount_pct = float(fee_entry["female_discount"]) if fee_entry.get("female_discount") else None
            except (TypeError, ValueError):
                female_discount_pct = None
        if fees is None:
            unmatched += 1

        batches.append({
            "course_name": b["course_name"],
            "category": b["category"],
            "batch_code": b["batch_code"],
            "start_date": b["start_date"],
            "end_date": b["end_date"],
            "batch_status": b["batch_status"],
            "fees": fees,  # FULL fee -- never the discounted women's rate
            "female_discount_pct": female_discount_pct,
        })

    print(f"[{cfg['slug']}] {unmatched}/{len(batches)} batches have no fee match (will show 'Contact Institute')", flush=True)

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

    session = requests.Session()
    print("Fetching program list + fees from FosmaPrograms...", flush=True)
    programs = get_program_list(session)
    print(f"{len(programs)} selectable programs found", flush=True)

    program_fee_map = []
    for p in programs:
        try:
            program_fee_map.append(get_fee_for_program(session, p))
        except Exception as e:
            print(f"  program {p['name']} FAILED: {e}", flush=True)

    groups = []
    for cfg in INSTITUTE_CONFIGS:
        try:
            groups.append(scrape_institute(cfg, program_fee_map))
        except Exception as e:
            print(f"[{cfg['slug']}] SCRAPE FAILED: {e}", flush=True)

    with open("fosma_courses.json", "w", encoding="utf-8") as f:
        json.dump(groups, f, indent=2, ensure_ascii=False)

    all_rows = [dict(b, institute=g["institute_name"]) for g in groups for b in g["batches"]]
    if all_rows:
        with open("fosma_courses.csv", "w", newline="", encoding="utf-8") as f:
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
