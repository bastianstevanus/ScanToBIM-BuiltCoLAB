"""
Export service — assembles per-task ZIP packages.

Two flavors:
  kind="cqa": Contains:
    - element_deviation_{project}_graph_segmentation_Revit_API.csv
    - Construction_quality_{project}_knowledge_graph.csv
    - element_deviation_{project}_knowledge_graph.ttl
    - {project}_annotated.ifc                (IFC4 with Pset_DeviationSchedule + IfcGroup for Revit schedules)
    - {project}_deviation_pointcloud.e57      (jet-colored scan in registered/IFC coordinates)
    - {project}_schedule_report.html

  kind="mqa": Contains:
    - element_deviation_{project}_graph_segmentation_registration_Revit_API.csv   (single deviation+quality CSV)
    - BIM_quality_{project}_knowledge_graph.csv
    - BIM_quality_{project}_knowledge_graph.ttl
    - {project}_annotated.ifc                (IFC4 with jet per-element colors + Pset_DeviationSchedule)
    - {project}_deviation_pointcloud.e57      (jet-colored scan in registered/IFC coordinates)
    - {project}_schedule_report.html
"""
from __future__ import annotations

import logging
import os
import tempfile
import zipfile
from datetime import datetime
from typing import Dict, Optional

log = logging.getLogger(__name__)


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
    """Map segmentation stats → Validation_Status label.

    Validation_Status reflects SEGMENTATION quality, not deviation magnitude.
    Deviation magnitude is reported separately in the Severity column.

    Reasons (set by segment_element fail_reason field):
      Valid                                   — all checks passed
      Need Validation (No BIM Points)                       — no source/BIM points found
      Need Validation (No Nearby Points)                   — AABB crop has 0 scan points
      Need Validation (No Nearby Surface Points)           — NBP filter removed all points
      Need Validation (No Dominant Plane Found)            — RANSAC filter removed all points
      Need Validation (Normal Filter Removed All Points)   — normal consistency filter removed all
      Need Validation (Proximity Filter Removed All Points)— plane proximity filter removed all
      Need Validation (No Graph Connectivity)              — KNN graph produced empty cluster
      Need Validation (All Deviations Out of Range)        — all deviations exceed max_deviation
      Need Validation (Low Coverage)                       — final result < min_points
    """
    if stats is not None:
        reason = stats.get("fail_reason", "")
        if reason in _FAIL_REASON_STATUS:
            return _FAIL_REASON_STATUS[reason]
        # Legacy fallback for stats without fail_reason
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


# ── IFC annotation ────────────────────────────────────────────────────────────

def _ensure_owner_history(ifc):
    """
    Return an IfcOwnerHistory entity, creating a minimal one if the file has none.
    Without this, IfcPropertySet creation silently fails in IFC2X3 where
    OwnerHistory is a mandatory attribute of all IfcRoot descendants.
    """
    oh_list = ifc.by_type("IfcOwnerHistory")
    if oh_list:
        return oh_list[0]
    # Build a minimal but valid OwnerHistory
    try:
        import time
        person   = ifc.createIfcPerson(None, "ScanToBIM", None, None, None, None, None, None)
        org      = ifc.createIfcOrganization(None, "ScanToBIM Analyzer", None, None, None)
        p_and_o  = ifc.createIfcPersonAndOrganization(person, org, None)
        app      = ifc.createIfcApplication(org, "1.0", "ScanToBIM Analyzer", "ScanToBIM")
        ts       = int(time.time())
        return ifc.createIfcOwnerHistory(
            p_and_o, app, None, "ADDED", None, None, None, ts)
    except Exception as exc:
        log.warning("Could not create fallback OwnerHistory: %s", exc)
        return None

def _build_data_props(ifc, guid: str, stats: Dict, mqa_entry: Optional[Dict]):
    """
    Build the list of IfcPropertySingleValue entries for the combined 'Data' pset.
    Contains EVERY column from element_deviation_*.csv plus every column
    from the corresponding knowledge_graph CSV.
    """
    def _label(v):  return ifc.createIfcLabel(str(v) if v is not None else "")
    def _real(v):   return ifc.createIfcReal(float(v) if v is not None else 0.0)
    def _int(v):    return ifc.createIfcInteger(int(v) if v is not None else 0)
    def _bool(v):   return ifc.createIfcBoolean(bool(v))
    def _text(v):   return ifc.createIfcText(str(v) if v is not None else "")

    props = [
        # ── element_deviation_*.csv columns ───────────────────────────
        ifc.createIfcPropertySingleValue("CustomName",            None, _label(stats.get("name", "")),            None),
        ifc.createIfcPropertySingleValue("ElementID",             None, _label(stats.get("element_id", "")),      None),
        ifc.createIfcPropertySingleValue("GlobalId",              None, _label(guid),                              None),
        ifc.createIfcPropertySingleValue("FamilyName",            None, _label(stats.get("family_name", "")),      None),
        ifc.createIfcPropertySingleValue("Mean_Deviation_m",      None, _real(round(stats.get("mean", 0.0),  10)), None),
        ifc.createIfcPropertySingleValue("Max_Deviation_m",       None, _real(round(stats.get("max",  0.0),  10)), None),
        ifc.createIfcPropertySingleValue("Std_Deviation_m",       None, _real(round(stats.get("std",  0.0),  10)), None),
        ifc.createIfcPropertySingleValue("RMSE_m",                None, _real(round(stats.get("rmse", 0.0),  10)), None),
        ifc.createIfcPropertySingleValue("NumPoints",             None, _int(stats.get("num_points", 0)),          None),
        ifc.createIfcPropertySingleValue("NumCandidates",         None, _int(stats.get("num_candidates", 0)),      None),
        ifc.createIfcPropertySingleValue("NumAfterNormalFilter",  None, _int(stats.get("num_after_normal_filter", 0)), None),
        ifc.createIfcPropertySingleValue("NumSegmented",          None, _int(stats.get("num_segmented", 0)),       None),
        ifc.createIfcPropertySingleValue("BIM_Volume_m3",         None, _real(round(stats.get("volume", 0.0), 10)),None),
        ifc.createIfcPropertySingleValue("Storey",                None, _label(stats.get("storey", "")),           None),
        ifc.createIfcPropertySingleValue("IfcType",               None, _label(stats.get("ifc_type", "")),         None),
        ifc.createIfcPropertySingleValue("Deviation_Status",      None, _label(stats.get("deviation_status", "")), None),
        ifc.createIfcPropertySingleValue("Importance",            None, _label(stats.get("importance", "")),       None),
        ifc.createIfcPropertySingleValue("Severity",              None, _label(stats.get("severity", "")),         None),
        ifc.createIfcPropertySingleValue("Tolerance",             None, _real(float(stats.get("tolerance_compliant", 0.0))), None),
        ifc.createIfcPropertySingleValue("Standard",              None, _label(stats.get("standard_ref", "")),     None),
    ]

    # ── LOD-based engineering quality columns (populated for MQA mode
    #    by enrich_stats_with_quality; CQA stats also carry these fields
    #    if enriched). Use blank/0 defaults when missing so the IFC stays
    #    schema-valid for CQA exports.
    props += [
        ifc.createIfcPropertySingleValue("LOD_Level",        None, _label(stats.get("lod_label", "")),                None),
        ifc.createIfcPropertySingleValue("Tolerance_mm",     None, _real(float(stats.get("tolerance_mm", 0.0) or 0.0)), None),
        ifc.createIfcPropertySingleValue("Quality_Score",    None, _real(float(stats.get("quality_score", 0.0) or 0.0)),None),
        ifc.createIfcPropertySingleValue("Grade",            None, _label(stats.get("grade", "")),                    None),
        ifc.createIfcPropertySingleValue("Pass_Fail_Status", None, _label(stats.get("pass_fail_status", "")),         None),
    ]

    # ── Extra CQA tolerance breakdown ─────────────────
    if "severity" in stats:
        props += [
            ifc.createIfcPropertySingleValue("Tolerance_Minor_m",     None, _real(float(stats.get("tolerance_minor", 0.0))),    None),
            ifc.createIfcPropertySingleValue("Tolerance_Moderate_m",  None, _real(float(stats.get("tolerance_moderate", 0.0))), None),
            ifc.createIfcPropertySingleValue("IsCompliant",           None, _bool((stats.get("severity") or "").lower() == "compliant"),    None),
        ]

    # ── BIM_quality_*.csv columns (MQA-only) — only Material kept here;
    #    QualityLevel/NumIssues live in the KG CSV, not the per-element Pset.
    if mqa_entry is not None:
        props += [
            ifc.createIfcPropertySingleValue("Material",         None, _label(mqa_entry.get("material", "")),    None),
        ]

    return props


def _build_quality_schedule(ifc, owner_history, annotated_elements,
                            deviation_results, mqa_results, kind: str) -> None:
    """Create an IfcWorkSchedule named 'Schedule_Quality' containing one
    IfcTask per annotated element. The schedule entity itself appears in
    Revit's IFC schedule browser; each IfcTask carries the deviation
    summary as its TaskId / LongDescription / Description so users see
    per-element quality data when expanding the schedule."""
    import ifcopenshell.guid as guid_mod
    schema = getattr(ifc, "schema", "IFC4").upper()

    # IfcWorkSchedule was added in IFC4. Skip silently on IFC2X3.
    if schema == "IFC2X3":
        log.info("Schedule_Quality skipped: IfcWorkSchedule is IFC4+ only.")
        return

    try:
        # Predefined enum varies by schema; "ACTUAL" is universally accepted.
        work_schedule = ifc.create_entity(
            "IfcWorkSchedule",
            GlobalId=guid_mod.new(),
            OwnerHistory=owner_history,
            Name="Schedule_Quality",
            Description=("Scan-to-BIM quality assessment schedule. "
                         "One IfcTask per element with deviation analysis."),
            PredefinedType="ACTUAL",
        )
    except Exception as exc:
        log.warning("Could not create IfcWorkSchedule: %s", exc)
        return

    tasks = []
    for idx, element in enumerate(annotated_elements, start=1):
        try:
            guid = element.GlobalId
        except Exception:
            continue
        stats = deviation_results.get(guid, {}) if deviation_results else {}
        mqa_entry = mqa_results.get(guid) if mqa_results else None

        # Build a short, human-readable description with the key columns
        sev = stats.get("severity", "")
        status = stats.get("deviation_status") or _severity_to_status(sev, stats)
        mean_dev = float(stats.get("mean", 0.0) or 0.0)
        importance = stats.get("importance", "")
        tol = stats.get("tolerance_compliant", "")
        std_ref = stats.get("standard_ref", "")
        quality = mqa_entry.get("quality", "") if mqa_entry else ""
        name = stats.get("name", "") or (mqa_entry.get("name", "") if mqa_entry else "")

        long_desc_parts = [
            f"Element={name}", f"GUID={guid}",
            f"Mean={mean_dev:.4f}m", f"Severity={sev}",
            f"Importance={importance}", f"Tolerance={tol}",
            f"Standard={std_ref}", f"Status={status}",
        ]
        if mqa_entry:
            long_desc_parts.append(f"Quality={quality}")
        long_desc = " | ".join(long_desc_parts)

        try:
            task = ifc.create_entity(
                "IfcTask",
                GlobalId=guid_mod.new(),
                OwnerHistory=owner_history,
                Name=name or f"Task_{idx}",
                Description=f"Quality task for {name}",
                LongDescription=long_desc,
                Identification=str(stats.get("element_id", idx)),
                Status=status,
                WorkMethod=None,
                IsMilestone=False,
                Priority=None,
                PredefinedType="NOTDEFINED",
            )
            tasks.append((task, element))
            # Attach all CSV columns as individual properties on the task.
            # Any IFC-aware viewer (Solibri, BIM Collab Zoom, Navisworks) will
            # expose them as separate columns; Revit shows them via Pset_ import.
            try:
                task_props = _build_data_props(ifc, guid, stats, mqa_entry)
                task_pset = ifc.createIfcPropertySet(
                    guid_mod.new(), owner_history,
                    "Pset_QualitySchedule",
                    "Scan-to-BIM quality schedule data – all CSV columns",
                    task_props)
                ifc.createIfcRelDefinesByProperties(
                    guid_mod.new(), owner_history, None, None, [task], task_pset)
            except Exception as pset_exc:
                log.debug("Could not attach Pset_QualitySchedule to task %s: %s",
                          guid, pset_exc)
        except Exception as exc:
            log.debug("Could not create IfcTask for %s: %s", guid, exc)

    if not tasks:
        return

    # Link tasks to the schedule via IfcRelAssignsToControl
    try:
        ifc.create_entity(
            "IfcRelAssignsToControl",
            GlobalId=guid_mod.new(),
            OwnerHistory=owner_history,
            RelatedObjects=[t for t, _ in tasks],
            RelatingControl=work_schedule,
        )
    except Exception as exc:
        log.warning("Could not assign tasks to Schedule_Quality: %s", exc)

    # Link each task to its element via IfcRelAssignsToProcess
    for task, element in tasks:
        try:
            ifc.create_entity(
                "IfcRelAssignsToProcess",
                GlobalId=guid_mod.new(),
                OwnerHistory=owner_history,
                RelatedObjects=[element],
                RelatingProcess=task,
            )
        except Exception:
            pass

    log.info("Built IfcWorkSchedule 'Schedule_Quality' with %d tasks.", len(tasks))


def _annotate_ifc(
    ifc_path: str,
    kind: str,
    deviation_results: Dict[str, Dict],
    mqa_results: Dict[str, Dict],
    element_colors: Optional[Dict[str, list]] = None,
) -> bytes:
    """
    Open the original IFC and attach a single combined 'Data' pset per element
    (containing every column from both relevant CSVs), plus schedule-style psets
    that Revit IFC import exposes as parameters / makes available in schedules.
    """
    import ifcopenshell
    import ifcopenshell.guid as guid_mod

    ifc = ifcopenshell.open(ifc_path)
    owner_history = _ensure_owner_history(ifc)  # always valid, even for IFC2X3

    use_dev  = kind in ("cqa", "mqa") and deviation_results
    use_mqa  = kind == "mqa"  and mqa_results
    use_cqa  = kind == "cqa"  and deviation_results

    # Iterate over union of GUIDs from both sources
    all_guids = set()
    if use_dev: all_guids |= set(deviation_results.keys())
    if use_mqa: all_guids |= set(mqa_results.keys())

    annotated_elements = []
    for guid in all_guids:
        try:
            element = ifc.by_guid(guid)
        except Exception:
            continue
        if element is None:
            continue

        stats     = deviation_results.get(guid, {}) if use_dev else {}
        mqa_entry = mqa_results.get(guid)          if use_mqa else None

        # Inject Deviation_Status into stats so _build_data_props picks it up.
        if stats:
            stats.setdefault(
                "deviation_status",
                _severity_to_status(stats.get("severity", ""), stats))
        elif use_dev:
            # Element is in MQA results but has no deviation data (segmentation
            # failed or no nearby scan points).  Build a placeholder dict that
            # still carries identity fields from the MQA audit so the IFC
            # schedule cell shows name/storey/ifc_type rather than blanks.
            stats = {"deviation_status": "Need Validation (No Scan Data)"}
            if mqa_entry:
                stats.update({
                    "name":         mqa_entry.get("name", ""),
                    "family_name":  mqa_entry.get("name", ""),
                    "ifc_type":     mqa_entry.get("ifc_type", ""),
                    "storey":       mqa_entry.get("storey", ""),
                    "volume":       mqa_entry.get("volume_m3", 0.0),
                })

        # Even when deviation data IS present, identity fields can be empty
        # (older deviation rows pre-stub).  Fill from the MQA entry so the
        # Revit-side Pset never shows a blank Name/Storey/IFCType.
        if mqa_entry:
            if not stats.get("name"):        stats["name"]        = mqa_entry.get("name", "")
            if not stats.get("ifc_type"):    stats["ifc_type"]    = mqa_entry.get("ifc_type", "")
            if not stats.get("storey"):      stats["storey"]      = mqa_entry.get("storey", "")
            if not stats.get("family_name"): stats["family_name"] = mqa_entry.get("name", "")

        # ── "Pset_DeviationSchedule" — Revit auto-imports Pset_* psets and exposes
        #    their fields as instance parameters available in Revit Schedules. ──
        try:
            data_props = _build_data_props(ifc, guid, stats, mqa_entry)
            data_pset = ifc.createIfcPropertySet(
                guid_mod.new(), owner_history,
                "Pset_DeviationSchedule",
                "Scan-to-BIM deviation analysis (per-element). "
                "Use Revit > View > Schedules > New Schedule to schedule these.",
                data_props)
            ifc.createIfcRelDefinesByProperties(
                guid_mod.new(), owner_history, None, None, [element], data_pset)
            annotated_elements.append(element)
        except Exception as exc:
            log.warning("Could not attach Pset_DeviationSchedule to %s: %s", guid, exc)

    # Apply per-element surface colors (jet colormap on deviation)
    if element_colors:
        try:
            _apply_ifc_element_colors(ifc, element_colors)
        except Exception as exc:
            log.warning("IFC element color styling failed: %s", exc)

    # ── IfcGroup for Revit schedule discoverability ─────────────────────────
    # Grouping all annotated elements under a named IfcGroup makes it easy
    # for the user to pick them as the basis of a Revit schedule (the group
    # appears as a filter target in Revit's "New Schedule" dialog).
    if annotated_elements:
        try:
            grp = ifc.createIfcGroup(
                guid_mod.new(), owner_history,
                "Deviation_Schedule",
                "All elements with Scan-to-BIM deviation analysis results",
                None)
            ifc.createIfcRelAssignsToGroup(
                guid_mod.new(), owner_history, None, None,
                annotated_elements, None, grp)
        except Exception as exc:
            log.warning("Could not build IfcGroup for schedule: %s", exc)

        # ── IfcWorkSchedule "Schedule_Quality" ──────────────────────────────
        # Adds an actual schedule entity to the IFC so it appears in Revit's
        # IFC schedule list (next to the existing "schedule working" /
        # "schedule typical" the user already sees). Each element becomes an
        # IfcTask carrying its deviation metadata via the Pset attached above.
        try:
            _build_quality_schedule(
                ifc, owner_history, annotated_elements,
                deviation_results, mqa_results, kind)
        except Exception as exc:
            log.warning("Could not build IfcWorkSchedule 'Schedule_Quality': %s", exc)

    # Write to bytes via tempfile
    with tempfile.NamedTemporaryFile(suffix=".ifc", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        ifc.write(tmp_path)
        with open(tmp_path, "rb") as fh:
            return fh.read()
    finally:
        try: os.unlink(tmp_path)
        except OSError: pass


def _colored_pcd_to_e57_bytes(colored_pcd, max_pts: int = 500_000) -> Optional[bytes]:
    """
    Serialize an Open3D PointCloud with colors to E57 format with RGB color.

    E57 is the standard interchange format for 3D point clouds and is natively
    importable in Autodesk ReCap, Revit (via ReCap), Navisworks, Leica Cyclone,
    and other AEC tools. Colors are stored as uint8 RGB per point.
    """
    if colored_pcd is None:
        return None
    try:
        import numpy as np
        import pye57
        pts  = np.asarray(colored_pcd.points)
        cols = np.asarray(colored_pcd.colors)  # float 0-1, may be empty
        if pts.shape[0] == 0:
            return None
        # If no colors (e.g. original scan without color data), use white
        if cols.shape[0] != pts.shape[0]:
            cols = np.ones((pts.shape[0], 3), dtype=np.float64)

        # Subsample if very large to keep file manageable
        if len(pts) > max_pts:
            idx  = np.random.default_rng(0).choice(len(pts), max_pts, replace=False)
            pts  = pts[idx]
            cols = cols[idx]

        rgb_int = (np.clip(cols, 0, 1) * 255).astype(np.uint8)

        with tempfile.NamedTemporaryFile(suffix=".e57", delete=False) as tmp:
            tmp_path = tmp.name
        try:
            e57 = pye57.E57(tmp_path, mode="w")
            data = {
                "cartesianX":  pts[:, 0].astype(np.float64),
                "cartesianY":  pts[:, 1].astype(np.float64),
                "cartesianZ":  pts[:, 2].astype(np.float64),
                "colorRed":    rgb_int[:, 0],
                "colorGreen":  rgb_int[:, 1],
                "colorBlue":   rgb_int[:, 2],
            }
            e57.write_scan_raw(data)
            e57.close()
            with open(tmp_path, "rb") as fh:
                return fh.read()
        finally:
            try: os.unlink(tmp_path)
            except OSError: pass
    except Exception as exc:
        log.warning("Could not write colored E57: %s", exc)
        return None


def _strip_element_styles(ifc, element) -> None:
    """Remove every IfcStyledItem and IfcRelAssociatesMaterial that would
    override the per-element jet color. Without this, Revit (and many viewers)
    keep showing the original material color even though our IfcStyledItem
    is also attached. Called once per element before applying the jet style.
    """
    schema = getattr(ifc, "schema", "IFC4").upper()
    # 1) Drop material associations (they bind to materials that carry styles)
    try:
        for rel in list(getattr(element, "HasAssociations", []) or []):
            if rel.is_a("IfcRelAssociatesMaterial"):
                ifc.remove(rel)
    except Exception:
        pass
    # 2) Drop existing IfcStyledItem on every representation item
    if not hasattr(element, "Representation") or not element.Representation:
        return
    try:
        for rep in element.Representation.Representations:
            for item in rep.Items:
                styled = []
                # IFC4 uses inverse StyledByItem; IFC2X3 too.
                styled.extend(list(getattr(item, "StyledByItem", []) or []))
                for si in styled:
                    try:
                        ifc.remove(si)
                    except Exception:
                        pass
    except Exception:
        pass


def _apply_ifc_element_colors(ifc, element_colors: dict) -> None:
    """
    Apply jet-color surface styles to IFC elements in-place.
    element_colors: {guid: [r, g, b]}  (values 0-1 float)

    Strips original material/style first so the new jet color is the only
    visual style, then attaches an IfcStyledItem to every representation item.
    Works in both IFC2X3 and IFC4.
    """
    if not element_colors:
        return
    schema = getattr(ifc, "schema", "IFC4").upper()
    for guid, rgb in element_colors.items():
        try:
            element = ifc.by_guid(guid)
        except Exception:
            element = None
        if element is None:
            continue
        if not hasattr(element, "Representation") or not element.Representation:
            continue

        # NEW: strip old colors first so jet replaces (not stacks on) the original
        _strip_element_styles(ifc, element)

        try:
            r, g, b   = float(rgb[0]), float(rgb[1]), float(rgb[2])
            color_rgb = ifc.createIfcColourRgb(None, r, g, b)
            rendering = ifc.createIfcSurfaceStyleRendering(
                color_rgb, None, None, None, None, None, None, None, "FLAT")
            surf_style = ifc.createIfcSurfaceStyle(None, "BOTH", [rendering])
            if schema == "IFC2X3":
                style_assign = ifc.createIfcPresentationStyleAssignment([surf_style])
                styles_arg   = [style_assign]
            else:
                styles_arg = [surf_style]
            for rep in element.Representation.Representations:
                for item in rep.Items:
                    try:
                        ifc.createIfcStyledItem(item, styles_arg, None)
                    except Exception:
                        pass
        except Exception as exc:
            log.debug("Could not color element %s: %s", guid, exc)


def _create_schedule_html(
    project_name: str,
    kind: str,
    deviation_results: Dict[str, Dict],
    mqa_results: Dict[str, Dict],
) -> bytes:
    """
    Generate a self-contained HTML schedule report with two tabs:
      - Tab 1: Element Deviation schedule (matches element_deviation_*.csv)
      - Tab 2: Knowledge Graph schedule  (matches Construction_quality_* or BIM_quality_*.csv)
    Opens in any browser, no external dependencies.
    """
    tag = "CQA – Construction Quality" if kind == "cqa" else "MQA – Model Quality"

    # ── colour helpers ──────────────────────────────────────────────────────
    def _row_class_cqa(s: Dict) -> str:
        sev = s.get("severity", "").lower()
        if sev == "compliant":   return "row-ok"
        if sev == "minor":       return "row-minor"
        if sev == "moderate":    return "row-moderate"
        if sev in ("critical", "severe"): return "row-critical"
        return ""

    def _row_class_mqa(q: str) -> str:
        ql = q.lower()
        if ql in ("high", "good", "compliant"):    return "row-ok"
        if ql in ("medium", "acceptable"):          return "row-minor"
        if ql in ("low", "poor"):                   return "row-critical"
        return ""

    # ── Table 1: Element Deviation ──────────────────────────────────────────
    # CQA and MQA share the deviation summary but with different columns:
    #   CQA — Importance/Tolerance/Standard/Severity (construction-quality)
    #   MQA — LOD_Level/Tolerance_mm/Quality_Score/Grade/Pass_Fail/Severity
    if kind == "mqa":
        t1_headers = [
            "CustomName", "ElementID", "GlobalId", "FamilyName", "IFCType",
            "LOD_Level", "Tolerance_mm",
            "NumPoints", "NumCandidates", "NumAfterNormalFilter", "NumSegmented",
            "Deviation_Status",
            "Max_Dev (m)", "Std_Dev (m)", "RMSE (m)", "Mean_Dev (m)",
            "Quality_Score", "Grade", "Pass_Fail_Status", "Severity",
            "BIM_Volume_m3",
        ]
        t1_rows = []
        for guid, s in deviation_results.items():
            sev = s.get("severity", "")
            sl = sev.lower()
            if   sl == "compliant": rc = "row-ok"
            elif sl == "minor":     rc = "row-minor"
            elif sl == "moderate":  rc = "row-moderate"
            elif sl in ("critical", "severe", "major"): rc = "row-critical"
            else:                   rc = ""
            status = s.get("deviation_status") or _severity_to_status(sev, s)
            t1_rows.append((rc, [
                s.get("name", ""), s.get("element_id", ""), guid,
                s.get("family_name", ""), s.get("ifc_type", ""),
                s.get("lod_label", ""), s.get("tolerance_mm", ""),
                s.get("num_points", 0), s.get("num_candidates", 0),
                s.get("num_after_normal_filter", 0), s.get("num_segmented", 0),
                status,
                f"{s.get('max', 0):.4f}", f"{s.get('std', 0):.4f}",
                f"{s.get('rmse', 0):.4f}", f"{s.get('mean', 0):.4f}",
                s.get("quality_score", ""), s.get("grade", ""),
                s.get("pass_fail_status", ""), sev,
                f"{s.get('volume', 0):.4f}",
            ]))
    else:
        t1_headers = [
            "CustomName", "ElementID", "GlobalId", "FamilyName",
            "Mean_Dev (m)", "Max_Dev (m)", "Std_Dev (m)", "RMSE (m)",
            "NumPoints", "NumSegmented", "Importance", "Severity",
            "Tolerance (m)", "Standard", "Deviation_Status",
            "BIM_Volume_m3", "Storey", "IfcType",
        ]
        t1_rows = []
        for guid, s in deviation_results.items():
            rc = _row_class_cqa(s)
            sev = s.get("severity", "")
            status = s.get("deviation_status") or _severity_to_status(sev, s)
            t1_rows.append((rc, [
                s.get("name", ""), s.get("element_id", ""), guid,
                s.get("family_name", ""),
                f"{s.get('mean', 0):.4f}", f"{s.get('max', 0):.4f}",
                f"{s.get('std', 0):.4f}", f"{s.get('rmse', 0):.4f}",
                s.get("num_points", 0), s.get("num_segmented", 0),
                s.get("importance", ""), sev,
                f"{s.get('tolerance_compliant', 0):.4f}" if s.get("tolerance_compliant") is not None else "",
                s.get("standard_ref", ""), status,
                f"{s.get('volume', 0):.4f}", s.get("storey", ""), s.get("ifc_type", ""),
            ]))

    # ── Table 2: Knowledge Graph ────────────────────────────────────────────
    if kind == "cqa":
        t2_headers = [
            "Element", "Type", "Storey", "Importance", "Severity",
            "Mean (m)", "Tolerance (m)", "Standard", "IsCompliant",
        ]
        t2_rows = []
        for guid, s in deviation_results.items():
            if "severity" not in s:
                continue
            rc = _row_class_cqa(s)
            t2_rows.append((rc, [
                s.get("name", ""),
                str(s.get("ifc_type", "")).replace("Ifc", ""),
                s.get("storey", ""),
                s.get("importance", ""),
                s.get("severity", ""),
                f"{s.get('mean', 0):.4f}",
                f"{s.get('tolerance_compliant', 0):.4f}",
                s.get("standard_ref", ""),
                "Yes" if s.get("severity", "") == "Compliant" else "No",
            ]))
    else:
        t2_headers = [
            "Element", "Type", "Storey", "BIM_Info_Quality",
            "NumIssues", "NumCritical", "NumMajor", "NumMinor", "IssueList", "Material",
        ]
        t2_rows = []
        for guid, m in mqa_results.items():
            rc = _row_class_mqa(m.get("quality", ""))
            issues_raw = m.get("issues", [])
            if issues_raw and isinstance(issues_raw[0], (list, tuple)):
                issue_str = "; ".join(str(c) for c, _ in issues_raw)
            else:
                issue_str = "; ".join(str(i) for i in issues_raw)
            t2_rows.append((rc, [
                m.get("name", ""),
                str(m.get("ifc_type", "")).replace("Ifc", ""),
                m.get("storey", ""),
                m.get("quality", ""),
                m.get("num_issues", 0),
                m.get("n_critical", 0),
                m.get("n_major", 0),
                m.get("n_minor", 0),
                issue_str,
                m.get("material", ""),
            ]))

    def _build_table(headers, rows, tab_id):
        th_cells = "".join(f"<th>{h}</th>" for h in headers)
        body_rows = ""
        for rc, cells in rows:
            tds = "".join(f"<td>{c}</td>" for c in cells)
            body_rows += f'<tr class="{rc}">{tds}</tr>\n'
        return (
            f'<div id="{tab_id}" class="tab-panel">'
            f'<p class="row-count">{len(rows)} elements</p>'
            f'<div class="tbl-wrap"><table>'
            f"<thead><tr>{th_cells}</tr></thead>"
            f"<tbody>{body_rows}</tbody>"
            f"</table></div></div>"
        )

    tbl1_label = "Schedule 1 — Element Deviation"
    tbl2_label = (
        "Schedule 2 — Construction Quality"
        if kind == "cqa" else
        "Schedule 2 — BIM Quality"
    )

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{project_name} – {tag}</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:Arial,sans-serif;background:#0d1117;color:#e6edf3;padding:20px}}
h1{{font-size:18px;margin-bottom:4px;color:#58a6ff}}
.subtitle{{font-size:12px;color:#8b949e;margin-bottom:16px}}
.tabs{{display:flex;gap:6px;margin-bottom:0;border-bottom:2px solid #30363d}}
.tab-btn{{padding:8px 18px;background:transparent;color:#8b949e;border:none;
  border-bottom:3px solid transparent;cursor:pointer;font-size:13px;margin-bottom:-2px}}
.tab-btn.active{{color:#58a6ff;border-bottom-color:#58a6ff;font-weight:bold}}
.tab-panel{{display:none;padding-top:14px}}
.tab-panel.active{{display:block}}
.row-count{{font-size:11px;color:#8b949e;margin-bottom:8px}}
.tbl-wrap{{overflow-x:auto}}
table{{border-collapse:collapse;width:100%;font-size:11px}}
th{{background:#161b22;color:#8b949e;padding:6px 8px;text-align:left;
   white-space:nowrap;border-bottom:2px solid #30363d;position:sticky;top:0}}
td{{padding:5px 8px;border-bottom:1px solid #21262d;white-space:nowrap}}
tr:hover td{{background:#161b22}}
.row-ok td{{background:#0f2a1a}}
.row-ok:hover td{{background:#163522}}
.row-minor td{{background:#2d2200}}
.row-minor:hover td{{background:#3d2e00}}
.row-moderate td{{background:#2d1a00}}
.row-moderate:hover td{{background:#3d2500}}
.row-critical td{{background:#2d0f0f}}
.row-critical:hover td{{background:#3d1515}}
</style>
</head>
<body>
<h1>{project_name}</h1>
<p class="subtitle">{tag} Schedule Report &nbsp;·&nbsp; Generated by ScanToBIM Analyzer</p>
<div class="tabs">
  <button class="tab-btn active" onclick="showTab('t1',this)">{tbl1_label}</button>
  <button class="tab-btn"        onclick="showTab('t2',this)">{tbl2_label}</button>
</div>
{_build_table(t1_headers, t1_rows, 't1')}
{_build_table(t2_headers, t2_rows, 't2')}
<script>
function showTab(id,btn){{
  document.querySelectorAll('.tab-panel').forEach(p=>p.classList.remove('active'));
  document.querySelectorAll('.tab-btn').forEach(b=>b.classList.remove('active'));
  document.getElementById(id).classList.add('active');
  btn.classList.add('active');
}}
document.getElementById('t1').classList.add('active');
</script>
</body></html>"""
    return html.encode("utf-8")


def create_export_zip(
    output_dir: str,
    project_name: str,
    kind: str,                           # "cqa" or "mqa"
    ifc_path: Optional[str],
    deviation_results: Dict[str, Dict],
    mqa_results: Dict[str, Dict],
    cqa_csv: str                     = "",
    deviation_csv: str               = "",
    mqa_kg_csv: str                  = "",
    mqa_dev_csv: str                 = "",
    construction_ttl: str            = "",
    model_quality_ttl: str           = "",
    colored_pcd                      = None,
    element_colors: Optional[dict]   = None,  # {guid: [r,g,b]} for MQA element coloring
) -> str:
    """
    Write the per-task export ZIP and return its path.
    """
    kind = (kind or "cqa").lower()
    if kind not in ("cqa", "mqa"):
        kind = "cqa"

    safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in project_name)
    ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
    tag  = "CQA" if kind == "cqa" else "MQA"
    zip_path = os.path.join(output_dir, f"{safe}_{tag}_Export_{ts}.zip")

    os.makedirs(output_dir, exist_ok=True)

    # UTF-8 BOM prefix so Excel auto-detects UTF-8 (fixes â€" → — and similar garbling)
    _BOM = "\ufeff"

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        if kind == "cqa":
            # ── CQA files (only) ──────────────────────────────────────
            if deviation_csv:
                zf.writestr(
                    f"element_deviation_{safe}_graph_segmentation_Revit_API.csv",
                    _BOM + deviation_csv)
            if cqa_csv:
                zf.writestr(
                    f"Construction_quality_{safe}_knowledge_graph.csv",
                    _BOM + cqa_csv)
            if construction_ttl:
                zf.writestr(
                    f"element_deviation_{safe}_knowledge_graph.ttl",
                    construction_ttl)
        else:
            # ── MQA files (only) ──────────────────────────────────────
            # Single deviation+quality CSV (replaces previous two-CSV split).
            if mqa_dev_csv:
                zf.writestr(
                    f"element_deviation_{safe}_graph_segmentation_registration_Revit_API.csv",
                    _BOM + mqa_dev_csv)
            if mqa_kg_csv:
                zf.writestr(
                    f"BIM_quality_{safe}_knowledge_graph.csv",
                    _BOM + mqa_kg_csv)
            if model_quality_ttl:
                zf.writestr(
                    f"BIM_quality_{safe}_knowledge_graph.ttl",
                    model_quality_ttl)

        # ── Annotated IFC (with Other pset + element colors) ──────────
        ifc_bytes = None
        if ifc_path and os.path.isfile(ifc_path):
            try:
                ifc_bytes = _annotate_ifc(
                    ifc_path, kind, deviation_results, mqa_results,
                    element_colors=element_colors)
                zf.writestr(f"{safe}_annotated.ifc", ifc_bytes)
            except Exception as exc:
                log.warning("Could not annotate IFC: %s", exc)
                with open(ifc_path, "rb") as fh:
                    raw = fh.read()
                zf.writestr(f"{safe}_original.ifc", raw)
                ifc_bytes = raw

        # ── Best-effort IFC2X3 downgrade for Revit Open IFC ──────────
        # Disabled: IFC2X3 downgrade produced corrupt data on user models.
        # Revit 2024+ handles IFC4 natively via 'Insert > Link IFC'.

        # ── HTML Schedule Report (two-tab, opens in any browser) ─────
        html_bytes = _create_schedule_html(
            project_name, kind, deviation_results, mqa_results)
        if html_bytes:
            zf.writestr(f"{safe}_schedule_report.html", html_bytes)

        # ── Jet-colored point cloud (.e57 — importable in ReCap, Revit, Navisworks) ─
        e57_bytes = _colored_pcd_to_e57_bytes(colored_pcd)
        if e57_bytes:
            e57_filename = f"{safe}_deviation_pointcloud.e57"
            zf.writestr(e57_filename, e57_bytes)
            if kind == "cqa":
                pcd_note = (
                    "POINT CLOUD: '{e57}'\n"
                    "============================================================\n"
                    "Coloured by deviation (blue = compliant, red = large deviation, jet scale).\n"
                    "Format: E57 with embedded RGB color — industry-standard interchange format.\n\n"
                    "Loading into Revit via Autodesk ReCap:\n"
                    "  Step 1. Open Autodesk ReCap Pro (ships with Revit / AEC Collection).\n"
                    "          File > New Project > Import Point Cloud > select '{e57}'\n"
                    "          ReCap will index the file and save '<name>.rcp' alongside it.\n"
                    "          (Indexing takes ~1 minute for typical scans.)\n"
                    "  Step 2. In Revit: Insert > Link Point Cloud > select the generated .rcp\n"
                    "          file. Set Positioning = 'Auto - Origin to Origin'.\n"
                    "  Step 3. Select the linked point cloud > Properties palette >\n"
                    "          Colorization = 'True Color' to display the jet deviation colors.\n\n"
                    "Direct import (without ReCap):\n"
                    "  Navisworks, Leica Cyclone, FARO SCENE, CloudCompare and Bentley\n"
                    "  ContextCapture all open .e57 files directly.\n\n"
                    "Loading the IFC model:\n"
                    "  Insert > Link IFC > select '{ifc}_annotated.ifc'\n"
                    "  Positioning = 'Auto - Origin to Origin'\n\n"
                    "Tip: open '{ifc}_schedule_report.html' in any browser for the full\n"
                    "deviation + quality schedule report.\n"
                ).format(e57=e57_filename, ifc=safe)
            else:
                pcd_note = (
                    "POINT CLOUD: '{e57}'\n"
                    "============================================================\n"
                    "Coloured by mean per-element deviation (jet scale).\n"
                    "Format: E57 with embedded RGB color — industry-standard interchange format.\n\n"
                    "Loading into Revit via Autodesk ReCap:\n"
                    "  Step 1. Open Autodesk ReCap Pro (ships with Revit / AEC Collection).\n"
                    "          File > New Project > Import Point Cloud > select '{e57}'\n"
                    "          ReCap will index the file and save '<name>.rcp' alongside it.\n"
                    "  Step 2. In Revit: Insert > Link Point Cloud > select the generated .rcp.\n"
                    "          Positioning = 'Auto - Origin to Origin'.\n"
                    "  Step 3. Properties palette > Colorization = 'True Color' for jet colours.\n\n"
                    "Direct import (without ReCap):\n"
                    "  Navisworks, Leica Cyclone, FARO SCENE, CloudCompare and Bentley\n"
                    "  ContextCapture all open .e57 files directly.\n\n"
                    "Loading the IFC model with element-level jet colours:\n"
                    "  Insert > Link IFC > '{ifc}_annotated.ifc'.\n"
                    "  Element jet colours are stored as IfcSurfaceStyle and are fully visible in\n"
                    "  Navisworks, BIM Collab Zoom, Solibri and Autodesk Viewer\n"
                    "  (viewer.autodesk.com). Revit may render them as a solid theme only.\n\n"
                    "Tip: open '{ifc}_schedule_report.html' in any browser for the BIM quality\n"
                    "and deviation schedule report.\n"
                ).format(e57=e57_filename, ifc=safe)
            zf.writestr("README_pointcloud.txt", pcd_note)

    log.info("Export ZIP (%s) written to %s", kind, zip_path)
    return zip_path
