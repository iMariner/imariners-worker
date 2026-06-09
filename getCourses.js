import axios from 'axios';

async function getAllCourses() {
    const base = 'https://aiimt.appexonline.com/booking/Booking';
    const allCourses = [];
    const courseTypes = ['DG Course', 'Value Added Course'];

    async function post(endpoint, data) {
        const form = new URLSearchParams(data).toString();
        const res = await axios.post(`${base}/${endpoint}`, form, {
            headers: { 'Content-Type': 'application/x-www-form-urlencoded' }
        });
        return res.data;
    }

    for (const type of courseTypes) {
        const courses = await post('GetCourseListByType', { courseType: type });
        if (!Array.isArray(courses)) continue;
        // Filter out the placeholder
        const realCourses = courses.filter(c => c.key !== '0');
        allCourses.push(...realCourses);
    }

    // Output all course names (with keys for reference)
    console.log('\n===== ALL COURSES FROM AIIMT =====\n');
    allCourses.forEach(course => {
        console.log(`KEY: ${course.key} | NAME: ${course.value}`);
    });
    console.log(`\nTotal: ${allCourses.length} courses`);
}

getAllCourses().catch(console.error);
