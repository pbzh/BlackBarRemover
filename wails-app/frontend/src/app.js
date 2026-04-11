'use strict';

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const HW_MODES_ALL = [
  { label: 'QSV – HW Encode',                key: 'qsv',        platforms: ['windows', 'linux'] },
  { label: 'QSV – Full HW Pipeline',         key: 'qsv_fullhw', platforms: ['windows', 'linux'] },
  { label: 'VideoToolbox – HW Encode',        key: 'vt',         platforms: ['darwin'] },
  { label: 'VideoToolbox – Full HW Pipeline', key: 'vt_fullhw',  platforms: ['darwin'] },
  { label: 'CPU – Software',                  key: 'cpu',        platforms: null },
];

const QSV_PRESETS = ['veryfast', 'faster', 'fast', 'medium', 'slow', 'slower', 'veryslow'];

const ASPECT_RATIOS = [
  { label: 'Custom',               ratio: null },
  { label: 'From detection',       ratio: null },
  { label: '16:9',                 ratio: [16, 9] },
  { label: '4:3',                  ratio: [4, 3] },
  { label: '21:9',                 ratio: [21, 9] },
  { label: '2.35:1 (Cinema)',      ratio: [2.35, 1] },
  { label: '2.39:1 (Anamorphic)', ratio: [2.39, 1] },
  { label: '1.85:1',              ratio: [1.85, 1] },
  { label: '1:1 (Square)',        ratio: [1, 1] },
  { label: '9:16 (Portrait)',     ratio: [9, 16] },
  { label: '4:5 (Portrait)',      ratio: [4, 5] },
  { label: '3:2',                 ratio: [3, 2] },
  { label: '5:4',                 ratio: [5, 4] },
];

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

let files = [];            // array of VideoInfo (from Go)
let selectedRow = -1;
let currentPreview = null; // VideoInfo currently shown in preview panel
let origFrameData = null;  // base64 string of the original frame
let isDetecting = false;
let isEncoding = false;
let scrubTimer = null;
let cropEditTimer = null;
let suppressCropSignals = false;
let platform = 'darwin';

// ---------------------------------------------------------------------------
// DOM helpers
// ---------------------------------------------------------------------------

const $ = id => document.getElementById(id);

function formatTime(sec) {
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = Math.floor(sec % 60);
  return `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
}

function parseCrop(str) {
  const s = str.replace('crop=', '');
  const [w, h, x, y] = s.split(':').map(Number);
  return { w, h, x, y };
}

function cropString(w, h, x, y) {
  return `crop=${w}:${h}:${x}:${y}`;
}

function addLog(msg) {
  const el = $('status-line');
  el.textContent = msg;
  // Colour by content
  if (/error|failed|warning|⚠/i.test(msg)) {
    el.style.color = 'var(--error)';
  } else if (/done|finished|complete|✔/i.test(msg)) {
    el.style.color = 'var(--success)';
  } else {
    el.style.color = 'var(--dim)';
  }
}

function setProgress(pct) {
  $('progress-fill').style.width = pct + '%';
  $('progress-text').textContent = Math.round(pct) + '%';
}

function showProgress(show) {
  $('progress-wrap').style.display = show ? 'flex' : 'none';
}

// ---------------------------------------------------------------------------
// Table
// ---------------------------------------------------------------------------

function buildTable() {
  const tbody = $('file-tbody');
  tbody.innerHTML = '';
  files.forEach((f, i) => {
    const tr = document.createElement('tr');
    tr.dataset.row = i;
    tr.addEventListener('click', () => selectRow(i));

    let codecLabel = f.video_codec;
    if (f.is_10bit) codecLabel += ' (10-bit)';

    tr.innerHTML = `
      <td title="${f.path}">${f.name}</td>
      <td>${f.width}×${f.height}</td>
      <td>${codecLabel}</td>
      <td id="td-crop-${i}"></td>
      <td id="td-newres-${i}"></td>
      <td id="td-status-${i}">Loaded</td>
    `;
    tbody.appendChild(tr);
  });
}

function updateTableRow(row, crop) {
  if (row < 0 || row >= files.length) return;
  const info = files[row];
  const tdCrop   = $(`td-crop-${row}`);
  const tdNewRes = $(`td-newres-${row}`);
  const tdStatus = $(`td-status-${row}`);

  if (!crop) {
    if (tdCrop)   tdCrop.textContent   = 'N/A';
    if (tdStatus) { tdStatus.textContent = 'Detection failed'; tdStatus.className = 'status-error'; }
    return;
  }

  const { w: cw, h: ch } = parseCrop(crop);
  if (tdCrop)   tdCrop.textContent   = crop;
  if (tdNewRes) tdNewRes.textContent = `${cw}×${ch}`;

  const removedH = info.height - ch;
  const removedW = info.width  - cw;
  const details = [];
  if (removedH > 0) details.push(`${removedH}px horizontal`);
  if (removedW > 0) details.push(`${removedW}px vertical`);
  const manual = (crop !== info.crop_auto && info.crop_auto) ? ' (manual)' : '';

  if (cw === info.width && ch === info.height) {
    if (tdStatus) { tdStatus.textContent = 'No black bars'; tdStatus.className = ''; }
  } else {
    if (tdStatus) {
      tdStatus.textContent = `Crop: ${details.join(', ')}${manual}`;
      tdStatus.className = '';
    }
  }
}

function setRowStatus(row, text, cls = '') {
  const td = $(`td-status-${row}`);
  if (td) { td.textContent = text; td.className = cls; }
}

function selectRow(row) {
  if (selectedRow >= 0) {
    const prev = $('file-tbody').querySelector(`tr[data-row="${selectedRow}"]`);
    if (prev) prev.classList.remove('selected');
  }
  selectedRow = row;
  const tr = $('file-tbody').querySelector(`tr[data-row="${row}"]`);
  if (tr) tr.classList.add('selected');

  // Auto-load preview if crop data available
  if (files[row] && files[row].crop) {
    loadPreview(files[row]);
  }
}

// ---------------------------------------------------------------------------
// Preview panel
// ---------------------------------------------------------------------------

function loadPreview(info) {
  currentPreview = info;
  origFrameData = null;

  const crop = info.crop;
  if (!crop) {
    setPreviewInfo(`${info.name} — no crop data (run detection first)`);
    clearCanvases();
    setCropControlsEnabled(false);
    $('scrubber').disabled = true;
    return;
  }

  const { w: cw, h: ch } = parseCrop(crop);
  if (cw === info.width && ch === info.height) {
    setPreviewInfo(`${info.name} — no black bars detected`);
    clearCanvases();
    setCropControlsEnabled(false);
    $('scrubber').disabled = true;
    return;
  }

  // Store auto crop if not set
  if (!info.crop_auto) info.crop_auto = crop;

  // Set spinbox limits and values
  setSpinboxLimits(info);
  setSpinboxValues(cw, ch, parseCrop(crop).x, parseCrop(crop).y);
  setCropControlsEnabled(true);

  suppressCropSignals = true;
  $('aspect-ratio').value = '1'; // "From detection"
  suppressCropSignals = false;
  $('ar-info').textContent = `Auto-detected: ${cw}×${ch} (${(cw/ch).toFixed(3)}:1)`;

  updatePreviewInfo(crop);

  const dur = info.duration || 0;
  const scrubber = $('scrubber');
  scrubber.disabled = false;
  scrubber.min = 0;
  scrubber.max = Math.max(1, Math.floor(dur));
  scrubber.value = Math.min(30, Math.floor(dur / 2));
  $('time-total').textContent = formatTime(dur);
  $('time-current').textContent = formatTime(scrubber.value);

  triggerFrameUpdate();
}

function clearPreview() {
  currentPreview = null;
  origFrameData = null;
  setPreviewInfo('Select a file and run detection, then click Preview');
  clearCanvases();
  setCropControlsEnabled(false);
  $('scrubber').disabled = true;
  $('time-current').textContent = '0:00:00';
  $('time-total').textContent   = '0:00:00';
  suppressCropSignals = true;
  $('aspect-ratio').value = '0';
  suppressCropSignals = false;
  $('ar-info').textContent = '';
}

function setPreviewInfo(txt) {
  $('preview-info').textContent = txt;
}

function updatePreviewInfo(crop) {
  const info = currentPreview;
  if (!info) return;
  const { w: cw, h: ch } = parseCrop(crop);
  const removedH = info.height - ch;
  const removedW = info.width  - cw;
  const parts = [];
  if (removedH > 0) parts.push(`${removedH}px horizontal`);
  if (removedW > 0) parts.push(`${removedW}px vertical`);
  const manual = (crop !== info.crop_auto && info.crop_auto) ? ' (manual)' : '';
  setPreviewInfo(
    `${info.name}  |  ${info.width}×${info.height} → ${cw}×${ch}  |  ` +
    `Removing ${parts.length ? parts.join(', ') : 'nothing'}  |  ${crop}${manual}`
  );
}

function setSpinboxLimits(info) {
  const sp = { w: $('sp-w'), h: $('sp-h'), x: $('sp-x'), y: $('sp-y') };
  sp.w.max = info.width;  sp.w.min = 2;
  sp.h.max = info.height; sp.h.min = 2;
  sp.x.max = info.width  - 2; sp.x.min = 0;
  sp.y.max = info.height - 2; sp.y.min = 0;
}

function setSpinboxValues(w, h, x, y) {
  suppressCropSignals = true;
  $('sp-w').value = w; $('sp-h').value = h;
  $('sp-x').value = x; $('sp-y').value = y;
  suppressCropSignals = false;
}

function setCropControlsEnabled(en) {
  ['sp-w','sp-h','sp-x','sp-y','btn-apply','btn-reset','aspect-ratio'].forEach(id => {
    const el = $(id);
    if (el) el.disabled = !en;
  });
}

function clearCanvases() {
  ['canvas-orig','canvas-crop'].forEach(id => {
    const c = $(id);
    if (c) {
      const ctx = c.getContext('2d');
      ctx.clearRect(0, 0, c.width, c.height);
      c.width = 0; c.height = 0;
    }
  });
  $('orig-size').textContent = '';
  $('crop-size').textContent = '';
  $('no-orig').style.display = 'flex';
  $('no-crop').style.display = 'flex';
}

// ---------------------------------------------------------------------------
// Frame rendering
// ---------------------------------------------------------------------------

async function triggerFrameUpdate() {
  if (!currentPreview || !currentPreview.crop) return;
  const info = currentPreview;
  const timestamp = parseFloat($('scrubber').value);
  const crop = info.crop;

  // Show loading state
  $('no-orig').textContent = 'Loading…';
  $('no-crop').textContent = 'Loading…';

  try {
    const [origB64, cropB64] = await Promise.all([
      window.go.main.App.GetFrame(info.path, timestamp, ''),
      window.go.main.App.GetFrame(info.path, timestamp, crop),
    ]);

    origFrameData = origB64;

    if (origB64) {
      $('no-orig').style.display = 'none';
      await drawOrigWithOverlay($('canvas-orig'), origB64, crop, info);
    } else {
      $('no-orig').textContent = 'Failed to extract frame';
    }

    if (cropB64) {
      $('no-crop').style.display = 'none';
      await drawSimpleFrame($('canvas-crop'), cropB64, $('crop-size'));
    } else {
      $('no-crop').textContent = 'Failed to extract frame';
    }
  } catch (e) {
    $('no-orig').textContent = 'Error';
    $('no-crop').textContent = 'Error';
    console.error('GetFrame error:', e);
  }
}

function loadImage(base64) {
  return new Promise((resolve, reject) => {
    const img = new Image();
    img.onload  = () => resolve(img);
    img.onerror = reject;
    img.src = 'data:image/png;base64,' + base64;
  });
}

async function drawOrigWithOverlay(canvas, base64, cropStr, info) {
  const img = await loadImage(base64);
  const natW = img.naturalWidth;
  const natH = img.naturalHeight;
  const { w: cw, h: ch, x: cx, y: cy } = parseCrop(cropStr);

  const container = canvas.parentElement;
  const scale = Math.min(1, (container.clientWidth - 8) / natW);
  canvas.width  = Math.max(1, Math.floor(natW * scale));
  canvas.height = Math.max(1, Math.floor(natH * scale));

  const ctx = canvas.getContext('2d');
  ctx.drawImage(img, 0, 0, canvas.width, canvas.height);

  const sx = canvas.width  / natW;
  const sy = canvas.height / natH;

  ctx.fillStyle = 'rgba(0,0,0,0.55)';
  ctx.fillRect(0, 0, canvas.width, canvas.height);

  // Restore crop region at full brightness
  ctx.drawImage(img, cx, cy, cw, ch, cx*sx, cy*sy, cw*sx, ch*sy);

  // Red border
  ctx.strokeStyle = '#ff3333';
  ctx.lineWidth = 2;
  ctx.strokeRect(cx*sx + 1, cy*sy + 1, cw*sx - 2, ch*sy - 2);

  $('orig-size').textContent = `${natW}×${natH}`;
}

async function drawSimpleFrame(canvas, base64, sizeEl) {
  const img = await loadImage(base64);
  const natW = img.naturalWidth;
  const natH = img.naturalHeight;

  const container = canvas.parentElement;
  const scale = Math.min(1, (container.clientWidth - 8) / natW);
  canvas.width  = Math.max(1, Math.floor(natW * scale));
  canvas.height = Math.max(1, Math.floor(natH * scale));

  const ctx = canvas.getContext('2d');
  ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
  if (sizeEl) sizeEl.textContent = `${natW}×${natH}`;
}

async function redrawOverlay() {
  if (!origFrameData || !currentPreview) return;
  const crop = getCropFromSpinboxes();
  const info = currentPreview;
  $('no-orig').style.display = 'none';
  await drawOrigWithOverlay($('canvas-orig'), origFrameData, crop, info);
  updatePreviewInfo(crop);
}

// ---------------------------------------------------------------------------
// Crop controls
// ---------------------------------------------------------------------------

function getCropFromSpinboxes() {
  return cropString(
    parseInt($('sp-w').value) || 0,
    parseInt($('sp-h').value) || 0,
    parseInt($('sp-x').value) || 0,
    parseInt($('sp-y').value) || 0
  );
}

function onSpinboxChanged() {
  if (suppressCropSignals || !currentPreview) return;
  const info = currentPreview;

  // Clamp W/H within bounds
  suppressCropSignals = true;
  const spW = $('sp-w'), spH = $('sp-h'), spX = $('sp-x'), spY = $('sp-y');
  if (parseInt(spX.value) + parseInt(spW.value) > info.width) {
    spW.value = info.width - parseInt(spX.value);
  }
  if (parseInt(spY.value) + parseInt(spH.value) > info.height) {
    spH.value = info.height - parseInt(spY.value);
  }
  // Switch aspect ratio selector back to "Custom"
  if ($('aspect-ratio').value !== '0') {
    $('aspect-ratio').value = '0';
    $('ar-info').textContent = 'Free edit';
  }
  suppressCropSignals = false;

  clearTimeout(cropEditTimer);
  cropEditTimer = setTimeout(redrawOverlay, 350);
}

async function onAspectRatioChanged() {
  if (suppressCropSignals || !currentPreview) return;
  const info = currentPreview;
  const idx  = parseInt($('aspect-ratio').value);
  const ar   = ASPECT_RATIOS[idx];
  if (!ar) return;

  if (ar.label === 'Custom') {
    $('ar-info').textContent = 'Free edit';
    return;
  }
  if (ar.label === 'From detection') {
    const auto = info.crop_auto;
    if (auto) {
      const { w: cw, h: ch, x: cx, y: cy } = parseCrop(auto);
      setSpinboxValues(cw, ch, cx, cy);
      $('ar-info').textContent = `Auto-detected: ${cw}×${ch} (${(cw/ch).toFixed(3)}:1)`;
      redrawOverlay();
    }
    return;
  }
  if (!ar.ratio) return;

  const result = await window.go.main.App.CalcCropForAspect(info.width, info.height, ar.ratio[0], ar.ratio[1]);
  setSpinboxValues(result.w, result.h, result.x, result.y);
  $('ar-info').textContent = `${result.w}×${result.h} (${(result.w/result.h).toFixed(3)}:1)`;
  redrawOverlay();
}

async function applyManualCrop() {
  if (!currentPreview) return;
  const info = currentPreview;
  const crop = getCropFromSpinboxes();
  info.crop = crop;
  updatePreviewInfo(crop);

  // Update Go-side state
  await window.go.main.App.UpdateCrop(info.path, crop);

  // Update table
  updateTableRow(selectedRow, crop);

  // Re-fetch cropped frame
  const timestamp = parseFloat($('scrubber').value);
  const cropB64 = await window.go.main.App.GetFrame(info.path, timestamp, crop);
  if (cropB64) {
    $('no-crop').style.display = 'none';
    await drawSimpleFrame($('canvas-crop'), cropB64, $('crop-size'));
  }
  if (origFrameData) {
    $('no-orig').style.display = 'none';
    await drawOrigWithOverlay($('canvas-orig'), origFrameData, crop, info);
  }
}

async function resetCrop() {
  if (!currentPreview) return;
  const info = currentPreview;
  const auto = info.crop_auto;
  if (!auto) return;

  info.crop = auto;
  const { w: cw, h: ch, x: cx, y: cy } = parseCrop(auto);
  setSpinboxValues(cw, ch, cx, cy);

  suppressCropSignals = true;
  $('aspect-ratio').value = '1'; // From detection
  suppressCropSignals = false;
  $('ar-info').textContent = `Auto-detected: ${cw}×${ch} (${(cw/ch).toFixed(3)}:1)`;

  await window.go.main.App.UpdateCrop(info.path, auto);
  updateTableRow(selectedRow, auto);
  updatePreviewInfo(auto);

  const timestamp = parseFloat($('scrubber').value);
  const cropB64 = await window.go.main.App.GetFrame(info.path, timestamp, auto);
  if (cropB64) {
    $('no-crop').style.display = 'none';
    await drawSimpleFrame($('canvas-crop'), cropB64, $('crop-size'));
  }
  if (origFrameData) {
    $('no-orig').style.display = 'none';
    await drawOrigWithOverlay($('canvas-orig'), origFrameData, auto, info);
  }
}

// ---------------------------------------------------------------------------
// Settings helpers
// ---------------------------------------------------------------------------

function getSettings() {
  return {
    hw_mode:         $('hw-mode').value,
    quality:         parseInt($('quality').value),
    preset:          $('preset').value,
    look_ahead:      $('lookahead').checked,
    sample_interval: parseInt($('interval').value),
    suffix:          $('suffix').value,
    overwrite:       $('overwrite').checked,
  };
}

function setupHWModes(plat) {
  const sel = $('hw-mode');
  sel.innerHTML = '';
  HW_MODES_ALL.forEach(m => {
    if (m.platforms === null || m.platforms.includes(plat)) {
      const opt = document.createElement('option');
      opt.value       = m.key;
      opt.textContent = m.label;
      sel.appendChild(opt);
    }
  });
  onHWModeChanged();
}

function onHWModeChanged() {
  const mode = $('hw-mode').value;
  $('lookahead').disabled  = mode !== 'qsv';
  $('preset').disabled     = mode === 'vt' || mode === 'vt_fullhw';
  if (mode !== 'qsv') $('lookahead').checked = false;
}

// ---------------------------------------------------------------------------
// Browse / load
// ---------------------------------------------------------------------------

async function browse() {
  const isFolder = $('mode-folder').checked;
  const path = isFolder
    ? await window.go.main.App.OpenFolderDialog()
    : await window.go.main.App.OpenFileDialog();
  if (path) {
    $('input-path').value = path;
    await loadFiles(path);
  }
}

async function loadFiles(path) {
  if (!path) return;
  clearPreview();
  files = (await window.go.main.App.LoadFiles(path)) || [];
  buildTable();
  $('btn-process').disabled = true;
  $('btn-preview').disabled = true;
}

// ---------------------------------------------------------------------------
// Detection
// ---------------------------------------------------------------------------

async function startDetection() {
  if (!files.length) { addLog('No files loaded.'); return; }

  isDetecting = true;
  setButtonState();
  setProgress(0);
  showProgress(true);

  await window.go.main.App.StartDetection(getSettings().sample_interval);
}

// ---------------------------------------------------------------------------
// Processing
// ---------------------------------------------------------------------------

async function startProcessing() {
  isEncoding = true;
  setButtonState();
  setProgress(0);
  showProgress(true);

  await window.go.main.App.StartProcessing(getSettings());
}

// ---------------------------------------------------------------------------
// Cancel
// ---------------------------------------------------------------------------

async function cancelOperation() {
  await window.go.main.App.CancelOperation();
}

// ---------------------------------------------------------------------------
// Button state
// ---------------------------------------------------------------------------

function setButtonState() {
  const busy = isDetecting || isEncoding;
  $('btn-detect').disabled  = busy;
  $('btn-process').disabled = busy || !files.length;
  $('btn-preview').disabled = busy || !files.length;
  $('btn-cancel').disabled  = !busy;
}

// ---------------------------------------------------------------------------
// Event handlers from Go
// ---------------------------------------------------------------------------

function onDetectStart(data) {
  setRowStatus(data.row, 'Detecting…', 'status-working');
}

function onDetectResult(data) {
  const row  = data.row;
  const crop = data.crop;  // empty string means failed
  const done = Number(data.done);
  const total = Number(data.total);

  if (row >= 0 && row < files.length) {
    files[row].crop      = crop || null;
    files[row].crop_auto = crop || null;
    updateTableRow(row, crop || null);
    if (!crop) setRowStatus(row, 'Detection failed', 'status-error');
  }

  setProgress((done / total) * 100);
}

function onDetectComplete() {
  isDetecting = false;
  setButtonState();
  showProgress(false);
  addLog('Detection complete.');
  $('btn-process').disabled = false;
  $('btn-preview').disabled = false;

  // Auto-preview first file with black bars
  for (let i = 0; i < files.length; i++) {
    const f = files[i];
    if (f.crop) {
      const { w: cw, h: ch } = parseCrop(f.crop);
      if (cw !== f.width || ch !== f.height) {
        selectRow(i);
        break;
      }
    }
  }
}

function onDetectCancelled() {
  isDetecting = false;
  setButtonState();
  showProgress(false);
}

function onEncodeStart(data) {
  setRowStatus(data.row, `Encoding → ${data.output}`, 'status-working');
}

function onEncodeProgress(data) {
  setProgress(data.pct);
}

function onEncodeDone(data) {
  if (data.success) {
    setRowStatus(data.row, 'Done ✔', 'status-done');
  } else {
    setRowStatus(data.row, 'Error ✖', 'status-error');
  }
}

function onEncodeComplete() {
  isEncoding = false;
  setButtonState();
  showProgress(false);
}

function onEncodeCancelled() {
  isEncoding = false;
  setButtonState();
  showProgress(false);
}

// ---------------------------------------------------------------------------
// Split-pane drag
// ---------------------------------------------------------------------------

function initSplitter() {
  const handle    = $('split-handle');
  const tableBox  = $('table-section');
  const splitBox  = $('main-split');
  let dragging = false, startY = 0, startH = 0;

  handle.addEventListener('mousedown', e => {
    dragging = true;
    startY   = e.clientY;
    startH   = tableBox.getBoundingClientRect().height;
    document.body.style.cursor = 'ns-resize';
    e.preventDefault();
  });
  document.addEventListener('mousemove', e => {
    if (!dragging) return;
    const splitH = splitBox.getBoundingClientRect().height;
    const newH   = Math.min(splitH - 120, Math.max(60, startH + (e.clientY - startY)));
    tableBox.style.height = newH + 'px';
  });
  document.addEventListener('mouseup', () => {
    dragging = false;
    document.body.style.cursor = '';
  });
}

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------

async function init() {
  // Populate presets
  const presetSel = $('preset');
  QSV_PRESETS.forEach(p => {
    const opt = document.createElement('option');
    opt.value = p; opt.textContent = p;
    if (p === 'medium') opt.selected = true;
    presetSel.appendChild(opt);
  });

  // Populate aspect ratios
  const arSel = $('aspect-ratio');
  ASPECT_RATIOS.forEach((ar, i) => {
    const opt = document.createElement('option');
    opt.value = i; opt.textContent = ar.label;
    arSel.appendChild(opt);
  });

  // Check ffmpeg and set up platform-specific HW modes
  const status = await window.go.main.App.CheckFFmpeg();
  platform = status.platform;
  setupHWModes(platform);

  if (status.found) {
    const hw = [];
    if (status.qsv_avail)  hw.push('QSV');
    if (status.vt_avail)   hw.push('VideoToolbox');
    addLog(hw.length
      ? `✔  ffmpeg found: ${status.path}  |  ${hw.join(', ')} available`
      : `⚠  ffmpeg found but no HW acceleration — CPU mode will work`
    );
  } else {
    addLog('⚠  ffmpeg not found. Install ffmpeg and ensure it is in your PATH.');
  }

  // Event listeners
  $('btn-browse').addEventListener('click',   browse);
  $('btn-detect').addEventListener('click',   startDetection);
  $('btn-preview').addEventListener('click',  previewSelected);
  $('btn-process').addEventListener('click',  startProcessing);
  $('btn-cancel').addEventListener('click',   cancelOperation);
  $('btn-apply').addEventListener('click',    applyManualCrop);
  $('btn-reset').addEventListener('click',    resetCrop);

  $('hw-mode').addEventListener('change', onHWModeChanged);
  $('aspect-ratio').addEventListener('change', onAspectRatioChanged);
  ['sp-w','sp-h','sp-x','sp-y'].forEach(id =>
    $(id).addEventListener('input', onSpinboxChanged)
  );

  $('input-path').addEventListener('keydown', e => {
    if (e.key === 'Enter') loadFiles($('input-path').value.trim());
  });

  const scrubber = $('scrubber');
  scrubber.addEventListener('input', () => {
    $('time-current').textContent = formatTime(parseFloat(scrubber.value));
    clearTimeout(scrubTimer);
    scrubTimer = setTimeout(triggerFrameUpdate, 300);
  });

  // Go → JS events
  window.runtime.EventsOn('log',              d => addLog(d));
  window.runtime.EventsOn('detect:start',     onDetectStart);
  window.runtime.EventsOn('detect:result',    onDetectResult);
  window.runtime.EventsOn('detect:complete',  onDetectComplete);
  window.runtime.EventsOn('detect:cancelled', onDetectCancelled);
  window.runtime.EventsOn('encode:start',     onEncodeStart);
  window.runtime.EventsOn('encode:progress',  onEncodeProgress);
  window.runtime.EventsOn('encode:done',      onEncodeDone);
  window.runtime.EventsOn('encode:complete',  onEncodeComplete);
  window.runtime.EventsOn('encode:cancelled', onEncodeCancelled);

  initSplitter();
  setButtonState();
}

function previewSelected() {
  if (selectedRow < 0 || selectedRow >= files.length) {
    addLog('Select a file in the table first.');
    return;
  }
  const info = files[selectedRow];
  if (!info.crop) {
    addLog(`No crop data for ${info.name} — run detection first.`);
    return;
  }
  loadPreview(info);
}

// Start when Wails runtime is ready
window.addEventListener('DOMContentLoaded', () => {
  // Wails injects window.go and window.runtime before DOMContentLoaded in the WebView.
  // If somehow not ready, retry briefly.
  const tryInit = (attempts = 0) => {
    if (typeof window.go !== 'undefined' && typeof window.runtime !== 'undefined') {
      init();
    } else if (attempts < 20) {
      setTimeout(() => tryInit(attempts + 1), 50);
    } else {
      console.error('Wails runtime not available after retries');
    }
  };
  tryInit();
});
