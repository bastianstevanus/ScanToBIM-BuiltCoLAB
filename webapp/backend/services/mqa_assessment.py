import csv
import io
from typing import Dict, List, Optional, Tuple

# fail_reason → human-readable status (mirrors backend.main._FAIL_REASON_STATUS)
_FAIL_REASON_STATUS: Dict[str, str] = {
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


def _deviation_status(stats: Dict) -> str:
    reason = (stats.get("fail_reason") or "").strip()
    if reason in _FAIL_REASON_STATUS:
        return _FAIL_REASON_STATUS[reason]
    explicit = stats.get("deviation_status")
    if explicit:
        return str(explicit)
    n_seg = int(stats.get("num_segmented", 0) or 0)
    if n_seg <= 0:
        return "Need Validation (No Segmented Points)"
    return "Valid"

# ── LOD assignment ────────────────────────────────────────────────────────────
_LOD_MAP: Dict[str, Tuple[str, float]] = {
    # LOD 350: ±10 mm — structural precision + MEP
    "IfcBeam":         ("LOD 350", 10.0),
    "IfcColumn":       ("LOD 350", 10.0),
    "IfcMember":       ("LOD 350", 10.0),
    "IfcPlate":        ("LOD 350", 10.0),
    "IfcPipeSegment":  ("LOD 350", 10.0),
    "IfcDuctSegment":  ("LOD 350", 10.0),
    "IfcFlowFitting":  ("LOD 350", 10.0),
    "IfcFlowTerminal": ("LOD 350", 10.0),
    # LOD 300: ±15 mm — general architectural + civil elements
    "IfcWall":         ("LOD 300", 15.0),
    "IfcSlab":         ("LOD 300", 15.0),
    "IfcRoof":         ("LOD 300", 15.0),
    "IfcStair":        ("LOD 300", 15.0),
    "IfcRamp":         ("LOD 300", 15.0),
    "IfcDoor":         ("LOD 300", 15.0),
    "IfcWindow":       ("LOD 300", 15.0),
    "IfcCovering":     ("LOD 300", 15.0),
    "IfcFooting":      ("LOD 300", 15.0),
    "IfcPile":         ("LOD 300", 15.0),
}
_LOD_DEFAULT: Tuple[str, float] = ("LOD 300", 15.0)


def get_lod(ifc_type: str) -> Tuple[str, float]:
    return _LOD_MAP.get(ifc_type, _LOD_DEFAULT)


# ── Quality scoring (mean-only) ──────────────────────────────────────────────

def quality_score(mean_mm: float, tolerance_mm: float) -> float:

    t = max(tolerance_mm, 1e-6)
    norm = min(max(mean_mm / t, 0.0), 1.0)
    return round(100.0 * (1.0 - norm), 1)


def grade(score: float) -> str:
    if score >= 90: return "A (Excellent)"
    if score >= 75: return "B (Good)"
    if score >= 60: return "C (Fair)"
    return "D (Poor)"


def pass_fail(max_mm: float, rmse_mm: float, tolerance_mm: float) -> str:
    t = max(tolerance_mm, 1e-6)
    if max_mm > t or rmse_mm > t * 1.5:
        return "FAIL"
    return "PASS"


def _severity_label(severity: str) -> str:
    s = (severity or "").strip()
    if not s:
        return "Compliant"  
    sl = s.lower()
    if sl == "compliant":            return "Compliant"
    if sl == "minor":                return "Minor"
    if sl == "moderate":             return "Moderate"
    if sl in ("severe", "critical", "major"): return "Critical"
    return s  # passthrough for unknown labels


# ── Public API ────────────────────────────────────────────────────────────────

def build_mqa_deviation_csv(deviation_results: Dict[str, Dict],
                            elements_data: Optional[List[Dict]] = None,
                            mqa_results: Optional[Dict[str, Dict]] = None) -> str:

    buf = io.StringIO()
    w   = csv.writer(buf)
    w.writerow([
        "ElementID", "CustomName", "GlobalId", "FamilyName", "IFCType",
        "LOD_Level", "Tolerance_mm",
        "NumPoints", "NumCandidates", "NumAfterNormalFilter", "NumSegmented",
        "Deviation_Status",
        "Max_Deviation_m", "Std_Deviation_m", "RMSE_m", "Mean_Deviation_m",
        "Quality_Score", "Grade", "Pass_Fail_Status", "Severity",
        "BIM_Volume_m3",
    ])
    for guid, s in deviation_results.items():
        ifc_type      = s.get("ifc_type", "")
        lod_label, tol_mm = get_lod(ifc_type)

        mean_m = float(s.get("mean", 0.0) or 0.0)
        max_m  = float(s.get("max",  0.0) or 0.0)
        std_m  = float(s.get("std",  0.0) or 0.0)
        rmse_m = float(s.get("rmse", 0.0) or 0.0)

        mean_mm = mean_m * 1000.0
        max_mm  = max_m  * 1000.0
        rmse_mm = rmse_m * 1000.0

        score = quality_score(mean_mm, tol_mm)
        g     = grade(score)
        pf    = pass_fail(max_mm, rmse_mm, tol_mm)
        sev   = _severity_label(s.get("severity", ""))
        status = _deviation_status(s)

        w.writerow([
            s.get("element_id", ""),
            s.get("name", ""),
            guid,
            s.get("family_name", ""),
            ifc_type,
            lod_label,
            tol_mm,
            s.get("num_points", 0),
            s.get("num_candidates", 0),
            s.get("num_after_normal_filter", 0),
            s.get("num_segmented", 0),
            status,
            round(max_m,  6),
            round(std_m,  6),
            round(rmse_m, 6),
            round(mean_m, 6),
            score,
            g,
            pf,
            sev,
            round(float(s.get("volume", 0.0) or 0.0), 4),
        ])

    # ── Trailing section: elements present in BIM but not in deviation_results
    if elements_data:
        skipped_rows = []
        for ed in elements_data:
            gid = ed.get("guid", "")
            if not gid or gid in deviation_results:
                continue
            ifc_type = ed.get("ifc_type", "")
            lod_label, tol_mm = get_lod(ifc_type)
     
            mqa_entry = (mqa_results or {}).get(gid, {})
            _v = ed.get("verts")
            if _v is None or (hasattr(_v, "__len__") and len(_v) == 0):
                reason = "Need Validation (No Geometry / Tessellation Failed)"
            elif mqa_entry.get("has_geometry") is False:
                reason = "Need Validation (No Geometry)"
            else:
                reason = "Need Validation (Not Segmented — No Scan Coverage)"
            skipped_rows.append([
                ed.get("element_id", "") or gid,
                ed.get("name", ""),
                gid,
                ed.get("family_name", ""),
                ifc_type,
                lod_label,
                tol_mm,
                0, 0, 0, 0,
                reason,
                "", "", "", "",
                "", "", "", "",
                round(float(ed.get("volume", 0.0) or 0.0), 4),
            ])
        if skipped_rows:
            w.writerow([])
            w.writerow(["=== Not Segmented / Not Found Elements ({} total) ===".format(len(skipped_rows))])
            w.writerow([
                "ElementID", "CustomName", "GlobalId", "FamilyName", "IFCType",
                "LOD_Level", "Tolerance_mm",
                "NumPoints", "NumCandidates", "NumAfterNormalFilter", "NumSegmented",
                "Deviation_Status",
                "Max_Deviation_m", "Std_Deviation_m", "RMSE_m", "Mean_Deviation_m",
                "Quality_Score", "Grade", "Pass_Fail_Status", "Severity",
                "BIM_Volume_m3",
            ])
            for r in skipped_rows:
                w.writerow(r)
    return buf.getvalue()


def enrich_stats_with_quality(deviation_results: Dict[str, Dict]) -> None:

    for s in deviation_results.values():
        ifc_type = s.get("ifc_type", "")
        lod_label, tol_mm = get_lod(ifc_type)
        mean_mm = float(s.get("mean", 0.0) or 0.0) * 1000.0
        max_mm  = float(s.get("max",  0.0) or 0.0) * 1000.0
        rmse_mm = float(s.get("rmse", 0.0) or 0.0) * 1000.0
        score = quality_score(mean_mm, tol_mm)
        s["lod_label"]        = lod_label
        s["tolerance_mm"]     = tol_mm
        s["quality_score"]    = score
        s["grade"]            = grade(score)
        s["pass_fail_status"] = pass_fail(max_mm, rmse_mm, tol_mm)
        s["severity"]         = _severity_label(s.get("severity", ""))
