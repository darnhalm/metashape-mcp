import asyncio
import tempfile
import unittest
from pathlib import Path

from processing_jobs import ProcessingJobs


class FakeBridge:
    def __init__(self, output_root):
        self.output_root = Path(output_root)
        self.calls = []

    async def call(self, method, **parameters):
        self.calls.append((method, parameters))
        if method.endswith("_prepare"):
            result = {"project": "test.psx", "chunk_label": "Source", "frame_index": 0,
                      "total": 3, "fingerprint": "fingerprint"}
            if method == "tie_point_filtering_prepare":
                result.update(filter_previews=[{"selected_fraction": 0.1}],
                              metrics_before={"tie_point_count": 1000},
                              preview_note="independent previews")
            if method == "sensor_calibration_prepare":
                result.update(sensor_before={"key": 4, "type": "Frame", "fixed": False},
                              proposed_changes={"fixed": True, "fixed_params": ["f"]})
            if method == "camera_reference_exif_prepare":
                result.update(metadata_preview={"gps_candidates": 3},
                              references_before={"locations": 0},
                              coordinate_system={"authority": "EPSG::4326", "name": "WGS 84"})
            if method == "raster_products_prepare":
                result["elevation_source"] = "point_cloud"
                result["coordinate_system"] = {"name": "Local Coordinates", "authority": None}
            if method == "point_cloud_prepare":
                result["depth_maps_key"] = 8
                result["depth_maps_label"] = "Depth Maps"
            if method == "point_cloud_cleanup_prepare":
                result.update(
                    point_cloud_before={"key": 12, "point_count": 1000},
                    selection_preview={"selected_points": 100, "selected_fraction": 0.1},
                    warning="copy only")
            if method == "model_import_prepare":
                result.update(
                    source_model={"key": 4, "faces": 100, "textures": 1},
                    coordinate_space="chunk_crs",
                    coordinate_system={"name": "Local Coordinates", "authority": None},
                    chunk_transform_max_delta=0.0,
                    modified_since_export=True,
                    validation_note="checked")
            return result
        if method.endswith("_copy"):
            return {"copy_key": 9, "restore_point": {
                "restore_point_id": "restore-1", "reason": "before:copy", "kind": "psx_metadata"
            }}
        if method == "reconstruction_depth_maps":
            return {"depth_maps_key": 4}
        if method == "reconstruction_model":
            return {"model_key": 5, "face_count": 123}
        if method == "raster_products_dem":
            return {"elevation_key": 6, "elevation_label": "DEM"}
        if method == "raster_products_orthomosaic":
            return {"orthomosaic_key": 7, "orthomosaic_label": "Orthomosaic"}
        if method == "marker_detection_detect":
            return {"marker_count": 4, "added_markers": 3,
                    "added_marker_keys": [10, 11, 12], "projection_count": 9}
        if method == "marker_refinement_refine":
            return {"refined_markers": 2, "projection_count": 6,
                    "changed_projections": 5, "max_shift_px": 0.4}
        if method == "point_cloud_build":
            return {"point_cloud_key": 12, "point_cloud_label": "Point Cloud",
                    "point_count": 54321,
                    "class_counts": [{"class_id": 0, "count": 54321}],
                    "depth_maps_preserved": True}
        if method == "point_cloud_cleanup_apply":
            return {"point_cloud_before": {"key": 12, "point_count": 1000},
                    "point_cloud_after": {"key": 12, "point_count": 900},
                    "removed_points": 100, "removed_fraction": 0.1,
                    "filters_reset": True, "compacted": False}
        if method == "model_import_import":
            return {"source_model_key": 4, "imported_model_key": 10,
                    "coordinate_validation": {"coordinate_match": True},
                    "texture_transfer": {"requested": True, "transferred": True}}
        if method.startswith("tie_point_filtering_pass"):
            index = int(method.removeprefix("tie_point_filtering_pass"))
            return {"pass_index": index, "selected_tie_points": 100 - index * 10,
                    "before": {"tie_point_count": 1000 - index * 100},
                    "after": {"tie_point_count": 900 - index * 100}}
        if method == "sensor_calibration_apply":
            return {"sensor_before": {"key": 4, "fixed": False},
                    "sensor_after": {"key": 4, "fixed": True},
                    "affected_cameras": 3}
        if method == "camera_reference_exif_apply":
            return {"references_before": {"locations": 0},
                    "references_after": {"locations": 3, "locations_with_accuracy": 3},
                    "loaded_locations": 3, "fallback_accuracies": 3,
                    "loaded_rotations": 0, "warnings": []}
        if method in ("texture_export_uv", "texture_export_texture"):
            return {}
        if method == "texture_export_export":
            output = self.output_root / "metashape-obj-test"
            output.mkdir()
            files = []
            for name in ("model.obj", "model.mtl", "model.jpg"):
                path = output / name
                path.write_text(name)
                files.append(str(path))
            return {"model_path": files[0], "output_directory": str(output), "files": files}
        if method == "raster_products_export":
            output = self.output_root / "metashape-raster-test"
            output.mkdir()
            files = []
            for name in ("dem.tif", "orthomosaic.tif"):
                path = output / name
                path.write_text(name)
                files.append(str(path))
            return {"dem_path": files[0], "orthomosaic_path": files[1],
                    "output_directory": str(output), "files": files}
        if method == "restore_point_register_outputs":
            return {"restore_point_id": parameters["restore_point_id"],
                    "generated_outputs": parameters["paths"]}
        raise AssertionError(f"Unexpected method: {method}")


class ProcessingJobTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.bridge = FakeBridge(self.root)
        self.events = []
        self.jobs = ProcessingJobs(
            self.root, self.bridge,
            lambda event, **fields: self.events.append((event, fields)),
        )

    async def asyncTearDown(self):
        await self.jobs.close()
        self.temporary.cleanup()

    async def test_reconstruction_job_and_wait(self):
        plan = await self.jobs.prepare_reconstruction(1, 4, "medium")
        self.jobs.start(plan["job_id"], "reconstruction")
        result = await self.jobs.wait(plan["job_id"], 5)
        self.assertEqual(result["state"], "completed")
        self.assertFalse(result["still_running"])
        self.assertEqual(result["restore_point_id"], "restore-1")
        self.assertEqual(result["assets"]["face_count"], 123)

    async def test_texture_export_registers_generated_directory(self):
        export_root = self.root / "exports"
        export_root.mkdir()
        plan = await self.jobs.prepare_texture_export(1, 1024, str(export_root))
        self.jobs.start(plan["job_id"], "texture_export")
        result = await self.jobs.wait(plan["job_id"], 5)
        self.assertEqual(result["state"], "completed")
        self.assertEqual(result["assets"]["restore_point_id"], "restore-1")
        registered = [call for call in self.bridge.calls
                      if call[0] == "restore_point_register_outputs"]
        self.assertEqual(len(registered), 1)
        self.assertEqual(registered[0][1]["restore_point_id"], "restore-1")

    async def test_raster_products_build_and_register_exports(self):
        export_root = self.root / "exports"
        export_root.mkdir()
        plan = await self.jobs.prepare_raster_products(1, str(export_root), 0, True)
        self.assertEqual(plan["parameters"]["format"], "GeoTIFF")
        self.jobs.start(plan["job_id"], "raster_products")
        result = await self.jobs.wait(plan["job_id"], 5)
        self.assertEqual(result["state"], "completed")
        self.assertEqual(result["assets"]["elevation_key"], 6)
        self.assertEqual(result["assets"]["orthomosaic_key"], 7)
        self.assertEqual(result["assets"]["restore_point_id"], "restore-1")
        methods = [method for method, _ in self.bridge.calls]
        self.assertEqual(methods[-5:], [
            "raster_products_copy", "raster_products_dem",
            "raster_products_orthomosaic", "raster_products_export",
            "restore_point_register_outputs",
        ])

    async def test_marker_detection_uses_copy_and_reports_results(self):
        plan = await self.jobs.prepare_marker_detection(1, "circular_12bit", 50, True)
        self.assertTrue(plan["parameters"]["filter_mask"])
        self.jobs.start(plan["job_id"], "marker_detection")
        result = await self.jobs.wait(plan["job_id"], 5)
        self.assertEqual(result["state"], "completed")
        self.assertEqual(result["assets"]["added_markers"], 3)
        self.assertEqual(result["restore_point_id"], "restore-1")
        methods = [method for method, _ in self.bridge.calls]
        self.assertEqual(methods[-3:], [
            "marker_detection_prepare", "marker_detection_copy", "marker_detection_detect"])

    async def test_marker_refinement_uses_exact_keys_on_copy(self):
        plan = await self.jobs.prepare_marker_refinement(1, [3, 7])
        self.assertEqual(plan["parameters"]["marker_keys"], [3, 7])
        self.jobs.start(plan["job_id"], "marker_refinement")
        result = await self.jobs.wait(plan["job_id"], 5)
        self.assertEqual(result["state"], "completed")
        self.assertEqual(result["assets"]["changed_projections"], 5)
        self.assertEqual(result["restore_point_id"], "restore-1")

    async def test_point_cloud_builds_from_reviewed_depth_maps_on_copy(self):
        plan = await self.jobs.prepare_point_cloud(1, True, True, True, 100, True, 0.1)
        self.assertEqual(plan["parameters"]["depth_maps_key"], 8)
        self.jobs.start(plan["job_id"], "point_cloud")
        result = await self.jobs.wait(plan["job_id"], 5)
        self.assertEqual(result["state"], "completed")
        self.assertEqual(result["assets"]["point_count"], 54321)
        self.assertTrue(result["assets"]["depth_maps_preserved"])
        self.assertEqual(result["restore_point_id"], "restore-1")

    async def test_point_cloud_cleanup_is_previewed_and_applied_on_copy(self):
        plan = await self.jobs.prepare_point_cloud_cleanup(
            1, 12, "confidence", [0], 0, 3, 0.3)
        self.assertEqual(plan["selection_preview"]["selected_points"], 100)
        self.jobs.start(plan["job_id"], "point_cloud_cleanup")
        result = await self.jobs.wait(plan["job_id"], 5)
        self.assertEqual(result["state"], "completed")
        self.assertEqual(result["assets"]["removed_points"], 100)
        self.assertTrue(result["assets"]["filters_reset"])
        methods = [method for method, _ in self.bridge.calls]
        self.assertEqual(methods[-3:], ["point_cloud_cleanup_prepare",
                                        "point_cloud_cleanup_copy",
                                        "point_cloud_cleanup_apply"])

    async def test_model_import_uses_copy_and_records_coordinate_validation(self):
        parameters = {"path": "/tmp/model.obj", "manifest": {}, "fingerprint": {},
                      "format": "obj", "modified_since_export": True,
                      "transfer_texture": True, "texture_size": 4096,
                      "coordinate_tolerance_ratio": 0.001}
        plan = await self.jobs.prepare_model_import(1, parameters)
        self.assertEqual("chunk_crs", plan["coordinate_space"])
        self.jobs.start(plan["job_id"], "model_import")
        result = await self.jobs.wait(plan["job_id"], 5)
        self.assertEqual("completed", result["state"])
        self.assertTrue(result["assets"]["coordinate_validation"]["coordinate_match"])
        self.assertTrue(result["assets"]["texture_transfer"]["transferred"])
        methods = [method for method, _ in self.bridge.calls]
        self.assertEqual(methods[-3:], ["model_import_prepare", "model_import_copy",
                                        "model_import_import"])

    async def test_tie_point_filtering_runs_ordered_passes_on_copy(self):
        passes = [
            {"criterion": "reconstruction_uncertainty", "threshold": 10},
            {"criterion": "reprojection_error", "threshold": 0.5},
        ]
        plan = await self.jobs.prepare_tie_point_filtering(
            1, passes, 0.3, True, False, False)
        self.assertEqual(plan["filter_previews"][0]["selected_fraction"], 0.1)
        self.jobs.start(plan["job_id"], "tie_point_filtering")
        result = await self.jobs.wait(plan["job_id"], 5)
        self.assertEqual(result["state"], "completed")
        self.assertEqual([item["pass_index"] for item in result["assets"]["passes"]], [0, 1])
        methods = [method for method, _ in self.bridge.calls]
        self.assertEqual(methods[-4:], ["tie_point_filtering_prepare", "tie_point_filtering_copy",
                                        "tie_point_filtering_pass0", "tie_point_filtering_pass1"])

    async def test_sensor_calibration_is_applied_only_on_copy(self):
        plan = await self.jobs.prepare_sensor_calibration(
            1, 4, fixed=False, fixed_params=["f"])
        self.assertFalse(plan["sensor_before"]["fixed"])
        self.jobs.start(plan["job_id"], "sensor_calibration")
        result = await self.jobs.wait(plan["job_id"], 5)
        self.assertEqual(result["state"], "completed")
        self.assertTrue(result["assets"]["sensor_after"]["fixed"])
        methods = [method for method, _ in self.bridge.calls]
        self.assertEqual(methods[-3:], ["sensor_calibration_prepare",
                                        "sensor_calibration_copy",
                                        "sensor_calibration_apply"])

    async def test_camera_reference_exif_is_loaded_only_on_copy(self):
        plan = await self.jobs.prepare_camera_reference_exif(
            1, 4326, [5, 5, 10], replace_existing=True)
        self.assertEqual(plan["metadata_preview"]["gps_candidates"], 3)
        self.jobs.start(plan["job_id"], "camera_reference_exif")
        result = await self.jobs.wait(plan["job_id"], 5)
        self.assertEqual(result["state"], "completed")
        self.assertEqual(result["assets"]["fallback_accuracies"], 3)
        methods = [method for method, _ in self.bridge.calls]
        self.assertEqual(methods[-3:], ["camera_reference_exif_prepare",
                                        "camera_reference_exif_copy",
                                        "camera_reference_exif_apply"])


class HostJobTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.jobs = ProcessingJobs(Path(self.temporary.name), FakeBridge(self.temporary.name),
                                   lambda event, **fields: None)

    async def asyncTearDown(self):
        await self.jobs.close()
        self.temporary.cleanup()

    async def test_background_export_job_completes_with_result(self):
        seen = []

        async def work(job_id):
            seen.append(job_id)
            return {"output_directory": "/tmp/metashape-model-x", "files": [{"bytes": 10}]}
        job = self.jobs.start_host_job("export_model", 3, "export_model", work)
        self.assertEqual(job["state"], "running")
        with self.assertRaises(ValueError):
            self.jobs.assert_idle()
        result = await self.jobs.wait(job["job_id"], 5)
        self.assertEqual(result["state"], "completed")
        self.assertEqual(result["kind"], "export_model")
        self.assertEqual(result["assets"]["output_directory"], "/tmp/metashape-model-x")
        self.assertEqual(seen, [job["job_id"]])
        self.assertEqual(self.jobs.positions[job["job_id"]], (0, 1))

    async def test_background_job_failure_is_reported(self):
        async def work(job_id):
            raise ValueError("Путь занят")
        job = self.jobs.start_host_job("point_cloud_import", 3, "point_cloud_import", work)
        result = await self.jobs.wait(job["job_id"], 5)
        self.assertEqual(result["state"], "failed")
        self.assertIn("Путь занят", result["message"])

    async def test_cancel_during_background_call_keeps_result_but_marks_cancelled(self):
        started = asyncio.Event()
        release = asyncio.Event()

        async def work(job_id):
            started.set()
            await release.wait()
            return {"output_directory": "/tmp/metashape-model-cancelled"}

        job = self.jobs.start_host_job("export_model", 3, "export_model", work)
        await asyncio.wait_for(started.wait(), 5)
        self.assertEqual(self.jobs.cancel(job["job_id"])["state"], "cancelling")
        release.set()
        result = await self.jobs.wait(job["job_id"], 5)
        self.assertEqual(result["state"], "cancelled")
        self.assertEqual(result["assets"]["output_directory"],
                         "/tmp/metashape-model-cancelled")


class PromotingBridge(FakeBridge):
    """Records chunk_promote; optionally holds the last reconstruction stage until released."""

    def __init__(self, output_root, hold=False):
        super().__init__(output_root)
        self.release = asyncio.Event()
        self.started = asyncio.Event()
        self.hold = hold

    async def call(self, method, **parameters):
        if method == "chunk_promote":
            self.calls.append((method, parameters))
            return {"accepted_copy_key": parameters["copy_key"], "restore_point": {
                "restore_point_id": "restore-2"}}
        if method == "reconstruction_model" and self.hold:
            self.started.set()
            await self.release.wait()
        return await super().call(method, **parameters)


class AutoAcceptTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()

    async def asyncTearDown(self):
        await self.jobs.close()
        self.temporary.cleanup()

    def make(self, hold):
        self.bridge = PromotingBridge(self.temporary.name, hold)
        self.jobs = ProcessingJobs(Path(self.temporary.name), self.bridge, lambda event, **fields: None)

    async def test_auto_accept_is_visible_and_applied_before_completed(self):
        self.make(hold=False)
        plan = await self.jobs.prepare_reconstruction(1, 4, "medium")
        self.assertIsNone(plan["auto_accept"])
        self.assertEqual(self.jobs.set_auto_accept(plan["job_id"], "archive")["auto_accept"], "archive")
        self.jobs.start(plan["job_id"], "reconstruction")
        result = await self.jobs.wait(plan["job_id"], 5)
        self.assertEqual(result["state"], "completed")
        self.assertEqual(result["decision"], "accepted (archive, auto)")
        promote = [call for call in self.bridge.calls if call[0] == "chunk_promote"]
        self.assertEqual(promote[0][1]["source_action"], "archive")

    async def test_cancel_during_last_stage_never_auto_accepts(self):
        self.make(hold=True)
        plan = await self.jobs.prepare_reconstruction(1, 4, "medium")
        self.jobs.set_auto_accept(plan["job_id"], "remove")
        self.jobs.start(plan["job_id"], "reconstruction")
        await asyncio.wait_for(self.bridge.started.wait(), 5)
        self.assertEqual(self.jobs.cancel(plan["job_id"])["state"], "cancelling")
        self.bridge.release.set()
        result = await self.jobs.wait(plan["job_id"], 5)
        self.assertEqual(result["state"], "cancelled")
        self.assertNotIn("decision", result)
        self.assertFalse([call for call in self.bridge.calls if call[0] == "chunk_promote"])


if __name__ == "__main__":
    unittest.main()
