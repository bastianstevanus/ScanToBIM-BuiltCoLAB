"""
E57 point cloud loader.

Uses pye57.read_scan(transform=True) which automatically applies the per-scan
pose stored in the E57 header, returning points in world/project coordinates.
Filters invalid points and optionally extracts RGB colour channels.
"""
from __future__ import annotations

import logging
from typing import Callable, Optional, Tuple

import numpy as np
import open3d as o3d

log = logging.getLogger(__name__)

MAX_TOTAL_POINTS = 1_500_000


def load_e57(
    filepath: str,
    max_points: int = MAX_TOTAL_POINTS,
    progress: Optional[Callable[[float, str], None]] = None,
) -> Tuple[o3d.geometry.PointCloud, Optional[np.ndarray]]:
    """
    Load an E57 file and return (PointCloud, colours_array_or_None).

    colours_array shape: (N, 3) float32 in [0, 1], or None if not available.
    """
    try:
        import pye57
    except ImportError:
        raise RuntimeError("pye57 is required to load E57 files.  Install with: pip install pye57")

    e57 = pye57.E57(filepath)
    n_scans = e57.scan_count
    if progress:
        progress(0.02, f"Opened E57: {n_scans} scan(s) found")

    max_per_scan = max(max_points // max(n_scans, 1), 50_000)

    all_xyz: list[np.ndarray] = []
    all_rgb: list[np.ndarray] = []
    has_color = True

    for idx in range(n_scans):
        if progress:
            progress(0.05 + 0.85 * (idx / n_scans), f"Reading scan {idx + 1}/{n_scans}…")

        try:
            data = e57.read_scan(idx, ignore_missing_fields=True)
        except Exception as exc:
            log.warning("Scan %d: read failed — %s", idx, exc)
            continue

        try:
            x = np.asarray(data["cartesianX"]).flatten()
            y = np.asarray(data["cartesianY"]).flatten()
            z = np.asarray(data["cartesianZ"]).flatten()
        except KeyError:
            log.warning("Scan %d: missing Cartesian fields — skipped", idx)
            continue

        # Filter invalid points
        if "cartesianInvalidState" in data:
            valid = np.asarray(data["cartesianInvalidState"]).flatten() == 0
            x, y, z = x[valid], y[valid], z[valid]

        xyz = np.column_stack([x, y, z]).astype(np.float64)
        # pye57.read_scan() uses transform=True by default, so the pose
        # (rotation + translation) is already baked into the XYZ values.
        # Do NOT apply it again — that would double-transform the coordinates.

        # Subsample per scan if needed
        if len(xyz) > max_per_scan:
            stride = len(xyz) // max_per_scan
            xyz = xyz[::stride]

        all_xyz.append(xyz)

        # Colour channels
        if has_color:
            try:
                r = np.asarray(data["colorRed"]).flatten()
                g = np.asarray(data["colorGreen"]).flatten()
                b = np.asarray(data["colorBlue"]).flatten()
                if "cartesianInvalidState" in data:
                    r, g, b = r[valid], g[valid], b[valid]
                # Subsample same stride
                if len(r) > max_per_scan:
                    r, g, b = r[::stride], g[::stride], b[::stride]
                rgb = np.column_stack([r, g, b]).astype(np.float32)
                # Normalise to [0,1] if uint8-range
                if rgb.max() > 1.0:
                    rgb /= 255.0
                all_rgb.append(rgb)
            except KeyError:
                has_color = False

    if not all_xyz:
        raise RuntimeError("No valid scan data found in E57 file.")

    xyz_all = np.vstack(all_xyz)

    # Global cap
    if len(xyz_all) > max_points:
        stride = len(xyz_all) // max_points
        xyz_all = xyz_all[::stride]

    pcd = o3d.geometry.PointCloud()
    pcd.points = o3d.utility.Vector3dVector(xyz_all)

    colors_out: Optional[np.ndarray] = None
    if has_color and all_rgb:
        rgb_all = np.vstack(all_rgb)
        if len(rgb_all) > max_points:
            rgb_all = rgb_all[::stride]
        colors_out = rgb_all[: len(xyz_all)]
        pcd.colors = o3d.utility.Vector3dVector(colors_out.astype(np.float64))

    if progress:
        progress(0.95, f"Loaded {len(xyz_all):,} points from E57")

    return pcd, colors_out


def load_point_cloud(
    filepath: str,
    max_points: int = MAX_TOTAL_POINTS,
    progress: Optional[Callable[[float, str], None]] = None,
) -> Tuple[o3d.geometry.PointCloud, Optional[np.ndarray]]:
    """
    Unified entry point — dispatches to the right loader based on file extension.

    Supported formats
    -----------------
    .e57              → pye57 (multi-scan, with pose)
    .ply / .pcd / .xyz / .pts / .xyzn / .xyzrgb
                      → Open3D generic reader

    Note: Autodesk ReCap (.rcp) files are not supported. Export to E57 first:
          ReCap → File → Export Scan → E57
    """
    import os
    ext = os.path.splitext(filepath)[1].lower()

    if ext == ".e57":
        return load_e57(filepath, max_points, progress)

    if ext in (".rcp", ".rcs"):
        raise RuntimeError(
            "Autodesk ReCap (.rcp) files cannot be read directly.\n"
            "Please export to E57 from ReCap first:\n"
            "  ReCap → File → Export Scan → E57\n"
            "Then re-import the .e57 file."
        )

    # Generic Open3D fallback (.ply, .pcd, .xyz, .pts, etc.)
    if progress:
        progress(0.05, f"Reading {ext} file via Open3D…")
    pcd = o3d.io.read_point_cloud(filepath)
    if not pcd.has_points():
        raise RuntimeError(f"Could not read point cloud from {os.path.basename(filepath)}. "
                           f"Supported formats: .e57, .ply, .pcd, .xyz, .pts")
    pts = np.asarray(pcd.points)
    if len(pts) > max_points:
        idx = np.random.default_rng(0).choice(len(pts), max_points, replace=False)
        pcd = pcd.select_by_index(idx.tolist())
    cols_out: Optional[np.ndarray] = None
    if pcd.has_colors():
        cols_out = np.asarray(pcd.colors, dtype=np.float32)
    if progress:
        progress(0.95, f"Loaded {len(np.asarray(pcd.points)):,} points")
    return pcd, cols_out
