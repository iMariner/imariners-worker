import axios from 'axios';

const WP_URL = process.env.WP_URL;
const WORKER_TOKEN = process.env.WORKER_TOKEN;

if (!WP_URL || !WORKER_TOKEN) {
    console.error('Missing WP_URL or WORKER_TOKEN');
    process.exit(1);
}

const INGEST_ENDPOINT = `${WP_URL}/wp-json/imcfi/v1/ingest`;
const SYNC_LOG_ENDPOINT = `${WP_URL}/wp-json/imcfi/v1/sync-log`;
const AUTH_HEADERS = { 'X-IMCFI-Token': WORKER_TOKEN };

const delay = ms => new Promise(r => setTimeout(r, ms));

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

// Improved date parsing: tries multiple formats, falls back to a default future date
function parseDateFromString(text) {
    if (!text) return null;
    // Try to extract a date in common formats: DD Mon YYYY, YYYY-MM-DD, DD-MM-YYYY
    const patterns = [
        /\b(\d{1,2}\s\w+\s\d{4})\b/,                 // 15 Jun 2026
        /\b(\d{4}-\d{2}-\d{2})\b/,                   // 2026-06-15
        /\b(\d{2}-\d{2}-\d{4})\b/                    // 15-06-2026
    ];
    for (const pattern of patterns) {
        const match = text.match(pattern);
        if (match) {
            const date = new Date(match[1]);
            if (!isNaN(date.getTime())) return date;
        }
    }
    return null;
}

async function scrapeApexInstitute() {
    console.log('[APEX] Scraping AIIMT');
    const base = 'https://aiimt.appexonline.com/booking/Booking';
    const allBatches = [];
    const courseTypes = ['DG Course', 'Value Added Course'];

    async function post(endpoint, data) {
        const form = new URLSearchParams(data).toString();
        console.log(`[API] Calling ${endpoint} with ${JSON.stringify(data)}`);
        const res = await axios.post(`${base}/${endpoint}`, form, {
            headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
            timeout: 15000
        });
        return res.data;
    }

    const defaultStart = new Date();
    defaultStart.setDate(defaultStart.getDate() + 30); // 30 days from now

    for (const type of courseTypes) {
        console.log(`[API] Fetching courses for type: ${type}`);
        const coursesRaw = await post('GetCourseListByType', { courseType: type });
        if (!Array.isArray(coursesRaw)) continue;
        const courses = coursesRaw.filter(c => c.key !== '0');
        console.log(`[API] Found ${courses.length} courses for type ${type}`);

        for (const course of courses) {
            console.log(`[API] Fetching batches for course: ${course.value} (${course.key})`);
            const batchesRaw = await post('GetCourseBatchListByCourse', {
                course: course.key,
                ExitExam: 'false',
                CourseType: ''
            });
            if (!Array.isArray(batchesRaw)) continue;
            const batches = batchesRaw.filter(b => b.key !== '0');
            console.log(`[API] Found ${batches.length} batches for course ${course.value}`);

            for (const batch of batches) {
                // Attempt to parse start and end dates from the batch label
                let start = null, end = null;
                // First try two‑date pattern
                const twoDates = batch.value.match(/(\d{1,2}\s\w+\s\d{4})\s*-\s*(\d{1,2}\s\w+\s\d{4})/);
                if (twoDates) {
                    start = new Date(twoDates[1]);
                    end   = new Date(twoDates[2]);
                } else {
                    // Single date? Use it as start, and no end date
                    const singleDate = parseDateFromString(batch.value);
                    if (singleDate) {
                        start = singleDate;
                        end = null;
                    }
                }

                // Fallback if still no start date
                if (!start || isNaN(start.getTime())) {
                    console.warn(`[WARN] Could not parse date from batch label: "${batch.value}". Using default date (30 days from now).`);
                    start = new Date(defaultStart);
                    end = null;
                }

                // Ensure start is not in the past (if it is, move to future? but keep original for now)
                // Convert to YYYY-MM-DD format
                const startDateStr = start ? start.toISOString().slice(0,10) : null;
                const endDateStr = end ? end.toISOString().slice(0,10) : null;

                const seatsMatch = batch.value.match(/Avl\.\s*Seats[- ](\d+)/i);
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
                await delay(1000);
            }
            await delay(500);
        }
    }
    console.log(`[APEX] Total batches collected: ${allBatches.length}`);
    return allBatches;
}

async function main() {
    console.log(`[START] ${new Date().toISOString()}`);
    const instituteId = 1; // AIIMT's ID in your WordPress
    let batches = [], status = 'error', errorMsg = '';
    try {
        batches = await scrapeApexInstitute();
        status = batches.length ? 'success' : 'partial';
        if (!batches.length) errorMsg = 'No batches found';
    } catch (err) {
        errorMsg = `${err.name}: ${err.message}`;
        console.error(`[ERROR] ${errorMsg}`);
        status = 'error';
    }
    if (batches.length) {
        try {
            await axios.post(INGEST_ENDPOINT, { institute_id: instituteId, batches }, { headers: AUTH_HEADERS, timeout: 60000 });
            console.log(`[SUCCESS] ${batches.length} batches sent.`);
        } catch (err) {
            errorMsg = `Ingest failed: ${err.message}`;
            status = 'error';
        }
    } else {
        console.log(`[NO DATA] ${errorMsg}`);
    }
    await logSync(instituteId, status, batches.length, status === 'success' ? batches.length : 0, errorMsg);
    console.log(`[FINISH] ${new Date().toISOString()}`);
}

main().catch(console.error);
