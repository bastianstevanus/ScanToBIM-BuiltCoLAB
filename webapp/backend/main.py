"""
FastAPI backend — Scan-to-BIM Quality Assessment Web Application.

Endpoints:
  GET  /                        → serves index.html
  GET  /api/status              → current job status
  POST /api/upload              → upload .e57 + .ifc files
  POST /api/register            → start FGR+ICP registration
  POST /api/cqa                 → Construction Quality Assessment
  POST /api/mqa                 → Model Quality Assessment
  GET  /api/viewer-data/{type}  → 3D data for Three.js viewer (type: upload|registration|cqa|mqa)
  GET  /api/export              → download ZIP with all results
"""
from __future__ import annotations

import asyncio
import base64
import copy
import logging
import os
import shutil
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Dict, Optional

import numpy as np
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from backend.state import app_state

log = logging.getLogger(__name__)

# ── Directories ────────────────────────────────────────────────────────────────
if getattr(sys, "frozen", False):
    _MEIPASS     = Path(sys._MEIPASS)  # type: ignore[attr-defined]
    FRONTEND_DIR = _MEIPASS / "frontend"
    _APP_DATA    = Path(os.environ.get("APPDATA", os.path.expanduser("~"))) / "ScanToBIM"
    UPLOAD_DIR   = _APP_DATA / "uploads"
    OUTPUT_DIR   = _APP_DATA / "outputs"
else:
    BASE_DIR     = Path(__file__).resolve().parent.parent
    UPLOAD_DIR   = BASE_DIR / "uploads"
    OUTPUT_DIR   = BASE_DIR / "outputs"
    FRONTEND_DIR = BASE_DIR / "frontend"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# ── App ────────────────────────────────────────────────────────────────────────
@asynccontextmanager
async def _lifespan(app: FastAPI):
    yield

app = FastAPI(title="Scan-to-BIM Quality Assessment", version="1.0.0", lifespan=_lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# CPU-bound executor (single worker to avoid memory issues with large PCDs)
_executor = ThreadPoolExecutor(max_workers=1)

# Serve frontend as the root mount
app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")


@app.get("/", include_in_schema=False)
async def root():
    return FileResponse(
        str(FRONTEND_DIR / "index.html"),
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    raise HTTPException(404)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _b64_float32(arr: np.ndarray) -> str:
    return base64.b64encode(arr.astype(np.float32).tobytes()).decode()


def _subsample_pcd(pcd, max_pts: int = 200_000):
    """Return numpy arrays (pts, colors) subsampled to max_pts."""
    pts = np.asarray(pcd.points)
    if pcd.has_colors():
        cols = np.asarray(pcd.colors)
    else:
        cols = np.full((len(pts), 3), 0.6)

    if len(pts) > max_pts:
        idx = np.random.default_rng(42).choice(len(pts), max_pts, replace=False)
        pts  = pts[idx]
        cols = cols[idx]

    return pts.astype(np.float32), cols.astype(np.float32)


# ── Status ─────────────────────────────────────────────────────────────────────

@app.get("/api/status")
async def get_status():
    s = app_state.status
    return {
        "step":     s.step,
        "progress": s.progress,
        "message":  s.message,
        "error":    s.error,
    }


@app.post("/api/reset")
async def reset_state():
    """Reset all state so the user can start a new session without restarting the server."""
    app_state.reset()
    for old in UPLOAD_DIR.glob("*"):
        try: old.unlink()
        except Exception: pass
    return {"status": "reset"}


# ── Upload ─────────────────────────────────────────────────────────────────────

@app.post("/api/upload")
async def upload_files(
    e57_file: UploadFile = File(...),
    ifc_file: UploadFile = File(...),
):
    if app_state.is_busy():
        raise HTTPException(409, "Another job is running. Please wait.")

    app_state.reset()

    for old in UPLOAD_DIR.glob("*"):
        try: old.unlink()
        except Exception: pass

    e57_path = UPLOAD_DIR / e57_file.filename
    ifc_path = UPLOAD_DIR / ifc_file.filename

    with open(e57_path, "wb") as f:
        shutil.copyfileobj(e57_file.file, f)
    with open(ifc_path, "wb") as f:
        shutil.copyfileobj(ifc_file.file, f)

    app_state.scan_path = str(e57_path)
    app_state.ifc_path  = str(ifc_path)

    app_state.update_status("loading", 0.01, "Files received, loading…")
    asyncio.get_running_loop().run_in_executor(_executor, _load_files_sync)

    return {"status": "loading", "message": "Files received, loading…"}


def _load_files_sync():
    from backend.services.e57_loader  import load_point_cloud
    from backend.services.ifc_service import load_ifc

    try:
        app_state.update_status("loading", 0.05, "Loading point cloud…")
        pcd, colors = load_point_cloud(
            app_state.scan_path,
            progress=lambda p, m: app_state.update_status("loading", 0.05 + p * 0.40, m))
        app_state.scan_pcd        = pcd
        app_state.scan_colors_raw = colors

        app_state.update_status("loading", 0.50, "Loading IFC model…")
        ifc_file, proj_name, elements_data, bim_pcd, _ = load_ifc(
            app_state.ifc_path,
            progress=lambda p, m: app_state.update_status("loading", 0.50 + p * 0.45, m),
            scan_pcd=pcd)

        app_state.ifc_file         = ifc_file
        app_state.ifc_project_name = Path(app_state.ifc_path).stem
        app_state.elements_data    = elements_data
        app_state.bim_pcd          = bim_pcd

        from backend.services.deviation_service import assign_custom_names
        assign_custom_names(elements_data, ifc_file)

        n_pts  = len(np.asarray(pcd.points))
        n_elem = len(elements_data)
        app_state.set_done(f"Loaded {n_pts:,} scan points · {n_elem} IFC elements")
    except Exception as exc:
        log.exception("File loading failed")
        app_state.set_error(str(exc))


# ── Registration ───────────────────────────────────────────────────────────────

@app.post("/api/register")
async def start_registration():
    if app_state.is_busy():
        raise HTTPException(409, "Another job is running.")
    if app_state.scan_pcd is None or app_state.bim_pcd is None:
        raise HTTPException(400, "Upload files first.")

    app_state.update_status("registration", 0.01, "Registration queued…")
    asyncio.get_running_loop().run_in_executor(_executor, _register_sync)
    return {"status": "started"}


def _register_sync():
    from backend.services.registration_service import run_registration
    try:
        app_state.update_status("registration", 0.02, "Starting registration…")
        res = run_registration(
            app_state.scan_pcd,
            app_state.bim_pcd,
            voxel_size=0.10,
            progress=lambda p, m: app_state.update_status("registration", p, m))

        app_state.registered_pcd       = res["aligned_pcd"]
        app_state.registration_T       = res["T"]
        app_state.registration_fitness = res["fitness"]
        app_state.registration_rmse    = res["rmse"]
        app_state.registration_method  = res["method"]

        app_state.set_done(
            f"Registration done — {res['method']} | "
            f"fitness={res['fitness']:.4f}  RMSE={res['rmse']*1000:.1f} mm")
    except Exception as exc:
        log.exception("Registration failed")
        app_state.set_error(str(exc))


@app.post("/api/register/skip")
async def skip_registration():
    """Use when scan and IFC are already in the same coordinate system."""
    if app_state.is_busy():
        raise HTTPException(409, "Another job is running.")
    if app_state.scan_pcd is None:
        raise HTTPException(400, "Upload files first.")

    app_state.registered_pcd       = copy.deepcopy(app_state.scan_pcd)
    app_state.registration_T       = np.eye(4, dtype=np.float64)
    app_state.registration_fitness = 1.0
    app_state.registration_rmse    = 0.0
    app_state.registration_method  = "None (pre-aligned)"
    app_state.set_done("Skipped registration — scan is pre-aligned with IFC model")
    return {"status": "done"}


# ── CQA ──────────────────────────────────────────────────────────────────────────────────

@app.post("/api/cqa")
async def start_cqa():
    if app_state.is_busy():
        raise HTTPException(409, "Another job is running.")
    if app_state.registered_pcd is None:
        if app_state.scan_pcd is None:
            raise HTTPException(400, "Upload files first.")
        app_state.registered_pcd       = copy.deepcopy(app_state.scan_pcd)
        app_state.registration_T       = np.eye(4, dtype=np.float64)
        app_state.registration_fitness = 1.0
        app_state.registration_rmse    = 0.0
        app_state.registration_method  = "None (pre-aligned)"

    app_state.update_status("cqa", 0.01, "CQA queued…")
    asyncio.get_running_loop().run_in_executor(_executor, _cqa_sync)
    return {"status": "started"}


_FAIL_REASON_STATUS: dict[str, str] = {
    "no_bim_points":            "Need Validation (No BIM Points)",
    "no_nearby_points":         "Need Validation (No Nearby Points)",
    "nbp_filter_empty":         "Need Validation (No Nearby Surface Points)",
    "ransac_empty":             "Need Validation (No Dominant Plane Found)",
    "normal_filter_empty":      "Need Validation (Normal Filter Removed All Points)",
    "proximity_filter_empty":   "Need Validation (Proximity Filter Removed All Points)",
    "graph_empty":              "Need Validation (No Graph Connectivity)",
    "no_valid_deviation":       "Need Validation (All Deviations Out of Range)",
    "low_coverage":             "Need Validation (Low Coverage)",
    "less_than_min_points":     "Need Validation (Less Than Min Points)",
    "no_result":                "Need Validation (Not Segmented)",
}


def _severity_to_status(severity: str, stats: dict | None = None) -> str:
    if stats is not None:
        reason = stats.get("fail_reason", "")
        if reason in _FAIL_REASON_STATUS:
            return _FAIL_REASON_STATUS[reason]
        n_cand = int(stats.get("num_candidates", 0) or 0)
        n_norm = int(stats.get("num_after_normal_filter", 0) or 0)
        n_seg  = int(stats.get("num_segmented", 0) or 0)
        if n_cand == 0:
            return "Need Validation (No Candidates)"
        if n_norm == 0:
            return "Need Validation (Normal Filter)"
        if n_seg < 25:
            return "Need Validation (Low Coverage)"
    return "Valid"


def _ensure_cqa_fields(dev_results: dict, context: str = "HydraulicStructure",
                        kind: str = "cqa") -> None:
    from backend.services.construction_kg import (
        _notebook_tolerance, _short_type, classify_severity)
    for s in dev_results.values():
        if s.get("severity"):
            continue
        ifc_short = _short_type(s.get("ifc_type", "IfcElement"))
        (t_c, t_m, t_mo, std_ref), importance = _notebook_tolerance(ifc_short, context)
        sev = classify_severity(float(s.get("mean", 0.0)), t_c, t_m, t_mo)
        s["severity"]            = sev
        s["importance"]          = importance
        s["tolerance_compliant"] = float(t_c)
        s["tolerance_minor"]     = float(t_m)
        s["tolerance_moderate"]  = float(t_mo)
        s["standard_ref"] = "ISO 19650 / IFC4" if kind == "mqa" else str(std_ref)


def _build_deviation_csv(dev_results: dict,
                          context: str = "HydraulicStructure",
                          kind: str = "cqa",
                          elements_data: list | None = None) -> str:
    import csv as _csv
    import io as _io
    _ensure_cqa_fields(dev_results, context=context, kind=kind)
    buf = _io.StringIO()
    writer = _csv.writer(buf)
    writer.writerow([
        "CustomName", "ElementID", "GlobalId", "FamilyName",
        "NumPoints", "NumCandidates", "NumAfterNormalFilter", "NumSegmented",
        "Mean_Deviation", "Max_Deviation", "Std_Deviation", "RMSE",
        "Importance", "Severity", "Tolerance", "Standard",
        "Deviation_Status",
        "BIM_Volume_m3",
    ])
    for guid, s in dev_results.items():
        writer.writerow([
            s.get("name", ""), s.get("element_id", ""), guid,
            s.get("family_name", ""),
            s["num_points"],
            s.get("num_candidates", 0),
            s.get("num_after_normal_filter", 0),
            s.get("num_segmented", 0),
            round(s["mean"],  10), round(s["max"],  10),
            round(s.get("std",  0.0), 10), round(s["rmse"], 10),
            s.get("importance", ""),
            s.get("severity", ""),
            s.get("tolerance_compliant", ""),
            s.get("standard_ref", ""),
            _severity_to_status(s.get("severity", ""), s),
            round(s.get("volume", 0.0), 10),
        ])

    if elements_data:
        skipped = []
        for ed in elements_data:
            gid = ed.get("guid", "")
            if not gid or gid in dev_results:
                continue
            _v = ed.get("verts")
            if _v is None or (hasattr(_v, "__len__") and len(_v) == 0):
                reason = "Need Validation (No Geometry / Tessellation Failed)"
            else:
                reason = "Need Validation (Not Segmented — No Scan Coverage)"
            skipped.append([
                ed.get("name", ""),
                ed.get("element_id", "") or gid,
                gid,
                ed.get("family_name", ""),
                0, 0, 0, 0,
                "", "", "", "",
                "", "", "", "",
                reason,
                round(float(ed.get("volume", 0.0) or 0.0), 10),
            ])
        if skipped:
            writer.writerow([])
            writer.writerow(["=== Not Segmented / Not Found Elements ({} total) ===".format(len(skipped))])
            writer.writerow([
                "CustomName", "ElementID", "GlobalId", "FamilyName",
                "NumPoints", "NumCandidates", "NumAfterNormalFilter", "NumSegmented",
                "Mean_Deviation", "Max_Deviation", "Std_Deviation", "RMSE",
                "Importance", "Severity", "Tolerance", "Standard",
                "Deviation_Status",
                "BIM_Volume_m3",
            ])
            for r in skipped:
                writer.writerow(r)
    return buf.getvalue()


def _cqa_sync():
    from backend.services.segmentation_service import segment_all_elements
    from backend.services.deviation_service    import (compute_all_deviations,
                                                        apply_jet_colormap,
                                                        mqa_element_colors as _elem_colors)
    from backend.services.construction_kg      import (build_construction_kg,
                                                        classify_severity,
                                                        detect_context)
    try:
        app_state.update_status("cqa", 0.02, "Graph-based segmentation…")

        seg_map, seg_stats = segment_all_elements(
            app_state.registered_pcd,
            app_state.elements_data,
            mode="cqa",
            progress=lambda p, m: app_state.update_status("cqa", 0.02 + p * 0.45, m))

        app_state.update_status("cqa", 0.50, "Computing point-to-plane deviation…")
        dev_results = compute_all_deviations(
            seg_map,
            app_state.elements_data,
            seg_stats=seg_stats,
            progress=lambda p, m: app_state.update_status("cqa", 0.50 + p * 0.20, m))

        app_state.update_status("cqa", 0.72, "Applying jet colormap…")
        colored_pcd, max_dev = apply_jet_colormap(dev_results, app_state.registered_pcd)
        app_state.cqa_colored_pcd    = colored_pcd
        app_state.cqa_max_dev        = max_dev
        app_state.cqa_element_colors = _elem_colors(dev_results)
        _grey_cqa = [0.55, 0.55, 0.55]
        for ed in app_state.elements_data:
            gid = ed.get("guid", "")
            if gid and gid not in app_state.cqa_element_colors:
                app_state.cqa_element_colors[gid] = _grey_cqa

        app_state.update_status("cqa", 0.80, "Building construction knowledge graph…")
        g, csv_str, ctx = build_construction_kg(
            dev_results, app_state.ifc_file, app_state.ifc_project_name,
            elements_data=app_state.elements_data)

        for guid, stats in dev_results.items():
            if "severity" not in stats:
                stats["severity"] = "Compliant"

        app_state.cqa_results        = dev_results
        app_state._cqa_kg            = g
        app_state._cqa_csv_str       = csv_str
        app_state._cqa_deviation_csv = _build_deviation_csv(
            dev_results, context=ctx, kind="cqa",
            elements_data=app_state.elements_data)
        app_state._cqa_context = ctx
        app_state._cqa_ttl     = g.serialize(format="turtle")

        n_total     = len(dev_results)
        n_compliant = sum(1 for v in dev_results.values() if v.get("severity") == "Compliant")
        app_state._last_completed = "cqa"
        app_state.set_done(
            f"CQA complete — {n_compliant}/{n_total} elements compliant | context: {ctx}")
    except Exception as exc:
        log.exception("CQA failed")
        app_state.set_error(str(exc))


# ── MQA ────────────────────────────────────────────────────────────────────────

@app.post("/api/mqa")
async def start_mqa():
    if app_state.is_busy():
        raise HTTPException(409, "Another job is running.")
    if app_state.registered_pcd is None:
        if app_state.scan_pcd is None:
            raise HTTPException(400, "Upload files first.")
        app_state.registered_pcd       = copy.deepcopy(app_state.scan_pcd)
        app_state.registration_T       = np.eye(4, dtype=np.float64)
        app_state.registration_fitness = 1.0
        app_state.registration_rmse    = 0.0
        app_state.registration_method  = "None (pre-aligned)"

    app_state.update_status("mqa", 0.01, "MQA queued…")
    asyncio.get_running_loop().run_in_executor(_executor, _mqa_sync)
    return {"status": "started"}


def _mqa_sync():
    from backend.services.segmentation_service import segment_all_elements
    from backend.services.deviation_service    import (compute_all_deviations,
                                                        mqa_element_colors)
    from backend.services.model_kg             import (audit_all_elements,
                                                        build_model_kg)
    try:
        app_state.update_status("mqa", 0.02, "Segmenting elements for deviation…")

        seg_map, seg_stats = segment_all_elements(
            app_state.registered_pcd,
            app_state.elements_data,
            mode="mqa",
            progress=lambda p, m: app_state.update_status("mqa", 0.02 + p * 0.35, m))

        app_state.update_status("mqa", 0.40, "Computing element deviations…")
        dev_results = compute_all_deviations(
            seg_map,
            app_state.elements_data,
            seg_stats=seg_stats,
            progress=lambda p, m: app_state.update_status("mqa", 0.40 + p * 0.15, m))

        app_state.update_status("mqa", 0.57, "Mapping element colours…")
        elem_colors = mqa_element_colors(dev_results)
        _grey = [0.55, 0.55, 0.55]
        for ed in app_state.elements_data:
            gid = ed.get("guid", "")
            if gid and gid not in elem_colors:
                elem_colors[gid] = _grey
        app_state.mqa_element_colors = elem_colors
        app_state.mqa_colored_pcd    = None

        app_state.update_status("mqa", 0.62, "Auditing BIM model quality…")
        mqa_qual = audit_all_elements(app_state.elements_data, app_state.ifc_file)
        app_state.mqa_results = mqa_qual

        _mqa_ctx = getattr(app_state, "_cqa_context", None)
        if not _mqa_ctx:
            from backend.services.construction_kg import detect_context
            _mqa_ctx = detect_context(app_state.ifc_file) if app_state.ifc_file else "CommercialBuilding"

        _ensure_cqa_fields(dev_results, context=_mqa_ctx, kind="mqa")
        from backend.services.mqa_assessment import (
            build_mqa_deviation_csv as _build_mqa_dev_csv,
            enrich_stats_with_quality as _enrich_quality,
        )
        _enrich_quality(dev_results)

        app_state.update_status("mqa", 0.78, "Building model quality knowledge graph…")
        g, kg_csv = build_model_kg(
            mqa_qual, app_state.ifc_file, app_state.ifc_project_name,
            dev_results=dev_results,
            elements_data=app_state.elements_data,
        )

        app_state._mqa_kg          = g
        app_state._mqa_kg_csv_str  = kg_csv
        app_state._mqa_summary_ttl = g.serialize(format="turtle")
        app_state._mqa_dev_results = dev_results

        app_state._mqa_dev_csv = _build_mqa_dev_csv(
            dev_results,
            elements_data=app_state.elements_data,
            mqa_results=app_state.mqa_results,
        )

        app_state.update_status("mqa", 0.90, "Building IFC mesh colour buffers…")
        app_state._mqa_mesh_data = _build_mqa_mesh_buffers(
            app_state.elements_data, dev_results, elem_colors)

        n_elem  = len(mqa_qual)
        n_excel = sum(1 for v in mqa_qual.values() if v["quality"] == "Excellent")
        app_state._last_completed = "mqa"
        app_state.set_done(
            f"MQA complete — {n_excel}/{n_elem} Excellent quality elements")
    except Exception as exc:
        log.exception("MQA failed")
        app_state.set_error(str(exc))


def _build_mqa_mesh_buffers(elements_data: list, dev_results: dict,
                             elem_colors: dict) -> Dict:
    MAX_TRIS = 400_000

    all_pos  = []
    all_cols = []

    if dev_results:
        _maxes = [float(s.get("max_deviation_used", 0.0)) for s in dev_results.values()
                  if s.get("max_deviation_used", 0.0) > 0]
        max_dev = max(_maxes) if _maxes else (max(s["mean"] for s in dev_results.values()) or 1e-6)
    else:
        max_dev = 1e-6

    for ed in elements_data:
        guid  = ed.get("guid", "")
        verts = ed.get("verts")
        faces = ed.get("faces")

        if verts is None or faces is None or len(verts) == 0 or len(faces) == 0:
            continue

        if guid in elem_colors:
            color = np.array(elem_colors[guid], dtype=np.float32)
        else:
            color = np.array([0.55, 0.55, 0.55], dtype=np.float32)

        verts = np.asarray(verts, dtype=np.float32)
        faces = np.asarray(faces, dtype=np.int32)

        try:
            tri_verts = verts[faces].reshape(-1, 3)
        except (IndexError, ValueError):
            continue

        n_rows = len(tri_verts)
        if n_rows == 0:
            continue

        all_pos.append(tri_verts)
        all_cols.append(np.tile(color, (n_rows, 1)))

    if not all_pos:
        return {}

    positions = np.vstack(all_pos).astype(np.float32)
    colors    = np.vstack(all_cols).astype(np.float32)

    n_tris = len(positions) // 3
    if n_tris > MAX_TRIS:
        stride_tris = (n_tris + MAX_TRIS - 1) // MAX_TRIS
        keep        = np.arange(0, n_tris, stride_tris)
        row_idx     = (keep[:, None] * 3 + np.array([0, 1, 2])[None, :]).ravel()
        positions   = positions[row_idx]
        colors      = colors[row_idx]

    return {
        "positions_b64": _b64_float32(positions.flatten()),
        "colors_b64":    _b64_float32(colors.flatten()),
        "count":         len(positions),
        "max_dev":       float(max_dev),
    }


# ── Viewer data ────────────────────────────────────────────────────────────────

@app.get("/api/viewer-data/{view_type}")
async def viewer_data(view_type: str):
    MAX_PTS  = 200_000
    payload: Dict = {}

    if view_type == "upload":
        if app_state.scan_pcd is None:
            raise HTTPException(400, "No scan loaded.")
        pts, cols = _subsample_pcd(app_state.scan_pcd, MAX_PTS)
        cols[:] = [0.3, 0.6, 1.0]

        scan_centroid = pts.mean(axis=0)
        payload["scan"] = {"positions_b64": _b64_float32((pts - scan_centroid).flatten()),
                            "colors_b64":   _b64_float32(cols.flatten()),
                            "count": len(pts)}

        if app_state.bim_pcd is not None:
            bpts, _ = _subsample_pcd(app_state.bim_pcd, MAX_PTS // 2)
            bcols   = np.full_like(bpts, [1.0, 0.55, 0.1])

            bim_centroid   = bpts.mean(axis=0)
            coord_distance = float(np.linalg.norm(scan_centroid - bim_centroid))
            scan_ext_max   = float(np.ptp(pts,  axis=0).max())
            bim_ext_max    = float(np.ptp(bpts, axis=0).max())

            if coord_distance <= 3.0 * max(scan_ext_max, bim_ext_max):
                bpts_vis = bpts - scan_centroid
            else:
                scan_extent = float(np.ptp(pts,  axis=0)[0])
                bim_extent  = float(np.ptp(bpts, axis=0)[0])
                side_gap    = (scan_extent + bim_extent) * 0.55 + 2.0
                bpts_vis    = (bpts - bim_centroid) + np.array([side_gap, 0.0, 0.0])

            payload["bim"] = {"positions_b64": _b64_float32(bpts_vis.flatten()),
                               "colors_b64":   _b64_float32(bcols.flatten()),
                               "count": len(bpts)}

    elif view_type == "registration":
        if app_state.registered_pcd is None:
            raise HTTPException(400, "Registration not done.")
        pts, _   = _subsample_pcd(app_state.registered_pcd, MAX_PTS)
        cols_s   = np.full_like(pts, [0.2, 0.45, 0.9])
        payload["scan"] = {"positions_b64": _b64_float32(pts.flatten()),
                            "colors_b64":   _b64_float32(cols_s.flatten()),
                            "count": len(pts)}
        if app_state.bim_pcd is not None:
            bpts, _ = _subsample_pcd(app_state.bim_pcd, MAX_PTS // 2)
            bcols   = np.full_like(bpts, [1.0, 0.55, 0.1])
            payload["bim"] = {"positions_b64": _b64_float32(bpts.flatten()),
                               "colors_b64":   _b64_float32(bcols.flatten()),
                               "count": len(bpts)}
        payload["meta"] = {
            "fitness": round(app_state.registration_fitness, 4),
            "rmse_mm": round(app_state.registration_rmse * 1000, 2),
            "method":  app_state.registration_method,
        }

    elif view_type == "cqa":
        if app_state.cqa_colored_pcd is None:
            raise HTTPException(400, "CQA not done.")
        pts, cols = _subsample_pcd(app_state.cqa_colored_pcd, MAX_PTS)
        payload["scan"] = {"positions_b64": _b64_float32(pts.flatten()),
                            "colors_b64":   _b64_float32(cols.flatten()),
                            "count": len(pts)}
        if app_state.bim_pcd is not None:
            bpts, _ = _subsample_pcd(app_state.bim_pcd, 80_000)
            bcols   = np.full_like(bpts, [0.9, 0.9, 0.9])
            payload["bim"] = {"positions_b64": _b64_float32(bpts.flatten()),
                               "colors_b64":   _b64_float32(bcols.flatten()),
                               "count": len(bpts)}
        payload["meta"] = {
            "max_dev_mm": round(app_state.cqa_max_dev * 1000, 2),
            "context":    getattr(app_state, "_cqa_context", "Unknown"),
        }

    elif view_type == "mqa":
        if not app_state.mqa_results and app_state._last_completed != "mqa":
            raise HTTPException(400, "MQA not done.")

        if app_state.mqa_colored_pcd is not None:
            pts, cols = _subsample_pcd(app_state.mqa_colored_pcd, MAX_PTS)
        elif app_state.registered_pcd is not None:
            pts, _ = _subsample_pcd(app_state.registered_pcd, MAX_PTS)
            cols = np.full_like(pts, 0.35)
        elif app_state.scan_pcd is not None:
            pts, _ = _subsample_pcd(app_state.scan_pcd, MAX_PTS)
            cols = np.full_like(pts, 0.35)
        else:
            raise HTTPException(400, "No scan available.")

        payload["scan"] = {"positions_b64": _b64_float32(pts.flatten()),
                            "colors_b64":   _b64_float32(cols.flatten()),
                            "count": len(pts)}

        mesh_data = getattr(app_state, "_mqa_mesh_data", None)
        if mesh_data:
            payload["bim"] = {
                "positions_b64": mesh_data["positions_b64"],
                "colors_b64":    mesh_data["colors_b64"],
                "count":         mesh_data["count"],
                "is_mesh":       True,
            }
        else:
            if app_state.bim_pcd is not None:
                bpts, _ = _subsample_pcd(app_state.bim_pcd, MAX_PTS // 2)
                bcols   = np.full_like(bpts, [0.7, 0.7, 0.7])
                payload["bim"] = {"positions_b64": _b64_float32(bpts.flatten()),
                                   "colors_b64":   _b64_float32(bcols.flatten()),
                                   "count": len(bpts)}

        q_counts = {}
        for v in app_state.mqa_results.values():
            q_counts[v["quality"]] = q_counts.get(v["quality"], 0) + 1
        max_dev = 0.0
        if mesh_data:
            max_dev = mesh_data.get("max_dev", 0.0)
        payload["meta"] = {
            "quality_distribution": q_counts,
            "max_dev_mm": round(max_dev * 1000, 2),
        }

    else:
        raise HTTPException(404, f"Unknown view type: {view_type}")

    return JSONResponse(payload)


# ── CQA / MQA result summaries ─────────────────────────────────────────────────

@app.get("/api/cqa/results")
async def cqa_results():
    if not app_state.cqa_results:
        raise HTTPException(400, "CQA not done.")
    rows = []
    for guid, s in app_state.cqa_results.items():
        rows.append({
            "guid":       guid,
            "name":       s.get("name", ""),
            "ifc_type":   s.get("ifc_type", ""),
            "storey":     s.get("storey", ""),
            "mean_mm":    round(s["mean"] * 1000, 2),
            "max_mm":     round(s["max"]  * 1000, 2),
            "rmse_mm":    round(s["rmse"] * 1000, 2),
            "num_points": s["num_points"],
            "severity":   s.get("severity", ""),
        })
    rows.sort(key=lambda r: r["mean_mm"], reverse=True)
    return {"elements": rows, "context": getattr(app_state, "_cqa_context", "")}


@app.get("/api/mqa/results")
async def mqa_results():
    if not app_state.mqa_results:
        raise HTTPException(400, "MQA not done.")
    dev_results = getattr(app_state, "_mqa_dev_results", {}) or {}
    rows = []
    for g, v in app_state.mqa_results.items():
        issues_raw = v.get("issues", [])
        if issues_raw and isinstance(issues_raw[0], (list, tuple)):
            issues_ser = [{"code": c, "severity": s} for c, s in issues_raw]
        else:
            issues_ser = [{"code": str(i), "severity": ""} for i in issues_raw]
        dev = dev_results.get(g, {})
        if dev:
            dev_status = _severity_to_status(dev.get("severity", ""), dev)
        else:
            dev_status = "Need Validation (No Scan Data)"
        rows.append({
            "guid":             g,
            "name":             v["name"],
            "ifc_type":         v["ifc_type"],
            "storey":           v["storey"],
            "quality":          v["quality"],
            "num_issues":       v["num_issues"],
            "issues":           issues_ser,
            "mean_mm":          round(float(dev.get("mean", 0.0) or 0.0) * 1000, 2) if dev else None,
            "max_mm":           round(float(dev.get("max",  0.0) or 0.0) * 1000, 2) if dev else None,
            "rmse_mm":          round(float(dev.get("rmse", 0.0) or 0.0) * 1000, 2) if dev else None,
            "lod_level":        dev.get("lod_label", ""),
            "tolerance_mm":     dev.get("tolerance_mm", ""),
            "quality_score":    dev.get("quality_score", ""),
            "grade":            dev.get("grade", ""),
            "pass_fail_status": dev.get("pass_fail_status", ""),
            "severity":         dev.get("severity", ""),
            "deviation_status": dev_status,
        })
    rows.sort(key=lambda r: r["num_issues"], reverse=True)
    q_counts = {}
    for v in app_state.mqa_results.values():
        q_counts[v["quality"]] = q_counts.get(v["quality"], 0) + 1
    return {"elements": rows, "quality_distribution": q_counts}


# ── Export ─────────────────────────────────────────────────────────────────────

@app.get("/api/export")
async def export_results(kind: str = ""):
    if not app_state.cqa_results and not app_state.mqa_results:
        raise HTTPException(400, "Run CQA and/or MQA first.")

    kind = (kind or "").lower().strip()
    if kind not in ("cqa", "mqa"):
        kind = app_state._last_completed or \
               ("mqa" if app_state.mqa_results else "cqa")
    if kind == "cqa" and not app_state.cqa_results:
        raise HTTPException(400, "CQA results not available — run CQA first.")
    if kind == "mqa" and not app_state.mqa_results:
        raise HTTPException(400, "MQA results not available — run MQA first.")

    from backend.services.export_service import create_export_zip

    cqa_csv       = getattr(app_state, "_cqa_csv_str", "")
    deviation_csv = getattr(app_state, "_cqa_deviation_csv", "")
    mqa_kg_csv    = getattr(app_state, "_mqa_kg_csv_str", "")
    mqa_dev_csv   = getattr(app_state, "_mqa_dev_csv", "")
    cqa_ttl       = getattr(app_state, "_cqa_ttl", "")
    mqa_ttl       = getattr(app_state, "_mqa_summary_ttl", "")

    if kind == "mqa":
        dev_res = getattr(app_state, "_mqa_dev_results", {}) or app_state.cqa_results
    else:
        dev_res = app_state.cqa_results

    colored_pcd = app_state.cqa_colored_pcd if kind == "cqa" else app_state.registered_pcd

    dev_res_export = dict(dev_res)
    for _ed in (app_state.elements_data or []):
        _gid = _ed.get("guid", "")
        if not _gid or _gid in dev_res_export:
            continue
        _v = _ed.get("verts")
        _no_geom = _v is None or (hasattr(_v, "__len__") and len(_v) == 0)
        dev_res_export[_gid] = {
            "deviation_status": "Need Validation (No Geometry)" if _no_geom
                                 else "Need Validation (Not Segmented)",
            "name":        _ed.get("name", ""),
            "ifc_type":    _ed.get("ifc_type", ""),
            "storey":      _ed.get("storey", "Unknown"),
            "family_name": _ed.get("name", ""),
            "num_points": 0, "num_candidates": 0,
            "num_after_normal_filter": 0, "num_segmented": 0,
        }

    zip_path = create_export_zip(
        output_dir        = str(OUTPUT_DIR),
        project_name      = app_state.ifc_project_name,
        kind              = kind,
        ifc_path          = app_state.ifc_path,
        deviation_results = dev_res_export,
        mqa_results       = app_state.mqa_results if kind == "mqa" else {},
        cqa_csv           = cqa_csv,
        deviation_csv     = deviation_csv,
        mqa_kg_csv        = mqa_kg_csv,
        mqa_dev_csv       = mqa_dev_csv,
        construction_ttl  = cqa_ttl,
        model_quality_ttl = mqa_ttl,
        colored_pcd       = colored_pcd,
        element_colors    = app_state.mqa_element_colors if kind == "mqa"
                            else getattr(app_state, "cqa_element_colors", None),
    )
    app_state.export_zip_path = zip_path

    return FileResponse(
        zip_path,
        media_type = "application/zip",
        filename   = os.path.basename(zip_path),
    )


@app.get("/api/upload-info")
async def upload_info():
    if app_state.scan_pcd is None:
        raise HTTPException(400, "No data loaded.")
    n_pts  = len(np.asarray(app_state.scan_pcd.points))
    n_elem = len(app_state.elements_data)
    return {
        "scan_count":    n_pts,
        "element_count": n_elem,
        "project_name":  app_state.ifc_project_name,
    }
