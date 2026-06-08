/**
 * api.js — thin fetch wrappers for all backend endpoints.
 */

const BASE = "";   // same-origin

export async function getStatus() {
  const r = await fetch(`${BASE}/api/status`);
  if (!r.ok) throw new Error(`Status fetch failed: ${r.status}`);
  return r.json();
}

export async function uploadFiles(e57File, ifcFile) {
  const fd = new FormData();
  fd.append("e57_file", e57File);
  fd.append("ifc_file", ifcFile);
  const r = await fetch(`${BASE}/api/upload`, { method: "POST", body: fd });
  if (!r.ok) throw new Error((await r.json()).detail || "Upload failed");
  return r.json();
}

export async function getUploadInfo() {
  const r = await fetch(`${BASE}/api/upload-info`);
  if (!r.ok) throw new Error((await r.json()).detail || "Info fetch failed");
  return r.json();
}

export async function startRegistration() {
  const r = await fetch(`${BASE}/api/register`, { method: "POST" });
  if (!r.ok) throw new Error((await r.json()).detail || "Registration start failed");
  return r.json();
}

export async function skipRegistration() {
  const r = await fetch(`${BASE}/api/register/skip`, { method: "POST" });
  if (!r.ok) throw new Error((await r.json()).detail || "Skip failed");
  return r.json();
}

export async function startCQA() {
  const r = await fetch(`${BASE}/api/cqa`, { method: "POST" });
  if (!r.ok) throw new Error((await r.json()).detail || "CQA start failed");
  return r.json();
}

export async function startMQA() {
  const r = await fetch(`${BASE}/api/mqa`, { method: "POST" });
  if (!r.ok) throw new Error((await r.json()).detail || "MQA start failed");
  return r.json();
}

export async function getCQAResults() {
  const r = await fetch(`${BASE}/api/cqa/results`);
  if (!r.ok) throw new Error((await r.json()).detail || "CQA results failed");
  return r.json();
}

export async function getMQAResults() {
  const r = await fetch(`${BASE}/api/mqa/results`);
  if (!r.ok) throw new Error((await r.json()).detail || "MQA results failed");
  return r.json();
}

export async function getViewerData(type) {
  const r = await fetch(`${BASE}/api/viewer-data/${type}`);
  if (!r.ok) throw new Error((await r.json()).detail || "Viewer data fetch failed");
  return r.json();
}

export function exportUrl(kind) {
  return kind ? `${BASE}/api/export?kind=${encodeURIComponent(kind)}` : `${BASE}/api/export`;
}

export async function exportBlob(kind) {
  const qs = kind ? `?kind=${encodeURIComponent(kind)}` : '';
  const r = await fetch(`${BASE}/api/export${qs}`);
  if (!r.ok) {
    const err = await r.json().catch(() => ({}));
    throw new Error(err.detail || `Export failed (${r.status})`);
  }
  const blob = await r.blob();
  const cd = r.headers.get('content-disposition') || '';
  const m = cd.match(/filename[^;=\n]*=((['"]).*?\2|[^;\n]*)/i);
  const filename = m ? m[1].replace(/['"]/g, '') : `export_${kind || 'result'}.zip`;
  return { blob, filename };
}

export async function resetState() {
  const r = await fetch(`${BASE}/api/reset`, { method: 'POST' });
  return r.json();
}

// ── Binary helpers ─────────────────────────────────────────────────────────────

export function decodeFloat32(b64) {
  const binary = atob(b64);
  const buf    = new ArrayBuffer(binary.length);
  const view   = new Uint8Array(buf);
  for (let i = 0; i < binary.length; i++) {
    view[i] = binary.charCodeAt(i);
  }
  return new Float32Array(buf);
}

export function decodeInt32(b64) {
  const binary = atob(b64);
  const buf    = new ArrayBuffer(binary.length);
  const view   = new Uint8Array(buf);
  for (let i = 0; i < binary.length; i++) {
    view[i] = binary.charCodeAt(i);
  }
  return new Int32Array(buf);
}
