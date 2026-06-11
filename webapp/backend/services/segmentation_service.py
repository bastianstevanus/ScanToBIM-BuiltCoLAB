from __future__ import annotations

import logging
from typing import Callable, Dict, Optional, Tuple

import numpy as np
import open3d as o3d
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import connected_components
from scipy.spatial import cKDTree

log = logging.getLogger(__name__)


# ── Mode-specific parameters ──────────────────────────────────────────────────

_CQA_PARAMS: Dict = dict(
    seed_radius               = 0.55,
    knn_k                     = 20,
    edge_max_dist             = 0.20,
    normal_angle_threshold    = 0.50,
    bim_plane_tight_threshold = 0.15,
    ransac_distance_threshold = 0.05,
    ransac_num_iterations     = 1000,
    ransac_min_inlier_ratio   = 0.10,
    max_deviation             = 0.50,
    min_points                = 50,
)

_MQA_PARAMS: Dict = dict(
    seed_radius               = 0.40,
    knn_k                     = 20,
    edge_max_dist             = 0.15,
    normal_angle_threshold    = 0.70,
    bim_plane_tight_threshold = 0.15,
    ransac_distance_threshold = 0.05,
    ransac_num_iterations     = 1000,
    ransac_min_inlier_ratio   = 0.10,
    max_deviation             = 0.30,
    min_points                = 25,
)


def _get_params(mode: str) -> Dict:
    return _MQA_PARAMS if mode.lower() == "mqa" else _CQA_PARAMS


# ── BIM plane detection ───────────────────────────────────────────────────────

def _get_bim_plane(
    target_pcd: o3d.geometry.PointCloud,
    scan_centroid: Optional[np.ndarray] = None,
    distance_threshold: float = 0.03,
    min_inlier_ratio: float = 0.50,
    num_iterations: int = 200,
) -> Tuple[Optional[np.ndarray], Optional[float]]:

    pts = np.asarray(target_pcd.points)
    n = len(pts)
    if n < 3:
        return None, None

    # Use only BIM samples facing the scanner to avoid back-face corruption.
    if scan_centroid is not None and target_pcd.has_normals():
        norms    = np.asarray(target_pcd.normals)
        to_scan  = scan_centroid[np.newaxis, :] - pts
        denom    = np.linalg.norm(to_scan, axis=1, keepdims=True) + 1e-8
        cos_vals = np.einsum("ij,ij->i", norms, to_scan / denom)
        front_idx = np.where(cos_vals > 0)[0]
        if len(front_idx) >= 3:
            fit_pcd = target_pcd.select_by_index(front_idx.tolist())
            n_fit   = len(front_idx)
        else:
            fit_pcd, n_fit = target_pcd, n
    else:
        fit_pcd, n_fit = target_pcd, n

    try:
        plane_model, inliers = fit_pcd.segment_plane(
            distance_threshold=distance_threshold,
            ransac_n=3,
            num_iterations=num_iterations,
        )
        if len(inliers) >= int(min_inlier_ratio * n_fit):
            return np.array(plane_model[:3], dtype=np.float64), float(plane_model[3])
    except Exception:
        pass
    return None, None


# ── Deviation helpers ─────────────────────────────────────────────────────────

def _deviation_to_plane(
    pts_arr: np.ndarray,
    bim_normal: np.ndarray,
    bim_d: float,
) -> np.ndarray:
    """Analytic perpendicular distance: |dot(p, n) + d|.  O(n), no KDTree."""
    return np.abs(pts_arr @ bim_normal + bim_d).astype(np.float64)


def _deviation_nbp(
    pts_arr: np.ndarray,
    bim_pts: np.ndarray,
    bim_tree: cKDTree,
) -> np.ndarray:
    """Nearest-BIM-point Euclidean distance (non-planar elements)."""
    _, idxs = bim_tree.query(pts_arr, k=1, workers=-1)
    return np.linalg.norm(pts_arr - bim_pts[idxs], axis=1).astype(np.float64)


# ── RANSAC plane filter (applied to scan candidates) ─────────────────────────

def _ransac_plane_filter(
    candidate_pcd: o3d.geometry.PointCloud,
    distance_threshold: float,
    num_iterations: int,
    min_inlier_ratio: float,
) -> Tuple[o3d.geometry.PointCloud, int]:

    n = len(candidate_pcd.points)
    if n < 3:
        return candidate_pcd, n
    try:
        _, inliers = candidate_pcd.segment_plane(
            distance_threshold=distance_threshold,
            ransac_n=3,
            num_iterations=num_iterations,
        )
    except Exception:
        return candidate_pcd, n
    if len(inliers) < max(3, int(min_inlier_ratio * n)):
        return candidate_pcd, n
    return candidate_pcd.select_by_index(inliers), len(inliers)


# ── Normal consistency filter ─────────────────────────────────────────────────

def _filter_by_normal_consistency(
    candidate_pcd: o3d.geometry.PointCloud,
    target_pcd: o3d.geometry.PointCloud,
    bim_normal: Optional[np.ndarray],
    normal_angle_threshold: float,
) -> o3d.geometry.PointCloud:

    cand_pts   = np.asarray(candidate_pcd.points)
    if len(cand_pts) == 0 or not candidate_pcd.has_normals():
        return candidate_pcd
    cand_norms = np.asarray(candidate_pcd.normals)

    if bim_normal is not None:
        # Planar fast path — no KDTree needed.
        cos_angles = np.abs(cand_norms @ bim_normal)
    else:
        if not target_pcd.has_normals():
            return candidate_pcd
        bim_norms = np.asarray(target_pcd.normals)
        tree      = cKDTree(np.asarray(target_pcd.points))
        _, idxs   = tree.query(cand_pts, k=1, workers=-1)
        cos_angles = np.abs(np.einsum("ij,ij->i", cand_norms, bim_norms[idxs]))

    keep = np.where(cos_angles >= normal_angle_threshold)[0]
    return candidate_pcd.select_by_index(keep.tolist())


# ── Graph segmentation — single largest connected component ──────────────────

def _graph_segment_element(
    candidate_pcd: o3d.geometry.PointCloud,
    knn_k: int,
    edge_max_dist: float,
) -> o3d.geometry.PointCloud:

    pts = np.asarray(candidate_pcd.points)
    n   = len(pts)
    if n < 2:
        return candidate_pcd

    k_actual    = min(knn_k + 1, n)
    tree        = cKDTree(pts)
    dists, idxs = tree.query(pts, k=k_actual, workers=-1)

    rows_i = np.repeat(np.arange(n), k_actual - 1)
    cols_j = idxs[:, 1:].ravel()
    edge_d = dists[:, 1:].ravel()
    valid  = edge_d <= edge_max_dist
    r, c, d = rows_i[valid], cols_j[valid], edge_d[valid]

    if len(r) == 0:
        return candidate_pcd  # no edges — fallback to full candidate set

    adj = coo_matrix(
        (np.concatenate([d, d]),
         (np.concatenate([r, c]), np.concatenate([c, r]))),
        shape=(n, n),
    ).tocsr()
    _, labels = connected_components(adj, directed=False)
    unique_labels, counts = np.unique(labels, return_counts=True)
    largest_label = unique_labels[np.argmax(counts)]
    mask = labels == largest_label

    seg = o3d.geometry.PointCloud()
    seg.points = o3d.utility.Vector3dVector(pts[mask])
    if candidate_pcd.has_normals():
        seg.normals = o3d.utility.Vector3dVector(
            np.asarray(candidate_pcd.normals)[mask])
    return seg


# ── Per-element segmentation ──────────────────────────────────────────────────

def segment_element(
    source_pts: np.ndarray,
    source_normals: np.ndarray,
    element_pcd: o3d.geometry.PointCloud,
    *,
    seed_radius: float,
    knn_k: int,
    edge_max_dist: float,
    normal_angle_threshold: float,
    bim_plane_tight_threshold: float,
    ransac_distance_threshold: float,
    ransac_num_iterations: int,
    ransac_min_inlier_ratio: float,
    max_deviation: float,
    min_points: int,
    _stat_bucket: Optional[Dict] = None,
) -> Optional[Tuple[o3d.geometry.PointCloud, np.ndarray]]:

    def _ws(**kw) -> None:
        if _stat_bucket is not None:
            _stat_bucket.update(kw)

    bim_pts_arr = np.asarray(element_pcd.points)
    if len(bim_pts_arr) == 0 or len(source_pts) == 0:
        _ws(num_candidates=0, fail_reason="no_bim_points")
        return None

    # ── Step 1: Reverse seed query (vectorized cKDTree) ──────────────────
    bim_tree  = cKDTree(bim_pts_arr)
    dists, _  = bim_tree.query(source_pts, k=1, workers=-1)
    cand_idx  = np.where(dists <= seed_radius)[0]
    n_cand    = int(len(cand_idx))
    if n_cand == 0:
        _ws(num_candidates=0, fail_reason="no_nearby_points")
        return None

    _ws(num_candidates=n_cand)
    scan_centroid = source_pts[cand_idx].mean(axis=0)

    candidate_pcd         = o3d.geometry.PointCloud()
    candidate_pcd.points  = o3d.utility.Vector3dVector(source_pts[cand_idx])
    candidate_pcd.normals = o3d.utility.Vector3dVector(source_normals[cand_idx])

    # ── Step 2: BIM plane detection ───────────────────────────────────────
    bim_normal, bim_d = _get_bim_plane(element_pcd, scan_centroid=scan_centroid)
    if bim_normal is not None:
        # Orient so that scan_centroid lies on the negative side of the plane.
        if float(scan_centroid @ bim_normal + bim_d) > 0:
            bim_normal = -bim_normal
            bim_d      = -bim_d

    bim_tree_local: Optional[cKDTree] = None  # built lazily for non-planar path

    # ── Step 3: RANSAC filter (planar) / NBP filter (non-planar) ─────────
    if bim_normal is not None:
        candidate_pcd, num_after_ransac = _ransac_plane_filter(
            candidate_pcd,
            distance_threshold=ransac_distance_threshold,
            num_iterations=ransac_num_iterations,
            min_inlier_ratio=ransac_min_inlier_ratio,
        )
    else:
        num_after_ransac = n_cand
        bim_tree_local   = cKDTree(bim_pts_arr)
        nbp_dists, _     = bim_tree_local.query(
            np.asarray(candidate_pcd.points), k=1, workers=-1)
        keep = np.where(nbp_dists <= bim_plane_tight_threshold)[0]
        if len(keep) == 0:
            _ws(num_candidates=n_cand, fail_reason="nbp_filter_empty")
            return None
        candidate_pcd = candidate_pcd.select_by_index(keep.tolist())

    if len(candidate_pcd.points) == 0:
        _ws(num_candidates=n_cand, fail_reason="ransac_empty")
        return None

    # ── Step 4: Normal consistency filter ────────────────────────────────
    candidate_pcd  = _filter_by_normal_consistency(
        candidate_pcd, element_pcd, bim_normal, normal_angle_threshold)
    n_after_normal = int(len(candidate_pcd.points))
    _ws(num_after_normal_filter=n_after_normal)
    if n_after_normal == 0:
        _ws(fail_reason="normal_filter_empty")
        return None

    # ── Step 5: BIM-plane proximity pre-filter (planar only) ─────────────
    if bim_normal is not None:
        pts_arr    = np.asarray(candidate_pcd.points)
        plane_dist = np.abs(pts_arr @ bim_normal + bim_d)
        keep       = np.where(plane_dist <= max_deviation)[0]
        if len(keep) > 0:
            candidate_pcd = candidate_pcd.select_by_index(keep.tolist())
        if len(candidate_pcd.points) == 0:
            _ws(fail_reason="proximity_filter_empty")
            return None

    # ── Step 6: Graph segmentation (largest connected component only) ─────
    segmented_pcd = _graph_segment_element(
        candidate_pcd, knn_k=knn_k, edge_max_dist=edge_max_dist)
    n_segmented = int(len(segmented_pcd.points))
    _ws(num_segmented=n_segmented, num_after_ransac=num_after_ransac)
    if n_segmented == 0:
        _ws(fail_reason="graph_empty")
        return None

    # ── Step 7: Deviation computation ────────────────────────────────────
    seg_pts_arr = np.asarray(segmented_pcd.points)
    if bim_normal is not None:
        # Planar: analytic signed distance — deterministic, O(n), no KDTree.
        distances = _deviation_to_plane(seg_pts_arr, bim_normal, bim_d)
    else:
        # Non-planar: nearest-BIM-point Euclidean distance.
        if bim_tree_local is None:
            bim_tree_local = cKDTree(bim_pts_arr)
        distances = _deviation_nbp(seg_pts_arr, bim_pts_arr, bim_tree_local)

    # ── Step 8: Max-deviation filter ──────────────────────────────────────
    valid_mask = distances <= max_deviation
    if not np.any(valid_mask):
        _ws(fail_reason="no_valid_deviation")
        return None

    filtered_pts = seg_pts_arr[valid_mask]
    deviation    = distances[valid_mask]

    result_pcd        = o3d.geometry.PointCloud()
    result_pcd.points = o3d.utility.Vector3dVector(filtered_pts)

    fail_reason = "low_coverage" if int(len(deviation)) < min_points else ""
    _ws(fail_reason=fail_reason)

    return result_pcd, deviation


# ── Batch driver ──────────────────────────────────────────────────────────────

def segment_all_elements(
    source_pcd: o3d.geometry.PointCloud,
    elements_data: list,
    mode: str = "cqa",
    progress: Optional[Callable[[float, str], None]] = None,
) -> Tuple[Dict[str, o3d.geometry.PointCloud], Dict[str, Dict]]:

    params = _get_params(mode)

    # Pre-compute source numpy arrays once — shared across all per-element
    # iterations, matching the notebook pattern (source_cropped_pts / normals).
    if not source_pcd.has_normals():
        source_pcd.estimate_normals(
            o3d.geometry.KDTreeSearchParamHybrid(radius=0.20, max_nn=30))
    src_pts     = np.asarray(source_pcd.points)
    src_normals = np.asarray(source_pcd.normals)

    # Ensure BIM element PCDs have normals for the normal consistency filter.
    for ed in elements_data:
        pcd = ed["pcd"]
        if pcd.has_points() and not pcd.has_normals():
            pcd.estimate_normals(
                o3d.geometry.KDTreeSearchParamHybrid(radius=0.20, max_nn=20))
            try:
                pcd.orient_normals_consistent_tangent_plane(k=10)
            except Exception:
                pass

    results:   Dict[str, o3d.geometry.PointCloud] = {}
    seg_stats: Dict[str, Dict]                    = {}
    n = len(elements_data)

    for i, ed in enumerate(elements_data):
        if progress:
            progress(i / max(n, 1),
                     f"Segmenting {ed.get('ifc_type', '')} {i+1}/{n}…")
        stat: Dict = {
            "min_points_used":    params["min_points"],
            "max_deviation_used": params["max_deviation"],
        }
        out = segment_element(
            src_pts, src_normals, ed["pcd"],
            _stat_bucket=stat,
            **params,
        )
        if out is not None:
            seg_pcd, deviation_arr  = out
            results[ed["guid"]]     = seg_pcd
            stat["deviation_arr"]   = deviation_arr
        else:
            stat.setdefault("fail_reason", "no_result")
        seg_stats[ed["guid"]] = stat

    log.info(
        "Segmentation (%s): %d / %d elements segmented",
        mode.upper(), len(results), n,
    )
    return results, seg_stats
