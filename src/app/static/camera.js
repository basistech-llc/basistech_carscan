const video = document.getElementById('video');
const canvas = document.getElementById('canvas');
const UPLOAD_TIMEOUT_SECONDS = 10;

// Abort/cancel support: when [live] is clicked, we stop upload and polling
let uploadAbortController = null;
let pollIntervalId = null;

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
    console.log("using rear campera");
    video.srcObject = stream;
  } catch (err) {
    console.warn("Rear camera failed, trying default", err);
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ video: true });
      video.srcObject = stream;
      console.log("using default campera");
    } catch {
      alert("Camera access denied.");
    }
  }
}

// Discount HTML tab selection
// Used in HTML: onclick="showTab('scan')"
// eslint-disable-next-line no-unused-vars
function showTab(tabName) {
  document.querySelectorAll('.tab-content').forEach(el => el.classList.remove('active'));
  document.getElementById(`${tabName}-tab`).classList.add('active');
  if (tabName === 'history') loadHistory();
}

async function post_image(image) {
  showResult("Processing...", "", null);

  // Abort any previous upload
  if (uploadAbortController) uploadAbortController.abort();
  uploadAbortController = new AbortController();
  const signal = uploadAbortController.signal;

  // 1. Get Presigned URL
  fetch('api/upload-url', { method: "GET", signal })
    .then(r => {
      if (r.status === 401) window.location.reload(); // Auth check
      if (!r.ok) {
        console.log("r:", r);
        showResult("Failed");
        throw new Error(`Failed to get signed upload URL: ${r}`);
      }
      return r.json();
    })
    .then(obj => {
      if (signal.aborted) return;
      console.log("obj:", obj);
      // 2. Now use the presigned_post to upload to s3
      const formData = new FormData();
      for (const field in obj.presigned.fields) {
        formData.append(field, obj.presigned.fields[field]);
      }
      formData.append('file', image); // must be last
      console.log("formData:", formData);
      const timeoutId = setTimeout(() => uploadAbortController?.abort(), UPLOAD_TIMEOUT_SECONDS * 1000);
      return fetch(obj.presigned.url, { method: 'POST', body: formData, signal })
        .then(() => { clearTimeout(timeoutId); return obj.job_id; })
        .catch(e => { clearTimeout(timeoutId); throw e; });
    })
    .then(jobId => {
      if (signal.aborted || !jobId) return;
      pollForResult(jobId);
    })
    .catch(err => {
      if (err?.name === 'AbortError') return; // user went live, ignore
      console.error("Upload error", err);
      showResult("Failed", "", false);
    });
}


/**
 * Draw 12pt yellow sans-serif date/time at bottom of canvas
 */
function addTimestampToCanvas(ctx, w, h) {
  const now = new Date();
  const text = now.toLocaleString();
  ctx.font = '12px sans-serif';
  ctx.fillStyle = 'yellow';
  ctx.textBaseline = 'bottom';
  ctx.fillText(text, 8, h - 8);
}

// Used in HTML: onclick="captureAndScan()"
async function captureAndScan() {
  // Capture frame while video is live, add timestamp
  const context = canvas.getContext('2d');
  canvas.width = video.videoWidth;
  canvas.height = video.videoHeight;
  context.drawImage(video, 0, 0, canvas.width, canvas.height);
  addTimestampToCanvas(context, canvas.width, canvas.height);

  // Stop video stream
  const stream = video.srcObject;
  if (stream) {
    stream.getTracks().forEach(t => t.stop());
    video.srcObject = null;
  }

  // Show captured image, hide live video
  video.style.display = 'none';
  canvas.style.display = 'block';
  canvas.style.width = '100%';
  canvas.style.height = '100%';
  canvas.style.objectFit = 'cover';

  // Change button to [live]
  const btn = document.getElementById('scan-btn');
  btn.textContent = '[live]';
  btn.onclick = goLive;

  canvas.toBlob(post_image, "image/jpeg", 0.95);
}

// Used when [live] button is clicked: cancel upload, resume video
function goLive() {
  // Abort in-progress upload and stop polling
  if (uploadAbortController) {
    uploadAbortController.abort();
    uploadAbortController = null;
  }
  if (pollIntervalId) {
    clearInterval(pollIntervalId);
    pollIntervalId = null;
  }

  // Show video, hide canvas
  video.style.display = '';
  canvas.style.display = 'none';

  // Change button to [scan]
  const btn = document.getElementById('scan-btn');
  btn.textContent = '[scan]';
  btn.onclick = captureAndScan;

  // Restart camera
  initCamera();
}

// Photo library: hidden file input is triggered by the [upload photo from library] button
(function setupLibraryUpload() {
  const input = document.getElementById('library-input');
  if (!input) return;
  input.addEventListener('change', function () {
    const file = input.files && input.files[0];
    if (file && file.type.startsWith('image/')) {
      post_image(file);
    }
    input.value = '';
  });
})();

// Used in HTML: onclick="manualSearch()"
async function manualSearch() {
  const text = document.getElementById('manual-plate').value;
  if(!text) return;

  showResult("Processing...", "Checking manual entry...", null);

  const res = await apiCall('api/manual', {
    method: 'POST',
    body: JSON.stringify({
      plate: text
    })
  });

  if (res.ok) {
    const { data } = await res.json();
    console.log("data=",data);
    var found = false;
    var show  = "";
    var show2 = "not found";
    if (data.result){
      found = true;
      show = data.result.firstName + " " + data.result.lastName;
      if (data.result.phoneNumbers && data.result.phoneNumbers[0]) {
        show2 += " " + data.result.phoneNumbers[0].number + " ";
      }
      show2 += data.result["Auto Registration - Plate Number"];
    }
    showResult(show, show2, found);
  } else {
    showResult("Error", "Could not save manual entry", false);
  }
}
document.addEventListener('DOMContentLoaded', () => {
  const manualBtn = document.getElementById('manual-plate-button');
  if (manualBtn) {
    manualBtn.addEventListener('click', manualSearch);
  }
});


/**
 * Polling loop to check DynamoDB status
 * Called from captureAndScan() after image upload
 */
async function pollForResult(jobId) {
  if (pollIntervalId) clearInterval(pollIntervalId);
  pollIntervalId = null;

  showResult("Processing...", "Analyzing Image...", null);
  const poll = setInterval(async () => {
    if (pollIntervalId === null) return; // cleared by goLive
    try {
      const statusRes = await apiCall(`api/status?job_id=${encodeURIComponent(jobId)}`);
      const statusData = await statusRes.json();

      if (statusData.status === 'complete') {
        clearInterval(poll);
        pollIntervalId = null;
        const data = statusData.data;
        const found = data.result !== "Unknown" && data.plate !== "NOT_FOUND";
        showResult(data.result, `${data.plate} (${data.state})`, found);
      }
    } catch (e) {
      console.error("Polling error", e);
    }
  }, 1000);
  pollIntervalId = poll;

  // Timeout after 10 seconds
  setTimeout(() => {
    if (pollIntervalId === poll) {
      clearInterval(poll);
      pollIntervalId = null;
      showResult("Failed", "Enter plate manually.", false);
    }
  }, 10000);
}

/**
 * UI Display Helper
 */
function showResult(title, subtitle, isSuccess) {
  console.log(`showResult(${title},${subtitle},${isSuccess});`);
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
