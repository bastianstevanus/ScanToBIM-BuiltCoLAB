from __future__ import annotations

import copy
import logging
from typing import Callable, Dict, Optional, Tuple

import numpy as np
import open3d as o3d

log = logging.getLogger(__name__)

# ── helpers ────────────────────────────────────────────────────────────────────

def _to_pcd(pts: np.ndarray) -> o3d.geometry.PointCloud:
    pcd = o3d.geometry.PointCloud()
    pcd.points = o3d.utility.Vector3dVector(pts.astype(np.float64))
    return pcd


def _preprocess(pcd: o3d.geometry.PointCloud, voxel_size: float):
    """Voxel-down, estimate normals, compute FPFH — same as notebook."""
    down = pcd.voxel_down_sample(voxel_size)
    down.estimate_normals(
        o3d.geometry.KDTreeSearchParamHybrid(radius=voxel_size * 2.0, max_nn=30))
    fpfh = o3d.pipelines.registration.compute_fpfh_feature(
        down,
        o3d.geometry.KDTreeSearchParamHybrid(radius=voxel_size * 5.0, max_nn=100))
    return down, fpfh


def _evaluate(src: o3d.geometry.PointCloud,
               tgt: o3d.geometry.PointCloud,
               T: np.ndarray,
               dist: float) -> Tuple[float, float, float]:
    """Returns (score, fitness, rmse) — notebook scoring: fitness - 0.35*rmse."""
    ev = o3d.pipelines.registration.evaluate_registration(src, tgt, dist, T)
    score = float(ev.fitness - 0.35 * ev.inlier_rmse)
    return score, float(ev.fitness), float(ev.inlier_rmse)


def _make_z_rotation(deg: float, center: np.ndarray) -> np.ndarray:
    a = np.deg2rad(deg)
    c, s = np.cos(a), np.sin(a)
    R = np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]], dtype=np.float64)
    T = np.eye(4)
    T[:3, :3] = R
    Tc = np.eye(4); Tc[:3, 3] = -center
    Tb = np.eye(4); Tb[:3, 3] =  center
    return Tb @ T @ Tc


# ── main entry ─────────────────────────────────────────────────────────────────

def run_registration(
    scan_pcd: o3d.geometry.PointCloud,
    bim_pcd:  o3d.geometry.PointCloud,
    voxel_size: float = 0.1,
    progress: Optional[Callable[[float, str], None]] = None,
) -> Dict:


    def _prog(p: float, msg: str):
        if progress:
            progress(p, msg)

    _prog(0.02, "Downsampling and computing FPFH features…")

    source      = copy.deepcopy(scan_pcd)
    target      = copy.deepcopy(bim_pcd)

    src_down, src_fpfh = _preprocess(source, voxel_size)
    tgt_down, tgt_fpfh = _preprocess(target, voxel_size)

    # Centroid translation seed (notebook Step 3)
    init_T = np.eye(4)
    init_T[:3, 3] = (np.asarray(tgt_down.get_center())
                     - np.asarray(src_down.get_center()))

    source_seed = copy.deepcopy(src_down)
    source_seed.transform(init_T)

    eval_dist = voxel_size * 3.0
    icp_dist  = voxel_size * 8.0

    candidates = []

    # ── Candidate A: FGR ───────────────────────────────────────────────
    _prog(0.10, "Running Fast Global Registration (FGR)…")
    try:
        fgr_opt = o3d.pipelines.registration.FastGlobalRegistrationOption(
            maximum_correspondence_distance=voxel_size * 3.0,
            iteration_number=64,
            maximum_tuple_count=1000,
        )
        res_fgr = o3d.pipelines.registration.registration_fgr_based_on_feature_matching(
            source_seed, tgt_down, src_fpfh, tgt_fpfh, fgr_opt)
        T_fgr = res_fgr.transformation @ init_T

        # ICP refinement of FGR
        src_fgr = copy.deepcopy(src_down)
        src_fgr.transform(T_fgr)
        res_fgr_icp = o3d.pipelines.registration.registration_icp(
            src_fgr, tgt_down, icp_dist, np.eye(4),
            o3d.pipelines.registration.TransformationEstimationPointToPoint(),
            o3d.pipelines.registration.ICPConvergenceCriteria(max_iteration=300))
        T_fgr_refined = res_fgr_icp.transformation @ T_fgr
        s, f, r = _evaluate(src_down, tgt_down, T_fgr_refined, eval_dist)
        candidates.append(("FGR+ICP", T_fgr_refined, s, f, r))
        log.info("FGR+ICP:  fitness=%.4f  rmse=%.4f", f, r)
    except Exception as exc:
        log.warning("FGR failed: %s", exc)

    # ── Candidate B: ICP from centroid seed ────────────────────────────
    _prog(0.40, "ICP from centroid seed…")
    res_icp = o3d.pipelines.registration.registration_icp(
        source_seed, tgt_down, icp_dist, np.eye(4),
        o3d.pipelines.registration.TransformationEstimationPointToPoint(),
        o3d.pipelines.registration.ICPConvergenceCriteria(max_iteration=300))
    T_icp = res_icp.transformation @ init_T
    s, f, r = _evaluate(src_down, tgt_down, T_icp, eval_dist)
    candidates.append(("ICP_seed", T_icp, s, f, r))
    log.info("ICP_seed: fitness=%.4f  rmse=%.4f", f, r)

    # ── Candidate C-J: Yaw-rotation + ICP (every 45°) ─────────────────
    yaw_center = np.asarray(tgt_down.get_center())
    for yaw_deg in [45, 90, 135, 180, 225, 270, 315]:
        _prog(0.55 + 0.04 * (yaw_deg / 315), f"Yaw {yaw_deg}° + ICP…")
        T_yaw = _make_z_rotation(yaw_deg, yaw_center) @ init_T
        src_yaw = copy.deepcopy(src_down)
        src_yaw.transform(T_yaw)
        res_yaw = o3d.pipelines.registration.registration_icp(
            src_yaw, tgt_down, icp_dist, np.eye(4),
            o3d.pipelines.registration.TransformationEstimationPointToPoint(),
            o3d.pipelines.registration.ICPConvergenceCriteria(max_iteration=200))
        T_y = res_yaw.transformation @ T_yaw
        s, f, r = _evaluate(src_down, tgt_down, T_y, eval_dist)
        candidates.append((f"Yaw{yaw_deg}+ICP", T_y, s, f, r))

    # ── Select best candidate ──────────────────────────────────────────
    best_name, best_T, best_score, best_fit, best_rmse = max(candidates, key=lambda x: x[2])
    _prog(0.90, f"Best: {best_name}  fitness={best_fit:.4f}")

    for name, _, sc, fi, rm in candidates:
        log.info("  %-18s score=%.4f  fitness=%.4f  rmse=%.4f", name, sc, fi, rm)

    # Apply best transform to full-resolution scan
    aligned = copy.deepcopy(scan_pcd)
    aligned.transform(best_T)

    _prog(0.99, "Registration complete")

    return dict(
        aligned_pcd=aligned,
        T=best_T,
        fitness=best_fit,
        rmse=best_rmse,
        method=best_name,
    )
