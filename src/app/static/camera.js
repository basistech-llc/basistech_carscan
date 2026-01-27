const video = document.getElementById('video');
const canvas = document.getElementById('canvas');
const stateSelect = document.getElementById('state-select');

// Get current API base path (e.g. /Prod/ or /Staging/) from window location
const API_BASE = window.location.pathname.endsWith('/')
    ? window.location.pathname
    : window.location.pathname + '/';

// Normalize fetch calls to include the stage path
async function apiCall(endpoint, options={}) {
    // Remove leading slash from endpoint if present
    const cleanEndpoint = endpoint.startsWith('/') ? endpoint.substring(1) : endpoint;
    const url = `${window.location.origin}${API_BASE}${cleanEndpoint}`;
    return fetch(url, options);
}

async function initCamera() {
    try {
        const stream = await navigator.mediaDevices.getUserMedia({
            video: { facingMode: { exact: "environment" } } // Force rear camera
        });
        video.srcObject = stream;
    } catch (err) {
        console.warn("Rear camera failed, trying default", err);
        try {
            const stream = await navigator.mediaDevices.getUserMedia({ video: true });
            video.srcObject = stream;
        } catch(e) {
            alert("Camera access denied.");
        }
    }
}

function showTab(tabName) {
    document.querySelectorAll('.tab-content').forEach(el => el.classList.remove('active'));
    document.getElementById(`${tabName}-tab`).classList.add('active');
    if (tabName === 'history') loadHistory();
}

async function captureAndScan() {
    const context = canvas.getContext('2d');
    canvas.width = video.videoWidth;
    canvas.height = video.videoHeight;
    context.drawImage(video, 0, 0, canvas.width, canvas.height);

    canvas.toBlob(async (blob) => {
        showResult("Processing...", "", null);

        // 1. Get Presigned URL
        const presignedRes = await apiCall('api/upload-url');
        if(presignedRes.status === 401) window.location.reload(); // Auth check

        const presignedData = await presignedRes.json();

        // 2. Upload to S3
        const formData = new FormData();
        Object.entries(presignedData.fields).forEach(([k, v]) => formData.append(k, v));
        formData.append('file', blob);

        await fetch(presignedData.url, { method: 'POST', body: formData });

        // 3. Scan
        doScan({
            image_key: presignedData.fields.key,
            state: stateSelect.value
        });

    }, 'image/jpeg');
}

async function manualSearch() {
    const text = document.getElementById('manual-plate').value;
    if(!text) return;

    showResult("Processing...", "Checking manual entry...", null);

    const res = await apiCall('api/manual', {
        method: 'POST',
        body: JSON.stringify({
            plate: text,
            state: stateSelect.value
        })
    });

    if (res.ok) {
        const { data } = await res.json();
        const found = data.result !== "Unknown";
        showResult(data.result, `${data.plate} (${data.state})`, found);
    } else {
        showResult("Error", "Could not save manual entry", false);
    }
}

/**
 * Polling loop to check DynamoDB status
 */
async function pollForResult(jobId) {
    showResult("Processing...", "Analyzing Image...", null);

    const poll = setInterval(async () => {
        try {
            // job_id contains slashes, must be encoded
            const statusRes = await apiCall(`api/status/${encodeURIComponent(jobId)}`);
            const statusData = await statusRes.json();

            if (statusData.status === 'complete') {
                clearInterval(poll);
                const data = statusData.data;
                const found = data.result !== "Unknown" && data.plate !== "NOT_FOUND";
                showResult(data.result, `${data.plate} (${data.state})`, found);
            }
        } catch (e) {
            console.error("Polling error", e);
        }
    }, 1500);

    // Timeout after 30 seconds
    setTimeout(() => {
        clearInterval(poll);
    }, 30000);
}

/**
 * UI Display Helper
 */
function showResult(title, subtitle, isSuccess) {
    const box = document.getElementById('result-display');
    box.className = 'result-box';
    if (isSuccess === true) box.classList.add('success');
    if (isSuccess === false) box.classList.add('failure');
    if (isSuccess === null) box.classList.add('processing');

    box.classList.remove('hidden');
    document.getElementById('result-name').innerText = title;
    document.getElementById('result-details').innerText = subtitle;
}

/**
 * History Loader
 */
async function loadHistory() {
    const res = await apiCall('api/history');
    if(res.status === 401) return window.location.reload();

    const items = await res.json();
    const list = document.getElementById('history-list');
    list.innerHTML = items.map(item => `
        <li>
            <div class="h-main">
                <strong>${item.result}</strong>
                <span>${item.plate} (${item.state})</span>
            </div>
            <div class="h-sub">
                ${new Date(item.timestamp * 1000).toLocaleString()}
            </div>
        </li>
    `).join('');
}

initCamera();
