from __future__ import annotations

import logging
from typing import Callable, Dict, List, Optional, Tuple

import numpy as np
import open3d as o3d

log = logging.getLogger(__name__)

TARGET_TYPES = [
    # Structural / architectural
    "IfcWall", "IfcSlab", "IfcColumn", "IfcBeam",
    "IfcRoof", "IfcDoor", "IfcWindow", "IfcCovering",
    "IfcStair", "IfcRamp", "IfcMember", "IfcPlate",
    "IfcFooting", "IfcPile",
    # MEP / services
    "IfcPipeSegment", "IfcDuctSegment",
    "IfcFlowTerminal", "IfcFlowFitting",
]


def _to_pcd(pts: np.ndarray) -> o3d.geometry.PointCloud:
    pcd = o3d.geometry.PointCloud()
    pcd.points = o3d.utility.Vector3dVector(pts.astype(np.float64))
    return pcd


def load_ifc(
    ifc_path: str,
    n_bim_points: int = 200_000,
    n_element_points: int = 20_000,
    progress: Optional[Callable[[float, str], None]] = None,
    scan_pcd: Optional[o3d.geometry.PointCloud] = None,
) -> Tuple[object, str, List[Dict], o3d.geometry.PointCloud, o3d.geometry.TriangleMesh]:

    import ifcopenshell
    import ifcopenshell.geom
    import ifcopenshell.util.element as ifc_util

    ifc_file = ifcopenshell.open(ifc_path)

    # Project name
    project_name = "Project"
    projects = ifc_file.by_type("IfcProject")
    if projects:
        project_name = getattr(projects[0], "Name", None) or project_name

    settings = ifcopenshell.geom.settings()
    settings.set(settings.USE_WORLD_COORDS, True)

    # Collect elements
    elements = []
    for type_name in TARGET_TYPES:
        elements.extend(ifc_file.by_type(type_name))

    if not elements:
        raise RuntimeError("IFC file contains no recognised structural elements.")

    if progress:
        progress(0.05, f"Tessellating {len(elements)} IFC elements…")

    # Scan centroid for back-face filtering (issue #6: avoid sampling both
    # sides of thin walls/slabs).
    scan_centroid = None
    if scan_pcd is not None and len(scan_pcd.points) > 0:
        scan_centroid = np.asarray(scan_pcd.points).mean(axis=0)

    elements_data: List[Dict] = []
    all_pts: List[np.ndarray] = []

    # Build storey lookup: element GUID → storey name
    storey_map: Dict[str, str] = {}
    for storey in ifc_file.by_type("IfcBuildingStorey"):
        storey_name = getattr(storey, "Name", "") or "Unknown"
        for rel in getattr(storey, "ContainsElements", []):
            for elem in getattr(rel, "RelatedElements", []):
                storey_map[elem.GlobalId] = storey_name

    # Detect duplicate GUIDs for MQA
    all_guids = [getattr(e, "GlobalId", "") for e in elements]
    dup_guids = {g for g in all_guids if all_guids.count(g) > 1}

    def _stub_record(element, reason: str) -> Dict:
        """Empty-geometry record so the element still appears in MQA audit / IFC."""
        guid     = getattr(element, "GlobalId", "") or ""
        name     = getattr(element, "Name", "") or ""
        return dict(
            element     = element,
            guid        = guid,
            name        = name,
            ifc_type    = element.is_a(),
            storey      = storey_map.get(guid, "Unknown"),
            pcd         = o3d.geometry.PointCloud(),
            mesh        = o3d.geometry.TriangleMesh(),
            verts       = None,
            faces       = None,
            volume      = 0.0,
            is_dup_guid = guid in dup_guids,
            geom_skip_reason = reason,
        )

    for i, element in enumerate(elements):
        if progress and i % 20 == 0:
            progress(0.05 + 0.80 * i / len(elements), f"Processing element {i+1}/{len(elements)}…")
        try:
            shape = ifcopenshell.geom.create_shape(settings, element)
            verts = np.asarray(shape.geometry.verts, dtype=np.float64).reshape(-1, 3)
            faces = np.asarray(shape.geometry.faces, dtype=np.int32).reshape(-1, 3)

            if len(verts) == 0 or len(faces) == 0:
                # Keep a stub so MQA audit picks up the element (NO_GEOMETRY)
                # and so it still appears in the annotated IFC export.
                elements_data.append(_stub_record(element, "empty_geometry"))
                continue

            mesh = o3d.geometry.TriangleMesh()
            mesh.vertices  = o3d.utility.Vector3dVector(verts)
            mesh.triangles = o3d.utility.Vector3iVector(faces)
            mesh.compute_vertex_normals()
            mesh.remove_duplicated_vertices()
            mesh.remove_degenerate_triangles()
            mesh.remove_unreferenced_vertices()

            # ── Area-proportional sampling ─────────────────────────────────

            _ifc_type_lower = element.is_a().lower()
            _DENSE_TYPES = {
                "ifcwall", "ifcwallstandardcase", "ifcwallelementedcase",
                "ifcslab", "ifcroof", "ifcroofing", "ifccovering",
                "ifccolumn", "ifcbeam", "ifcplate", "ifcfooting",
                "ifccurtainwall", "ifcramp", "ifcstair",
            }

            _SMALL_TYPES = {
                "ifcwindow", "ifcdoor", "ifcopeningelement",
                "ifcfurnishingelement", "ifcflowsegment", "ifcflowterminal",
                "ifcflowfitting", "ifcdistributionelement",
            }
            if _ifc_type_lower in _DENSE_TYPES:
                _pts_per_m2 = 8_000   # large structural surfaces — high density
                _n_pts_min  = 1_000
                _n_pts_cap  = 100_000  # wide ceiling for big elements
            elif _ifc_type_lower in _SMALL_TYPES:
                _pts_per_m2 = 200     # small detail elements — low density
                _n_pts_min  = 80
                _n_pts_cap  = n_element_points
            else:
                _pts_per_m2 = 500     # misc elements
                _n_pts_min  = 200
                _n_pts_cap  = n_element_points
            try:
                _area = mesh.get_surface_area()   # m²
            except Exception:
                _area = 0.0
            if _area > 1e-6:
                n_pts = int(_area * _pts_per_m2)
            else:
                n_pts = _n_pts_cap                # fallback: use per-type cap
            n_pts = max(_n_pts_min, min(_n_pts_cap, n_pts))
            n_pts = min(n_pts, len(verts) * 5)
            n_pts = max(n_pts, _n_pts_min)        # never below floor

            try:
                elem_pcd = mesh.sample_points_uniformly(
                    number_of_points=n_pts, use_triangle_normal=True)
            except TypeError:
                # older Open3D: fall back to default sampling
                elem_pcd = mesh.sample_points_uniformly(number_of_points=n_pts)

            # Filter to scan-facing surface only when scan centroid is known
            if scan_centroid is not None and elem_pcd.has_normals():
                pts_arr = np.asarray(elem_pcd.points)
                nrm_arr = np.asarray(elem_pcd.normals)
                vec     = scan_centroid - pts_arr      # toward scanner
                dot     = (nrm_arr * vec).sum(axis=1)
                keep    = dot > 0.0
                if keep.sum() >= 50:                    # safety floor
                    filt = o3d.geometry.PointCloud()
                    filt.points  = o3d.utility.Vector3dVector(pts_arr[keep])
                    filt.normals = o3d.utility.Vector3dVector(nrm_arr[keep])
                    elem_pcd = filt

            guid   = getattr(element, "GlobalId", "") or ""
            name   = getattr(element, "Name",     "") or ""
            ifc_type = element.is_a()

            try:
                vol = mesh.get_volume()
            except Exception:
                vol = 0.0
            if abs(vol) < 1e-9:
                try:
                    bb  = mesh.get_axis_aligned_bounding_box()
                    ext = bb.get_extent()
                    vol = float(ext[0]) * float(ext[1]) * float(ext[2])
                except Exception:
                    vol = 0.0

            elements_data.append(dict(
                element  = element,
                guid     = guid,
                name     = name,
                ifc_type = ifc_type,
                storey   = storey_map.get(guid, "Unknown"),
                pcd      = elem_pcd,
                mesh     = mesh,
                verts    = verts,
                faces    = faces,
                volume   = abs(vol),
                is_dup_guid = guid in dup_guids,
            ))
            all_pts.append(np.asarray(elem_pcd.points))

        except Exception as exc:
            log.debug("Element %s skipped (tessellation failed): %s",
                      getattr(element, "GlobalId", "?"), exc)
            # Keep stub so the element still appears in MQA audit / IFC export.
            try:
                elements_data.append(_stub_record(element, "tessellation_failed"))
            except Exception:
                pass
            continue

    # all_pts may be empty if every element produced a stub — guard against that
    if not all_pts:
        raise RuntimeError("Could not tessellate any IFC elements (check geometry settings).")

    if progress:
        progress(0.88, "Building combined BIM point cloud…")

    combined_pts = np.vstack(all_pts)
    if len(combined_pts) > n_bim_points:
        idx = np.random.default_rng(0).choice(len(combined_pts), n_bim_points, replace=False)
        combined_pts = combined_pts[idx]
    combined_bim_pcd = _to_pcd(combined_pts)

    # Build combined mesh for visualisation
    combined_mesh = o3d.geometry.TriangleMesh()
    for ed in elements_data:
        combined_mesh += ed["mesh"]
    combined_mesh.remove_duplicated_vertices()

    if progress:
        progress(0.98, f"IFC loaded: {len(elements_data)} elements")

    return ifc_file, project_name, elements_data, combined_bim_pcd, combined_mesh
