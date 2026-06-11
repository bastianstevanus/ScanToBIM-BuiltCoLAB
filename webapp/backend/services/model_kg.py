from __future__ import annotations

import csv
import io
import logging
import urllib.parse
from collections import Counter
from typing import Dict, List, Optional, Tuple

import numpy as np

log = logging.getLogger(__name__)

# ── Issue registry — matches UC notebook exactly ───────────────────────────────
# (code, severity, dimension, description)
ISSUE_REGISTRY: Dict[str, Tuple[str, str, str]] = {
    # Semantic Correctness
    "NO_GLOBAL_ID":        ("SemanticCorrectness",  "Critical", "Element has no valid GlobalId"),
    "NO_NAME":             ("SemanticCorrectness",  "Minor",    "Element has no Name attribute"),
    "NO_STOREY":           ("SemanticCorrectness",  "Major",    "Element not assigned to any IfcBuildingStorey"),
    "INVALID_OBJECTTYPE":  ("SemanticCorrectness",  "Minor",    "ObjectType is null or placeholder"),
    "PREDEFINED_NOTDEF":   ("SemanticCorrectness",  "Minor",    "PredefinedType is NOTDEFINED or USERDEFINED"),
    # Property Completeness
    "NO_MATERIAL":         ("PropertyCompleteness", "Major",    "No material association found"),
    "NO_PSET":             ("PropertyCompleteness", "Major",    "No property sets (Psets) assigned"),
    "NO_LOAD_BEARING":     ("PropertyCompleteness", "Minor",    "LoadBearing property not defined"),
    "NO_IS_EXTERNAL":      ("PropertyCompleteness", "Minor",    "IsExternal property not defined"),
    "NO_TAG":              ("PropertyCompleteness", "Minor",    "Element has no Tag/ElementId"),
    "NO_DESCRIPTION":      ("PropertyCompleteness", "Minor",    "Element has no Description"),
    # Geometric Consistency
    "NO_GEOMETRY":         ("GeometricConsistency", "Critical", "Element has no computable geometry"),
    "ZERO_VOLUME":         ("GeometricConsistency", "Major",    "Element geometry has zero or negligible volume"),
    "DEGENERATE_BBOX":     ("GeometricConsistency", "Major",    "Element bounding box is degenerate (< 0.1 mm in any axis)"),
    # Schema Compliance
    "NO_OWNER_HISTORY":    ("SchemaCompliance",     "Major",    "Element missing OwnerHistory — no change traceability"),
    "DUPLICATE_GUID":      ("SchemaCompliance",     "Critical", "GlobalId is duplicated in the model"),
}

SEVERITY_ORDER = {"Critical": 3, "Major": 2, "Minor": 1}

# Quality level thresholds — matches UC notebook
QUALITY_THRESHOLDS = [
    ("Excellent", 0, 0),
    ("Good",      0, 2),
    ("Fair",      1, 5),
    ("Poor",      99, 999),
]

_QUALITY_COLORS = {
    "Excellent": [0.20, 0.80, 0.20],
    "Good":      [0.60, 0.90, 0.30],
    "Fair":      [1.00, 0.80, 0.00],
    "Poor":      [0.90, 0.20, 0.20],
}


def _quality_level(issues: List[Tuple[str, str]]) -> str:
    """issues = list of (issue_id, severity)."""
    n_critical = sum(1 for _, s in issues if s == "Critical")
    n_total    = len(issues)
    for level, max_crit, max_total in QUALITY_THRESHOLDS:
        if n_critical <= max_crit and n_total <= max_total:
            return level
    return "Poor"


def _safe_uri(text: str) -> str:
    return urllib.parse.quote(str(text).strip().replace(" ", "_"), safe="")


# ── Material helper ────────────────────────────────────────────────────────────

def _get_material(element) -> Optional[str]:
    """Return comma-joined material name string, or None."""
    try:
        for rel in (getattr(element, "HasAssociations", []) or []):
            if not rel.is_a("IfcRelAssociatesMaterial"):
                continue
            mat = rel.RelatingMaterial
            if mat is None:
                continue
            if mat.is_a("IfcMaterial"):
                return str(mat.Name)
            if mat.is_a("IfcMaterialList"):
                names = [str(m.Name) for m in (mat.Materials or []) if m]
                return ", ".join(names) if names else None
            if mat.is_a("IfcMaterialLayerSetUsage"):
                ls = getattr(mat, "ForLayerSet", None)
                if ls:
                    names = [str(l.Material.Name) for l in (ls.MaterialLayers or []) if l.Material]
                    return ", ".join(names) if names else None
            if mat.is_a("IfcMaterialProfileSetUsage"):
                ps = getattr(mat, "ForProfileSet", None)
                if ps:
                    names = [str(p.Material.Name) for p in (ps.MaterialProfiles or []) if p.Material]
                    return ", ".join(names) if names else None
            if mat.is_a("IfcMaterialConstituentSet"):
                names = [str(c.Material.Name) for c in (mat.MaterialConstituents or []) if c.Material]
                return ", ".join(names) if names else None
    except Exception:
        pass
    return None


def _get_bool_prop(psets: dict, prop_name: str) -> Optional[bool]:
    """Return True/False/None for a named boolean property across all Psets."""
    for props in psets.values():
        if not isinstance(props, dict):
            continue
        for key, val in props.items():
            if key.strip().lower() == prop_name.lower():
                if isinstance(val, bool):
                    return val
                if isinstance(val, str):
                    return val.lower() in ("true", "yes", "1")
    return None


def _get_element_storey_gid(element) -> Optional[str]:
    """Walk IFC containment graph upward to find parent IfcBuildingStorey GlobalId."""
    relations = list(getattr(element, "ContainedInStructure", []) or [])
    relations += list(getattr(element, "ReferencedInStructures", []) or [])
    for rel in relations:
        spatial = getattr(rel, "RelatingStructure", None)
        if spatial is None:
            continue
        visited, current = set(), spatial
        while current is not None:
            cid = getattr(current, "id", lambda: None)()
            if cid in visited:
                break
            visited.add(cid)
            if current.is_a("IfcBuildingStorey"):
                return getattr(current, "GlobalId", None)
            rels = getattr(current, "Decomposes", None)
            current = rels[0].RelatingObject if rels else None
    return None


# ── Per-element audit ──────────────────────────────────────────────────────────

def audit_all_elements(elements_data: list, ifc_file) -> Dict[str, Dict]:

    try:
        import ifcopenshell.util.element as ifc_element_utils
    except ImportError:
        ifc_element_utils = None

    # Detect duplicate GlobalIds
    all_guids = [ed.get("guid", "") for ed in elements_data]
    duplicate_guids = {g for g, c in Counter(all_guids).items() if c > 1 and g}

    results: Dict[str, Dict] = {}

    for ed in elements_data:
        element   = ed.get("element")
        guid      = ed.get("guid", "")
        if not guid or str(guid).strip() in ("", "$", "None"):
            guid = f"_missing_{id(element)}"

        issues: List[Tuple[str, str]] = []

        # ── 1. Semantic Correctness ────────────────────────────────────────────
        raw_guid = getattr(element, "GlobalId", None) if element else None
        if not raw_guid or str(raw_guid).strip() in ("", "$", "None"):
            issues.append(("NO_GLOBAL_ID", "Critical"))

        name = getattr(element, "Name", None) if element else None
        if not name or str(name).strip() in ("", "$", "None"):
            issues.append(("NO_NAME", "Minor"))

        storey_gid = _get_element_storey_gid(element) if element else None
        if storey_gid is None:
            issues.append(("NO_STOREY", "Major"))

        obj_type = getattr(element, "ObjectType", None) if element else None
        if not obj_type or str(obj_type).strip() in ("", "$", "None", "NOTDEFINED"):
            issues.append(("INVALID_OBJECTTYPE", "Minor"))

        predef = getattr(element, "PredefinedType", None) if element else None
        if predef and str(predef) in ("NOTDEFINED", "USERDEFINED"):
            issues.append(("PREDEFINED_NOTDEF", "Minor"))

        # ── 2. Property Completeness ───────────────────────────────────────────
        material = _get_material(element) if element else None
        if material is None:
            issues.append(("NO_MATERIAL", "Major"))

        try:
            psets = ifc_element_utils.get_psets(element) if ifc_element_utils and element else {}
        except Exception:
            psets = {}
        if not psets:
            issues.append(("NO_PSET", "Major"))

        if _get_bool_prop(psets, "LoadBearing") is None:
            issues.append(("NO_LOAD_BEARING", "Minor"))
        if _get_bool_prop(psets, "IsExternal") is None:
            issues.append(("NO_IS_EXTERNAL", "Minor"))

        tag = getattr(element, "Tag", None) if element else None
        if not tag or str(tag).strip() in ("", "$", "None"):
            issues.append(("NO_TAG", "Minor"))

        desc = getattr(element, "Description", None) if element else None
        if not desc or str(desc).strip() in ("", "$", "None"):
            issues.append(("NO_DESCRIPTION", "Minor"))

        # ── 3. Geometric Consistency ───────────────────────────────────────────
        verts = ed.get("verts")
        vol   = ed.get("volume", 0.0)
        has_geom = verts is not None and len(verts) > 0

        if not has_geom:
            issues.append(("NO_GEOMETRY", "Critical"))
        elif vol < 1e-6:
            issues.append(("ZERO_VOLUME", "Major"))
        elif has_geom and len(verts) > 2:
            mins = verts.min(axis=0)
            maxs = verts.max(axis=0)
            extents = maxs - mins
            if any(float(e) < 1e-4 for e in extents):
                issues.append(("DEGENERATE_BBOX", "Major"))

        # ── 4. Schema Compliance ───────────────────────────────────────────────
        if element and getattr(element, "OwnerHistory", None) is None:
            issues.append(("NO_OWNER_HISTORY", "Major"))
        if raw_guid and raw_guid in duplicate_guids:
            issues.append(("DUPLICATE_GUID", "Critical"))

        quality_level = _quality_level(issues)

        results[guid] = {
            "ifc_type":      ed.get("ifc_type", "Unknown"),
            # Prefer storey-based CustomName (assigned at IFC-load time);
            # fall back to the raw IFC Name attribute if not set.
            "name":          str(ed.get("name") or name or ""),
            "material":      material or "",
            "storey_gid":    storey_gid or "",
            "storey":        ed.get("storey", "Unknown"),
            "has_geometry":  has_geom,
            "volume_m3":     float(vol),
            "quality":       quality_level,      # kept for backward compat
            "quality_level": quality_level,
            "issues":        issues,              # list of (code, severity)
            "n_issues":      len(issues),
            "n_critical":    sum(1 for _, s in issues if s == "Critical"),
            "n_major":       sum(1 for _, s in issues if s == "Major"),
            "n_minor":       sum(1 for _, s in issues if s == "Minor"),
            "num_issues":    len(issues),
            "color":         _QUALITY_COLORS[quality_level],
        }

    return results


# ── RDF graph ──────────────────────────────────────────────────────────────────

def build_model_kg(
    mqa_results: Dict[str, Dict],
    ifc_file,
    project_name: str,
    dev_results: Optional[Dict[str, Dict]] = None,
    elements_data: Optional[List[Dict]] = None,
) -> Tuple[object, str]:

    from rdflib import Graph, Namespace, Literal, RDF, RDFS, OWL, XSD

    BIM  = Namespace("http://bim.thesis.org/ontology/bim#")
    QUAL = Namespace("http://bim.thesis.org/ontology/quality#")
    safe = "".join(c if c.isalnum() else "_" for c in project_name)
    PROJ = Namespace(f"http://bim.thesis.org/project/{safe}#")
    IFC  = Namespace("http://standards.buildingsmart.org/IFC/DEF/IFC4x3/")

    g = Graph()
    g.bind("bim",  BIM)
    g.bind("qual", QUAL)
    g.bind("proj", PROJ)
    g.bind("ifc",  IFC)
    g.bind("xsd",  XSD)
    g.bind("rdfs", RDFS)
    g.bind("owl",  OWL)

    # ── Ontology class declarations ────────────────────────────────────────────
    for cls in [QUAL.BIMQualityReport, QUAL.ElementQualityAssessment,
                QUAL.QualityIssue,     QUAL.QualityIssueType,
                QUAL.QualityLevel,     QUAL.QualityDimension,
                BIM.BuildingElement,   BIM.Storey, BIM.Project]:
        g.add((cls, RDF.type, OWL.Class))

    # ── Quality level individuals ──────────────────────────────────────────────
    for lvl_idx, lvl in enumerate(["Excellent", "Good", "Fair", "Poor"]):
        node = QUAL[lvl]
        g.add((node, RDF.type,        QUAL.QualityLevel))
        g.add((node, RDFS.label,      Literal(lvl, lang="en")))
        g.add((node, QUAL.levelIndex, Literal(lvl_idx, datatype=XSD.integer)))

    # ── Quality dimension individuals ──────────────────────────────────────────
    for dim in ["SemanticCorrectness", "PropertyCompleteness",
                "GeometricConsistency", "SchemaCompliance"]:
        node = QUAL[dim]
        g.add((node, RDF.type,   QUAL.QualityDimension))
        g.add((node, RDFS.label, Literal(dim, lang="en")))

    # ── Issue type catalogue ───────────────────────────────────────────────────
    for issue_id, (dim, sev, desc) in ISSUE_REGISTRY.items():
        node = QUAL[f"Issue_{issue_id}"]
        g.add((node, RDF.type,           QUAL.QualityIssueType))
        g.add((node, RDFS.label,         Literal(issue_id, lang="en")))
        g.add((node, RDFS.comment,       Literal(desc, lang="en")))
        g.add((node, QUAL.dimension,     QUAL[dim]))
        g.add((node, QUAL.severity,      Literal(sev)))
        g.add((node, QUAL.severityLevel, Literal(SEVERITY_ORDER[sev], datatype=XSD.integer)))

    # ── Project node ───────────────────────────────────────────────────────────
    project_uri = PROJ.Project
    g.add((project_uri, RDF.type,         BIM.Project))
    g.add((project_uri, RDFS.label,       Literal(f"{project_name} BIM Model", lang="en")))
    g.add((project_uri, QUAL.standard,    Literal("ISO 19650 / IFC4x3")))

    # ── Report root node ───────────────────────────────────────────────────────
    report_uri = PROJ.BIMQualityReport
    g.add((report_uri, RDF.type,                   QUAL.BIMQualityReport))
    g.add((report_uri, RDFS.label,                 Literal(f"{project_name} BIM Quality Assessment", lang="en")))
    g.add((report_uri, QUAL.forProject,            project_uri))
    g.add((report_uri, QUAL.totalElementsAssessed, Literal(len(mqa_results), datatype=XSD.integer)))

    # ── Storey nodes ───────────────────────────────────────────────────────────
    storey_uri_map: Dict[str, object] = {}
    try:
        for ifc_storey in ifc_file.by_type("IfcBuildingStorey"):
            sgid       = getattr(ifc_storey, "GlobalId", None)
            sname      = str(getattr(ifc_storey, "Name", None) or f"Storey_{sgid}")
            selev      = getattr(ifc_storey, "Elevation", None)
            storey_uri = PROJ[f"Storey_{_safe_uri(sgid or sname)}"]
            g.add((storey_uri, RDF.type,   BIM.Storey))
            g.add((storey_uri, RDFS.label, Literal(sname, lang="en")))
            if selev is not None:
                try:
                    g.add((storey_uri, QUAL.elevation, Literal(float(selev), datatype=XSD.double)))
                except (TypeError, ValueError):
                    pass
            if sgid:
                storey_uri_map[sgid] = storey_uri
    except Exception:
        pass

    # ── Element + assessment triples ───────────────────────────────────────────
    for guid, data in mqa_results.items():
        elem_uri   = PROJ[f"Element_{_safe_uri(guid)}"]
        assess_uri = PROJ[f"Assessment_{_safe_uri(guid)}"]

        # BIM element node
        g.add((elem_uri, RDF.type,         BIM.BuildingElement))
        g.add((elem_uri, RDF.type,         IFC[data["ifc_type"]]))
        g.add((elem_uri, QUAL.globalId,    Literal(guid)))
        g.add((elem_uri, QUAL.ifcType,     Literal(data["ifc_type"])))
        g.add((elem_uri, QUAL.name,        Literal(data["name"])))
        g.add((elem_uri, QUAL.material,    Literal(data["material"])))
        g.add((elem_uri, QUAL.volumeM3,    Literal(round(data["volume_m3"], 6), datatype=XSD.double)))
        g.add((elem_uri, QUAL.hasGeometry, Literal(data["has_geometry"], datatype=XSD.boolean)))
        sgid = data.get("storey_gid", "")
        if sgid and sgid in storey_uri_map:
            g.add((elem_uri, QUAL.onStorey, storey_uri_map[sgid]))

        # Assessment node
        g.add((assess_uri, RDF.type,              QUAL.ElementQualityAssessment))
        g.add((assess_uri, QUAL.assessesElement,  elem_uri))
        g.add((assess_uri, QUAL.hasQualityLevel,  QUAL[data["quality_level"]]))
        g.add((assess_uri, QUAL.totalIssues,      Literal(data["n_issues"],    datatype=XSD.integer)))
        g.add((assess_uri, QUAL.criticalIssues,   Literal(data["n_critical"],  datatype=XSD.integer)))
        g.add((assess_uri, QUAL.majorIssues,      Literal(data["n_major"],     datatype=XSD.integer)))
        g.add((assess_uri, QUAL.minorIssues,      Literal(data["n_minor"],     datatype=XSD.integer)))
        g.add((assess_uri, QUAL.isModelCompliant,
               Literal(data["quality_level"] in ("Excellent", "Good"), datatype=XSD.boolean)))
        g.add((report_uri, QUAL.hasAssessment, assess_uri))

        # Individual issue instances
        for i, (issue_id, sev) in enumerate(data["issues"]):
            issue_uri = PROJ[f"Issue_{_safe_uri(guid)}_{i}"]
            dim_info  = ISSUE_REGISTRY.get(issue_id, ("Unknown", sev, issue_id))
            dim       = dim_info[0]
            desc      = dim_info[2]
            g.add((issue_uri, RDF.type,              QUAL.QualityIssue))
            g.add((issue_uri, QUAL.issueType,        QUAL[f"Issue_{issue_id}"]))
            g.add((issue_uri, QUAL.foundOnElement,   elem_uri))
            g.add((issue_uri, QUAL.issueDimension,   QUAL[dim]))
            g.add((issue_uri, QUAL.issueSeverity,    Literal(sev)))
            g.add((issue_uri, QUAL.issueDescription, Literal(desc, lang="en")))
            g.add((assess_uri, QUAL.hasIssue,        issue_uri))

    kg_csv = _build_kg_csv(g, mqa_results, dev_results, elements_data)
    return g, kg_csv


# ── CSV from SPARQL Q1-Q4 ──────────────────────────────────────────────────────

def _build_kg_csv(g,
                  mqa_results: Optional[Dict[str, Dict]] = None,
                  dev_results: Optional[Dict[str, Dict]] = None,
                  elements_data: Optional[List[Dict]] = None) -> str:

    from rdflib import Graph

    PREFIXES = """
    PREFIX bim:  <http://bim.thesis.org/ontology/bim#>
    PREFIX qual: <http://bim.thesis.org/ontology/quality#>
    PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
    PREFIX xsd:  <http://www.w3.org/2001/XMLSchema#>
    """

    buf = io.StringIO()
    w   = csv.writer(buf)

    mqa_results = mqa_results or {}
    dev_results = dev_results or {}

    # ── Q0: Per-element BIM quality + deviation summary ────────────────────────
    if mqa_results:
        w.writerow(["Q0: Per-element BIM quality + deviation summary"])
        w.writerow([
            "CustomName", "IFCType", "Storey", "QualityLevel",
            "Critical", "Major", "Minor",
            "LOD_Level", "Tolerance_mm",
            "Mean_Deviation_mm", "Max_Deviation_mm",
            "Quality_Score", "Grade", "Pass_Fail", "Severity",
        ])
        for guid, m in mqa_results.items():
            d = dev_results.get(guid, {})
            mean_mm = round(float(d.get("mean", 0.0) or 0.0) * 1000, 2) if d else ""
            max_mm  = round(float(d.get("max",  0.0) or 0.0) * 1000, 2) if d else ""
            w.writerow([
                m.get("name", ""),
                m.get("ifc_type", ""),
                m.get("storey", "") or "Unknown",
                m.get("quality_level") or m.get("quality", ""),
                m.get("n_critical", 0),
                m.get("n_major", 0),
                m.get("n_minor", 0),
                d.get("lod_label", ""),
                d.get("tolerance_mm", ""),
                mean_mm,
                max_mm,
                d.get("quality_score", ""),
                d.get("grade", ""),
                d.get("pass_fail_status", ""),
                d.get("severity", ""),
            ])
        w.writerow([])

    # ── Q1: Non-compliant elements (storey from mqa_results, never blank) ─────
    w.writerow(["Q1: Non-compliant BIM elements (Fair / Poor quality — issue counts)"])
    w.writerow(["CustomName", "IFCType", "Storey",
                "QualityLevel", "Critical", "Major", "Minor", "NumIssues"])
    nc_rows = []
    for guid, m in mqa_results.items():
        lvl = m.get("quality_level") or m.get("quality", "")
        if lvl in ("Fair", "Poor"):
            nc_rows.append((
                m.get("name", ""),
                m.get("ifc_type", ""),
                m.get("storey", "") or "Unknown",
                lvl,
                int(m.get("n_critical", 0) or 0),
                int(m.get("n_major", 0) or 0),
                int(m.get("n_minor", 0) or 0),
                int(m.get("n_issues", m.get("num_issues", 0)) or 0),
            ))
    nc_rows.sort(key=lambda r: (-r[4], -r[5], -r[6]))
    if nc_rows:
        for r in nc_rows:
            w.writerow(r)
    else:
        w.writerow(["All elements are compliant under their BIM quality criteria."])
    w.writerow([])

    # ── Q2: Quality distribution by type ─────────────────────────────────────
    w.writerow(["Q2: Quality level distribution by IFC element type"])
    w.writerow(["IFCType", "QualityLevel", "Count"])
    q2 = PREFIXES + """
SELECT ?ifcType ?qualityLevel (COUNT(?a) AS ?count)
WHERE {
    ?a  a                    qual:ElementQualityAssessment ;
        qual:assessesElement ?e ;
        qual:hasQualityLevel ?ql .
    ?e  qual:ifcType         ?ifcType .
    ?ql rdfs:label           ?qualityLevel .
}
GROUP BY ?ifcType ?qualityLevel
ORDER BY ?ifcType ?qualityLevel
"""
    for row in g.query(q2):
        w.writerow([str(row.ifcType), str(row.qualityLevel), int(row["count"])])
    w.writerow([])

    # ── Q3: Most frequent issues ──────────────────────────────────────────────
    w.writerow(["Q3: Most frequent quality issues (ranked by occurrence)"])
    w.writerow(["IssueType", "Dimension", "Severity", "Count"])
    q3 = PREFIXES + """
SELECT ?issueType ?dimension ?severity (COUNT(?i) AS ?count)
WHERE {
    ?i  a                   qual:QualityIssue ;
        qual:issueType      ?it ;
        qual:issueDimension ?dim_uri ;
        qual:issueSeverity  ?severity .
    ?it rdfs:label          ?issueType .
    ?dim_uri rdfs:label     ?dimension .
}
GROUP BY ?issueType ?dimension ?severity
ORDER BY DESC(?count)
"""
    for row in g.query(q3):
        w.writerow([str(row.issueType), str(row.dimension),
                    str(row.severity), int(row["count"])])
    w.writerow([])

    # ── Q4: Storey-level audit (storey from mqa_results, not SPARQL) ──────────
    w.writerow(["Q4: BIM quality audit by storey (ISO 19650)"])
    w.writerow(["Storey", "TotalElements", "TotalCritical", "TotalMajor", "AvgIssuesPerElement"])
    by_storey: Dict[str, List[Dict]] = {}
    for m in mqa_results.values():
        key = m.get("storey", "") or "Unknown"
        by_storey.setdefault(key, []).append(m)
    for storey in sorted(by_storey.keys()):
        items = by_storey[storey]
        total = len(items)
        tot_c = sum(int(x.get("n_critical", 0) or 0) for x in items)
        tot_m = sum(int(x.get("n_major", 0)    or 0) for x in items)
        avg_n = (sum(int(x.get("n_issues", x.get("num_issues", 0)) or 0) for x in items)
                 / total) if total else 0.0
        w.writerow([storey, total, tot_c, tot_m, f"{avg_n:.1f}"])
    w.writerow([])

    # ── Summary block ─────────────────────────────────────────────────────────
    if mqa_results:
        total = len(mqa_results)
        level_counts = {"Excellent": 0, "Good": 0, "Fair": 0, "Poor": 0}
        dim_counts: Dict[str, int] = {}
        for v in mqa_results.values():
            lvl = v.get("quality_level") or v.get("quality", "Poor")
            if lvl in level_counts:
                level_counts[lvl] += 1
            for issue_id, _sev in v.get("issues", []):
                dim = ISSUE_REGISTRY.get(issue_id, ("Unknown", "", ""))[0]
                dim_counts[dim] = dim_counts.get(dim, 0) + 1

        w.writerow(["=== BIM Model Quality Assessment Summary (ISO 19650 / IFC4x3) ==="])
        w.writerow(["Note: QualityLevel = BIM information quality (Excellent/Good/Fair/Poor)",
                    "based on count + severity of audit issues (NO_NAME, NO_MATERIAL, etc.)",
                    "It is INDEPENDENT of geometric Grade (A/B/C/D) which is deviation-based."])
        w.writerow([f"Total elements assessed : {total}"])
        w.writerow([])
        w.writerow(["Quality Level", "Count", "Percentage"])
        for lvl in ("Excellent", "Good", "Fair", "Poor"):
            cnt = level_counts[lvl]
            pct = (cnt / total * 100.0) if total else 0.0
            w.writerow([lvl, cnt, f"{pct:.2f}%"])
        w.writerow([])
        w.writerow(["Issue occurrences by quality dimension:"])
        w.writerow(["Dimension", "Issue Count"])
        for dim in sorted(dim_counts.keys(), key=lambda d: -dim_counts[d]):
            w.writerow([dim, dim_counts[dim]])
        w.writerow([])

    # ── Trailing: not-segmented / not-found elements ──────────────────────────
    if elements_data is not None:
        skipped = []
        for ed in elements_data:
            gid = ed.get("guid", "")
            if not gid:
                continue
            if gid in dev_results:
                continue
            verts = ed.get("verts")
            # numpy-safe emptiness check (cannot use `not verts` on ndarray)
            if verts is None or (hasattr(verts, "__len__") and len(verts) == 0):
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
            w.writerow(["=== Not Segmented / Not Found Elements ({} total) ===".format(len(skipped))])
            w.writerow(["CustomName", "IFCType", "Storey", "GlobalId", "Reason"])
            for r in skipped:
                w.writerow(r)
            w.writerow([])

    return buf.getvalue()



