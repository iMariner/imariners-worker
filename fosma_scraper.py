"""
Scraper for fosma.net (FOSMA Maritime Institute & Research Organisation --
Noida and Kolkata centres, each with its own wp_imcfi_institutes row).
Classic session-based ASP.NET MVC app, not a JSON API -- needs no browser
automation (Playwright), plain requests + a persistent session/cookie jar
+ BeautifulSoup for parsing is enough.

Flow per institute:
  1. POST institute=NOIDA|KOLKATA to /CourseBooking/FosmaInstitute to select
     it in session.
  2. GET /CourseBooking/FosmaPrograms -- this "Select Program" list IS
     institute-specific (Noida has 23 bookable programs, Kolkata has 32),
     unlike the batch listing below which shows both institutes at once.
     Each program is its own tiny <form> containing hidden
     item.ApId / item.ApTypeId inputs and a submit button whose value is
     the program name.
  3. For each program: POST item.ApId/item.ApTypeId to FosmaPrograms, then
     GET /CourseBooking/CourseBatches/OnGoing and scrape the hidden
     item.Fees / item.FemaleDiscount fields. Fee is a per-program
     attribute, constant across all of that program's batches.
  4. GET /CourseBooking/FosmaInstitute once (fetched ONCE for both
     institutes in main(), not per-institute -- confirmed live that this
     page renders BOTH institutes' on-going/future batches regardless of
     which institute is selected in session). Reliable section boundaries
     are the literal phrases "View Noida Batches" / "View Kolkata
     Batches" (NOT the "NOIDA"/"KOLKATA" toggle-button labels, which are
     mislabeled/reversed). Each institute's div.row1 batch cards share one
     common parent element, but Noida's and Kolkata's parents are two
     DIFFERENT elements -- so we locate each marker, use BeautifulSoup's
     find_all_next() to get that institute's row1 cards in document order,
     then walk their shared parent's direct children to associate each
     batch with its nearest preceding course-name/category header (a
     course's header only precedes its FIRST batch; later batches of the
     same course have no repeated header). Each batch card contains
     "Batch: <code> ... Duration <start> - <end>".
  5. Fuzzy-match (token overlap ratio, threshold 0.5) each batch's course
     name against that institute's own fee map. Batches with no match
     simply have no fee -- normal "Contact Institute" fallback, expected
     for courses not in the bookable "Select Program" list, not a bug.
  6. Post one combined JSON array (one group per institute) to
     /wp-json/imcfi/v1/ingest-fosma, matching ingest-fosma.php's shape,
     fees=FULL fee only (female_discount_pct passed through as metadata,
     never used to reduce the fee).
"""

import json
import csv
import os
import re
import sys

import requests

from ingest import push, retry_session
from bs4 import BeautifulSoup

BASE_URL = "https://fosma.net"

INSTITUTE_CONFIGS = [
    {
        "slug": "fosma-maritime-institute-research-organisation",
        "name": "Fosma Maritime Institute & Research Organisation",
        "session_value": "NOIDA",
        "section_marker": "View Noida Batches",
    },
    {
        "slug": "fosma-maritime-institute-research-organisation-kol",
        "name": "Fosma Maritime Institute & Research Organisation (Kol)",
        "session_value": "KOLKATA",
        "section_marker": "View Kolkata Batches",
    },
    # Add more fosma.net centres here as they're onboarded. Always check
    # wp-admin's Institutes page first for an existing DG-approved entry
    # and reuse its slug -- never invent a new slug for an institute
    # that's already manually curated there.
]

WP_INGEST_URL = os.environ.get("IMCFI_INGEST_URL", "https://imariners.com/wp-json/imcfi/v1/ingest-fosma")
WORKER_TOKEN = os.environ.get("WORKER_TOKEN", "")
SOURCE_URL = f"{BASE_URL}/CourseBooking/FosmaInstitute"

MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

BATCH_RE = re.compile(
    r"Batch:\s*(.*?)\s*Duration\s*(\d{1,2}\s+[A-Za-z]{3,}\s+\d{4})\s*-\s*(\d{1,2}\s+[A-Za-z]{3,}\s+\d{4})",
    re.S,
)


def parse_date(txt):
    """'07 Jul 2026' -> '2026-07-07'"""
    m = re.match(r"(\d{1,2})\s+([A-Za-z]{3,})\s+(\d{4})", txt.strip())
    if not m:
        return None
    day, mon, year = m.groups()
    month = MONTHS.get(mon[:3].lower())
    if not month:
        return None
    return f"{year}-{month:02d}-{int(day):02d}"


def tokenize(name):
    return set(re.findall(r"[a-z0-9]+", name.lower()))


def fuzzy_match(course_name, fee_map):
    """Token-overlap-ratio match against the fee-bearing program list for
    THIS institute. Returns the best fee entry if overlap ratio >= 0.5."""
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


def get_program_fee_map(session):
    """GET the FosmaPrograms page (institute-specific list) and walk each
    program to collect its fee/discount fields."""
    r = session.get(f"{BASE_URL}/CourseBooking/FosmaPrograms", timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    programs = []
    for form in soup.find_all("form"):
        ap_id_input = form.find("input", id="item_ApId")
        if not ap_id_input:
            continue
        ap_type_input = form.find("input", id="item_ApTypeId")
        submit_input = form.find("input", attrs={"type": "submit"})
        programs.append({
            "apId": ap_id_input.get("value"),
            "apTypeId": ap_type_input.get("value") if ap_type_input else None,
            "name": (submit_input.get("value") or "").strip() if submit_input else "",
        })

    fee_map = []
    for p in programs:
        session.post(
            f"{BASE_URL}/CourseBooking/FosmaPrograms",
            data={"item.ApId": p["apId"], "item.ApTypeId": p["apTypeId"]},
            timeout=30,
        )
        r2 = session.get(f"{BASE_URL}/CourseBooking/CourseBatches/OnGoing", timeout=30)
        fee_m = re.search(r'name="item\.Fees"[^>]*value="([^"]*)"', r2.text)
        fem_m = re.search(r'name="item\.FemaleDiscount"[^>]*value="([^"]*)"', r2.text)
        fee_map.append({
            "name": p["name"],
            "fees": fee_m.group(1) if fee_m else None,
            "female_discount": fem_m.group(1) if fem_m else None,
        })
    return fee_map


def _find_marker_element(soup, marker_text):
    """Find the element containing the given marker phrase as a text node,
    matching the browser-verified approach exactly (walk text nodes, not
    a naive string search, since the phrase may be split by whitespace)."""
    for string in soup.find_all(string=True):
        if marker_text in string:
            return string.parent
    return None


def parse_institute_section(soup, section_marker, next_marker=None):
    """Parse the batch cards + preceding course-name headers belonging to
    ONE institute's section within the full (already-parsed) FosmaInstitute
    page. Both institutes' sections live in the SAME document but under
    two DIFFERENT parent containers -- confirmed live (each institute's
    div.row1 batch cards share one common parent, but Noida's and
    Kolkata's parents are two distinct elements). So instead of fragile
    string-slicing, we:
      1. locate the marker element (e.g. "View Noida Batches"),
      2. use BeautifulSoup's document-order traversal (find_all_next) to
         get all div.row1 batch cards that come after this marker (and,
         if there's a next_marker, before it),
      3. take the row1 elements' shared parent and walk ITS direct
         children in order, exactly mirroring the browser-verified JS
         state machine: track the last two non-row1 sibling texts as
         (category, course_name), and read the batch fields off each
         row1 sibling as it's encountered.
    Returns a list of {category, course_name, batch_code, start_raw, end_raw}.
    """
    marker_el = _find_marker_element(soup, section_marker)
    if marker_el is None:
        return []

    row1_after_marker = marker_el.find_all_next("div", class_="row1")
    if not row1_after_marker:
        return []

    if next_marker:
        next_marker_el = _find_marker_element(soup, next_marker)
        if next_marker_el is not None:
            row1_after_next = next_marker_el.find_all_next("div", class_="row1")
            cutoff = len(row1_after_marker) - len(row1_after_next)
            institute_row1s = row1_after_marker[:cutoff]
        else:
            institute_row1s = row1_after_marker
    else:
        institute_row1s = row1_after_marker

    if not institute_row1s:
        return []

    parent = institute_row1s[0].parent
    children = parent.find_all(recursive=False)

    results = []
    last_texts = []
    for child in children:
        classes = child.get("class") or []
        if "row1" in classes:
            card = child.find("div", class_=lambda c: c and "card" in c and "bg-light" in c)
            if not card:
                continue
            card_text = re.sub(r"\s+", " ", card.get_text(" ", strip=True))
            m = BATCH_RE.search(card_text)
            if not m:
                continue
            results.append({
                "category": last_texts[0] if len(last_texts) > 0 else "",
                "course_name": last_texts[1] if len(last_texts) > 1 else (last_texts[0] if last_texts else ""),
                "batch_code": m.group(1).strip(),
                "start_raw": m.group(2),
                "end_raw": m.group(3),
            })
        else:
            text = re.sub(r"\s+", " ", child.get_text(" ", strip=True))
            if text:
                last_texts.append(text)
                if len(last_texts) > 2:
                    last_texts.pop(0)

    return results


def scrape_institute(cfg, all_markers, shared_soup):
    """shared_soup is the ONE BeautifulSoup-parsed FosmaInstitute page --
    confirmed live that this page renders BOTH institutes' batches
    regardless of which institute is selected in session, so we only need
    to fetch + parse it once (see fetch_shared_institute_soup) and reuse
    it here purely for section extraction. Only the fee walk below is
    genuinely institute-specific (each institute has its own "Select
    Program" list)."""
    session = retry_session()
    print(f"[{cfg['slug']}] selecting institute + fetching program/fee list...", flush=True)

    session.post(
        f"{BASE_URL}/CourseBooking/FosmaInstitute",
        data={"institute": cfg["session_value"]},
        timeout=30,
    )

    fee_map = get_program_fee_map(session)
    print(f"[{cfg['slug']}] {len(fee_map)} programs/fees fetched", flush=True)

    idx = all_markers.index(cfg["section_marker"])
    next_marker = all_markers[idx + 1] if idx + 1 < len(all_markers) else None
    raw_batches = parse_institute_section(shared_soup, cfg["section_marker"], next_marker)
    print(f"[{cfg['slug']}] {len(raw_batches)} batches parsed", flush=True)

    batches = []
    unmatched = 0
    for b in raw_batches:
        start = parse_date(b["start_raw"])
        end = parse_date(b["end_raw"])
        if not start:
            continue
        fee_entry = fuzzy_match(b["course_name"], fee_map)
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
            "start_date": start,
            "end_date": end,
            "batch_status": "active",
            "fees": fees,  # FULL fee -- never the discounted women's rate
            "female_discount_pct": female_discount_pct,
        })

    print(f"[{cfg['slug']}] {unmatched}/{len(batches)} batches have no fee match (will show 'Contact Institute')", flush=True)

    return {
        "institute_slug": cfg["slug"],
        "institute_name": cfg["name"],
        "source_url": SOURCE_URL,
        "batches": batches,
    }


def main():
    if not WORKER_TOKEN:
        print("ERROR: WORKER_TOKEN env var not set", flush=True)
        sys.exit(1)

    all_markers = [cfg["section_marker"] for cfg in INSTITUTE_CONFIGS]

    print("Fetching shared FosmaInstitute batch listing page (covers all institutes)...", flush=True)
    shared_session = retry_session()
    shared_r = shared_session.get(f"{BASE_URL}/CourseBooking/FosmaInstitute", timeout=30)
    shared_r.raise_for_status()
    shared_soup = BeautifulSoup(shared_r.text, "html.parser")

    groups = []
    for cfg in INSTITUTE_CONFIGS:
        try:
            groups.append(scrape_institute(cfg, all_markers, shared_soup))
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

    push(WP_INGEST_URL, groups)


if __name__ == "__main__":
    main()
