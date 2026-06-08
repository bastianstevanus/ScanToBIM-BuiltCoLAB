"""
Construction Quality Assessment knowledge graph.
Mirrors Deviation Graph-based_Reservoir_Revit API Semantic Web.ipynb exactly:
  - Auto-detects project context from IFC metadata keywords
  - Applies EN 13670 / ISO 4463-1 tolerance standards per element type
  - Severity individuals (Compliant/Minor/Moderate/Major) with severityLevel int
  - ToleranceProfile individuals per (type, importance)
  - Per-element assessment triples queryable by SPARQL
  - Exports CSV with Q1-Q4 SPARQL sections + summary (matching reference notebook)
"""
from __future__ import annotations

import csv
import io
import logging
from datetime import datetime
from typing import Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

# ── Context detection ──────────────────────────────────────────────────────────
# Priority order matters: more-specific contexts are checked before generic ones.
_CONTEXT_KEYWORDS: Dict[str, List[str]] = {
    "HydraulicStructure":  ["reservoir", "hydraulic", "water", "tank", "dam",
                             "wastewater", "sewage", "basin", "cistern", "pump",
                             "irrigation", "wetland", "treatment", "aqueduct"],
    "Bridge":              ["bridge", "viaduct", "flyover", "overpass", "underpass",
                             "culvert", "pontoon", "jetty", "pier", "span"],
    "Tunnel":              ["tunnel", "metro", "subway", "underground", "shaft",
                             "adit", "boring", "underway", "transit"],
    "IndustrialBuilding":  ["factory", "industrial", "plant", "warehouse",
                             "manufacturing", "logistics", "refinery", "depot"],
    "EducationalBuilding": ["university", "faculty", "campus", "lecture", "laboratory",
                             "lab", "school", "academy", "college", "institute",
                             "classroom", "auditorium", "polytechnic", "student"],
    "MedicalBuilding":     ["hospital", "clinic", "medical", "healthcare",
                             "surgery", "ward", "pharmacy", "dental", "radiology",
                             "operating", "patient"],
    "ResidentialBuilding": ["residential", "housing", "apartment", "flat",
                             "dwelling", "villa", "condominium", "dormitory",
                             "hostel", "lodging"],
    "CommercialBuilding":  ["office", "commercial", "retail", "mall", "shopping",
                             "hotel", "restaurant", "cafe", "bank", "courthouse",
                             "exhibition", "convention"],
}


def detect_context(ifc_file, project_name: str = "") -> str:
    """Scan IFC metadata, storey/space/group names, and a sample of element
    names for context keywords. Falls back to CommercialBuilding."""
    candidates: List[str] = []

    # 1. Project-level entities (most reliable source)
    for ifc_type in ("IfcProject", "IfcSite", "IfcBuilding"):
        for obj in ifc_file.by_type(ifc_type):
            for attr in ("Name", "LongName", "Description", "ObjectType"):
                val = getattr(obj, attr, None)
                if val:
                    candidates.append(str(val).lower())

    # 2. Supplied project name (IFC filename stem)
    if project_name:
        candidates.append(str(project_name).lower())

    # 3. Storey / space / zone / system names
    for ifc_type in ("IfcBuildingStorey", "IfcSpace", "IfcZone",
                     "IfcSystem", "IfcGroup", "IfcSpatialZone"):
        for obj in ifc_file.by_type(ifc_type):
            for attr in ("Name", "LongName", "ObjectType"):
                val = getattr(obj, attr, None)
                if val:
                    candidates.append(str(val).lower())

    # Early exit if already matched — use priority order so hydraulic/bridge/tunnel
    # always win over generic words like "office" / "hospital".
    text = " ".join(candidates)
    for context in ("HydraulicStructure", "Bridge", "Tunnel",
                    "IndustrialBuilding", "EducationalBuilding", "MedicalBuilding",
                    "ResidentialBuilding", "CommercialBuilding"):
        if any(kw in text for kw in _CONTEXT_KEYWORDS[context]):
            return context

    # 4. Sample up to 300 element names / ObjectTypes as last resort
    try:
        products = list(ifc_file.by_type("IfcProduct"))
        step = max(1, len(products) // 300)
        for obj in products[::step]:
            for attr in ("Name", "ObjectType"):
                val = getattr(obj, attr, None)
                if val:
                    candidates.append(str(val).lower())
    except Exception:
        pass

    text = " ".join(candidates)
    # Score-based detection: pick context with most keyword hits (hydraulic/bridge/tunnel priority on ties)
    priority = ["HydraulicStructure", "Bridge", "Tunnel",
                "IndustrialBuilding", "EducationalBuilding", "MedicalBuilding",
                "ResidentialBuilding", "CommercialBuilding"]
    scores = {c: sum(1 for kw in _CONTEXT_KEYWORDS[c] if kw in text)
              for c in priority}
    best_ctx = max(priority, key=lambda c: scores[c])
    if scores[best_ctx] > 0:
        return best_ctx
    # No keywords matched → default to HydraulicStructure (project domain default)
    return "HydraulicStructure"


# ── Tolerance standards: (t_compliant, t_minor, t_moderate, standard_ref) ─────
# Values in metres.  standard_ref is the normative document for the tolerance.
# CQA (construction quality) uses these for severity classification.
# MQA (model quality) keeps deviation values but uses "ISO 19650 / IFC4" as ref.
_TOLERANCES: Dict[str, Dict[str, Dict[str, Tuple[float, float, float, str]]]] = {
    # Hydraulic/water-retaining: tightest tolerances, EN 13670 Cl.2 + EN 1992-3
    "HydraulicStructure": {
        "IfcWall":         {"High":   (0.005, 0.015, 0.030, "EN 13670 Cl.2 + EN 1992-3")},
        "IfcSlab":         {"High":   (0.005, 0.015, 0.030, "EN 13670 Cl.2 + EN 1992-3")},
        "IfcColumn":       {"High":   (0.005, 0.010, 0.020, "EN 13670 Cl.2")},
        "IfcBeam":         {"High":   (0.005, 0.010, 0.020, "EN 13670 Cl.2")},
        "IfcMember":       {"High":   (0.005, 0.010, 0.020, "EN 13670 Cl.2")},
        "IfcPlate":        {"High":   (0.005, 0.010, 0.020, "EN 13670 Cl.2")},
        "IfcFooting":      {"High":   (0.008, 0.020, 0.040, "EN 13670 Cl.2 + EN 1992-3")},
        "IfcPile":         {"High":   (0.008, 0.020, 0.040, "EN 13670 Cl.2 + EN 1992-3")},
        "IfcRoof":         {"Medium": (0.008, 0.020, 0.040, "EN 13670 Cl.2")},
        "IfcStair":        {"Medium": (0.008, 0.020, 0.040, "ISO 4463-1")},
        "IfcPipeSegment":  {"Medium": (0.010, 0.025, 0.050, "ISO 4463-1")},
        "IfcDuctSegment":  {"Medium": (0.010, 0.025, 0.050, "ISO 4463-1")},
        "IfcFlowFitting":  {"Medium": (0.010, 0.025, 0.050, "ISO 4463-1")},
        "IfcDoor":         {"Low":    (0.015, 0.030, 0.050, "ISO 4463-1")},
        "IfcWindow":       {"Low":    (0.015, 0.030, 0.050, "ISO 4463-1")},
        "IfcFlowTerminal": {"Low":    (0.015, 0.030, 0.050, "ISO 4463-1")},
    },
    # Bridges/viaducts: precision structural concrete, EN 13670 Cl.2 + EN 1992-2
    "Bridge": {
        "IfcWall":         {"High":   (0.005, 0.015, 0.030, "EN 13670 Cl.2 + EN 1992-2")},
        "IfcSlab":         {"High":   (0.005, 0.015, 0.030, "EN 13670 Cl.2 + EN 1992-2")},
        "IfcColumn":       {"High":   (0.005, 0.010, 0.020, "EN 13670 Cl.2 + EN 1992-2")},
        "IfcBeam":         {"High":   (0.005, 0.010, 0.020, "EN 13670 Cl.2 + EN 1992-2")},
        "IfcMember":       {"High":   (0.005, 0.010, 0.020, "EN 13670 Cl.2 + EN 1992-2")},
        "IfcPlate":        {"High":   (0.005, 0.010, 0.020, "EN 13670 Cl.2 + EN 1992-2")},
        "IfcFooting":      {"High":   (0.008, 0.020, 0.040, "EN 13670 Cl.2 + EN 1992-2")},
        "IfcPile":         {"High":   (0.008, 0.020, 0.040, "EN 13670 Cl.2 + EN 1992-2")},
        "IfcPipeSegment":  {"Medium": (0.012, 0.025, 0.050, "ISO 4463-1")},
        "IfcDuctSegment":  {"Medium": (0.012, 0.025, 0.050, "ISO 4463-1")},
        "IfcFlowFitting":  {"Medium": (0.012, 0.025, 0.050, "ISO 4463-1")},
        "IfcDoor":         {"Low":    (0.015, 0.030, 0.050, "ISO 4463-1")},
        "IfcWindow":       {"Low":    (0.015, 0.030, 0.050, "ISO 4463-1")},
        "IfcFlowTerminal": {"Low":    (0.015, 0.030, 0.050, "ISO 4463-1")},
    },
    # Tunnels/underground structures: EN 13670 Cl.2 + ITA/AFTES guidelines
    "Tunnel": {
        "IfcWall":         {"High":   (0.008, 0.020, 0.040, "EN 13670 Cl.2 + ITA/AFTES")},
        "IfcSlab":         {"High":   (0.008, 0.020, 0.040, "EN 13670 Cl.2 + ITA/AFTES")},
        "IfcColumn":       {"High":   (0.008, 0.015, 0.030, "EN 13670 Cl.2")},
        "IfcBeam":         {"High":   (0.008, 0.015, 0.030, "EN 13670 Cl.2")},
        "IfcMember":       {"High":   (0.008, 0.015, 0.030, "EN 13670 Cl.2")},
        "IfcPlate":        {"High":   (0.008, 0.015, 0.030, "EN 13670 Cl.2")},
        "IfcFooting":      {"High":   (0.010, 0.025, 0.050, "EN 13670 Cl.2 + ITA/AFTES")},
        "IfcPile":         {"High":   (0.010, 0.025, 0.050, "EN 13670 Cl.2 + ITA/AFTES")},
        "IfcPipeSegment":  {"Medium": (0.012, 0.025, 0.050, "ISO 4463-1")},
        "IfcDuctSegment":  {"Medium": (0.012, 0.025, 0.050, "ISO 4463-1")},
        "IfcFlowFitting":  {"Medium": (0.012, 0.025, 0.050, "ISO 4463-1")},
        "IfcDoor":         {"Low":    (0.015, 0.030, 0.050, "ISO 4463-1")},
        "IfcFlowTerminal": {"Low":    (0.015, 0.030, 0.050, "ISO 4463-1")},
    },
    # Industrial: EN 13670 Cl.1 (normal concrete quality)
    "IndustrialBuilding": {
        "IfcWall":         {"High":   (0.008, 0.020, 0.040, "EN 13670 Cl.1")},
        "IfcSlab":         {"High":   (0.008, 0.020, 0.040, "EN 13670 Cl.1")},
        "IfcColumn":       {"High":   (0.008, 0.015, 0.030, "EN 13670 Cl.1")},
        "IfcBeam":         {"High":   (0.008, 0.015, 0.030, "EN 13670 Cl.1")},
        "IfcMember":       {"High":   (0.008, 0.015, 0.030, "EN 13670 Cl.1")},
        "IfcPlate":        {"High":   (0.008, 0.015, 0.030, "EN 13670 Cl.1")},
        "IfcFooting":      {"High":   (0.010, 0.025, 0.050, "EN 13670 Cl.1")},
        "IfcPile":         {"High":   (0.010, 0.025, 0.050, "EN 13670 Cl.1")},
        "IfcPipeSegment":  {"Medium": (0.012, 0.025, 0.050, "ISO 4463-1")},
        "IfcDuctSegment":  {"Medium": (0.012, 0.025, 0.050, "ISO 4463-1")},
        "IfcFlowFitting":  {"Medium": (0.012, 0.025, 0.050, "ISO 4463-1")},
        "IfcDoor":         {"Low":    (0.015, 0.030, 0.050, "ISO 4463-1")},
        "IfcWindow":       {"Low":    (0.015, 0.030, 0.050, "ISO 4463-1")},
        "IfcFlowTerminal": {"Low":    (0.015, 0.030, 0.050, "ISO 4463-1")},
    },
    # Educational (university, school): EN 13670 Cl.1 + EN 1992-1-1
    "EducationalBuilding": {
        "IfcWall":         {"High":   (0.010, 0.020, 0.040, "EN 13670 Cl.1 + EN 1992-1-1")},
        "IfcSlab":         {"High":   (0.010, 0.020, 0.040, "EN 13670 Cl.1 + EN 1992-1-1")},
        "IfcColumn":       {"High":   (0.008, 0.015, 0.030, "EN 13670 Cl.1")},
        "IfcBeam":         {"High":   (0.008, 0.015, 0.030, "EN 13670 Cl.1")},
        "IfcMember":       {"High":   (0.008, 0.015, 0.030, "EN 13670 Cl.1")},
        "IfcPlate":        {"High":   (0.008, 0.015, 0.030, "EN 13670 Cl.1")},
        "IfcFooting":      {"High":   (0.010, 0.025, 0.050, "EN 13670 Cl.1")},
        "IfcPile":         {"High":   (0.010, 0.025, 0.050, "EN 13670 Cl.1")},
        "IfcRoof":         {"Medium": (0.010, 0.025, 0.050, "EN 13670 Cl.1")},
        "IfcStair":        {"Medium": (0.010, 0.025, 0.050, "ISO 4463-1")},
        "IfcPipeSegment":  {"Medium": (0.012, 0.025, 0.050, "ISO 4463-1")},
        "IfcDuctSegment":  {"Medium": (0.012, 0.025, 0.050, "ISO 4463-1")},
        "IfcFlowFitting":  {"Medium": (0.012, 0.025, 0.050, "ISO 4463-1")},
        "IfcDoor":         {"Low":    (0.015, 0.030, 0.050, "ISO 4463-1")},
        "IfcWindow":       {"Low":    (0.015, 0.030, 0.050, "ISO 4463-1")},
        "IfcFlowTerminal": {"Low":    (0.015, 0.030, 0.050, "ISO 4463-1")},
    },
    # Medical (hospital, clinic): EN 13670 Cl.1 + EN 1992-1-1 (critical occupancy)
    "MedicalBuilding": {
        "IfcWall":         {"High":   (0.008, 0.020, 0.040, "EN 13670 Cl.1 + EN 1992-1-1")},
        "IfcSlab":         {"High":   (0.008, 0.020, 0.040, "EN 13670 Cl.1 + EN 1992-1-1")},
        "IfcColumn":       {"High":   (0.008, 0.015, 0.030, "EN 13670 Cl.1")},
        "IfcBeam":         {"High":   (0.008, 0.015, 0.030, "EN 13670 Cl.1")},
        "IfcMember":       {"High":   (0.008, 0.015, 0.030, "EN 13670 Cl.1")},
        "IfcPlate":        {"High":   (0.008, 0.015, 0.030, "EN 13670 Cl.1")},
        "IfcFooting":      {"High":   (0.010, 0.025, 0.050, "EN 13670 Cl.1")},
        "IfcPile":         {"High":   (0.010, 0.025, 0.050, "EN 13670 Cl.1")},
        "IfcPipeSegment":  {"Medium": (0.010, 0.025, 0.050, "ISO 4463-1")},
        "IfcDuctSegment":  {"Medium": (0.010, 0.025, 0.050, "ISO 4463-1")},
        "IfcFlowFitting":  {"Medium": (0.010, 0.025, 0.050, "ISO 4463-1")},
        "IfcDoor":         {"Low":    (0.010, 0.025, 0.040, "ISO 4463-1")},
        "IfcWindow":       {"Low":    (0.010, 0.025, 0.040, "ISO 4463-1")},
        "IfcStair":        {"Medium": (0.008, 0.020, 0.040, "ISO 4463-1")},
        "IfcFlowTerminal": {"Low":    (0.010, 0.025, 0.040, "ISO 4463-1")},
    },
    # Residential: EN 13670 Cl.1 (standard tolerance)
    "ResidentialBuilding": {
        "IfcWall":         {"High":   (0.010, 0.020, 0.040, "EN 13670 Cl.1")},
        "IfcSlab":         {"High":   (0.010, 0.020, 0.040, "EN 13670 Cl.1")},
        "IfcColumn":       {"High":   (0.008, 0.015, 0.030, "EN 13670 Cl.1")},
        "IfcBeam":         {"High":   (0.008, 0.015, 0.030, "EN 13670 Cl.1")},
        "IfcMember":       {"High":   (0.008, 0.015, 0.030, "EN 13670 Cl.1")},
        "IfcPlate":        {"High":   (0.008, 0.015, 0.030, "EN 13670 Cl.1")},
        "IfcFooting":      {"High":   (0.010, 0.025, 0.050, "EN 13670 Cl.1")},
        "IfcPile":         {"High":   (0.010, 0.025, 0.050, "EN 13670 Cl.1")},
        "IfcPipeSegment":  {"Medium": (0.012, 0.025, 0.050, "ISO 4463-1")},
        "IfcDuctSegment":  {"Medium": (0.012, 0.025, 0.050, "ISO 4463-1")},
        "IfcFlowFitting":  {"Medium": (0.012, 0.025, 0.050, "ISO 4463-1")},
        "IfcDoor":         {"Low":    (0.015, 0.030, 0.050, "ISO 4463-1")},
        "IfcWindow":       {"Low":    (0.015, 0.030, 0.050, "ISO 4463-1")},
        "IfcFlowTerminal": {"Low":    (0.015, 0.030, 0.050, "ISO 4463-1")},
    },
    # Commercial (office, hotel, retail): EN 13670 Cl.1
    "CommercialBuilding": {
        "IfcWall":         {"High":   (0.010, 0.020, 0.040, "EN 13670 Cl.1")},
        "IfcSlab":         {"High":   (0.010, 0.020, 0.040, "EN 13670 Cl.1")},
        "IfcColumn":       {"High":   (0.008, 0.015, 0.030, "EN 13670 Cl.1")},
        "IfcBeam":         {"High":   (0.008, 0.015, 0.030, "EN 13670 Cl.1")},
        "IfcMember":       {"High":   (0.008, 0.015, 0.030, "EN 13670 Cl.1")},
        "IfcPlate":        {"High":   (0.008, 0.015, 0.030, "EN 13670 Cl.1")},
        "IfcFooting":      {"High":   (0.010, 0.025, 0.050, "EN 13670 Cl.1")},
        "IfcPile":         {"High":   (0.010, 0.025, 0.050, "EN 13670 Cl.1")},
        "IfcPipeSegment":  {"Medium": (0.012, 0.025, 0.050, "ISO 4463-1")},
        "IfcDuctSegment":  {"Medium": (0.012, 0.025, 0.050, "ISO 4463-1")},
        "IfcFlowFitting":  {"Medium": (0.012, 0.025, 0.050, "ISO 4463-1")},
        "IfcDoor":         {"Low":    (0.015, 0.030, 0.050, "ISO 4463-1")},
        "IfcWindow":       {"Low":    (0.015, 0.030, 0.050, "ISO 4463-1")},
        "IfcFlowTerminal": {"Low":    (0.015, 0.030, 0.050, "ISO 4463-1")},
    },
}

_IMPORTANCE: Dict[str, str] = {
    # Structural / load-bearing — High
    "IfcWall": "High", "IfcSlab": "High", "IfcColumn": "High", "IfcBeam": "High",
    "IfcMember": "High", "IfcPlate": "High", "IfcFooting": "High", "IfcPile": "High",
    # Semi-structural / circulation — Medium
    "IfcRoof": "Medium", "IfcStair": "Medium", "IfcRamp": "Medium",
    "IfcPipeSegment": "Medium", "IfcDuctSegment": "Medium", "IfcFlowFitting": "Medium",
    # Architectural / finishes / MEP terminals — Low
    "IfcDoor": "Low", "IfcWindow": "Low", "IfcCovering": "Low",
    "IfcFlowTerminal": "Low",
}

_DEFAULT_TOL = (0.012, 0.025, 0.050, "ISO 4463-1")


def _get_tolerance(context: str, ifc_type: str, importance: str
                   ) -> Tuple[float, float, float, str]:
    ctx = _TOLERANCES.get(context, _TOLERANCES["CommercialBuilding"])
    type_tols = ctx.get(ifc_type)
    if type_tols is None:
        return _DEFAULT_TOL
    return type_tols.get(importance, type_tols.get("High", _DEFAULT_TOL))


def classify_severity(mean_dev: float, t_c: float, t_m: float, t_mo: float) -> str:
    if mean_dev <= t_c:  return "Compliant"
    if mean_dev <= t_m:  return "Minor"
    if mean_dev <= t_mo: return "Moderate"
    return "Major"


# ── Knowledge graph builder ────────────────────────────────────────────────────

def _notebook_tolerance(ifc_type_short: str, context: str = "HydraulicStructure"):
    """Return (t_c, t_m, t_mo, std_ref), importance — context-aware lookup.
    Kept for backward compat; callers should prefer _get_tolerance() directly."""
    importance = _IMPORTANCE.get(f"Ifc{ifc_type_short}", "Low")
    tol = _get_tolerance(context, f"Ifc{ifc_type_short}", importance)
    return tol, importance

def _short_type(ifc_type: str) -> str:
    """'IfcWall' → 'Wall', 'Wall' → 'Wall'."""
    return ifc_type.replace("Ifc", "")


def build_construction_kg(
    deviation_results: Dict[str, Dict],
    ifc_file,
    project_name: str,
    context: Optional[str] = None,
    elements_data: Optional[list] = None,
) -> Tuple[object, str, str]:
    """
    Build RDF knowledge graph matching reference notebook structure exactly.
    Returns (rdflib.Graph, pre_formatted_csv_string, context_str).
    The graph has:
      - Severity individuals with rdfs:label and severityLevel int
      - ToleranceProfile individuals per (type, importance)
      - Per-element (BIM.BuildingElement) nodes + Assessment nodes
      - Storey nodes with rdfs:label
      - All triples needed for the 4 reference SPARQL queries
    """
    import urllib.parse
    from collections import Counter
    from rdflib import Graph, Namespace, Literal, RDF, RDFS, OWL, XSD

    if context is None:
        context = detect_context(ifc_file, project_name)

    BIM  = Namespace("http://bim.thesis.org/ontology/bim#")
    DEV  = Namespace("http://bim.thesis.org/ontology/deviation#")
    PROJ_NS = Namespace("http://bim.thesis.org/project/reservoir#")
    IFC  = Namespace("http://standards.buildingsmart.org/IFC/DEF/IFC4x3/")

    g = Graph()
    g.bind("bim",  BIM)
    g.bind("dev",  DEV)
    g.bind("proj", PROJ_NS)
    g.bind("ifc",  IFC)
    g.bind("xsd",  XSD)
    g.bind("rdfs", RDFS)
    g.bind("owl",  OWL)

    # ── OWL class declarations ─────────────────────────────────────
    for cls in [DEV.DeviationReport, DEV.ElementAssessment, DEV.Severity,
                DEV.ToleranceProfile, DEV.StructuralImportance,
                BIM.BuildingElement, BIM.Storey, BIM.Project]:
        g.add((cls, RDF.type, OWL.Class))

    # ── Severity individuals ───────────────────────────────────────
    for level, sev in enumerate(["Compliant", "Minor", "Moderate", "Major"]):
        node = DEV[sev]
        g.add((node, RDF.type,          DEV.Severity))
        g.add((node, RDFS.label,        Literal(sev, lang="en")))
        g.add((node, DEV.severityLevel, Literal(level, datatype=XSD.integer)))

    # ── Structural importance individuals ──────────────────────────
    for imp in ["High", "Medium", "Low"]:
        node = DEV[f"Importance_{imp}"]
        g.add((node, RDF.type,   DEV.StructuralImportance))
        g.add((node, RDFS.label, Literal(imp, lang="en")))

    # ── ToleranceProfile individuals — built from context-aware _TOLERANCES ──
    # Collect unique (short_type, importance) combos present in this context
    profile_uris = {}
    ctx_tols = _TOLERANCES.get(context, _TOLERANCES["CommercialBuilding"])
    for ifc_type_full, imp_map in ctx_tols.items():
        ifc_type_short = ifc_type_full.replace("Ifc", "")
        for importance, (t_c, t_m, t_mo, std_ref) in imp_map.items():
            prof_uri = DEV[f"ToleranceProfile_{ifc_type_short}_{importance}"]
            g.add((prof_uri, RDF.type,                 DEV.ToleranceProfile))
            g.add((prof_uri, RDFS.label,               Literal(f"{ifc_type_short} ({importance}) – {std_ref}", lang="en")))
            g.add((prof_uri, DEV.appliesToIfcType,      Literal(ifc_type_short)))
            g.add((prof_uri, DEV.structuralImportance,  DEV[f"Importance_{importance}"]))
            g.add((prof_uri, DEV.toleranceCompliant,    Literal(t_c,   datatype=XSD.double)))
            g.add((prof_uri, DEV.toleranceMinor,        Literal(t_m,   datatype=XSD.double)))
            g.add((prof_uri, DEV.toleranceModerate,     Literal(t_mo,  datatype=XSD.double)))
            g.add((prof_uri, DEV.standardReference,     Literal(std_ref)))
            g.add((prof_uri, DEV.projectContext,        Literal(context)))
            profile_uris[(ifc_type_short, importance)] = prof_uri

    # ── Project node ───────────────────────────────────────────────
    safe_proj = "".join(c if c.isalnum() else "_" for c in project_name)
    project_uri = PROJ_NS[f"Project_{safe_proj}"]
    g.add((project_uri, RDF.type,        BIM.Project))
    g.add((project_uri, RDFS.label,      Literal(project_name, lang="en")))
    g.add((project_uri, DEV.projectType, Literal(context)))

    # ── Report node ────────────────────────────────────────────────
    report_uri = PROJ_NS[f"DeviationReport_{safe_proj}"]
    g.add((report_uri, RDF.type,       DEV.DeviationReport))
    g.add((report_uri, RDFS.label,     Literal(f"{project_name} Scan-to-BIM Deviation Report", lang="en")))
    g.add((report_uri, DEV.forProject, project_uri))

    def _safe_uri(text: str) -> str:
        return urllib.parse.quote(str(text).strip().replace(" ", "_"), safe="")

    # ── Per-element assessment triples ─────────────────────────────
    storey_nodes: Dict = {}
    element_severities: Dict[str, str] = {}

    for guid, stats in deviation_results.items():
        ifc_type     = stats.get("ifc_type", "IfcElement")
        ifc_short    = _short_type(ifc_type)
        name         = stats.get("name", "")
        storey_label = stats.get("storey", "Unknown")
        mean_dev     = stats["mean"]
        max_dev      = stats["max"]
        rmse         = stats.get("rmse", 0.0)
        n_pts        = stats.get("num_points", stats.get("num_segmented", 0))
        n_cand       = stats.get("num_candidates", 0)
        n_norm       = stats.get("num_after_normal_filter", 0)
        elem_id      = stats.get("element_id", "")
        family_name  = stats.get("family_name", "")

        (t_c, t_m, t_mo, std_ref), importance = _notebook_tolerance(ifc_short, context)
        severity = classify_severity(mean_dev, t_c, t_m, t_mo)
        element_severities[guid] = severity

        # Write computed CQA classification back into the stats dict so the
        # /api/export annotation step can read it.
        stats["severity"]             = severity
        stats["importance"]           = importance
        stats["tolerance_compliant"]  = float(t_c)
        stats["tolerance_minor"]      = float(t_m)
        stats["tolerance_moderate"]   = float(t_mo)
        stats["standard_ref"]         = str(std_ref)

        # Storey node
        storey_uri = PROJ_NS[_safe_uri(f"Storey_{storey_label}")]
        if storey_uri not in storey_nodes:
            g.add((storey_uri, RDF.type,   BIM.Storey))
            g.add((storey_uri, RDFS.label, Literal(storey_label, lang="en")))
            storey_nodes[storey_uri] = True

        # BIM element node
        elem_uri = PROJ_NS[f"Element_{_safe_uri(guid)}"]
        g.add((elem_uri, RDF.type,                 BIM.BuildingElement))
        g.add((elem_uri, RDF.type,                 IFC[ifc_short]))
        g.add((elem_uri, DEV.globalId,             Literal(guid)))
        g.add((elem_uri, DEV.elementId,            Literal(elem_id)))
        g.add((elem_uri, DEV.familyName,           Literal(family_name)))
        g.add((elem_uri, DEV.customName,           Literal(name)))
        g.add((elem_uri, DEV.ifcType,              Literal(ifc_short)))
        g.add((elem_uri, DEV.onStorey,             storey_uri))
        g.add((elem_uri, DEV.structuralImportance, DEV[f"Importance_{importance}"]))
        prof_uri = profile_uris.get(
            (ifc_short, importance),
            DEV[f"ToleranceProfile_{ifc_short}_{importance}"])
        g.add((elem_uri, DEV.hasToleranceProfile,  prof_uri))

        # Assessment node
        assess_uri = PROJ_NS[f"Assessment_{_safe_uri(guid)}"]
        g.add((assess_uri, RDF.type,                      DEV.ElementAssessment))
        g.add((assess_uri, DEV.assessesElement,            elem_uri))
        g.add((assess_uri, DEV.usesToleranceProfile,       prof_uri))
        g.add((assess_uri, DEV.hasSeverity,                DEV[severity]))
        g.add((assess_uri, DEV.meanDeviation,              Literal(round(mean_dev, 6), datatype=XSD.double)))
        g.add((assess_uri, DEV.maxDeviation,               Literal(round(max_dev,  6), datatype=XSD.double)))
        g.add((assess_uri, DEV.stdDeviation,               Literal(round(stats.get("std", 0.0), 6), datatype=XSD.double)))
        g.add((assess_uri, DEV.rmse,                       Literal(round(rmse,     6), datatype=XSD.double)))
        g.add((assess_uri, DEV.numPoints,                  Literal(n_pts,             datatype=XSD.integer)))
        g.add((assess_uri, DEV.numCandidates,              Literal(n_cand,            datatype=XSD.integer)))
        g.add((assess_uri, DEV.numAfterNormal,             Literal(n_norm,            datatype=XSD.integer)))
        g.add((assess_uri, DEV.isCompliant,                Literal(severity == "Compliant", datatype=XSD.boolean)))
        g.add((assess_uri, DEV.appliedToleranceCompliant,  Literal(t_c,  datatype=XSD.double)))
        g.add((assess_uri, DEV.appliedToleranceMinor,      Literal(t_m,  datatype=XSD.double)))
        g.add((assess_uri, DEV.appliedToleranceModerate,   Literal(t_mo, datatype=XSD.double)))
        g.add((report_uri, DEV.hasAssessment,              assess_uri))

    # ── Build pre-formatted CSV ────────────────────────────────────
    csv_str = _build_kg_csv(g, element_severities, deviation_results, context, elements_data)

    return g, csv_str, context


def _build_kg_csv(g, element_severities: Dict[str, str],
                  deviation_results: Dict[str, Dict], context: str,
                  elements_data: Optional[list] = None) -> str:
    """
    Run the 4 SPARQL queries over g and produce the Q1-Q4 sectioned CSV
    matching reference notebook Step 7 exactly.
    """
    from collections import Counter

    PREFIXES = """
        PREFIX bim:  <http://bim.thesis.org/ontology/bim#>
        PREFIX dev:  <http://bim.thesis.org/ontology/deviation#>
        PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
        PREFIX xsd:  <http://www.w3.org/2001/XMLSchema#>
    """

    buf = io.StringIO()
    writer = csv.writer(buf)

    ttl_triples = len(g)
    writer.writerow([f"RDF knowledge graph exported : element_deviation_Reservoir_knowledge_graph.ttl"])
    writer.writerow([f"Total RDF triples            : {ttl_triples}"])
    writer.writerow([f"Project context detected     : {context}"])
    writer.writerow([])

    # ── Q1: Non-compliant elements ─────────────────────────────────
    writer.writerow(["=== Q1: Non-compliant elements (context-specific tolerance applied per element) ==="])
    writer.writerow(["Element", "Type", "Importance", "Severity", "Mean(m)", "Tol.(m)", "Standard", "Storey"])
    q1 = PREFIXES + """
SELECT ?name ?ifcType ?importance ?severity ?mean ?appliedTol ?standard ?storey
WHERE {
    ?a  a                               dev:ElementAssessment ;
        dev:assessesElement             ?e ;
        dev:hasSeverity                 ?sev ;
        dev:meanDeviation               ?mean ;
        dev:isCompliant                 false ;
        dev:appliedToleranceCompliant   ?appliedTol ;
        dev:usesToleranceProfile        ?prof .
    ?e  dev:customName           ?name ;
        dev:ifcType              ?ifcType ;
        dev:structuralImportance ?imp_uri ;
        dev:onStorey             ?storey_uri .
    ?storey_uri rdfs:label  ?storey .
    ?sev        rdfs:label  ?severity .
    ?imp_uri    rdfs:label  ?importance .
    ?prof       dev:standardReference ?standard .
}
ORDER BY DESC(?mean)
"""
    q1_rows = list(g.query(q1))
    if q1_rows:
        for row in q1_rows:
            writer.writerow([
                str(row.name), str(row.ifcType), str(row.importance),
                str(row.severity), round(float(row.mean), 4),
                round(float(row.appliedTol), 4), str(row.standard), str(row.storey),
            ])
    else:
        writer.writerow(["All elements are compliant under their applicable tolerance profiles."])
    writer.writerow([])

    # ── Q2: Severity distribution ──────────────────────────────────
    writer.writerow(["=== Q2: Severity distribution by element type and structural importance ==="])
    writer.writerow(["IFC Type", "Importance", "Severity", "Count"])
    q2 = PREFIXES + """
SELECT ?ifcType ?importance ?severity (COUNT(?a) AS ?count)
WHERE {
    ?a  a                          dev:ElementAssessment ;
        dev:assessesElement        ?e ;
        dev:hasSeverity            ?sev .
    ?e  dev:ifcType                ?ifcType ;
        dev:structuralImportance   ?imp_uri .
    ?sev     rdfs:label ?severity .
    ?imp_uri rdfs:label ?importance .
}
GROUP BY ?ifcType ?importance ?severity
ORDER BY ?importance ?ifcType ?severity
"""
    for row in g.query(q2):
        writer.writerow([str(row.ifcType), str(row.importance),
                         str(row.severity), int(row["count"])])
    writer.writerow([])

    # ── Q3: Storey summary ─────────────────────────────────────────
    writer.writerow(["=== Q3: Storey summary — measured deviation vs. context-derived tolerance ==="])
    writer.writerow(["Storey", "N", "Avg Dev(m)", "Max Dev(m)", "Avg Tol(m)"])
    q3 = PREFIXES + """
SELECT ?storey
       (COUNT(?a)    AS ?count)
       (AVG(?mean)   AS ?avg_mean)
       (MAX(?mean)   AS ?max_mean)
       (AVG(?tol)    AS ?avg_applied_tol)
WHERE {
    ?a  a                             dev:ElementAssessment ;
        dev:assessesElement           ?e ;
        dev:meanDeviation             ?mean ;
        dev:appliedToleranceCompliant ?tol .
    ?e  dev:onStorey    ?storey_uri .
    ?storey_uri rdfs:label  ?storey .
}
GROUP BY ?storey
ORDER BY ?storey
"""
    q3_rows = list(g.query(q3))
    if q3_rows:
        for row in q3_rows:
            writer.writerow([
                str(row.storey), int(row["count"]),
                round(float(row.avg_mean), 4),
                round(float(row.max_mean), 4),
                round(float(row.avg_applied_tol), 4),
            ])
    else:
        writer.writerow(["No storey assignments found in the model."])
    writer.writerow([])

    # ── Q4: High-importance critical elements ──────────────────────
    writer.writerow(["=== Q4: High-importance elements with critical deviation (Moderate or Major) ==="])
    writer.writerow(["Element", "Type", "Severity", "Mean(m)", "Max(m)", "RMSE", "Standard", "Storey"])
    q4 = PREFIXES + """
SELECT ?name ?ifcType ?severity ?mean ?max ?rmse ?standard ?storey
WHERE {
    ?a  a                          dev:ElementAssessment ;
        dev:assessesElement        ?e ;
        dev:hasSeverity            ?sev ;
        dev:meanDeviation          ?mean ;
        dev:maxDeviation           ?max ;
        dev:rmse                   ?rmse ;
        dev:usesToleranceProfile   ?prof .
    ?e  dev:customName             ?name ;
        dev:ifcType                ?ifcType ;
        dev:structuralImportance   dev:Importance_High ;
        dev:onStorey               ?storey_uri .
    ?storey_uri rdfs:label         ?storey .
    ?sev        rdfs:label         ?severity .
    ?sev        dev:severityLevel  ?sev_level .
    ?prof       dev:standardReference ?standard .
    FILTER (?sev_level >= 2)
}
ORDER BY DESC(?mean)
"""
    q4_rows = list(g.query(q4))
    if q4_rows:
        for row in q4_rows:
            writer.writerow([
                str(row.name), str(row.ifcType), str(row.severity),
                round(float(row.mean), 4), round(float(row.max), 4),
                round(float(row.rmse), 4), str(row.standard), str(row.storey),
            ])
    else:
        writer.writerow(["No high-importance elements with critical deviations found."])
    writer.writerow([])

    # ── Summary ────────────────────────────────────────────────────
    writer.writerow(["=== Knowledge Graph Assessment Summary (Context-Aware Tolerance) ==="])
    total = len(deviation_results)
    writer.writerow([f"Total elements assessed : {total}"])
    writer.writerow([])

    sev_counts = Counter(element_severities.values())
    writer.writerow(["Severity", "Count", "Percentage"])
    for sev in ["Compliant", "Minor", "Moderate", "Major"]:
        count = sev_counts.get(sev, 0)
        pct = round(100.0 * count / total, 1) if total else 0.0
        writer.writerow([sev, count, f"{pct}%"])
    writer.writerow([])

    writer.writerow(["Tolerance profiles applied (project context: " + context + "):"])
    writer.writerow(["Type", "Importance", "Compliant(mm)", "Minor(mm)", "Moderate(mm)", "Standard"])
    ctx_tols = _TOLERANCES.get(context, _TOLERANCES["CommercialBuilding"])
    for ifc_type_full, imp_map in ctx_tols.items():
        ifc_type_short = ifc_type_full.replace("Ifc", "")
        for importance, (t_c, t_m, t_mo, std_ref) in imp_map.items():
            writer.writerow([
                ifc_type_short, importance,
                f"<={t_c*1000:.0f}", f"<={t_m*1000:.0f}", f"<={t_mo*1000:.0f}", std_ref,
            ])

    # ── Trailing: not-segmented / not-found elements ───────────────────────────
    if elements_data is not None:
        writer.writerow([])
        skipped = []
        for ed in elements_data:
            gid = ed.get("guid", "")
            if not gid or gid in deviation_results:
                continue
            _v = ed.get("verts")
            if _v is None or (hasattr(_v, "__len__") and len(_v) == 0):
                reason = "No Geometry / Tessellation Failed"
            else:
                reason = "Not Segmented — No Scan Coverage"
            skipped.append([
                ed.get("name", ""),
                ed.get("ifc_type", ""),
                ed.get("storey", "Unknown"),
                gid,
                reason,
            ])
        if skipped:
            writer.writerow(["=== Not Segmented / Not Found Elements ({} total) ===".format(len(skipped))])
            writer.writerow(["CustomName", "IFCType", "Storey", "GlobalId", "Reason"])
            for r in skipped:
                writer.writerow(r)

    return buf.getvalue()



