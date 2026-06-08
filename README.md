# Scan-to-BIM Quality Assessment Tool

A standalone web application for **Construction Quality Assessment (CQA)** and **Model Quality Assessment (MQA)** using 3D point clouds and IFC BIM models.

Developed as part of a Master's thesis research at the University of Cantabria, in collaboration with **BuiltCoLAB**.

---

## Features

- **Import**: Load E57/PLY point cloud scans and IFC BIM models
- **Registration**: Automatic FGR + ICP scan-to-BIM alignment
- **Construction Quality Assessment (CQA)**: Graph-based element segmentation + point-to-plane deviation analysis
- **Model Quality Assessment (MQA)**: BIM information quality audit with LOD grading
- **3D Viewer**: Interactive Three.js visualization of scan, BIM, and deviation colormaps
- **Export**: ZIP package with deviation CSVs, annotated IFC, knowledge graph (Turtle/RDF), and PLY point cloud

---

## Quick Start (Standalone EXE)

1. Download and unzip `ScanToBIM.zip`
2. Double-click `ScanToBIM.exe`
3. A browser opens automatically at `http://127.0.0.1:8000`
4. Click **Import Files** and load your `.e57` / `.ply` scan and `.ifc` model

---

## Development Setup

### Prerequisites

- Python 3.11
- Windows 10/11 (recommended; Linux/macOS may work with minor adjustments)

### Install dependencies

```bash
python -m venv .venv
.venv\Scripts\activate        # Windows
pip install -r requirements.txt
```

### Run locally

```bash
python webapp/_launcher.py
```

Browser opens at `http://127.0.0.1:8000`

### Build standalone EXE

```powershell
.\build_exe.ps1
```

Output will be in `dist\ScanToBIM\`. Zip and distribute that folder.

---

## Project Structure

```
ScanToBIM-BuiltCoLAB/
├── webapp/
│   ├── backend/
│   │   ├── main.py              # FastAPI application
│   │   ├── state.py             # Shared application state
│   │   └── services/
│   │       ├── e57_loader.py         # E57/PLY point cloud loading
│   │       ├── ifc_service.py        # IFC parsing and tessellation
│   │       ├── registration_service.py  # FGR + ICP registration
│   │       ├── segmentation_service.py  # Graph-based segmentation
│   │       ├── deviation_service.py     # Point-to-plane deviation
│   │       ├── construction_kg.py       # CQA knowledge graph
│   │       ├── model_kg.py              # MQA knowledge graph
│   │       ├── mqa_assessment.py        # LOD quality grading
│   │       └── export_service.py        # ZIP export packaging
│   ├── frontend/
│   │   ├── index.html
│   │   ├── css/style.css
│   │   └── js/
│   │       ├── main.js          # UI logic
│   │       ├── api.js           # Backend API calls
│   │       └── viewer3d.js      # Three.js 3D viewer
│   ├── _launcher.py             # Entry point (dev + EXE)
│   └── start.py                 # Docker / server start
├── scan2bim/                    # Core Python library
├── ScanToBIM.spec               # PyInstaller build spec
├── build_exe.ps1                # Build script
├── requirements.txt
└── LICENSE
```

---

## Supported File Formats

| Input | Formats |
|-------|---------|
| Point Cloud | `.e57`, `.ply`, `.pcd`, `.xyz`, `.pts` |
| BIM Model   | `.ifc` (IFC2x3, IFC4, IFC4x3) |

---

## Export Outputs

| File | Description |
|------|-------------|
| `*_CQA_deviation.csv` | Per-element deviation measurements |
| `*_CQA_knowledge_graph.csv` | Construction quality knowledge graph (CSV) |
| `*_construction_kg.ttl` | RDF/Turtle knowledge graph |
| `*_annotated.ifc` | IFC with `Pset_DeviationSchedule` property sets |
| `*_CQA_scan.ply` | Jet-colored point cloud (deviation map) |
| `*_MQA_deviation.csv` | MQA per-element deviation + LOD quality |
| `*_MQA_knowledge_graph.csv` | Model quality knowledge graph (CSV) |

---

## License

MIT License — see [LICENSE](LICENSE) for details.

---

## Citation / Acknowledgement

If you use this tool in research, please acknowledge:

> Situmorang, B.S. (2026). *Scan-to-BIM Quality Assessment Tool*. BuiltCoLAB collaboration, University of Porto.
