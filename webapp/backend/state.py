import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import numpy as np


@dataclass
class JobStatus:
    step: str = "idle"          # idle | loading | registration | cqa | mqa | export | done | error
    progress: float = 0.0       # 0.0 – 1.0
    message: str = "Ready"
    error: Optional[str] = None


class AppState:

    def __init__(self) -> None:
        self._lock = threading.Lock()

        # ── uploaded files ──────────────────────────────────────────────
        self.scan_path: Optional[str] = None
        self.ifc_path: Optional[str] = None

        # ── loaded geometry ──────────────────────────────────────────────
        self.scan_pcd = None          # open3d.PointCloud  (raw, full resolution)
        self.scan_colors_raw: Optional[np.ndarray] = None  # (N,3) float32 original RGB or None
        self.ifc_file = None          # ifcopenshell.file
        self.ifc_project_name: str = "Project"
        self.elements_data: List[Dict] = []  # [{element, guid, name, ifc_type, pcd, mesh, verts, faces}, ...]
        self.bim_pcd = None           # combined BIM PointCloud (for registration)

        # ── registration ─────────────────────────────────────────────────
        self.registered_pcd = None    # aligned scan PointCloud
        self.registration_T: Optional[np.ndarray] = None   # (4,4) float64
        self.registration_fitness: float = 0.0
        self.registration_rmse: float = 0.0
        self.registration_method: str = ""

        # ── CQA results ──────────────────────────────────────────────────
        self.cqa_results: Dict[str, Dict] = {}  # guid → stats
        self.cqa_colored_pcd = None   # jet-colored scan PointCloud
        self.cqa_max_dev: float = 0.0
        self.cqa_element_colors: Dict[str, List[float]] = {}  # guid → [R,G,B]
        # ── MQA results ──────────────────────────────────────────────────
        self.mqa_results: Dict[str, Dict] = {}  # guid → quality dict
        self.mqa_element_colors: Dict[str, List[float]] = {}  # guid → [R,G,B]
        self.mqa_colored_pcd = None   # jet-coloured scan PointCloud (MQA dev)

        # Tracks which analysis the user most recently completed so the export
        # endpoint knows which file set to bundle.  "cqa" | "mqa" | None
        self._last_completed: Optional[str] = None

        # ── CQA working storage (populated by main.py _cqa_sync) ────────
        self._cqa_kg         = None
        self._cqa_csv_str:   str = ""      # Q1-Q4 sectioned CSV (Construction_quality_*.csv)
        self._cqa_deviation_csv: str = ""  # per-element deviation CSV (element_deviation_*.csv)
        self._cqa_context:   str = ""
        self._cqa_ttl:       str = ""

        # ── MQA working storage (populated by main.py _mqa_sync) ────────
        self._mqa_kg           = None
        self._mqa_summary_ttl:  str = ""
        self._mqa_dev_results:  Dict = {}
        self._mqa_kg_csv_str:   str = ""       # BIM_quality_*_knowledge_graph.csv
        self._mqa_dev_csv:      str = ""       # element_deviation_*_graph_segmentation_registration_Revit_API.csv
        self._mqa_mesh_data: Optional[Dict] = None  

        # ── export ───────────────────────────────────────────────────────
        self.export_zip_path: Optional[str] = None

        self._cqa_exported: bool = False
        self._mqa_exported: bool = False

        # ── job status ───────────────────────────────────────────────────
        self.status = JobStatus()

    # ── thread-safe helpers ────────────────────────────────────────────

    def update_status(self, step: str, progress: float, message: str) -> None:
        with self._lock:
            self.status.step = step
            self.status.progress = round(progress, 3)
            self.status.message = message
            self.status.error = None

    def set_done(self, message: str = "Done") -> None:
        with self._lock:
            self.status.step = "done"
            self.status.progress = 1.0
            self.status.message = message
            self.status.error = None

    def set_error(self, error: str) -> None:
        with self._lock:
            self.status.step = "error"
            self.status.error = error
            self.status.message = f"Error: {error}"

    def is_busy(self) -> bool:
        with self._lock:
            return self.status.step not in ("idle", "done", "error")

    def reset(self) -> None:
        """Reset all computed state so the user can start a new session."""
        with self._lock:
            self.scan_path            = None
            self.ifc_path             = None
            self.scan_pcd             = None
            self.scan_colors_raw      = None
            self.ifc_file             = None
            self.ifc_project_name     = "Project"
            self.elements_data        = []
            self.bim_pcd              = None
            self.registered_pcd       = None
            self.registration_T       = None
            self.registration_fitness = 0.0
            self.registration_rmse    = 0.0
            self.registration_method  = ""
            self.cqa_results          = {}
            self.cqa_colored_pcd      = None
            self.cqa_max_dev          = 0.0
            self.cqa_element_colors   = {}
            self.mqa_results          = {}
            self.mqa_element_colors   = {}
            self.mqa_colored_pcd      = None
            self._last_completed      = None
            self._cqa_kg              = None
            self._cqa_csv_str         = ""
            self._cqa_deviation_csv   = ""
            self._cqa_context         = ""
            self._cqa_ttl             = ""
            self._mqa_kg          = None
            self._mqa_summary_ttl  = ""
            self._mqa_dev_results     = {}
            self._mqa_kg_csv_str      = ""
            self._mqa_dev_csv         = ""
            self._mqa_mesh_data       = None
            self.export_zip_path      = None
            self._cqa_exported        = False
            self._mqa_exported        = False
            self.status               = JobStatus()


# Module-level singleton used by all route handlers and services.
app_state = AppState()
