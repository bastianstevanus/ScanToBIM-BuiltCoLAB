/**
 * main.js — Application entry point.
 *
 * State machine:
 *   idle → uploading → loaded → registering → registered
 *        → cqa-running → cqa-done → mqa-running → mqa-done
 *
 * Button enable rules:
 *   Import:    always enabled
 *   Register:  requires loaded
 *   CQA:       requires loaded
 *   MQA:       requires loaded
 *   Export:    requires cqa-done OR mqa-done
 */

import * as api     from './api.js';
import { Viewer3D } from './viewer3d.js';

// Auto-reset on every page load so a refresh always gives a clean slate
(async () => { try { await api.resetState(); } catch(_) {} })();

// ── State ──────────────────────────────────────────────────────────────────────
const S = {
  loaded:      false,
  registered:  false,
  cqaDone:     false,
  mqaDone:     false,
  busy:        false,
  lastAnalysis: null,   // 'cqa' | 'mqa'
};

// ── DOM refs ───────────────────────────────────────────────────────────────────
const btnImport   = document.getElementById('btn-import');
const btnRegister = document.getElementById('btn-register');
const btnCQA      = document.getElementById('btn-cqa');
const btnMQA      = document.getElementById('btn-mqa');
const btnExport   = document.getElementById('btn-export');

const progressSection = document.getElementById('progress-section');
const progressBar     = document.getElementById('progress-bar');
const progressMsg     = document.getElementById('progress-msg');
const progressPct     = document.getElementById('progress-pct');

const regResultSection = document.getElementById('reg-result-section');
const regFitnessBadge  = document.getElementById('reg-fitness-badge');
const regMethod        = document.getElementById('reg-method');
const regRmse          = document.getElementById('reg-rmse');

const infoSection = document.getElementById('info-section');
const infoScanPts = document.getElementById('info-scan-pts');
const infoElemCnt = document.getElementById('info-elem-cnt');
const infoReg     = document.getElementById('info-reg');

const cqaPanel     = document.getElementById('cqa-panel');
const cqaStats     = document.getElementById('cqa-stats');
const cqaTableWrap = document.getElementById('cqa-table-wrap');

const mqaPanel     = document.getElementById('mqa-panel');
const mqaStats     = document.getElementById('mqa-stats');
const mqaLegend    = document.getElementById('mqa-legend');
const mqaTableWrap = document.getElementById('mqa-table-wrap');

const legendSection  = document.getElementById('legend-section');
const legendCanvas   = document.getElementById('legend-canvas');
const legendTicks    = document.getElementById('legend-ticks');
const qualitySection = document.getElementById('quality-summary-section');
const qualityBody    = document.getElementById('quality-summary-body');

const viewTabs      = document.getElementById('view-tabs');
const viewerOverlay = document.getElementById('viewer-overlay');
const overlayMsg    = document.getElementById('overlay-msg');
const overlaySpinner= document.getElementById('overlay-spinner');

const projectBadge = document.getElementById('project-badge');
const importModal  = new bootstrap.Modal(document.getElementById('import-modal'));
const appToast     = new bootstrap.Toast(document.getElementById('app-toast'),
                                          { delay: 4000 });

// ── Viewer ─────────────────────────────────────────────────────────────────────
const viewer = new Viewer3D('viewer-canvas');

// ── Status polling ─────────────────────────────────────────────────────────────
let _pollTimer = null;

function startPolling(onDone) {
  stopPolling();
  _pollTimer = setInterval(async () => {
    try {
      const st = await api.getStatus();
      setProgress(st.progress, st.message);
      if (st.step === 'done' || st.step === 'error') {
        stopPolling();
        if (st.step === 'error') {
          showError(st.error || 'Unknown error');
        } else {
          onDone(st);
        }
      }
    } catch (_) { /* network hiccup, ignore */ }
  }, 800);
}

function stopPolling() {
  if (_pollTimer) { clearInterval(_pollTimer); _pollTimer = null; }
}

// ── Progress helpers ───────────────────────────────────────────────────────────
function setProgress(p, msg) {
  progressSection.style.display = '';
  const pct = Math.round(p * 100);
  progressBar.style.width = `${pct}%`;
  progressBar.setAttribute('aria-valuenow', pct);
  progressBar.classList.remove('bg-danger');
  progressBar.classList.add('progress-bar-striped', 'progress-bar-animated');
  progressPct.textContent = `${pct}%`;
  progressPct.className   = 'badge bg-info text-dark';
  progressMsg.textContent = msg;
}

function hideProgress() {
  progressSection.style.display = 'none';
  progressBar.classList.remove('bg-danger');
  progressBar.classList.add('progress-bar-striped', 'progress-bar-animated');
  progressBar.style.width = '0%';
  progressPct.textContent = '0%';
  progressPct.className   = 'badge bg-secondary';
  progressMsg.textContent = '';
}

function setBusy(busy) {
  S.busy = busy;
  [btnImport, btnRegister, btnCQA, btnMQA, btnExport].forEach(b => {
    if (busy) {
      b.setAttribute('data-prev-disabled', b.disabled ? '1' : '0');
      b.disabled = true;
    } else {
      b.disabled = b.getAttribute('data-prev-disabled') === '1' ? true : false;
    }
  });
  syncButtons();
}

function syncButtons() {
  if (S.busy) return;
  btnImport.disabled   = false;
  btnRegister.disabled = !S.loaded;
  btnCQA.disabled      = !S.loaded;
  btnMQA.disabled      = !S.loaded;
  btnExport.disabled   = !(S.cqaDone || S.mqaDone);
  [btnRegister, btnCQA, btnMQA].forEach(b => {
    b.classList.toggle('btn-primary',   !b.disabled);
    b.classList.toggle('btn-secondary',  b.disabled);
  });
}

// ── Toast / overlay ────────────────────────────────────────────────────────────
function showToast(msg) {
  document.getElementById('toast-body').textContent = msg;
  appToast.show();
}

function showError(msg) {
  setBusy(false);
  progressSection.style.display = '';
  progressBar.classList.remove('progress-bar-striped', 'progress-bar-animated');
  progressBar.classList.add('bg-danger');
  progressBar.style.width = '100%';
  progressPct.textContent = 'Error';
  progressPct.className   = 'badge bg-danger';
  progressMsg.textContent = msg || 'An unknown error occurred.';
  showToast('Error: ' + (msg || 'Unknown error'));
  setOverlay(false, '', false);
}

function setOverlay(visible, msg = '', spinner = false) {
  viewerOverlay.classList.toggle('hidden', !visible);
  overlayMsg.textContent      = msg;
  overlaySpinner.style.display = spinner ? '' : 'none';
}

// ── Button handlers ────────────────────────────────────────────────────────────

// 1. Import
btnImport.addEventListener('click', () => importModal.show());

document.getElementById('btn-import-confirm').addEventListener('click', async () => {
  const e57 = document.getElementById('e57-input').files[0];
  const ifc = document.getElementById('ifc-input').files[0];
  const errDiv = document.getElementById('import-error');

  if (!e57 || !ifc) {
    errDiv.textContent = 'Please select both a point cloud (E57/PLY/PCD) and an IFC file.';
    errDiv.classList.remove('d-none');
    return;
  }
  errDiv.classList.add('d-none');
  importModal.hide();

  S.loaded = false;
  S.registered = false;
  S.cqaDone = false;
  S.mqaDone = false;
  S.lastAnalysis = null;
  [cqaPanel, mqaPanel, legendSection, qualitySection, regResultSection, infoSection].forEach(el => {
    if (el) el.classList.add('d-none');
  });
  viewTabs.classList.add('d-none');
  viewer.clearAll();

  setBusy(true);
  setOverlay(true, 'Uploading and loading files…', true);
  setProgress(0.01, 'Uploading…');

  try {
    await api.uploadFiles(e57, ifc);
    startPolling(async (st) => {
      try {
        const info = await api.getUploadInfo();
        infoScanPts.textContent = info.scan_count.toLocaleString();
        infoElemCnt.textContent = info.element_count;
        infoSection.classList.remove('d-none');
        projectBadge.textContent = info.project_name;
        projectBadge.classList.remove('d-none');

        S.loaded = true;
        setBusy(false);
        hideProgress();

        try {
          const vd = await api.getViewerData('upload');
          _loadViewerData(vd);
          setOverlay(false);
          viewTabs.classList.remove('d-none');
          showToast(`Loaded ${info.scan_count.toLocaleString()} pts · ${info.element_count} elements`);
        } catch (e) {
          setOverlay(false);
          showToast('Files loaded. 3D preview unavailable: ' + e.message);
        }
      } catch(e) { showError(e.message); }
    });
  } catch (err) {
    showError(err.message);
  }
});

// 2. Registration
btnRegister.addEventListener('click', async () => {
  if (!S.loaded) return;
  setBusy(true);
  setOverlay(true, 'Running FGR + ICP registration…', true);
  setProgress(0.01, 'Starting…');
  try {
    await api.startRegistration();
    startPolling(async (st) => {
      try {
        S.registered = true;
        setBusy(false);
        hideProgress();

        const vd = await api.getViewerData('registration');
        _loadViewerData(vd);
        setOverlay(false);

        if (vd.meta) {
          const fit  = vd.meta.fitness;
          const rmse = vd.meta.rmse_mm;
          infoReg.textContent = `${vd.meta.method} | fit=${fit} | RMSE=${rmse} mm`;
          const fitPct = Math.round(fit * 100);
          regFitnessBadge.textContent = `Fitness: ${fit} (${fitPct}%)`;
          regFitnessBadge.className   = 'badge fs-6 px-3 py-2 ' +
            (fit >= 0.70 ? 'bg-success' : fit >= 0.40 ? 'bg-warning text-dark' : 'bg-danger');
          regMethod.textContent = vd.meta.method;
          regRmse.textContent   = `${rmse} mm`;
          regResultSection.classList.remove('d-none');
          btnRegister.classList.add('btn-outline-success');
          btnRegister.classList.remove('btn-primary');
        }
        showToast(st.message);
      } catch(e) { showError(e.message); }
    });
  } catch (err) { showError(err.message); }
});

// 3. CQA
btnCQA.addEventListener('click', async () => {
  if (!S.loaded) return;
  setBusy(true);
  setOverlay(true, 'Running Construction Quality Assessment…', true);
  setProgress(0.01, 'Starting…');
  try {
    await api.startCQA();
    startPolling(async (st) => {
      try {
        S.cqaDone = true;
        S.lastAnalysis = 'cqa';
        setBusy(false);
        hideProgress();

        const vd = await api.getViewerData('cqa');
        _loadViewerData(vd);
        setOverlay(false);

        legendSection.classList.remove('d-none');
        const maxDevM = vd.meta ? vd.meta.max_dev_mm / 1000 : 1.0;
        Viewer3D.drawJetLegend(legendCanvas, maxDevM);
        _drawLegendTicks(legendTicks, maxDevM);

        const res = await api.getCQAResults();
        _renderCQATable(res);
        cqaPanel.classList.remove('d-none');

        const sevDist = {};
        for (const e of res.elements) {
          sevDist[e.severity] = (sevDist[e.severity] || 0) + 1;
        }
        _renderQualityPanel(sevDist, 'sev');

        showToast(st.message);
      } catch(e) { showError(e.message); }
    });
  } catch (err) { showError(err.message); }
});

// 4. MQA
btnMQA.addEventListener('click', async () => {
  if (!S.loaded) return;
  setBusy(true);
  setOverlay(true, 'Running Model Quality Assessment…', true);
  setProgress(0.01, 'Starting…');
  try {
    await api.startMQA();
    startPolling(async (st) => {
      try {
        S.mqaDone = true;
        S.lastAnalysis = 'mqa';
        setBusy(false);
        hideProgress();

        const vd = await api.getViewerData('mqa');
        _loadViewerData(vd);
        setOverlay(false);

        const res = await api.getMQAResults();
        _renderMQATable(res);
        mqaPanel.classList.remove('d-none');

        legendSection.classList.remove('d-none');
        const maxDevM = vd.meta ? vd.meta.max_dev_mm / 1000 : 1.0;
        Viewer3D.drawJetLegend(legendCanvas, maxDevM);
        _drawLegendTicks(legendTicks, maxDevM);

        _renderQualityPanel(res.quality_distribution || {}, 'qual');

        showToast(st.message);
      } catch(e) { showError(e.message); }
    });
  } catch (err) { showError(err.message); }
});

// 5. Export
btnExport.addEventListener('click', async () => {
  const kind = S.lastAnalysis || (S.cqaDone ? 'cqa' : (S.mqaDone ? 'mqa' : null));
  btnExport.disabled = true;
  const origLabel = btnExport.innerHTML;
  btnExport.innerHTML = 'Exporting…';
  try {
    const { blob, filename } = await api.exportBlob(kind);
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    setTimeout(() => URL.revokeObjectURL(url), 2000);
    showToast('Export downloaded: ' + filename);
  } catch (e) {
    showError(e.message);
  } finally {
    btnExport.innerHTML = origLabel;
    syncButtons();
  }
});

// ── View-mode tabs ─────────────────────────────────────────────────────────────
document.querySelectorAll('.view-tab').forEach(tab => {
  tab.addEventListener('click', () => {
    document.querySelectorAll('.view-tab').forEach(t => t.classList.remove('active'));
    tab.classList.add('active');
    const mode = tab.dataset.view;
    viewer.showLayer('scan', mode !== 'bim');
    viewer.showLayer('bim',  mode !== 'scan');
  });
});

// ── Viewer data loader ─────────────────────────────────────────────────────────
function _loadViewerData(vd) {
  viewer.clearAll();

  if (vd.scan) {
    const pos = api.decodeFloat32(vd.scan.positions_b64);
    const col = api.decodeFloat32(vd.scan.colors_b64);
    viewer.loadLayer('scan', pos, col, 0.03);
  }
  if (vd.bim) {
    const pos = api.decodeFloat32(vd.bim.positions_b64);
    const col = api.decodeFloat32(vd.bim.colors_b64);
    if (vd.bim.is_mesh) {
      viewer.loadMesh('bim', pos, col);
    } else {
      viewer.loadLayer('bim', pos, col, 0.04);
    }
  }
  viewer.centerCamera();

  document.querySelectorAll('.view-tab').forEach(t =>
    t.classList.toggle('active', t.dataset.view === 'both'));
  viewer.showLayer('scan', true);
  viewer.showLayer('bim',  true);
}

// ── CQA results table ──────────────────────────────────────────────────────────
function _renderCQATable(res) {
  const n      = res.elements.length;
  const nCompl = res.elements.filter(e => e.severity === 'Compliant').length;
  const avgMean = n > 0
    ? (res.elements.reduce((a, e) => a + e.mean_mm, 0) / n).toFixed(1)
    : 0;

  cqaStats.innerHTML =
    `<b class="text-white">${n}</b> elements assessed · ` +
    `<span class="sev-Compliant">${nCompl} compliant</span> · ` +
    `avg ${avgMean} mm` +
    (res.context ? ` · <em>${res.context}</em>` : '');

  const rows = res.elements.slice(0, 60).map(e => `
    <tr>
      <td title="${e.guid}">${_shortType(e.ifc_type)}</td>
      <td>${_esc(e.name) || '—'}</td>
      <td>${e.mean_mm}</td>
      <td><span class="sev-${e.severity}">${e.severity}</span></td>
    </tr>`).join('');

  cqaTableWrap.innerHTML = `
    <table>
      <thead><tr><th>Type</th><th>Name</th><th>Mean mm</th><th>Severity</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>`;
}

// ── MQA results table ──────────────────────────────────────────────────────────
function _renderMQATable(res) {
  const dist = res.quality_distribution || {};

  mqaStats.innerHTML = Object.entries(dist).map(([q, c]) =>
    `<span class="qual-${q} me-2">● ${q}: ${c}</span>`).join('');

  mqaLegend.innerHTML = [
    ['Excellent', '#33cc66'],
    ['Good',      '#8dea4e'],
    ['Fair',      '#e3b341'],
    ['Poor',      '#da3633'],
  ].map(([l, c]) =>
    `<span class="legend-dot" style="background:${c}"></span>${l} `
  ).join('&nbsp;&nbsp;');

  const fmt = (v) => (v === '' || v === null || v === undefined) ? '—' : v;
  const sevClass = (s) => {
    const sl = (s || '').toLowerCase();
    if (sl === 'compliant') return 'sev-Compliant';
    if (sl === 'minor')     return 'sev-Minor';
    if (sl === 'moderate')  return 'sev-Moderate';
    if (sl === 'critical' || sl === 'severe' || sl === 'major') return 'sev-Critical';
    return '';
  };
  const pfClass = (s) => (s === 'PASS' ? 'sev-Compliant' : (s === 'FAIL' ? 'sev-Critical' : ''));

  const rows = res.elements.slice(0, 60).map(e => `
    <tr>
      <td title="${e.guid}">${_esc(e.name) || '—'}</td>
      <td>${_shortType(e.ifc_type)}</td>
      <td>${fmt(e.lod_level)}</td>
      <td>${fmt(e.tolerance_mm)}</td>
      <td>${fmt(e.mean_mm)}</td>
      <td>${fmt(e.max_mm)}</td>
      <td>${fmt(e.quality_score)}</td>
      <td>${fmt(e.grade)}</td>
      <td><span class="${pfClass(e.pass_fail_status)}">${fmt(e.pass_fail_status)}</span></td>
      <td><span class="${sevClass(e.severity)}">${fmt(e.severity)}</span></td>
      <td>${_esc(fmt(e.deviation_status))}</td>
    </tr>`).join('');

  mqaTableWrap.innerHTML = `
    <table>
      <thead><tr>
        <th>CustomName</th><th>Type</th><th>LOD</th><th>Tol mm</th>
        <th>Mean mm</th><th>Max mm</th>
        <th>Quality Score</th><th>Grade</th>
        <th>Pass/Fail</th><th>Severity</th>
        <th>Deviation_Status</th>
      </tr></thead>
      <tbody>${rows}</tbody>
    </table>`;
}

// ── Utilities ──────────────────────────────────────────────────────────────────
function _shortType(t) {
  return t ? t.replace('Ifc', '') : t;
}

function _esc(s) {
  if (!s) return '';
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

// ── Legend tick labels ─────────────────────────────────────────────────────────
function _drawLegendTicks(container, maxDevM) {
  if (!container) return;
  container.innerHTML = '';
  const steps = 5;
  for (let i = 0; i <= steps; i++) {
    const t = i / steps;
    const val = (t * maxDevM).toFixed(2) + ' m';
    const span = document.createElement('span');
    span.textContent = val;
    span.style.cssText = `
      position:absolute; left:${t * 100}%;
      transform:translateX(-50%);
      color:#e6edf3; font-size:0.65rem; white-space:nowrap;
    `;
    container.appendChild(span);
  }
  container.style.position = 'relative';
  container.style.height   = '1.1rem';
}

// ── Quality summary panel ─────────────────────────────────────────────────────
function _renderQualityPanel(dist, mode = 'qual') {
  if (!qualitySection || !qualityBody) return;
  const QUAL_COLORS = { Excellent: '#33cc66', Good: '#8dea4e', Fair: '#e3b341', Poor: '#da3633' };
  const SEV_COLORS  = { Compliant: '#33cc66', Minor: '#8dea4e', Major: '#e3b341',
                        Severe: '#da3633', Critical: '#da3633', Warning: '#e3b341' };
  const COLORS = mode === 'sev' ? SEV_COLORS : QUAL_COLORS;
  const ORDER  = mode === 'sev'
    ? ['Compliant', 'Minor', 'Major', 'Severe', 'Critical', 'Warning']
    : ['Excellent', 'Good', 'Fair', 'Poor'];
  const total = Object.values(dist).reduce((a, b) => a + b, 0);
  const entries = ORDER.filter(k => k in dist)
    .concat(Object.keys(dist).filter(k => !ORDER.includes(k)))
    .map(k => [k, dist[k]]);
  qualityBody.innerHTML = entries.map(([level, count]) => {
    const pct = total > 0 ? Math.round(count / total * 100) : 0;
    const col = COLORS[level] || '#888';
    return `
      <div class="quality-row d-flex align-items-center mb-1">
        <span style="background:${col};border-radius:50%;width:10px;height:10px;flex-shrink:0;display:inline-block;margin-right:6px;"></span>
        <span class="text-white" style="width:72px;font-size:0.8rem;">${level}</span>
        <div class="flex-grow-1 me-2" style="background:#2d333b;border-radius:3px;height:8px;">
          <div style="width:${pct}%;background:${col};height:8px;border-radius:3px;"></div>
        </div>
        <span class="text-white" style="font-size:0.75rem;min-width:42px;text-align:right;">
          ${count} <small style="color:#aaa;">(${pct}%)</small>
        </span>
      </div>`;
  }).join('');
  qualitySection.classList.remove('d-none');
}

// ── Init ───────────────────────────────────────────────────────────────────────
syncButtons();
