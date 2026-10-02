import axios from 'axios';

const WP_URL = process.env.WP_URL;
const WORKER_TOKEN = process.env.WORKER_TOKEN;

if (!WP_URL || !WORKER_TOKEN) {
    console.error('Missing WP_URL or WORKER_TOKEN');
    process.exit(1);
}

const INSTITUTES_ENDPOINT = `${WP_URL}/wp-json/imcfi/v1/institutes`;
const INGEST_ENDPOINT = `${WP_URL}/wp-json/imcfi/v1/ingest`;
const SYNC_LOG_ENDPOINT = `${WP_URL}/wp-json/imcfi/v1/sync-log`;
const UPDATE_SYNC_ENDPOINT = `${WP_URL}/wp-json/imcfi/v1/update-sync-time`;
const AUTH_HEADERS = {
    'X-IMCFI-Token': WORKER_TOKEN,
    'User-Agent': 'imariners-worker/1.0 (+https://github.com/iMariner/imariners-worker)',
    'Accept': 'application/json'
};

// Only these source types are scraped here. Others are either handled by their own
// workflow (bpmarine, appex-ui, marineims, fosma, simf) or entered manually in wp-admin.
const SCRAPED_HERE = new Set(['appex']);

let failures = 0;

// imariners.com is behind Cloudflare, which can answer GitHub runners with a
// "Just a moment..." challenge (HTTP 403). Report that clearly instead of a bare 403.
function describeHttpError(err) {
    const status = err.response && err.response.status;
    const body = err.response && typeof err.response.data === 'string' ? err.response.data : '';
    if ((status === 403 || status === 503) && body.includes('Just a moment')) {
        return 'Cloudflare bot challenge blocked the request (add a WAF skip rule for /wp-json/imcfi/v1/ and disable Bot Fight Mode)';
    }
    if (status === 401 || status === 403) {
        return `WordPress rejected the token (${status}). Check the WORKER_TOKEN secret matches STCW Finder > Settings`;
    }
    return err.message;
}

const DELAY_BETWEEN_INSTITUTES = 5000;
const REQUEST_DELAY = 1000;

function delay(ms) { return new Promise(r => setTimeout(r, ms)); }

async function logSync(instituteId, status, found, saved, error = '') {
    try {
        await axios.post(SYNC_LOG_ENDPOINT, {
            institute_id: instituteId,
            status,
            records_found: found,
            records_saved: saved,
            message: error.substring(0, 500),
            error_detail: error
        }, { headers: AUTH_HEADERS, timeout: 10000 });
        console.log(`[LOG] ${instituteId}: ${status} (found ${found})`);
    } catch (err) {
        console.error(`[LOG ERROR] ${instituteId}:`, err.message);
    }
}

async function updateSyncTime(instituteId) {
    try {
        await axios.post(UPDATE_SYNC_ENDPOINT, { institute_id: instituteId }, { headers: AUTH_HEADERS });
    } catch (err) {
        console.error(`[UPDATE ERROR] ${instituteId}:`, err.message);
    }
}

async function fetchInstitutes() {
    try {
        const res = await axios.get(INSTITUTES_ENDPOINT, { headers: AUTH_HEADERS, timeout: 15000 });
        return res.data;
    } catch (err) {
        console.error('[FETCH ERROR] Could not retrieve institutes:', describeHttpError(err));
        process.exit(1);
    }
}

// ========== GENERIC APEX SCRAPER (works for any Appexonline institute) ==========
async function scrapeApexInstitute(inst) {
    console.log(`[APEX] Scraping ${inst.name} (ID: ${inst.id}) – URL: ${inst.source_url}`);

    // ─── NOTE: source_url storage is inconsistent across institutes ───────────
    // Confirmed against a live 404 (https://ami.appexonline.com/booking/
    // GetCourseListByType) that some institutes have source_url stored as the
    // bare APPEx app root (".../booking") and need "/Booking" appended to
    // reach the actual controller. A previous revision of this file assumed
    // every source_url already included the trailing "/Booking" segment
    // (based on the readme/admin-placeholder text, which turned out not to
    // match what's actually stored for every institute) and removed the
    // append entirely — that broke institutes like this one. This version
    // only appends "/Booking" when it isn't already on the end, so it works
    // correctly for both conventions instead of guessing one or the other.
    let apiBase = inst.source_url.replace(/\/$/i, '');
    if (!/\/booking\/booking$/i.test(apiBase)) {
        apiBase += '/Booking';
    }
    
    const allBatches = [];
    const courseTypes = ['DG Course', 'Value Added Course'];

    async function post(endpoint, data, retries = 2) {
        const form = new URLSearchParams(data).toString();
        console.log(`[API] Calling ${apiBase}/${endpoint} with ${JSON.stringify(data)}`);

        let attempt = 0;
        let lastErr;
        while (attempt <= retries) {
            try {
                const res = await axios.post(`${apiBase}/${endpoint}`, form, {
                    headers: {
                        'Content-Type': 'application/x-www-form-urlencoded',
                        'Accept': '*/*',
                        'User-Agent': 'axios/1.6.2'
                    },
                    timeout: 15000
                });
                return res.data;
            } catch (err) {
                lastErr = err;
                attempt++;
                if (attempt <= retries) {
                    await delay(attempt * 1000);
                }
            }
        }
        throw lastErr;
    }

    // Helper: parse date like "15 Jun" or "15 Jun 2026"
    function parseDate(dateStr, defaultYear = null) {
        const months = {
            jan: 0, feb: 1, mar: 2, apr: 3, may: 4, jun: 5,
            jul: 6, aug: 7, sep: 8, oct: 9, nov: 10, dec: 11
        };
        const trimmed = dateStr.trim();
        let match = trimmed.match(/\b(\d{1,2})\s+(\w{3})(?:\s+(\d{4}))?\b/);
        if (!match) return null;
        const day = parseInt(match[1], 10);
        const month = months[match[2].toLowerCase().slice(0,3)];
        if (month === undefined) return null;
        let year = match[3] ? parseInt(match[3], 10) : (defaultYear !== null ? defaultYear : new Date().getFullYear());
        const testDate = new Date(year, month, day);
        if (testDate < new Date() && defaultYear === null) year += 1;
        return { year, month, day };
    }

    function toDateString(year, month, day) {
        const y = year.toString().padStart(4, '0');
        const m = (month + 1).toString().padStart(2, '0');
        const d = day.toString().padStart(2, '0');
        return `${y}-${m}-${d}`;
    }

    for (const type of courseTypes) {
        let coursesRaw;
        try {
            coursesRaw = await post('GetCourseListByType', { courseType: type });
        } catch (e) {
            console.error(`[APEX ERROR] Could not fetch courses for type ${type}:`, e.message);
            console.error(`[DEBUG] Full error:`, e);
            continue;
        }
        if (!Array.isArray(coursesRaw)) continue;
        const courses = coursesRaw.filter(c => c.key !== '0');
        for (const course of courses) {
            let batchesRaw;
            try {
                batchesRaw = await post('GetCourseBatchListByCourse', {
                    course: course.key,
                    ExitExam: 'false',
                    CourseType: ''
                });
            } catch (e) {
                console.error(`[APEX ERROR] Could not fetch batches for course ${course.value}:`, e.message);
                continue;
            }
            if (!Array.isArray(batchesRaw)) continue;
            const batches = batchesRaw.filter(b => b.key !== '0');
            for (const batch of batches) {
                const label = batch.value;
                let startDateStr = null, endDateStr = null;
                const rangeMatch = label.match(/(\d{1,2}\s+\w{3}(?:\s+\d{4})?)\s*-\s*(\d{1,2}\s+\w{3}(?:\s+\d{4})?)/i);
                if (rangeMatch) {
                    const startComp = parseDate(rangeMatch[1]);
                    const endComp = parseDate(rangeMatch[2], startComp ? startComp.year : null);
                    if (startComp) startDateStr = toDateString(startComp.year, startComp.month, startComp.day);
                    if (endComp) endDateStr = toDateString(endComp.year, endComp.month, endComp.day);
                } else {
                    const single = parseDate(label);
                    if (single) startDateStr = toDateString(single.year, single.month, single.day);
                }
                const seatsMatch = label.match(/Avl\.\s*Seats[- ](\d+)/i);
                let seatsText = seatsMatch ? `${seatsMatch[1]} seats left` : '';
                let fee = null;
                try {
                    const feeData = await post('GetCourseFeeByCourseBatch', { batch: batch.key, type });
                    fee = parseFloat(feeData);
                    if (isNaN(fee)) fee = null;
                } catch(e) { console.warn(`Fee not found for batch ${batch.key}`); }
                allBatches.push({
                    course_name: course.value,
                    batch_start_date: startDateStr,
                    batch_end_date: endDateStr,
                    fees: fee,
                    seats_text: seatsText
                });
                await delay(REQUEST_DELAY);
            }
            await delay(500);
        }
    }
    return allBatches;
}

// ========== PLACEHOLDER SCRAPERs FOR OTHER SOURCE TYPES ==========
async function scrapeSimpleHtml(inst) {
    console.warn(`[WARN] simple_html scraper not implemented yet for ${inst.name}`);
    return [];
}
async function scrapeJsonApi(inst) {
    console.warn(`[WARN] json_api scraper not implemented yet for ${inst.name}`);
    return [];
}

async function scrapeInstitute(inst) {
    console.log(`[SCRAPE] ${inst.name} (ID: ${inst.id}) - ${inst.source_type}`);
    let batches = [];
    let status = 'error';
    let errorMsg = '';

    try {
        if (inst.source_type === 'appex') {
            batches = await scrapeApexInstitute(inst);
        } else if (inst.source_type === 'simple_html') {
            batches = await scrapeSimpleHtml(inst);
        } else if (inst.source_type === 'json_api') {
            batches = await scrapeJsonApi(inst);
        } else {
            errorMsg = `Unknown source_type: ${inst.source_type}`;
        }
        status = batches.length ? 'success' : 'partial';
        if (!batches.length && !errorMsg) errorMsg = 'No batches found';
    } catch (err) {
        errorMsg = `${err.name}: ${err.message}`;
        console.error(`[ERROR] ${inst.name}:`, errorMsg);
        status = 'error';
    }

    if (batches.length) {
        try {
            await axios.post(INGEST_ENDPOINT, { institute_id: inst.id, batches }, { headers: AUTH_HEADERS, timeout: 60000 });
            console.log(`[SUCCESS] ${inst.name} – ${batches.length} batches sent.`);
            await updateSyncTime(inst.id);
        } catch (err) {
            errorMsg = `Ingest failed: ${describeHttpError(err)}`;
            status = 'error';
            failures++;
            console.error(`[ERROR] ${inst.name}: ${errorMsg}`);
        }
    } else {
        console.log(`[NO DATA] ${inst.name} – ${errorMsg}`);
    }
    await logSync(inst.id, status, batches.length, status === 'success' ? batches.length : 0, errorMsg);
}

async function run() {
    console.log(`[START] Worker started at ${new Date().toISOString()}`);
    const all = await fetchInstitutes();
    const institutes = (Array.isArray(all) ? all : []).filter(i => SCRAPED_HERE.has(i.source_type));
    console.log(`[INFO] ${institutes.length} of ${Array.isArray(all) ? all.length : 0} active institutes use a source type scraped here (${[...SCRAPED_HERE].join(', ')}).`);
    if (!institutes.length) {
        console.error('[ERROR] No institutes to scrape. Check source_type values in STCW Finder > Institutes.');
        process.exit(1);
    }
    for (const inst of institutes) {
        await scrapeInstitute(inst);
        await delay(DELAY_BETWEEN_INSTITUTES);
    }
    console.log(`[FINISH] Worker finished at ${new Date().toISOString()} with ${failures} ingest failure(s).`);
    if (failures) process.exit(1);
}
run().catch(err => { console.error('[FATAL]', err); process.exit(1); });

