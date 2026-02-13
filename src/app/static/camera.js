const video = document.getElementById('video');
const canvas = document.getElementById('canvas');
const UPLOAD_TIMEOUT_SECONDS = 10;

// Abort/cancel support: when [live] is clicked, we stop upload and polling
let uploadAbortController = null;
let pollIntervalId = null;

// Timestamp overlay on live preview
let timestampIntervalId = null;

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

function stopTimestampOverlay() {
  if (timestampIntervalId != null) {
    clearInterval(timestampIntervalId);
    timestampIntervalId = null;
  }
}

function updateTimestampOverlay() {
  const el = document.getElementById('timestamp-overlay');
  if (el) el.textContent = new Date().toLocaleString();
}

function startTimestampOverlay() {
  stopTimestampOverlay();
  updateTimestampOverlay();
  timestampIntervalId = setInterval(updateTimestampOverlay, 1000);
}

async function initCamera() {
  try {
    const stream = await navigator.mediaDevices.getUserMedia({
      video: { facingMode: { exact: "environment" } } // Force rear camera
    });
    console.log("using rear camera");
    video.srcObject = stream;
    startTimestampOverlay();
  } catch (err) {
    console.warn("Rear camera failed, trying default", err);
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ video: true });
      video.srcObject = stream;
      console.log("using default camera");
      startTimestampOverlay();
    } catch {
      alert("Camera access denied.");
    }
  }
}

// All-plates data: pre-loaded on page load, filtered by search
let allPlatesData = [];

// Discount HTML tab selection
// Used in HTML: onclick="showTab('scan')"
// eslint-disable-next-line no-unused-vars
function showTab(tabName) {
  document.querySelectorAll('.tab-content').forEach(el => el.classList.remove('active'));
  document.querySelectorAll('.tabs button').forEach(btn => btn.classList.remove('active'));
  document.getElementById(`${tabName}-tab`).classList.add('active');
  const activeBtn = document.querySelector(`.tabs button[onclick="showTab('${tabName}')"]`);
  if (activeBtn) activeBtn.classList.add('active');
  if (tabName === 'history') loadHistory();
  if (tabName === 'all') renderAllPlatesTable();
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
  stopTimestampOverlay();
  // Draw video frame to canvas with timestamp
  const w = video.videoWidth;
  const h = video.videoHeight;
  if (w > 0 && h > 0) {
    canvas.width = w;
    canvas.height = h;
    const ctx = canvas.getContext('2d');
    ctx.drawImage(video, 0, 0, w, h);
    addTimestampToCanvas(ctx, w, h);
  }

  // Stop video stream and switch to canvas view
  const stream = video.srcObject;
  if (stream) {
    stream.getTracks().forEach(t => t.stop());
    video.srcObject = null;
  }
  video.style.display = 'none';
  document.getElementById('timestamp-overlay').style.display = 'none';
  canvas.style.display = 'block';

  // Change button to [live]
  const btn = document.getElementById('scan-btn');
  btn.className = 'scan-btn live-btn';
  btn.innerHTML = '<span>[live]</span>';
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

  // Switch back to video view
  canvas.style.display = 'none';
  video.style.display = 'block';
  const overlay = document.getElementById('timestamp-overlay');
  if (overlay) overlay.style.display = '';

  // Change button back to scan (camera + OCR)
  const btn = document.getElementById('scan-btn');
  btn.className = 'scan-btn';
  btn.innerHTML = '<svg class="btn-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M23 19a2 2 0 0 1-2 2H3a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4l2-3h6l2 3h4a2 2 0 0 1 2 2z"/><circle cx="12" cy="13" r="4"/></svg><span>OCR</span>';
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
        const title = formatResultDisplay(data.result);
        const subtitle = data.plate || '—';
        showResult(title, subtitle, found);
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
 * Format result for display (Brivo user dict or null).
 */
function formatResultDisplay(result) {
  if (result == null) return 'Not found';
  if (typeof result === 'object') {
    const first = result.firstName || '';
    const last = result.lastName || '';
    return `${first} ${last}`.trim() || 'Unknown';
  }
  return String(result);
}

/** Show image modal from presigned URL. Called from onclick in history list. */
// eslint-disable-next-line no-unused-vars
async function showImageModal(imageKey) {
  if (imageKey === 'manual') return;
  const sk = `job#${imageKey}`;
  const res = await apiCall(`api/image-url?key=${encodeURIComponent(imageKey)}`);
  if (!res.ok) return;
  const { url } = await res.json();
  const modal = document.getElementById('image-modal');
  const img = document.getElementById('image-modal-img');
  const deleteBtn = document.getElementById('image-modal-delete-btn');
  if (!modal || !img) return;
  img.src = url;
  if (deleteBtn) {
    deleteBtn.onclick = (e) => { e.stopPropagation(); deleteScanAndClose(sk); };
  }
  modal.classList.remove('hidden');
}

/** Close image modal. Called from onclick. */
function closeImageModal() {
  const modal = document.getElementById('image-modal');
  const img = document.getElementById('image-modal-img');
  if (modal) modal.classList.add('hidden');
  if (img) img.removeAttribute('src');
}

async function deleteScanAndClose(sk) {
  const res = await apiCall(`api/scan?sk=${encodeURIComponent(sk)}`, { method: 'DELETE' });
  if (res.ok) {
    closeImageModal();
    loadHistory(historyShowAll);
  }
}

/** Delete scan from list. Called from onclick in history item. */
// eslint-disable-next-line no-unused-vars
async function deleteScan(sk) {
  const res = await apiCall(`api/scan?sk=${encodeURIComponent(sk)}`, { method: 'DELETE' });
  if (res.ok) {
    loadHistory(historyShowAll);
  }
}

function showSaveNotImplemented() {
  alert('Save is not currently implemented');
}

/**
 * History Loader
 */
let historyShowAll = false;

async function loadHistory(showAll = false) {
  historyShowAll = showAll;
  const url = showAll ? 'api/history?show_all=1' : 'api/history';
  const res = await apiCall(url);
  if (res.status === 401) return window.location.reload();

  const items = await res.json();
  const list = document.getElementById('history-list');
  const showAllBtn = document.getElementById('history-show-all-btn');

  list.innerHTML = items.map(item => {
    const resultDisplay = item.result_display ?? formatResultDisplay(item.result);
    const plateDisplay = item.plate_display ?? (item.plate || '—');
    const status = item.status ?? (item.image_key === 'manual' ? 'manual' : 'complete');
    const canShowImage = item.image_key && item.image_key !== 'manual';
    const hasOcr = item.ocr_text != null;
    const hasTopMatches = Array.isArray(item.top_matches) && item.top_matches.length > 0;
    const sk = item.sk || `job#${item.image_key || ''}`;

    return `
        <li class="history-item">
            <div class="history-item-swipe" data-sk="${escapeHtml(sk)}">
                <div class="history-item-content">
                    <div class="h-main">
                        <strong>${escapeHtml(resultDisplay)}</strong>
                        <span>${escapeHtml(plateDisplay)}</span>
                        ${status === 'manual' ? '<span class="h-status">Manual</span>' : ''}
                    </div>
                    ${hasOcr ? `<div class="h-ocr">OCR: ${escapeHtml(item.ocr_text)}</div>` : ''}
                    ${hasTopMatches ? `<div class="h-matches">Top: ${item.top_matches.slice(0, 3).map(m => escapeHtml(m.plate || m)).join(', ')}</div>` : ''}
                    <div class="h-sub">
                        ${new Date((item.timestamp || 0) * 1000).toLocaleString()}
                        ${canShowImage ? `<button type="button" class="btn-show-image" data-image-key="${escapeHtml(item.image_key)}" onclick="event.stopPropagation(); showImageModal(this.getAttribute('data-image-key'))">Show</button>` : ''}
                    </div>
                </div>
                <button type="button" class="history-item-delete" onclick="event.stopPropagation(); deleteScan(this.closest('.history-item-swipe').dataset.sk)">
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/><line x1="10" y1="11" x2="10" y2="17"/><line x1="14" y1="11" x2="14" y2="17"/></svg>
                </button>
            </div>
        </li>
    `;
  }).join('');

  setupSwipeHandlers(list);

  if (showAllBtn) {
    showAllBtn.style.display = showAll ? 'none' : 'block';
  }
}

/**
 * Pre-load all plates from API (background, on page load)
 */
async function loadAllPlates() {
  try {
    const res = await apiCall('api/all-plates');
    if (res.status === 401) return;
    if (res.ok) {
      allPlatesData = await res.json();
    }
  } catch (e) {
    console.error('loadAllPlates failed', e);
  }
}

/**
 * Render the all-plates table with current search filter
 */
function renderAllPlatesTable() {
  const query = (document.getElementById('all-plates-search')?.value || '').toLowerCase();
  const filtered = query
    ? allPlatesData.filter(row =>
        (row.plate || '').toLowerCase().includes(query) ||
        (row.name || '').toLowerCase().includes(query)
      )
    : allPlatesData;
  const tbody = document.getElementById('all-plates-body');
  if (!tbody) return;
  tbody.innerHTML = filtered.map(row =>
    `<tr><td>${escapeHtml(row.plate || '')}</td><td>${escapeHtml(row.name || '')}</td></tr>`
  ).join('');
}

function escapeHtml(s) {
  const div = document.createElement('div');
  div.textContent = s;
  return div.innerHTML;
}

const SWIPE_THRESHOLD = 50;
const DELETE_BUTTON_WIDTH = 64;

function setupSwipeHandlers(listEl) {
  if (!listEl) return;
  listEl.querySelectorAll('.history-item-swipe').forEach((swipeEl) => {
    const content = swipeEl.querySelector('.history-item-content');
    if (!content) return;
    let startX = 0;
    let currentX = 0;

    const onStart = (e) => {
      startX = (e.touches ? e.touches[0] : e).clientX;
      currentX = 0;
    };
    const onMove = (e) => {
      const x = (e.touches ? e.touches[0] : e).clientX;
      const delta = x - startX;
      const revealed = content.classList.contains('swipe-reveal');
      const base = revealed ? -DELETE_BUTTON_WIDTH : 0;
      currentX = Math.max(-DELETE_BUTTON_WIDTH, Math.min(DELETE_BUTTON_WIDTH, base + delta));
      content.style.transform = `translateX(${currentX}px)`;
    };
    const onEnd = () => {
      const revealed = content.classList.contains('swipe-reveal');
      if (currentX < -SWIPE_THRESHOLD) {
        content.classList.add('swipe-reveal');
        content.style.transform = `translateX(-${DELETE_BUTTON_WIDTH}px)`;
      } else if (currentX > SWIPE_THRESHOLD && revealed) {
        content.classList.remove('swipe-reveal');
        content.style.transform = '';
      } else if (currentX > SWIPE_THRESHOLD && !revealed) {
        content.style.transform = '';
        showSaveNotImplemented();
      } else {
        content.style.transform = revealed ? `translateX(-${DELETE_BUTTON_WIDTH}px)` : '';
      }
      currentX = 0;
    };

    content.addEventListener('touchstart', onStart, { passive: true });
    content.addEventListener('touchmove', onMove, { passive: true });
    content.addEventListener('touchend', onEnd);
    content.addEventListener('mousedown', onStart);
    content.addEventListener('mousemove', (e) => { if (e.buttons) onMove(e); });
    content.addEventListener('mouseup', onEnd);
    content.addEventListener('mouseleave', onEnd);
  });
}

document.addEventListener('DOMContentLoaded', () => {
  const manualBtn = document.getElementById('manual-plate-button');
  if (manualBtn) {
    manualBtn.addEventListener('click', manualSearch);
  }
  loadAllPlates();
  const searchInput = document.getElementById('all-plates-search');
  if (searchInput) {
    searchInput.addEventListener('input', renderAllPlatesTable);
  }
});

initCamera();
