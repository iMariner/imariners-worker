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

// Convert month name (e.g., "Jun") to month number (0-11)
function monthNameToNumber(monthName) {
    const months = {
        jan: 0, feb: 1, mar: 2, apr: 3, may: 4, jun: 5,
        jul: 6, aug: 7, sep: 8, oct: 9, nov: 10, dec: 11
    };
    const lower = monthName.toLowerCase().substring(0,3);
    return months[lower] ?? null;
}

// Parse a date string that may or may not contain a year
function parseFlexibleDate(dateStr, defaultYear = null) {
    if (!dateStr) return null;
    // Try to match full format: DD Mon YYYY
    let match = dateStr.match(/\b(\d{1,2})\s+(\w{3})\s+(\d{4})\b/);
    if (match) {
        const day = parseInt(match[1], 10);
        const monthName = match[2];
        const year = parseInt(match[3], 10);
        const month = monthNameToNumber(monthName);
        if (month !== null && !isNaN(day) && !isNaN(year)) {
            return new Date(year, month, day);
        }
    }
    // Try to match DD Mon (no year)
    match = dateStr.match(/\b(\d{1,2})\s+(\w{3})\b/);
    if (match) {
        const day = parseInt(match[1], 10);
        const monthName = match[2];
        const month = monthNameToNumber(monthName);
        if (month !== null && !isNaN(day)) {
            let year = defaultYear !== null ? defaultYear : new Date().getFullYear();
            let date = new Date(year, month, day);
            // If the date is in the past, add one year
            if (date < new Date()) {
                date = new Date(year + 1, month, day);
            }
            return date;
        }
    }
    // Fallback to generic Date parsing
    const d = new Date(dateStr);
    if (!isNaN(d.getTime())) return d;
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
                const label = batch.value;
                let startDate = null, endDate = null;

                // Try to extract two dates: "DD Mon - DD Mon" or "DD Mon YYYY - DD Mon YYYY"
                const twoDateMatch = label.match(/(\d{1,2}\s+\w{3}(?:\s+\d{4})?)\s*-\s*(\d{1,2}\s+\w{3}(?:\s+\d{4})?)/i);
                if (twoDateMatch) {
                    const startStr = twoDateMatch[1];
                    const endStr = twoDateMatch[2];
                    startDate = parseFlexibleDate(startStr);
                    endDate = parseFlexibleDate(endStr, startDate ? startDate.getFullYear() : null);
                } else {
                    // Try single date
                    const singleDate = parseFlexibleDate(label);
                    if (singleDate) {
                        startDate = singleDate;
                        endDate = null;
                    }
                }

                // Fallback if still no date
                if (!startDate || isNaN(startDate.getTime())) {
                    console.warn(`[WARN] Could not parse date from batch label: "${label}". Using default date (30 days from now).`);
                    startDate = new Date(defaultStart);
                    endDate = null;
                }

                const startDateStr = startDate ? startDate.toISOString().slice(0,10) : null;
                const endDateStr = endDate ? endDate.toISOString().slice(0,10) : null;

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
    const instituteId = 1;
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
