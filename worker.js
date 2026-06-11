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
const AUTH_HEADERS = { 'X-IMCFI-Token': WORKER_TOKEN };

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
        const res = await axios.get(INSTITUTES_ENDPOINT, { headers: AUTH_HEADERS });
        return res.data;
    } catch (err) {
        console.error('[FETCH ERROR] Could not retrieve institutes:', err.message);
        return [];
    }
}

// ========== GENERIC APEX SCRAPER (works for any Appexonline institute) ==========
async function scrapeApexInstitute(inst) {
    console.log(`[APEX] Scraping ${inst.name} (ID: ${inst.id}) – URL: ${inst.source_url}`);
    // FIXED: Remove trailing slash only, keep the base URL as provided by institute
    let base = inst.source_url.replace(/\/$/i, '');
    // Add the API endpoint path
    const apiBase = `${base}/booking/Booking`;
    const allBatches = [];
    const courseTypes = ['DG Course', 'Value Added Course'];

    async function post(endpoint, data) {
        const form = new URLSearchParams(data).toString();
        console.log(`[API] Calling ${endpoint} with ${JSON.stringify(data)}`);
        const res = await axios.post(`${apiBase}/${endpoint}`, form, {
            headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
            timeout: 15000
        });
        return res.data;
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
            errorMsg = `Ingest failed: ${err.message}`;
            status = 'error';
        }
    } else {
        console.log(`[NO DATA] ${inst.name} – ${errorMsg}`);
    }
    await logSync(inst.id, status, batches.length, status === 'success' ? batches.length : 0, errorMsg);
}

async function run() {
    console.log(`[START] Worker started at ${new Date().toISOString()}`);
    const institutes = await fetchInstitutes();
    if (!institutes.length) {
        console.log('[WARN] No active institutes found.');
        return;
    }
    for (const inst of institutes) {
        await scrapeInstitute(inst);
        await delay(DELAY_BETWEEN_INSTITUTES);
    }
    console.log(`[FINISH] Worker finished at ${new Date().toISOString()}`);
}

run().catch(console.error);
