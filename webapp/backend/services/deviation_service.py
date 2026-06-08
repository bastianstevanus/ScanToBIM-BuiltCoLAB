"""
Deviation measurement and jet-colourmap assignment.
Implements point-to-plane deviation via KD-tree nearest-neighbour lookup,
identical to the reference notebooks.
"""
from __future__ import annotations

import logging
from typing import Dict, List, Optional, Tuple

import matplotlib.pyplot as plt
import numpy as np
import open3d as o3d

log = logging.getLogger(__name__)


# ── CustomName: storey-based per-category naming ─────────────────────────────
# Format: "{Category} {LevelNumber}{LetterSuffix}"  →  e.g. "Wall 2A", "Column 3B"
# Letters increment per (level, category) bucket in iteration order.

def _number_to_letters(n: int) -> str:
    letters = ""
    while n > 0:
        n -= 1
        letters = chr(65 + (n % 26)) + letters
        n //= 26
    return letters


def _climb_to_storey(spatial):
    visited, current = set(), spatial
    while current is not None:
        try:
            cid = current.id()
        except Exception:
            return None
        if cid in visited:
            return None
        visited.add(cid)
        if current.is_a("IfcBuildingStorey"):
            return current
        rels = getattr(current, "Decomposes", None)
        current = rels[0].RelatingObject if rels else None
    return None


def _estimate_element_z(element) -> Optional[float]:
    try:
        import ifcopenshell.geom as _geom
        settings = _geom.settings()
        try:
            settings.set(settings.USE_WORLD_COORDS, True)
        except Exception:
            pass
        shape = _geom.create_shape(settings, element)
        verts = np.asarray(shape.geometry.verts).reshape(-1, 3)
        return float(verts[:, 2].mean()) if verts.size else None
    except Exception:
        return None


def _get_storey_info(ifc_file):
    """Return (level_by_gid, elev_levels) for storey resolution."""
    if ifc_file is None:
        return {}, []
    try:
        storeys = sorted(
            ifc_file.by_type("IfcBuildingStorey"),
            key=lambda s: (
                float(getattr(s, "Elevation", float("inf")) or float("inf")),
                str(getattr(s, "Name", "") or ""),
                str(getattr(s, "GlobalId", "") or ""),
            ),
        )
    except Exception:
        return {}, []
    level_by_gid = {s.GlobalId: i + 1 for i, s in enumerate(storeys)}
    elev_levels = []
    for i, s in enumerate(storeys, start=1):
        elev = getattr(s, "Elevation", None)
        if elev is None:
            continue
        try:
            elev_levels.append((i, float(elev)))
        except (TypeError, ValueError):
            pass
    return level_by_gid, elev_levels


def _resolve_storey_level(element, level_by_gid, elev_levels) -> int:
    relations = list(getattr(element, "ContainedInStructure", []) or [])
    relations += list(getattr(element, "ReferencedInStructures", []) or [])
    for rel in relations:
        spatial = getattr(rel, "RelatingStructure", None)
        if spatial is None:
            continue
        storey = _climb_to_storey(spatial)
        if storey is None:
            continue
        gid = getattr(storey, "GlobalId", None)
        if gid in level_by_gid:
            return level_by_gid[gid]
    if elev_levels:
        z = _estimate_element_z(element)
        if z is not None:
            return min(elev_levels, key=lambda lv: abs(z - lv[1]))[0]
    return 1


def assign_custom_names(elements_data: list, ifc_file) -> Dict[str, str]:
    """
    Build {guid: "Category Level+Letter"} names (e.g. "Wall 2A").
    Iterates elements in the order given and assigns letters per (level, category).
    Mutates each element_data dict's "name" field and returns the map.
    """
    level_by_gid, elev_levels = _get_storey_info(ifc_file)
    counters: Dict[tuple, int] = {}
    out: Dict[str, str] = {}
    for ed in elements_data:
        element = ed.get("element")
        guid    = ed.get("guid", "")
        ifc_t   = ed.get("ifc_type", "") or (element.is_a() if element is not None else "IfcElement")
        category = ifc_t.replace("Ifc", "") or "Element"

        if element is not None:
            level = _resolve_storey_level(element, level_by_gid, elev_levels)
        else:
            level = 1

        key = (level, category)
        counters[key] = counters.get(key, 0) + 1
        name = f"{category} {level}{_number_to_letters(counters[key])}"
        out[guid] = name
        ed["name"] = name  # propagate so downstream stages see it
    return out


def _point_to_plane_dev(pt: np.ndarray, bim_pts: np.ndarray,
                         bim_normals: np.ndarray, tree: o3d.geometry.KDTreeFlann) -> float:
    """Signed distance from pt to the nearest BIM facet's plane."""
    k, idx, _ = tree.search_knn_vector_3d(pt, 1)
    if k == 0:
        return 0.0
    closest = bim_pts[idx[0]]
    normal  = bim_normals[idx[0]]
    nrm = np.linalg.norm(normal)
    if nrm < 1e-9:
        return float(np.linalg.norm(pt - closest))
    return abs(float(np.dot(pt - closest, normal / nrm)))


def compute_deviation_for_element(
    seg_pcd: o3d.geometry.PointCloud,
    elem_pcd: o3d.geometry.PointCloud,
) -> Optional[Dict]:
    """
    Compute per-point point-to-plane deviation for one element segment.

    Returns dict with: deviation_arr, mean, max, std, rmse, num_points
    """
    if not seg_pcd.has_points() or not elem_pcd.has_points():
        return None

    if not elem_pcd.has_normals():
        elem_pcd.estimate_normals(
            o3d.geometry.KDTreeSearchParamHybrid(radius=0.20, max_nn=20))

    bim_pts     = np.asarray(elem_pcd.points)
    bim_normals = np.asarray(elem_pcd.normals)
    tree        = o3d.geometry.KDTreeFlann(elem_pcd)

    src_pts = np.asarray(seg_pcd.points)
    devs = np.array([
        _point_to_plane_dev(p, bim_pts, bim_normals, tree)
        for p in src_pts
    ], dtype=np.float32)

    return dict(
        deviation_arr = devs,
        mean          = float(np.mean(devs)),
        max           = float(np.max(devs)),
        std           = float(np.std(devs)),
        rmse          = float(np.sqrt(np.mean(devs ** 2))),
        num_points    = len(devs),
    )


def compute_all_deviations(
    segment_map: Dict[str, o3d.geometry.PointCloud],
    elements_data: list,
    seg_stats: Optional[Dict] = None,
    progress: Optional[callable] = None,
) -> Dict[str, Dict]:
    """
    For each element that has a segment, build deviation stats.

    When segmentation_service has already computed the deviation (the
    notebook algorithm stores ``deviation_arr`` in seg_stats), that
    pre-computed array is used directly — no re-computation, no duplicate
    max_deviation filtering.  For backward-compatibility the legacy
    point-to-plane path is kept as a fallback.

    Returns {guid: stats_dict}  where stats_dict also contains 'volume'.
    """
    elem_by_guid = {ed["guid"]: ed for ed in elements_data}
    results: Dict[str, Dict] = {}
    n = len(segment_map)

    for i, (guid, seg_pcd) in enumerate(segment_map.items()):
        if progress:
            progress(i / max(n, 1), f"Computing deviation {i+1}/{n}…")
        ed = elem_by_guid.get(guid)
        if ed is None:
            continue

        stats: Dict = {}

        # ── Retrieve pre-computed deviation from segmentation (notebook path) ──
        pre_devs: Optional[np.ndarray] = None
        if seg_stats and guid in seg_stats:
            _pre = seg_stats[guid].get("deviation_arr")
            if _pre is not None and len(_pre) > 0:
                pre_devs = np.asarray(_pre, dtype=np.float32)

        if pre_devs is not None:
            # New notebook path — segmentation already handled phantom detection
            # (segment_element returns None for elements with no real scan coverage,
            # so they never reach compute_all_deviations). Use deviation directly.
            filtered_devs = pre_devs
            filtered_seg  = seg_pcd  # already max-deviation-filtered by segmentation
        else:
            # ── Fallback: compute deviation here (legacy / backward-compat) ──
            _raw = compute_deviation_for_element(seg_pcd, ed["pcd"])
            if _raw is None:
                continue
            # Apply max_deviation filter (400 mm default).
            max_dev = 0.40
            devs    = _raw["deviation_arr"]
            mask    = devs <= max_dev
            if not np.any(mask):
                closest = int(np.argmin(devs))
                mask    = np.zeros_like(devs, dtype=bool)
                mask[closest] = True
            filtered_seg  = seg_pcd.select_by_index(np.where(mask)[0].tolist())
            filtered_devs = devs[mask]

            if len(filtered_devs) == 0:
                continue

            # ── Phantom guard (legacy path only) ─────────────────────────
            # On the legacy path the segmentation may incorrectly assign
            # neighbour scan points to phantom elements. Exclude the element
            # if no point is within 100 mm of the BIM surface.
            # NOTE: this check is intentionally ABSENT on the new path
            # (pre_devs is not None) because graph-based segmentation already
            # excludes phantom elements by returning None, and applying a
            # fixed 100 mm gate would wrongly reject real elements with
            # systematic deviations > 100 mm (e.g. walls built 150 mm off).
            _TIGHT_DIST      = 0.10   # 100 mm
            _PHANTOM_MIN_PTS = 25
            n_tight = int(np.sum(filtered_devs <= _TIGHT_DIST))
            if n_tight < _PHANTOM_MIN_PTS:
                log.info(
                    "Excluding phantom element %s (legacy): "
                    "only %d/%d points within %.0f mm",
                    guid, n_tight, len(filtered_devs), _TIGHT_DIST * 1000,
                )
                continue  # phantom — no real scan coverage

        if len(filtered_devs) == 0:
            continue

        # Mode-specific min_points threshold from segmentation (50 CQA, 25 MQA).
        _min_pts = 50
        if seg_stats and guid in seg_stats:
            _min_pts = int(seg_stats[guid].get("min_points_used", _min_pts))

        stats["deviation_arr"] = filtered_devs
        stats["mean"]          = float(np.mean(filtered_devs))
        stats["max"]           = float(np.max(filtered_devs))
        stats["std"]           = float(np.std(filtered_devs))
        stats["rmse"]          = float(np.sqrt(np.mean(filtered_devs ** 2)))
        stats["num_points"]    = len(filtered_devs)

        # Less-than-min-points flag (element still included in results).
        if len(filtered_devs) < _min_pts:
            stats["fail_reason"] = "less_than_min_points"

        stats["pcd"]     = filtered_seg
        stats["ifc_type"] = ed["ifc_type"]
        stats["name"]    = ed["name"]
        stats["storey"]  = ed.get("storey", "Unknown")
        stats["volume"]  = ed.get("volume", 0.0)

        # ElementID and FamilyName from the IFC element.
        element = ed.get("element")
        if element is not None:
            tag = getattr(element, "Tag", None)
            stats["element_id"] = (
                str(tag) if (tag and str(tag).strip() not in ("", "$", "None"))
                else str(ed.get("guid", ""))
            )
            obj_type  = getattr(element, "ObjectType", None)
            name_attr = getattr(element, "Name", None)
            stats["family_name"] = (
                str(obj_type)  if (obj_type  and str(obj_type).strip()  not in ("", "$", "None")) else
                str(name_attr) if (name_attr and str(name_attr).strip() not in ("", "$", "None")) else
                ed.get("ifc_type", "").replace("Ifc", "")
            )
        else:
            stats["element_id"]  = str(ed.get("guid", ""))
            stats["family_name"] = ed.get("name", "")

        # Segmentation phase counts from seg_stats.
        if seg_stats and guid in seg_stats:
            st = seg_stats[guid]
            stats["num_candidates"]          = st.get("num_candidates", 0)
            stats["num_after_normal_filter"] = st.get("num_after_normal_filter", 0)
            stats["num_segmented"]           = st.get("num_segmented", 0)
            # Propagate mode-specific max_deviation so the colormap can use
            # a fixed scale identical to the notebook (0.50 CQA / 0.30 MQA).
            stats["max_deviation_used"]      = float(st.get("max_deviation_used", 0.0))
            # Preserve fail_reason already set above; only override when
            # segmentation recorded a reason and we haven't set one yet.
            if not stats.get("fail_reason"):
                stats["fail_reason"] = st.get("fail_reason", "")
        else:
            stats["num_candidates"]          = 0
            stats["num_after_normal_filter"] = 0
            stats["num_segmented"]           = stats["num_points"]
            if not stats.get("fail_reason"):
                stats["fail_reason"] = ""

        results[guid] = stats

    return results


def apply_jet_colormap(
    deviation_results: Dict[str, Dict],
    source_pcd: o3d.geometry.PointCloud,
    max_dev_visual: Optional[float] = None,
) -> Tuple[o3d.geometry.PointCloud, float]:
    """
    Build a coloured copy of source_pcd using jet colormap on deviation values.
    Returns (coloured_pcd, max_dev_visual).

    The colour scale uses a **fixed** ``color_max = max_deviation`` taken from
    the per-element ``max_deviation_used`` stat (0.50 m CQA / 0.30 m MQA),
    matching the reference notebook's fixed-scale jet legend.
    """
    src_pts = np.asarray(source_pcd.points)
    N       = len(src_pts)
    colors  = np.full((N, 3), 0.6, dtype=np.float32)  # grey for unmatched pts

    if not deviation_results:
        # Nothing was segmented — return grey cloud
        coloured = o3d.geometry.PointCloud()
        coloured.points = source_pcd.points
        coloured.colors = o3d.utility.Vector3dVector(colors.astype(np.float64))
        return coloured, 0.05

    if max_dev_visual is None:
        # Use the segmentation max_deviation as the fixed colour ceiling
        # (same convention as the Reservoir / UC notebooks).
        _maxes = [
            float(v.get("max_deviation_used", 0.0))
            for v in deviation_results.values()
            if v.get("max_deviation_used", 0.0) > 0
        ]
        if _maxes:
            max_dev_visual = max(_maxes)
        else:
            all_devs = np.concatenate(
                [v["deviation_arr"] for v in deviation_results.values()])
            max_dev_visual = (
                float(np.max(all_devs)) if len(all_devs) > 0 else 0.05)
        max_dev_visual = max(max_dev_visual, 1e-6)

    cmap = plt.get_cmap("jet")

    # Build a KD-tree of source_pcd for point → colour assignment
    tree = o3d.geometry.KDTreeFlann(source_pcd)
    for guid, stats in deviation_results.items():
        seg_pts = np.asarray(stats["pcd"].points)
        devs    = stats["deviation_arr"]
        for pt, dev in zip(seg_pts, devs):
            k, idx, _ = tree.search_knn_vector_3d(pt, 1)
            if k > 0:
                norm_val = min(dev / max_dev_visual, 1.0)
                colors[idx[0]] = cmap(norm_val)[:3]

    coloured = o3d.geometry.PointCloud()
    coloured.points = source_pcd.points
    coloured.colors = o3d.utility.Vector3dVector(colors.astype(np.float64))
    return coloured, max_dev_visual


def mqa_element_colors(deviation_results: Dict[str, Dict],
                       max_dev_visual: Optional[float] = None) -> Dict[str, List[float]]:
    """
    For MQA: map each element's MEAN deviation to a jet colour.
    Returns {guid: [R, G, B]}.

    Uses fixed ``color_max = max_deviation_used`` (0.30 m MQA / 0.50 m CQA)
    so colours are comparable across runs — matches the notebook convention.
    """
    means = {g: v["mean"] for g, v in deviation_results.items()}
    if not means:
        return {}

    if max_dev_visual is None:
        _maxes = [
            float(v.get("max_deviation_used", 0.0))
            for v in deviation_results.values()
            if v.get("max_deviation_used", 0.0) > 0
        ]
        max_dev_visual = max(_maxes) if _maxes else max(means.values())
        max_dev_visual = max(max_dev_visual, 1e-6)

    cmap = plt.get_cmap("jet")
    return {
        g: list(cmap(min(m / max_dev_visual, 1.0))[:3])
        for g, m in means.items()
    }
