#!/usr/bin/env bash
# Run every scraper from your own computer and push the results to imariners.com.
#
# Use this when GitHub Actions can't deliver (billing hold, or Cloudflare
# challenging GitHub's servers). Your home or office connection is not
# challenged by Cloudflare, so the same scrapers work from here.
#
#   ./run-local.sh            # everything
#   ./run-local.sh fast       # skip the two slow browser-based scrapers
#
# You will be asked for the Worker Token once (STCW Finder > Settings in
# wp-admin). It is not printed or saved anywhere.
set -u
cd "$(dirname "$0")"

if [ -z "${WORKER_TOKEN:-}" ]; then
  printf "Worker Token (STCW Finder > Settings): "
  stty -echo; read -r WORKER_TOKEN; stty echo; echo
fi
export WORKER_TOKEN
export WP_URL="${WP_URL:-https://imariners.com}"

python3 -m venv .venv >/dev/null 2>&1 || true
# shellcheck disable=SC1091
. .venv/bin/activate
pip install -q requests beautifulsoup4 playwright
[ -d node_modules ] || npm ci --silent

failed=()
run() { echo; echo "=== $1"; shift; "$@" || failed+=("$*"); }

run "AppEx API institutes (worker.js)" node worker.js
run "MarineIMS institutes"            python marineims_scraper.py
run "FOSMA"                           python fosma_scraper.py
run "SIMF"                            python simf_scraper.py

if [ "${1:-}" != "fast" ]; then
  python -m playwright install chromium >/dev/null
  run "BP Marine" sh -c 'python bpmarine_scraper.py && python ingest.py ingest-bpmarine bpmarine_courses.json'
  run "AppEx UI institutes" sh -c 'python appex_ui_scraper.py && python ingest.py ingest-appex-ui appex_ui_courses.json'
fi

echo
if [ ${#failed[@]} -eq 0 ]; then
  echo "All scrapers finished and pushed successfully."
else
  echo "Finished with ${#failed[@]} failure(s):"; printf '  - %s\n' "${failed[@]}"; exit 1
fi
