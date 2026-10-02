"""Push scraped batches to the iMariners STCW Course Finder plugin (imcfi/v1).

Used by every Python scraper, and from the command line by workflows that
produce a JSON file first:

    python ingest.py ingest-bpmarine bpmarine_courses.json

Why this exists: imariners.com sits behind Cloudflare, which answers requests
from GitHub Actions runners with a "Just a moment..." challenge page (HTTP 403).
The old push steps either crashed with an opaque traceback or, worse, used curl
without --fail and reported success while nothing was saved. This helper checks
the real response and fails the run with a clear explanation.
"""
import json
import os
import sys

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

USER_AGENT = "imariners-worker/1.0 (+https://github.com/iMariner/imariners-worker)"


def retry_session():
    """requests.Session that retries dropped connections and 5xx errors.

    Institute booking sites (MarineIMS in particular) sometimes close the
    connection mid-run ("RemoteDisconnected"); one blip used to drop that
    institute's whole catalogue for the run. Their POST endpoints only read
    data, so retrying them is safe.
    """
    retry = Retry(total=4, connect=4, read=4, backoff_factor=2,
                  status_forcelist=(500, 502, 503, 504),
                  allowed_methods=frozenset(["GET", "POST"]), raise_on_status=False)
    s = requests.Session()
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.mount("http://", HTTPAdapter(max_retries=retry))
    return s


def base_url():
    return (os.environ.get("WP_URL") or "https://imariners.com").rstrip("/")


def endpoint(name):
    """ingest-fosma -> https://imariners.com/wp-json/imcfi/v1/ingest-fosma"""
    return f"{base_url()}/wp-json/imcfi/v1/{name}"


def is_cloudflare_challenge(resp):
    return resp.status_code in (403, 503) and (
        resp.headers.get("cf-mitigated") == "challenge" or "Just a moment" in resp.text[:2000]
    )


def push(url, payload, label=""):
    token = os.environ.get("WORKER_TOKEN", "")
    if not token:
        sys.exit("ERROR: WORKER_TOKEN is not set (GitHub secret WORKER_TOKEN).")

    rows = payload_rows(payload)
    print(f"Posting {rows} batch rows{f' ({label})' if label else ''} to {url}", flush=True)
    resp = requests.post(
        url,
        json=payload,
        headers={"X-IMCFI-Token": token, "User-Agent": USER_AGENT, "Accept": "application/json"},
        timeout=180,
    )

    if is_cloudflare_challenge(resp):
        sys.exit(
            "ERROR: Cloudflare blocked the request with a bot challenge, so nothing was saved.\n"
            "Fix in Cloudflare: Security > WAF > Custom rules > add a rule that SKIPS security for\n"
            '  URI Path starts with "/wp-json/imcfi/v1/" AND header "X-IMCFI-Token" exists,\n'
            "and turn off Bot Fight Mode if it is enabled (it cannot be skipped by rules)."
        )
    if resp.status_code in (401, 403):
        sys.exit(f"ERROR: WordPress rejected the token ({resp.status_code}): {resp.text[:300]}\n"
                 "Check that the GitHub secret WORKER_TOKEN matches STCW Finder > Settings > Worker Token.")

    print(f"Ingest response: {resp.status_code} {resp.text[:1000]}", flush=True)
    if not resp.ok:
        sys.exit(f"ERROR: ingest failed with HTTP {resp.status_code}.")
    try:
        body = resp.json()
    except ValueError:
        sys.exit("ERROR: ingest returned a non-JSON response, so the save cannot be confirmed.")
    if isinstance(body, dict) and body.get("success") is False:
        sys.exit(f"ERROR: plugin reported failure: {body}")
    return body


def payload_rows(payload):
    if isinstance(payload, list):
        return sum(len(g.get("batches", [])) if isinstance(g, dict) and "batches" in g else 1 for g in payload)
    if isinstance(payload, dict):
        return len(payload.get("batches", []))
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit("usage: python ingest.py <endpoint-name> <file.json>")
    with open(sys.argv[2], encoding="utf-8") as f:
        data = json.load(f)
    push(endpoint(sys.argv[1]), data, label=sys.argv[2])
