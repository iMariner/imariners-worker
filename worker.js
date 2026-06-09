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

async function scrapeApexInstitute() {
    console.log('[APEX] Scraping AIIMT');
    const base = 'https://aiimt.appexonline.com/booking/Booking';
    const allBatches = [];
    // Limit to one course type for testing
    const courseTypes = ['DG Course']; // ['DG Course', 'Value Added Course'];

    async function post(endpoint, data) {
        const form = new URLSearchParams(data).toString();
        console.log(`[API] Calling ${endpoint} with ${JSON.stringify(data)}`);
        const res = await axios.post(`${base}/${endpoint}`, form, {
            headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
            timeout: 15000 // 15 seconds timeout
        });
        return res.data;
    }

    for (const type of courseTypes) {
        console.log(`[API] Fetching courses for type: ${type}`);
        const coursesRaw = await post('GetCourseListByType', { courseType: type });
        if (!Array.isArray(coursesRaw)) continue;
        const courses = coursesRaw.filter(c => c.key !== '0');
        console.log(`[API] Found ${courses.length} courses for type ${type}`);

        // Limit to first 3 courses for faster test
        for (const course of courses.slice(0, 3)) {
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
                const dateMatch = batch.value.match(/(\d{1,2}\s\w+\s\d{4})\s*-\s*(\d{1,2}\s\w+\s\d{4})/);
                let start = null, end = null;
                if (dateMatch) {
                    start = new Date(dateMatch[1]).toISOString().slice(0,10);
                    end   = new Date(dateMatch[2]).toISOString().slice(0,10);
                }
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
                    batch_start_date: start,
                    batch_end_date: end,
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
