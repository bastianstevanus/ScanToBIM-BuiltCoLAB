# Scan-to-BIM Quality Assessment — Web Application

A standalone local web application for point-cloud-to-BIM registration,
construction quality assessment (CQA), and model quality assessment (MQA).

---

## Quick Start

### Option A — Python (recommended for development)

**Install dependencies** (once):
```cmd
pip install -r requirements.txt
```

**Launch:**
```cmd
python start.py
```
The app opens automatically at http://localhost:8000

---

### Option B — Docker

```cmd
docker compose up --build
```
Then navigate to http://localhost:8000

---

## Workflow

| Step | Button | Description |
|------|--------|-------------|
| 1 | **Import Files** | Upload `.e57` scan + `.ifc` model |
| 2 | **Registration** | FGR + ICP + 8-candidate yaw search |
| 3 | **Construction Quality** | Segment → deviation → jet-coloured scan + tolerance KG |
| 4 | **Model Quality** | Segment → deviation → BIM element colours + IFC info audit KG |
| 5 | **Export Results** | ZIP with annotated IFC, CSVs, TTL graphs |

---

## File Layout

```
webapp/
├── backend/
│   ├── main.py                   FastAPI app + all API endpoints
│   ├── state.py                  Thread-safe singleton session state
│   └── services/
│       ├── e57_loader.py         E57 → Open3D PointCloud (pye57)
│       ├── ifc_service.py        IFC → tessellated meshes + per-element PCDs
│       ├── registration_service.py  FGR + ICP + yaw search (voxel=0.1 m)
│       ├── segmentation_service.py  Graph-based segmentation (KNN + scipy)
│       ├── deviation_service.py     Point-to-plane deviation + jet colourmap
│       ├── construction_kg.py    CQA: tolerance standards + RDF + SPARQL
│       ├── model_kg.py           MQA: 15 IFC quality checks + RDF + SPARQL
│       └── export_service.py     ZIP: annotated IFC + CSVs + TTLs
├── frontend/
│   ├── index.html                Bootstrap 5 UI
│   ├── css/style.css             Dark-theme styles
│   └── js/
│       ├── main.js               Button handlers + status polling
│       ├── viewer3d.js           Three.js r152 3D viewer (ESM)
│       └── api.js                Fetch wrappers + base64 binary helpers
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
└── start.py                      Launcher (opens browser automatically)
```

---

## Algorithm Details

### Registration (Step 2)
- Voxel size: 0.1 m
- Preprocessing: voxel downsample → normals → FPFH features
- Candidates: FGR+ICP, ICP-seed, Yaw(45°,90°,135°,180°,225°,270°,315°)+ICP
- Selection: best by `fitness − 0.35 × inlier_RMSE`

### Construction Quality Assessment (Step 3)
- **Segmentation**: AABB crop → seed proximity → normal filter (|cos θ|≥0.50) → KNN-graph largest connected component (k=15, edge≤0.15 m). Planar IFC types (Wall/Slab/Roof/Covering) additionally apply RANSAC refinement (dist=25 mm, plane-normal≥0.60) to strip neighbouring-surface leakage. Round columns, beams, doors, windows etc. use graph + normal only. Cascading fallbacks guarantee every element with nearby scan points is segmented.
- **Deviation**: point-to-plane via KD-tree nearest neighbour
- **Tolerances**: auto-detects context (HydraulicStructure / Industrial / Residential / Commercial) → EN 13670 / ISO 4463-1
- **Severity**: Compliant / Minor / Moderate / Major
- **KG namespace**: `bim.thesis.org/ontology/deviation#`

### Model Quality Assessment (Step 4)
- **Segmentation**: same pipeline as CQA (seed_radius=0.40 m, normal_angle_threshold=0.50, knn_k=15, edge_max_dist=0.15 m, min_points=25)
- **15 IFC quality checks** across 4 dimensions: SemanticCorrectness, PropertyCompleteness, GeometricConsistency, SchemaCompliance
- **Quality levels**: Excellent / Good / Fair / Poor
- **KG namespace**: `bim.thesis.org/ontology/quality#`

### Export (Step 5)
Contents of the ZIP file:
- `{project}_deviation_schedule.csv`   — per-element deviation statistics
- `{project}_quality_summary.csv`      — per-element MQA quality levels
- `{project}_quality_issues_detail.csv`— all 15-type IFC issues per element
- `{project}_construction_kg.ttl`      — CQA RDF knowledge graph (Turtle)
- `{project}_model_quality_kg.ttl`     — MQA RDF knowledge graph (Turtle)
- `{project}_annotated.ifc`            — IFC with `Pset_ScanToBIM_DeviationAssessment` + `Pset_ScanToBIM_ModelQuality` added to every element

---

## Dependencies

| Package | Purpose |
|---------|---------|
| fastapi, uvicorn | HTTP server |
| open3d 0.18 | Point cloud processing, FGR, ICP |
| ifcopenshell ≥0.7 | IFC loading and geometry tessellation |
| pye57 | E57 file reading with pose transform |
| rdflib ≥7.0 | RDF knowledge graph + SPARQL |
| scipy | Graph segmentation (connected_components) |
| matplotlib | Jet colourmap |
| Three.js r152 (CDN) | 3D viewer in browser |
| Bootstrap 5 (CDN) | UI framework |
