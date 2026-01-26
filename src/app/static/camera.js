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
    doScan({
        manual_text: text,
        state: stateSelect.value
    });
}

async function doScan(payload) {
    showResult("Processing...", "Analyzing Image...", null);

    const res = await apiCall('api/scan', {
        method: 'POST',
        body: JSON.stringify(payload)
    });
    const { job_id } = await res.json();

    // Poll for results
    const poll = setInterval(async () => {
        const statusRes = await apiCall(`api/status/${job_id}`);
        const statusData = await statusRes.json();

        if (statusData.status === 'complete') {
            clearInterval(poll);
            const data = statusData.data;
            const found = data.result !== "Not Found";
            showResult(data.result, `${data.plate}`, found);
        }
    }, 1500);
}

function showResult(title, subtitle, isSuccess) {
    const box = document.getElementById('result-display');
    box.className = 'result-box'; // reset
    if (isSuccess === true) box.classList.add('success');
    if (isSuccess === false) box.classList.add('failure');
    if (isSuccess === null) box.classList.add('processing'); // Grey/Loading state

    box.classList.remove('hidden');
    document.getElementById('result-name').innerText = title;
    document.getElementById('result-details').innerText = subtitle;
}

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
                ${new Date(parseInt(item.sk.split('#')[1])*1000).toLocaleString()}
            </div>
        </li>
    `).join('');
}

initCamera();
