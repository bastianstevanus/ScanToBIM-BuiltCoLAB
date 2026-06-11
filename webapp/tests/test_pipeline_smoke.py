import sys
import os
import types
import unittest

# Make "backend" importable without installing the package

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# 1.  _severity_to_status  (in main.py  AND  export_service.py)

class TestSeverityToStatus(unittest.TestCase):

    def _status(self, severity: str, stats: dict):
        """Call both independent copies and assert they agree."""
        from backend.main import _severity_to_status as main_fn
        from backend.services.export_service import _severity_to_status as exp_fn
        r1 = main_fn(severity, stats)
        r2 = exp_fn(severity, stats)
        self.assertEqual(r1, r2, "main.py and export_service.py disagree")
        return r1

    def test_no_candidates_gives_no_valid_geometry(self):
        stats = {"num_candidates": 0, "num_after_normal_filter": 80, "num_segmented": 120}
        self.assertEqual(self._status("compliant", stats), "Need Validation (No Candidates)")

    def test_no_normal_filter_gives_no_valid_geometry(self):
        stats = {"num_candidates": 200, "num_after_normal_filter": 0, "num_segmented": 100}
        self.assertEqual(self._status("compliant", stats), "Need Validation (Normal Filter)")

    def test_too_few_segmented_gives_no_valid_geometry(self):
        stats = {"num_candidates": 200, "num_after_normal_filter": 80, "num_segmented": 5}
        self.assertEqual(self._status("compliant", stats), "Need Validation (Low Coverage)")

    def test_valid_segmentation_compliant(self):
        stats = {"num_candidates": 500, "num_after_normal_filter": 400, "num_segmented": 350}
        self.assertEqual(self._status("compliant", stats), "Valid")

    def test_valid_segmentation_minor(self):
        stats = {"num_candidates": 500, "num_after_normal_filter": 400, "num_segmented": 350}
        self.assertEqual(self._status("minor", stats), "Valid")

    def test_valid_segmentation_moderate(self):
        stats = {"num_candidates": 500, "num_after_normal_filter": 400, "num_segmented": 350}
        self.assertEqual(self._status("moderate", stats), "Valid")

    def test_valid_segmentation_major(self):
        stats = {"num_candidates": 500, "num_after_normal_filter": 400, "num_segmented": 350}
        self.assertEqual(self._status("major", stats), "Valid")

    def test_unknown_severity_is_no_valid_geometry(self):
        """Unknown severity with good segmentation → Valid (status only tracks segmentation)."""
        stats = {"num_candidates": 500, "num_after_normal_filter": 400, "num_segmented": 350}
        self.assertEqual(self._status("??unknown??", stats), "Valid")

    def test_no_stats_dict_still_works(self):
        """Calling without stats (legacy call-site fallback) must not crash."""
        from backend.main import _severity_to_status
        result = _severity_to_status("compliant")
        self.assertEqual(result, "Valid")

    def test_boundary_segmented_exactly_25(self):
        """Exactly 25 segmented points = borderline VALID (>= 25 passes)."""
        stats = {"num_candidates": 200, "num_after_normal_filter": 100, "num_segmented": 25}
        self.assertEqual(self._status("compliant", stats), "Valid")

    def test_boundary_segmented_24(self):
        """24 segmented points is below min_points=25 → Low Coverage."""
        stats = {"num_candidates": 200, "num_after_normal_filter": 100, "num_segmented": 24}
        self.assertEqual(self._status("compliant", stats), "Need Validation (Low Coverage)")

    def test_fail_reason_less_than_min_points(self):
        """fail_reason='less_than_min_points' → Need Validation (Less Than Min Points)."""
        stats = {"num_candidates": 165, "num_after_normal_filter": 140,
                 "num_segmented": 165, "num_points": 10,
                 "fail_reason": "less_than_min_points"}
        self.assertEqual(self._status("compliant", stats), "Need Validation (Less Than Min Points)")


# 2.  detect_context  (construction_kg.py)

class TestDetectContext(unittest.TestCase):

    def _detect(self, ifc_mock):
        from backend.services.construction_kg import detect_context
        return detect_context(ifc_mock)

    def _make_ifc(self, names: list[str]):
        """Build a minimal fake IFC object with named entities."""
        class FakeEnt:
            def __init__(self, name):
                self.Name = name
                self.LongName = None
                self.Description = None
                self.ObjectType = None

        class FakeIfc:
            def by_type(self, t):
                if t in ("IfcProject", "IfcSite", "IfcBuilding"):
                    return [FakeEnt(n) for n in names]
                return []

        return FakeIfc()

    def test_hydraulic_keyword_reservoir(self):
        ifc = self._make_ifc(["Reservoir_North"])
        self.assertEqual(self._detect(ifc), "HydraulicStructure")

    def test_hydraulic_keyword_dam(self):
        ifc = self._make_ifc(["Dam_Site_A"])
        self.assertEqual(self._detect(ifc), "HydraulicStructure")

    def test_hydraulic_keyword_tank(self):
        ifc = self._make_ifc(["Water_Tank_B2"])
        self.assertEqual(self._detect(ifc), "HydraulicStructure")

    def test_hydraulic_keyword_pipe(self):
        ifc = self._make_ifc(["Pipeline_System"])
        self.assertEqual(self._detect(ifc), "HydraulicStructure")

    def test_default_no_keywords(self):
        ifc = self._make_ifc(["BuildingXYZ_001"])
        ctx = self._detect(ifc)
        self.assertEqual(ctx, "HydraulicStructure",
                         f"Expected HydraulicStructure default, got {ctx!r}")

    def test_commercial_keyword(self):
        ifc = self._make_ifc(["OfficeTower_CBD"])
        # "office" should map to CommercialBuilding (not default)
        ctx = self._detect(ifc)
        self.assertEqual(ctx, "CommercialBuilding")

    def test_hydraulic_wins_over_generic(self):
        ifc = self._make_ifc(["Reservoir_Office_Complex"])
        self.assertEqual(self._detect(ifc), "HydraulicStructure")

    def test_empty_ifc_defaults_to_hydraulic(self):
        """Completely empty IFC → default HydraulicStructure."""
        class EmptyIfc:
            def by_type(self, t):
                return []
        self.assertEqual(self._detect(EmptyIfc()), "HydraulicStructure")


# 3.  _build_quality_schedule  (export_service.py)

class TestBuildQualitySchedule(unittest.TestCase):

    def _make_mock_ifc(self, schema_name="IFC4"):
        """Return a minimal mock IFC that records created entities."""
        created = []

        class MockEntity:
            def __init__(self, type_name, **kw):
                self.is_a = lambda: type_name
                self.__dict__.update(kw)
                self.GlobalId = kw.get("GlobalId", "MOCK_GUID")

        class MockIfc:
            def create_entity(self, type_name, **kwargs):
                e = MockEntity(type_name, **kwargs)
                created.append(e)
                return e

        ifc = MockIfc()
        ifc.schema = schema_name
        return ifc, created

    def _make_element(self, guid, name="Wall"):
        class El:
            GlobalId = guid
            Name = name
        return El()

    def test_creates_work_schedule_entity(self):
        from backend.services.export_service import _build_quality_schedule
        ifc, created = self._make_mock_ifc("IFC4")
        elements = [self._make_element("GUID-001", "Wall-1")]
        deviation_results = {
            "GUID-001": {
                "severity": "minor", "mean": 0.015, "max": 0.025,
                "num_segmented": 80, "num_candidates": 200,
                "num_after_normal_filter": 150,
                "deviation_status": "Need Validation (Minor)",
                "importance": "High", "standard_ref": "EN 13670",
            }
        }
        _build_quality_schedule(ifc, None, elements, deviation_results, {}, "cqa")
        types_created = [e.is_a() for e in created]
        self.assertIn("IfcWorkSchedule", types_created)
        self.assertIn("IfcTask", types_created)

    def test_schedule_name_is_schedule_quality(self):
        from backend.services.export_service import _build_quality_schedule
        ifc, created = self._make_mock_ifc("IFC4")
        elements = [self._make_element("GUID-002")]
        _build_quality_schedule(ifc, None, elements, {}, {}, "cqa")
        schedules = [e for e in created if e.is_a() == "IfcWorkSchedule"]
        self.assertTrue(any(getattr(s, "Name", "") == "Schedule_Quality"
                            for s in schedules))

    def test_skips_on_ifc2x3(self):
        """IFC2X3 must silently skip without creating any entities."""
        from backend.services.export_service import _build_quality_schedule
        ifc, created = self._make_mock_ifc("IFC2X3")
        elements = [self._make_element("GUID-003")]
        _build_quality_schedule(ifc, None, elements, {}, {}, "cqa")
        self.assertEqual(created, [], "Expected no entities on IFC2X3")

    def test_empty_elements_list(self):
        """Empty annotated_elements must not crash."""
        from backend.services.export_service import _build_quality_schedule
        ifc, created = self._make_mock_ifc("IFC4")
        # Should return immediately without creating anything meaningful
        _build_quality_schedule(ifc, None, [], {}, {}, "cqa")

    def test_none_deviation_results(self):
        """None deviation_results must not crash."""
        from backend.services.export_service import _build_quality_schedule
        ifc, created = self._make_mock_ifc("IFC4")
        elements = [self._make_element("GUID-004")]
        _build_quality_schedule(ifc, None, elements, None, None, "cqa")

    def test_task_count_matches_element_count(self):
        from backend.services.export_service import _build_quality_schedule
        ifc, created = self._make_mock_ifc("IFC4")
        elements = [self._make_element(f"GUID-{i:03d}") for i in range(5)]
        _build_quality_schedule(ifc, None, elements, {}, {}, "cqa")
        tasks = [e for e in created if e.is_a() == "IfcTask"]
        self.assertEqual(len(tasks), 5)



# 4.  Module import sanity  (no circular imports, no missing deps at startup)

class TestImports(unittest.TestCase):
    """All backend modules must import cleanly without side-effects."""

    def test_import_main(self):
        import backend.main  # noqa: F401

    def test_import_export_service(self):
        import backend.services.export_service  # noqa: F401

    def test_import_construction_kg(self):
        import backend.services.construction_kg  # noqa: F401

    def test_import_ifc_service(self):
        import backend.services.ifc_service  # noqa: F401

    def test_import_deviation_service(self):
        import backend.services.deviation_service  # noqa: F401

    def test_import_model_kg(self):
        import backend.services.model_kg  # noqa: F401



# 5.  build_construction_kg  (construction_kg.py)  — quick structural check

class TestBuildConstructionKg(unittest.TestCase):

    def test_returns_csv_string_with_header(self):
        from backend.services.construction_kg import build_construction_kg

        # Minimal fake data matching what cqa_service produces
        dev_results = {
            "GUID-A": {
                "name": "Wall-A", "element_id": "001",
                "ifc_class": "IfcWall", "mean": 0.008, "max": 0.012,
                "std": 0.002, "rmse": 0.009, "num_segmented": 120,
                "num_points": 120, "num_candidates": 300, "num_after_normal_filter": 250,
                "severity": "compliant", "importance": "High",
                "tolerance_compliant": 0.010,
                "tolerance_moderate": 0.020,
                "tolerance_major": 0.040,
                "standard_ref": "EN 13670",
                "deviation_status": "Valid",
                "volume": 1.5,
            }
        }

        class FakeIfc:
            def by_type(self, t):
                return []

        result = build_construction_kg(dev_results, FakeIfc(), "TestProject")
        # build_construction_kg returns (rdflib.Graph, report_str, context_str)
        self.assertIsInstance(result, tuple)
        self.assertEqual(len(result), 3)
        _graph, report, context = result
        self.assertIsInstance(report, str)
        self.assertIn("element", report.lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)


# 5.  MQA quality scoring & severity (mean-only formula, never blank)

class TestMQAQualityScoring(unittest.TestCase):
    def test_quality_score_mean_only_perfect(self):
        from backend.services.mqa_assessment import quality_score
        self.assertEqual(quality_score(0.0, 15.0), 100.0)

    def test_quality_score_mean_at_tolerance_is_zero(self):
        from backend.services.mqa_assessment import quality_score
        self.assertEqual(quality_score(15.0, 15.0), 0.0)

    def test_quality_score_clamped_above_tolerance(self):
        from backend.services.mqa_assessment import quality_score
        self.assertEqual(quality_score(30.0, 15.0), 0.0)

    def test_quality_score_linear(self):
        from backend.services.mqa_assessment import quality_score
        # mean = 50%% of tolerance -> score 50
        self.assertEqual(quality_score(7.5, 15.0), 50.0)

    def test_pass_fail_uses_max_and_rmse_not_just_mean(self):
        from backend.services.mqa_assessment import pass_fail

        self.assertEqual(pass_fail(max_mm=20.0, rmse_mm=5.0, tolerance_mm=15.0), 'FAIL')

        self.assertEqual(pass_fail(max_mm=10.0, rmse_mm=25.0, tolerance_mm=15.0), 'FAIL')

        self.assertEqual(pass_fail(max_mm=10.0, rmse_mm=10.0, tolerance_mm=15.0), 'PASS')

    def test_csv_columns_exact_order(self):
        from backend.services.mqa_assessment import build_mqa_deviation_csv
        dev = {'g1': {
            'element_id': 'E1', 'name': 'Wall 1A', 'family_name': 'Basic',
            'ifc_type': 'IfcWall', 'num_points': 100, 'num_candidates': 120,
            'num_after_normal_filter': 110, 'num_segmented': 105,
            'mean': 0.005, 'max': 0.012, 'std': 0.003, 'rmse': 0.006,
            'volume': 1.23, 'severity': 'Compliant',
        }}
        csv_str = build_mqa_deviation_csv(dev)
        header = csv_str.splitlines()[0]
        self.assertEqual(header,
            'ElementID,CustomName,GlobalId,FamilyName,IFCType,'
            'LOD_Level,Tolerance_mm,'
            'NumPoints,NumCandidates,NumAfterNormalFilter,NumSegmented,'
            'Deviation_Status,'
            'Max_Deviation_m,Std_Deviation_m,RMSE_m,Mean_Deviation_m,'
            'Quality_Score,Grade,Pass_Fail_Status,Severity,'
            'BIM_Volume_m3')

    def test_csv_no_quality_level_consistency_or_risk_index(self):
        from backend.services.mqa_assessment import build_mqa_deviation_csv
        csv_str = build_mqa_deviation_csv({})
        # Removed semantic-noise columns must not appear
        self.assertNotIn('QualityLevel', csv_str)
        self.assertNotIn('Consistency_Flag', csv_str)
        self.assertNotIn('Issue_Risk_Index', csv_str)

    def test_severity_never_blank_in_csv(self):
        from backend.services.mqa_assessment import build_mqa_deviation_csv
        dev = {'g1': {
            'element_id': 'E1', 'name': 'X', 'family_name': '',
            'ifc_type': 'IfcWall', 'num_points': 100, 'num_candidates': 100,
            'num_after_normal_filter': 100, 'num_segmented': 100,
            'mean': 0.005, 'max': 0.010, 'std': 0.002, 'rmse': 0.006,
            'volume': 1.0,
            # No 'severity' key intentionally -> must default to 'Compliant'
        }}
        csv_str = build_mqa_deviation_csv(dev)
        row = csv_str.splitlines()[1]
        self.assertIn('Compliant', row)
        # Severity column must not be empty
        cells = row.split(',')
        self.assertNotEqual(cells[-2].strip(), '')


# 6.  Phantom element rejection (critical correctness bug fix)

class TestPhantomRejection(unittest.TestCase):
    '''An element with no scan points within 100mm of its surface must be
    excluded from results. Phantom segmentation from neighbour surfaces
    should never produce a deviation entry.'''

    def test_phantom_element_excluded_from_results(self):
        import numpy as np
        import open3d as o3d
        from backend.services.deviation_service import compute_all_deviations

        # BIM element at origin (10x10 plane)
        xs = np.linspace(-5, 5, 50)
        ys = np.linspace(-5, 5, 50)
        xx, yy = np.meshgrid(xs, ys)
        bim_pts = np.column_stack([xx.ravel(), yy.ravel(), np.zeros(xx.size)])
        bim_pcd = o3d.geometry.PointCloud()
        bim_pcd.points = o3d.utility.Vector3dVector(bim_pts)

        # Phantom 'scan' points all 200mm away from surface (z=0.20)
        phantom_pts = np.column_stack([
            np.random.uniform(-1, 1, 30),
            np.random.uniform(-1, 1, 30),
            np.full(30, 0.20),  # 200mm -> > _TIGHT_DIST=100mm
        ])
        seg_pcd = o3d.geometry.PointCloud()
        seg_pcd.points = o3d.utility.Vector3dVector(phantom_pts)

        seg_map = {'phantom_guid': seg_pcd}
        elements_data = [{'guid': 'phantom_guid', 'name': 'Phantom', 'family_name': '',
                          'ifc_type': 'IfcWall', 'storey': 'L1', 'element_id': 'PH1',
                          'pcd': bim_pcd, 'volume': 1.0}]
        seg_stats = {'phantom_guid': {'num_candidates': 30,
                                       'num_after_normal_filter': 30,
                                       'num_segmented': 30}}
        results = compute_all_deviations(seg_map, elements_data, seg_stats=seg_stats)
        self.assertNotIn('phantom_guid', results,
                         'Phantom element with all points >100mm from surface MUST be rejected')

    def test_real_element_kept_in_results(self):
        import numpy as np
        import open3d as o3d
        from backend.services.deviation_service import compute_all_deviations

        # BIM element at origin
        xs = np.linspace(-5, 5, 50)
        ys = np.linspace(-5, 5, 50)
        xx, yy = np.meshgrid(xs, ys)
        bim_pts = np.column_stack([xx.ravel(), yy.ravel(), np.zeros(xx.size)])
        bim_pcd = o3d.geometry.PointCloud()
        bim_pcd.points = o3d.utility.Vector3dVector(bim_pts)

        # Real scan: 50 points within 10mm of surface (well within _TIGHT_DIST)
        real_pts = np.column_stack([
            np.random.uniform(-1, 1, 50),
            np.random.uniform(-1, 1, 50),
            np.random.uniform(-0.005, 0.005, 50),  # within 5mm
        ])
        seg_pcd = o3d.geometry.PointCloud()
        seg_pcd.points = o3d.utility.Vector3dVector(real_pts)

        seg_map = {'real_guid': seg_pcd}
        elements_data = [{'guid': 'real_guid', 'name': 'Real', 'family_name': '',
                          'ifc_type': 'IfcWall', 'storey': 'L1', 'element_id': 'R1',
                          'pcd': bim_pcd, 'volume': 1.0}]
        seg_stats = {'real_guid': {'num_candidates': 50,
                                    'num_after_normal_filter': 50,
                                    'num_segmented': 50}}
        results = compute_all_deviations(seg_map, elements_data, seg_stats=seg_stats)
        self.assertIn('real_guid', results,
                      'Element with tight scan coverage MUST be kept')
        self.assertLess(results['real_guid']['mean'], 0.01)


