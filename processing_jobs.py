"""Durable, cancellable Metashape jobs for the external MCP process."""

import asyncio
import json
import subprocess
import sys
import sqlite3
from pathlib import Path
from uuid import uuid4

from autopilot import notification_text, required_inputs, validate_steps
from tie_point_filtering import (optimization_parameters, validate_max_selected_fraction,
                                 validate_passes)
from sensor_calibration import validate_sensor_calibration_plan
from camera_reference_exif import validate_reference_exif_plan
from reference_crs_transform import validate_reference_crs_transform
from point_cloud_editing import validate_point_cloud_cleanup


def notify(title, text):
    """macOS notification with sound; silently skipped elsewhere or on failure."""
    if sys.platform != "darwin":
        return
    script = f'display notification {json.dumps(text)} with title {json.dumps(title)} sound name "Glass"'
    try:
        subprocess.Popen(["osascript", "-e", script], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass


# Jobs that change existing chunks in place (protected by a restore point) instead of a copy.
INPLACE_KINDS = ("chunk_alignment", "chunk_merge")
# background host calls started by start_host_job (exports are "export_<kind>")
HOST_JOB_KINDS = ("point_cloud_import", "mask_import", "mask_generate")


class ProcessingJobs:
    def __init__(self, directory, bridge, record):
        self.bridge = bridge
        self.record = record
        self.task = None
        # job_id -> (index of the running stage, number of stages), for the overall percentage
        self.positions = {}
        self.db = sqlite3.connect(directory / "quality.sqlite3")
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, state TEXT NOT NULL, project TEXT, chunk_key INTEGER,
                chunk_label TEXT, total INTEGER NOT NULL, message TEXT);
            CREATE TABLE IF NOT EXISTS images (
                job_id TEXT, position INTEGER, camera_key INTEGER, label TEXT, path TEXT,
                layer INTEGER, score REAL, error TEXT, PRIMARY KEY (job_id, position));
            CREATE TABLE IF NOT EXISTS alignments (
                job_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, frame_index INTEGER NOT NULL,
                parameters TEXT NOT NULL, copy_label TEXT NOT NULL, copy_key INTEGER, stage TEXT);
            CREATE TABLE IF NOT EXISTS reconstructions (job_id TEXT PRIMARY KEY, result TEXT);
            CREATE TABLE IF NOT EXISTS decisions (job_id TEXT PRIMARY KEY, decision TEXT, at TEXT);
            CREATE TABLE IF NOT EXISTS auto_accepts (job_id TEXT PRIMARY KEY, action TEXT);
            CREATE TABLE IF NOT EXISTS camera_results (
                job_id TEXT, position INTEGER, camera_key INTEGER, label TEXT, aligned INTEGER,
                PRIMARY KEY(job_id, position));
        """)
        if "kind" not in {row["name"] for row in self.db.execute("PRAGMA table_info(reconstructions)")}:
            with self.db:
                self.db.execute("ALTER TABLE reconstructions ADD COLUMN kind TEXT NOT NULL DEFAULT 'reconstruction'")
        if "restore_point_id" not in {row["name"] for row in self.db.execute("PRAGMA table_info(alignments)")}:
            with self.db:
                self.db.execute("ALTER TABLE alignments ADD COLUMN restore_point_id TEXT")
        for row in self.db.execute("SELECT id FROM jobs WHERE state IN ('running','cancelling')").fetchall():
            self.set_state(row["id"], "interrupted", "Предыдущий процесс сервера завершился.")

    def set_state(self, job_id, state, message=None):
        with self.db:
            self.db.execute("UPDATE jobs SET state=?, message=? WHERE id=?", (state, message, job_id))
        kind = self.info(job_id)["kind"]
        self.record(kind + "_" + state, job_id=job_id, message=message)
        if kind == "autopilot" and state in ("completed", "completed_with_errors", "failed",
                                              "cancelled", "interrupted"):
            notify("Metashape MCP", notification_text(kind, state, message))

    def start_host_job(self, kind, chunk_key, label, work):
        """Run one long host call (export, import) as a background job; work(job_id) -> dict.

        The call cannot be cancelled midway (Metashape would show a modal dialog); its progress
        is reported through get_job_status like any other job.
        """
        if self.task is not None and not self.task.done():
            raise ValueError("Другое задание выполняется. Повторите позднее.")
        job_id = uuid4().hex
        with self.db:
            self.db.execute("INSERT INTO jobs VALUES (?, 'running', '', ?, ?, 1, NULL)",
                            (job_id, chunk_key, label))
            self.db.execute("INSERT INTO reconstructions(job_id,result,kind) VALUES (?, '{}', ?)",
                            (job_id, kind))
        self.record(kind + "_started", job_id=job_id, chunk_key=chunk_key)
        self.positions[job_id] = (0, 1)

        async def run():
            try:
                result = await work(job_id)
                with self.db:
                    self.db.execute("UPDATE reconstructions SET result=? WHERE job_id=?",
                                    (json.dumps(result, ensure_ascii=False, default=str), job_id))
                if self.info(job_id)["state"] == "cancelling":
                    self.set_state(job_id, "cancelled", "Отменено во время операции; "
                                   "созданный результат оставлен для проверки.")
                else:
                    self.set_state(job_id, "completed")
            except (asyncio.CancelledError, ConnectionError, BrokenPipeError):
                self.set_state(job_id, "interrupted", "Связь с исполнителем прервана.")
                raise
            except Exception as error:
                self.set_state(job_id, "failed", str(error))
        self.task = asyncio.create_task(run())
        return self.info(job_id)

    def assert_idle(self):
        if self.task is not None and not self.task.done():
            raise ValueError("Дождитесь завершения текущего задания или отмените его.")

    def active_job(self):
        row = self.db.execute(
            "SELECT id FROM jobs WHERE state IN ('running','cancelling') ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        return self.info(row["id"]) if row is not None else None

    def recent(self, limit):
        """Most recent jobs from the service database (no Metashape call)."""
        rows = self.db.execute("SELECT id FROM jobs ORDER BY rowid DESC LIMIT ?", (limit,)).fetchall()
        result = []
        for row in rows:
            info = self.info(row["id"])
            result.append({key: info.get(key) for key in (
                "job_id", "kind", "state", "stage", "message", "chunk_key", "chunk_label", "copy_key",
                "restore_point_id")})
        return result

    def info(self, job_id):
        row = self.db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise ValueError("Задание не найдено.")
        result = dict(row)
        result["job_id"] = result.pop("id")
        alignment = self.db.execute(
            "SELECT frame_index, parameters, copy_key, copy_label, stage, restore_point_id "
            "FROM alignments WHERE job_id=?",
            (job_id,)).fetchone()
        result["kind"] = "alignment" if alignment else "image_quality"
        reconstruction = self.db.execute("SELECT result, kind FROM reconstructions WHERE job_id=?", (job_id,)).fetchone()
        if reconstruction:
            result.update(kind=reconstruction["kind"], assets=json.loads(reconstruction["result"]))
        decision = self.decision(job_id)
        if decision:
            result["decision"] = decision
        auto = self.db.execute("SELECT action FROM auto_accepts WHERE job_id=?", (job_id,)).fetchone()
        result["auto_accept"] = auto[0] if auto else None
        if alignment:
            result.update(dict(alignment))
            result["parameters"] = json.loads(result["parameters"])
            result["save_policy"] = "manual"
        return result

    def set_auto_accept(self, job_id, action):
        job = self.info(job_id)
        if job["kind"] in INPLACE_KINDS or job["kind"] == "image_quality":
            raise ValueError("Это задание работает без копии чанка; принимать нечего.")
        if (job.get("parameters") or {}).get("in_place"):
            raise ValueError("Задание идёт на месте, без копии; принимать нечего.")
        if job["state"] not in ("prepared", "running"):
            raise ValueError(f"Задание в состоянии {job['state']}; авто-принятие задаётся до "
                             "завершения. Для готового задания вызовите accept_job_result.")
        with self.db:
            if action is None:
                self.db.execute("DELETE FROM auto_accepts WHERE job_id=?", (job_id,))
            else:
                self.db.execute("INSERT OR REPLACE INTO auto_accepts VALUES (?, ?)", (job_id, action))
        self.record("job_auto_accept_set", job_id=job_id, action=action)
        return self.info(job_id)

    async def run_auto_accept(self, job_id):
        """Called only for a fully successful copy job, before it is marked completed: let the copy
        replace the source (archive/remove)."""
        row = self.db.execute("SELECT action FROM auto_accepts WHERE job_id=?", (job_id,)).fetchone()
        job = self.info(job_id)
        if (row is None or job.get("decision") or job.get("copy_key") is None
                or job["state"] != "running"):
            return
        action = row[0]
        try:
            result = await self.bridge.call(
                "chunk_promote", timeout=None, source_key=job["chunk_key"], copy_key=job["copy_key"],
                copy_label=job["copy_label"], source_label=job["chunk_label"], source_action=action,
                confirm_source_label=job["chunk_label"])
        except Exception as error:
            self.record("job_auto_accept_failed", job_id=job_id, message=str(error))
            with self.db:
                self.db.execute("UPDATE jobs SET message=? WHERE id=?",
                                (f"Результат не принят автоматически: {error}", job_id))
            return
        self.decide(job_id, f"accepted ({action}, auto)")
        self.record("job_result_accepted", job_id=job_id, source_action=action, auto=True,
                    restore_point_id=(result.get("restore_point") or {}).get("restore_point_id"))

    def decision(self, job_id):
        row = self.db.execute("SELECT decision FROM decisions WHERE job_id=?", (job_id,)).fetchone()
        return row[0] if row else None

    def decide(self, job_id, decision):
        with self.db:
            self.db.execute("INSERT INTO decisions VALUES (?, ?, datetime('now'))", (job_id, decision))

    async def prepare(self, chunk_key):
        plan = await self.bridge.call("quality_sources", chunk_key=chunk_key)
        images = plan["images"]
        if not images:
            raise ValueError("В чанке нет включённых фотокамер со снимками.")
        job_id = uuid4().hex
        with self.db:
            self.db.execute("INSERT INTO jobs VALUES (?, 'prepared', ?, ?, ?, ?, NULL)",
                            (job_id, plan["project"], chunk_key, plan["chunk_label"], len(images)))
            self.db.executemany(
                "INSERT INTO images(job_id, position, camera_key, label, path, layer) VALUES (?,?,?,?,?,?)",
                ((job_id, i, im["key"], im["label"], im["path"], im["layer"]) for i, im in enumerate(images)))
        self.record("image_quality_prepared", job_id=job_id, chunk_key=chunk_key, total=len(images))
        return self.info(job_id)

    async def prepare_alignment(self, chunk_key, downscale, keypoint_limit, tiepoint_limit,
                                filter_mask=False, mask_tiepoints=False):
        match = {"downscale": downscale, "keypoint_limit": keypoint_limit,
                 "tiepoint_limit": tiepoint_limit, "generic_preselection": True,
                 "reference_preselection": False, "reset_matches": True,
                 "filter_mask": filter_mask, "mask_tiepoints": mask_tiepoints}
        plan = await self.bridge.call("alignment_prepare", chunk_key=chunk_key,
                                      parameters={"match": match})
        if (filter_mask or mask_tiepoints) and not plan.get("cameras_with_masks"):
            raise ValueError("Фильтр масок включён, но у включённых камер чанка нет масок.")
        parameters = {"match": match,
                      "align": {"reset_alignment": True, "adaptive_fitting": False}}
        return self.prepare_copy_job(chunk_key, plan, parameters, "alignment")

    async def prepare_reconstruction(self, chunk_key, downscale, faces):
        plan = await self.bridge.call("reconstruction_prepare", chunk_key=chunk_key)
        return self.prepare_copy_job(chunk_key, plan, {"downscale": downscale, "faces": faces,
                                     "filter": "mild", "source": "depth_maps"}, "reconstruction")

    async def prepare_texture_export(self, key, size, directory):
        directory = Path(directory).expanduser()
        if not directory.is_absolute() or not directory.is_dir():
            raise ValueError("Укажите абсолютный путь к существующей папке экспорта.")
        plan = await self.bridge.call("texture_export_prepare", chunk_key=key)
        return self.prepare_copy_job(key, plan, {"texture_size": size, "directory": str(directory.resolve()),
                                                 "format": "OBJ"}, "texture_export")

    async def prepare_camera_optimization(self, chunk_key, fit_principal_point, fit_skew, adaptive_fitting):
        plan = await self.bridge.call("camera_optimization_prepare", chunk_key=chunk_key)
        parameters = {
            "fit_f": True, "fit_cx": fit_principal_point, "fit_cy": fit_principal_point,
            "fit_b1": fit_skew, "fit_b2": fit_skew,
            "fit_k1": True, "fit_k2": True, "fit_k3": True, "fit_k4": False,
            "fit_p1": True, "fit_p2": True, "fit_corrections": False,
            "adaptive_fitting": adaptive_fitting, "tiepoint_covariance": False,
        }
        return self.prepare_copy_job(chunk_key, plan, parameters, "camera_optimization")

    async def prepare_tie_point_filtering(self, chunk_key, passes, max_selected_fraction,
                                          fit_principal_point, fit_skew, adaptive_fitting):
        passes = validate_passes(passes)
        parameters = {
            "passes": passes,
            "max_selected_fraction": validate_max_selected_fraction(max_selected_fraction),
            "optimization": optimization_parameters(
                fit_principal_point, fit_skew, adaptive_fitting),
        }
        plan = await self.bridge.call("tie_point_filtering_prepare", timeout=None,
                                      chunk_key=chunk_key, parameters=parameters)
        job = self.prepare_copy_job(chunk_key, plan, parameters, "tie_point_filtering")
        return {**job, "filter_previews": plan["filter_previews"],
                "metrics_before": plan["metrics_before"],
                "preview_note": plan["preview_note"]}

    async def prepare_sensor_calibration(self, chunk_key, sensor_key, calibration_path=None,
                                         sensor_type=None, fixed=None, fixed_params=None):
        parameters = validate_sensor_calibration_plan(
            sensor_key, calibration_path, sensor_type, fixed, fixed_params)
        plan = await self.bridge.call("sensor_calibration_prepare", timeout=None,
                                      chunk_key=chunk_key, parameters=parameters)
        job = self.prepare_copy_job(chunk_key, plan, parameters, "sensor_calibration")
        return {**job, "sensor_before": plan["sensor_before"],
                "proposed_changes": plan["proposed_changes"]}

    async def prepare_camera_reference_exif(
            self, chunk_key, epsg, fallback_accuracy_m, load_rotation=False,
            rotation_accuracy_deg=None, load_accuracy=True, replace_existing=False,
            enable_locations=True, enable_rotations=False, update_transform=False):
        parameters = validate_reference_exif_plan(
            epsg, fallback_accuracy_m, load_rotation, rotation_accuracy_deg, load_accuracy,
            replace_existing, enable_locations, enable_rotations, update_transform)
        plan = await self.bridge.call("camera_reference_exif_prepare", timeout=None,
                                      chunk_key=chunk_key, parameters=parameters)
        job = self.prepare_copy_job(chunk_key, plan, parameters, "camera_reference_exif")
        return {**job, "metadata_preview": plan["metadata_preview"],
                "references_before": plan["references_before"],
                "coordinate_system": plan["coordinate_system"]}

    async def prepare_reference_crs_transform(
            self, chunk_key, source_epsg, target_epsg, transform_cameras=True,
            transform_markers=True, set_chunk_crs=False, update_transform=False):
        parameters = validate_reference_crs_transform(
            source_epsg, target_epsg, transform_cameras, transform_markers,
            set_chunk_crs, update_transform)
        plan = await self.bridge.call("reference_crs_transform_prepare", timeout=None,
                                      chunk_key=chunk_key, parameters=parameters)
        job = self.prepare_copy_job(chunk_key, plan, parameters, "reference_crs_transform")
        return {**job, "source_coordinate_system": plan["source_coordinate_system"],
                "target_coordinate_system": plan["target_coordinate_system"],
                "coordinate_preview": plan["coordinate_preview"],
                "counts": plan["counts"], "warnings": plan["warnings"]}

    async def prepare_autopilot(self, chunk_key, steps, in_place=False):
        steps = validate_steps(steps)
        parameters = {"steps": steps, "inputs": required_inputs(steps)}
        if in_place:
            parameters["in_place"] = True
        plan = await self.bridge.call("autopilot_prepare", timeout=None, chunk_key=chunk_key,
                                      parameters=parameters)
        job = self.prepare_copy_job(chunk_key, plan, parameters, "autopilot")
        return {**job, "steps_effective": plan.get("steps_effective"),
                "region_size_m": plan.get("region_size_m"),
                "metric_warnings": plan.get("metric_warnings") or [],
                "copied_inputs": parameters["inputs"],
                "notes": ["Цепочка идёт на копии чанка и останавливается на первой ошибке; "
                          "отмена — между шагами. По завершении — уведомление macOS."]}

    async def prepare_ground_classification(self, chunk_key, in_place, parameters):
        plan = await self.bridge.call("ground_classification_prepare", chunk_key=chunk_key,
                                      in_place=in_place)
        if in_place:
            parameters = {**parameters, "in_place": True}
        return self.prepare_copy_job(chunk_key, plan, parameters, "ground_classification")

    async def prepare_contours(self, chunk_key, directory, parameters):
        directory = Path(directory).expanduser()
        if not directory.is_absolute() or not directory.is_dir():
            raise ValueError("Укажите абсолютный путь к существующей папке экспорта.")
        if parameters["min_value"] >= parameters["max_value"]:
            raise ValueError("min_value должен быть меньше max_value.")
        plan = await self.bridge.call("contours_prepare", chunk_key=chunk_key)
        crs = plan.get("coordinate_system") or {}
        if parameters["format"] in ("geojson", "kml") and not crs.get("authority"):
            raise ValueError(f"GeoJSON и KML требуют географической системы координат, а у чанка "
                             f"«{crs.get('name') or 'нет'}». Выберите shp или dxf или задайте EPSG.")
        return self.prepare_copy_job(chunk_key, plan, {**parameters, "directory": str(directory.resolve())},
                                     "contours")

    async def prepare_raster_products(self, chunk_key, directory, resolution, refine_seamlines, classes=None):
        directory = Path(directory).expanduser()
        if not directory.is_absolute() or not directory.is_dir():
            raise ValueError("Укажите абсолютный путь к существующей папке экспорта.")
        plan = await self.bridge.call("raster_products_prepare", chunk_key=chunk_key)
        parameters = {
            "directory": str(directory.resolve()),
            "resolution": resolution,
            "refine_seamlines": refine_seamlines,
            "elevation_source": plan["elevation_source"],
            "format": "GeoTIFF",
            "classes": classes,
        }
        if classes and plan["elevation_source"] != "point_cloud":
            raise ValueError("Классы точек есть только у плотного облака; в чанке DEM строится по связующим точкам.")
        return self.prepare_copy_job(chunk_key, plan, parameters, "raster_products")

    async def prepare_marker_detection(self, chunk_key, target_type, tolerance, filter_mask):
        parameters = {
            "target_type": target_type,
            "tolerance": tolerance,
            "filter_mask": filter_mask,
            "inverted": False,
            "noparity": False,
            "merge_markers": False,
        }
        plan = await self.bridge.call(
            "marker_detection_prepare", chunk_key=chunk_key, filter_mask=filter_mask)
        return self.prepare_copy_job(chunk_key, plan, parameters, "marker_detection")

    async def prepare_marker_refinement(self, chunk_key, marker_keys):
        parameters = {"marker_keys": marker_keys}
        plan = await self.bridge.call(
            "marker_refinement_prepare", chunk_key=chunk_key, parameters=parameters)
        return self.prepare_copy_job(chunk_key, plan, parameters, "marker_refinement")

    async def prepare_point_cloud(self, chunk_key, point_colors, point_confidence, keep_depth,
                                  max_neighbors, uniform_sampling, points_spacing, in_place=False):
        plan = await self.bridge.call("point_cloud_prepare", chunk_key=chunk_key, in_place=in_place)
        parameters = {
            "point_colors": point_colors,
            "point_confidence": point_confidence,
            "keep_depth": keep_depth,
            "max_neighbors": max_neighbors,
            "uniform_sampling": uniform_sampling,
            "points_spacing": points_spacing,
            "source": "depth_maps",
            "depth_maps_key": plan["depth_maps_key"],
        }
        if in_place:
            parameters["in_place"] = True
        return self.prepare_copy_job(chunk_key, plan, parameters, "point_cloud")

    async def prepare_point_cloud_cleanup(
            self, chunk_key, point_cloud_key, selector, point_classes=None,
            min_confidence=None, max_confidence=None, max_selected_fraction=0.30):
        parameters = validate_point_cloud_cleanup(
            point_cloud_key, selector, point_classes, min_confidence,
            max_confidence, max_selected_fraction)
        plan = await self.bridge.call("point_cloud_cleanup_prepare", timeout=None,
                                      chunk_key=chunk_key, parameters=parameters)
        job = self.prepare_copy_job(chunk_key, plan, parameters, "point_cloud_cleanup")
        return {**job, "point_cloud_before": plan["point_cloud_before"],
                "selection_preview": plan["selection_preview"],
                "warning": plan["warning"]}

    def prepare_copy_job(self, chunk_key, plan, parameters, kind):
        job_id = uuid4().hex
        in_place = bool(parameters.get("in_place"))
        # in place: the job works on the source chunk, its label is not changed
        label = plan["chunk_label"] if in_place else f"{plan['chunk_label']} · MCP {job_id[:12]}"
        with self.db:
            self.db.execute("INSERT INTO jobs VALUES (?, 'prepared', ?, ?, ?, ?, NULL)",
                            (job_id, plan["project"], chunk_key, plan["chunk_label"], plan["total"]))
            self.db.execute("INSERT INTO alignments "
                            "(job_id,fingerprint,frame_index,parameters,copy_label,copy_key,stage) "
                            "VALUES (?, ?, ?, ?, ?, NULL, 'preview')",
                            (job_id, plan["fingerprint"], plan["frame_index"], json.dumps(parameters), label))
            if kind != "alignment":
                self.db.execute("INSERT INTO reconstructions(job_id,result,kind) VALUES (?, '{}', ?)", (job_id, kind))
        self.record(kind + "_prepared", job_id=job_id, chunk_key=chunk_key, total=plan["total"],
                    parameters=parameters, copy_label=label)
        result = self.info(job_id)
        for key in ("cameras_with_masks", "copy_estimate", "in_place_note"):
            if key in plan:
                result[key] = plan[key]
        result["in_place"] = in_place
        return result

    def start(self, job_id, kind):
        job = self.info(job_id)
        if job["kind"] != kind:
            raise ValueError("ID относится к другому виду задания.")
        if job["state"] != "prepared":
            return job
        if self.task is not None and not self.task.done():
            raise ValueError("Другое задание выполняется. Повторите запуск позднее с тем же ID.")
        self.set_state(job_id, "running")
        if kind in INPLACE_KINDS:
            self.task = asyncio.create_task(self.process_inplace(job))
        elif kind != "image_quality":
            self.task = asyncio.create_task(self.process_copy(job))
        else:
            self.task = asyncio.create_task(self.run(job_id, job["total"]))
        return self.info(job_id)

    def cancel(self, job_id):
        state = self.info(job_id)["state"]
        if state == "prepared":
            self.set_state(job_id, "cancelled")
        elif state == "running":
            self.set_state(job_id, "cancelling")
        return self.info(job_id)

    async def process_copy(self, job):
        job_id = job["job_id"]
        fingerprint = self.db.execute("SELECT fingerprint FROM alignments WHERE job_id=?", (job_id,)).fetchone()[0]
        request = {"job_id": job_id, "chunk_key": job["chunk_key"], "fingerprint": fingerprint,
                   "copy_label": job["copy_label"], "parameters": job["parameters"]}
        try:
            kind = job["kind"]
            stages = {"alignment": ("copy", "match", "align"), "reconstruction": ("copy", "depth_maps", "model"),
                      "texture_export": ("copy", "uv", "texture", "export"),
                      "camera_optimization": ("copy", "optimize"),
                      "tie_point_filtering": ("copy",) + tuple(
                          f"pass{i}" for i in range(len(job["parameters"].get("passes", [])))),
                      "sensor_calibration": ("copy", "apply"),
                      "camera_reference_exif": ("copy", "apply"),
                      "reference_crs_transform": ("copy", "apply"),
                      "marker_detection": ("copy", "detect"),
                      "marker_refinement": ("copy", "refine"),
                      "point_cloud": ("copy", "build"),
                      "point_cloud_cleanup": ("copy", "apply"),
                      "model_finishing": ("copy", "finish"),
                      "model_import": ("copy", "import"),
                      "ground_classification": ("copy", "classify"),
                      "contours": ("copy", "contours", "export"),
                      "autopilot": ("copy",) + tuple(f"step{i}" for i in range(len(job["parameters"].get("steps", [])))),
                      "raster_products": ("copy", "dem", "orthomosaic", "export")}[kind]
            for position, stage in enumerate(stages):
                self.positions[job_id] = (position, len(stages))
                if self.info(job_id)["state"] == "cancelling":
                    self.set_state(job_id, "cancelled")
                    return
                with self.db:
                    self.db.execute("UPDATE alignments SET stage=? WHERE job_id=?", (stage, job_id))
                self.record(kind + "_stage_started", job_id=job_id, stage=stage)
                reply = await self.bridge.call(kind + "_" + stage, timeout=None, **request)
                if stage == "copy":
                    if reply.get("restore_point"):
                        point = reply["restore_point"]
                        request["restore_point_id"] = point["restore_point_id"]
                        self.record("restore_point_created", job_id=job_id, stage=stage,
                                    restore_point_id=point["restore_point_id"],
                                    reason=point["reason"], kind=point["kind"])
                    request["copy_key"] = reply["copy_key"]
                    with self.db:
                        self.db.execute(
                            "UPDATE alignments SET copy_key=?, restore_point_id=? WHERE job_id=?",
                            (reply["copy_key"], request.get("restore_point_id"), job_id))
                if stage == "align":
                    cameras = reply["cameras"]
                    with self.db:
                        self.db.executemany("INSERT INTO camera_results VALUES(?, ?, ?, ?, ?)",
                                            ((job_id, i, c["key"], c["label"], c["aligned"])
                                             for i, c in enumerate(cameras)))
                    aligned = sum(c["aligned"] for c in cameras)
                if stage.startswith(("step", "pass")) or stage in ("depth_maps", "model", "texture", "dem", "orthomosaic", "export", "apply",
                             "optimize", "detect", "refine", "build", "finish",
                             "classify", "contours", "import"):
                    if stage == "export" and reply.get("output_directory"):
                        registered = await self.bridge.call(
                            "restore_point_register_outputs", timeout=None,
                            restore_point_id=request["restore_point_id"],
                            paths=[reply["output_directory"]])
                        reply["restore_point_id"] = registered["restore_point_id"]
                    assets = self.info(job_id)["assets"]
                    if stage.startswith("pass"):
                        assets.setdefault("passes", []).append(reply)
                    else:
                        assets.update(reply)
                    with self.db:
                        self.db.execute("UPDATE reconstructions SET result=? WHERE job_id=?", (json.dumps(assets), job_id))
                self.record(kind + "_stage_finished", job_id=job_id, stage=stage, copy_key=request["copy_key"])
            if kind != "alignment":
                if self.info(job_id)["state"] == "cancelling":
                    # cancelled during the last stage: never accept, keep the copy for review
                    self.set_state(job_id, "cancelled", "Отменено во время последнего этапа; "
                                   "результат не принят.")
                    return
                # auto-accept before "completed", so nobody sees a finished job not yet accepted
                await self.run_auto_accept(job_id)
                self.set_state(job_id, "completed", self.info(job_id).get("message"))
            elif not aligned or reply.get("error"):
                self.set_state(job_id, "failed", reply.get("error") or "Не выровнена ни одна камера.")
            elif aligned != job["total"]:
                self.set_state(job_id, "completed_with_errors", f"Выровнено камер: {aligned} из {job['total']}.")
            elif self.info(job_id)["state"] == "cancelling":
                self.set_state(job_id, "cancelled", "Отменено во время последнего этапа; "
                               "результат не принят.")
            else:
                await self.run_auto_accept(job_id)
                self.set_state(job_id, "completed", self.info(job_id).get("message"))
        except (asyncio.CancelledError, ConnectionError, BrokenPipeError):
            self.set_state(job_id, "interrupted", "Связь с исполнителем прервана. Проверьте рабочую копию.")
            raise
        except Exception as error:
            cancelled = self.info(job_id)["state"] == "cancelling"
            self.set_state(job_id, "cancelled" if cancelled else "failed", str(error))

    async def prepare_model_finishing(self, chunk_key, steps, in_place=False):
        from model_finishing import plan_warnings, validate_steps
        steps = validate_steps(steps)
        statistics = await self.bridge.call("get_model_statistics", timeout=None, chunk_key=chunk_key)
        active = next((model for model in statistics["models"] if model["active"]), None)
        if active is None:
            raise ValueError("В чанке нет активной полигональной модели.")
        plan = await self.bridge.call("model_finishing_prepare", chunk_key=chunk_key, in_place=in_place)
        parameters = {"steps": steps, **({"in_place": True} if in_place else {})}
        job = self.prepare_copy_job(chunk_key, plan, parameters, "model_finishing")
        return {**job, "source_model": active, "warnings": plan_warnings(steps, active)}

    async def prepare_model_import(self, chunk_key, parameters):
        plan = await self.bridge.call("model_import_prepare", timeout=None,
                                      chunk_key=chunk_key, parameters=parameters)
        job = self.prepare_copy_job(chunk_key, plan, parameters, "model_import")
        return {
            **job,
            "source_model": plan["source_model"],
            "coordinate_space": plan["coordinate_space"],
            "coordinate_system": plan["coordinate_system"],
            "chunk_transform_max_delta": plan["chunk_transform_max_delta"],
            "modified_since_export": plan["modified_since_export"],
            "validation_note": plan["validation_note"],
        }

    async def prepare_chunk_alignment(self, reference_chunk_key, chunk_keys, parameters):
        plan = await self.bridge.call(
            "chunk_alignment_prepare", timeout=None, chunk_key=reference_chunk_key,
            chunk_keys=chunk_keys, parameters=parameters)
        job = self.prepare_copy_job(reference_chunk_key, plan, {**parameters, "chunk_keys": chunk_keys},
                                    "chunk_alignment")
        return {**job, "plan": plan["summary"]}

    async def prepare_chunk_merge(self, chunk_keys, parameters):
        plan = await self.bridge.call(
            "chunk_merge_prepare", timeout=None, chunk_key=chunk_keys[0],
            chunk_keys=chunk_keys, parameters=parameters)
        job = self.prepare_copy_job(chunk_keys[0], plan, {**parameters, "chunk_keys": chunk_keys},
                                    "chunk_merge")
        return {**job, "plan": plan["summary"]}

    async def process_inplace(self, job):
        """Run one checkpointed host stage that changes existing chunks of the open project."""
        job_id, kind = job["job_id"], job["kind"]
        fingerprint = self.db.execute("SELECT fingerprint FROM alignments WHERE job_id=?", (job_id,)).fetchone()[0]
        request = {"job_id": job_id, "chunk_key": job["chunk_key"], "fingerprint": fingerprint,
                   "copy_label": job["copy_label"], "parameters": job["parameters"]}
        try:
            if self.info(job_id)["state"] == "cancelling":
                self.set_state(job_id, "cancelled")
                return
            self.positions[job_id] = (0, 1)
            with self.db:
                self.db.execute("UPDATE alignments SET stage='apply' WHERE job_id=?", (job_id,))
            self.record(kind + "_stage_started", job_id=job_id, stage="apply")
            reply = await self.bridge.call(kind + "_apply", timeout=None, **request)
            point = reply.get("restore_point")
            if point:
                self.record("restore_point_created", job_id=job_id, stage="apply",
                            restore_point_id=point["restore_point_id"], reason=point["reason"],
                            kind=point["kind"])
            with self.db:
                self.db.execute("UPDATE alignments SET copy_key=?, restore_point_id=? WHERE job_id=?",
                                (reply.get("chunk_key_created"), point and point["restore_point_id"], job_id))
                self.db.execute("UPDATE reconstructions SET result=? WHERE job_id=?",
                                (json.dumps(reply), job_id))
            self.record(kind + "_stage_finished", job_id=job_id, stage="apply")
            if self.info(job_id)["state"] == "cancelling":
                self.set_state(job_id, "cancelled", "Отменено во время операции; "
                               "изменения сохранены и могут быть возвращены по точке восстановления.")
            else:
                self.set_state(job_id, "completed")
        except (asyncio.CancelledError, ConnectionError, BrokenPipeError):
            self.set_state(job_id, "interrupted", "Связь с исполнителем прервана. Проверьте проект и точку восстановления.")
            raise
        except Exception as error:
            cancelled = self.info(job_id)["state"] == "cancelling"
            self.set_state(job_id, "cancelled" if cancelled else "failed", str(error))

    async def run(self, job_id, total):
        errors = 0
        try:
            for position in range(total):
                if self.info(job_id)["state"] == "cancelling":
                    self.set_state(job_id, "cancelled")
                    return
                image = self.db.execute("SELECT * FROM images WHERE job_id=? AND position=?",
                                        (job_id, position)).fetchone()
                # The host returns per-image decode errors; bridge failure stops the whole job.
                reply = await self.bridge.call("quality_measure", timeout=300,
                                               path=image["path"], layer=image["layer"])
                score, error = reply.get("score"), reply.get("error")
                if error:
                    errors += 1
                with self.db:
                    self.db.execute("UPDATE images SET score=?, error=? WHERE job_id=? AND position=?",
                                    (score, error, job_id, position))
                self.record("image_quality_result", job_id=job_id, camera_key=image["camera_key"],
                            score=score, error=error)
            state = "completed"
            if errors:
                state = "failed" if errors == total else "completed_with_errors"
            self.set_state(job_id, state)
        except asyncio.CancelledError:
            self.set_state(job_id, "interrupted", "Сервер остановлен до окончания задания.")
            raise
        except Exception as error:
            cancelled = self.info(job_id)["state"] == "cancelling"
            self.set_state(job_id, "cancelled" if cancelled else "failed", str(error))

    def status(self, job_id, offset, limit, only_unaligned=False):
        result = self.info(job_id)
        if result["kind"] == "alignment":
            n, aligned = self.db.execute(
                "SELECT count(*),coalesce(sum(aligned),0) FROM camera_results WHERE job_id=?", (job_id,)).fetchone()
            rows = self.db.execute("""
                SELECT position, camera_key, label, aligned FROM camera_results
                WHERE job_id=? AND position>=? AND (?=0 OR aligned=0) ORDER BY position LIMIT ?""",
                (job_id, offset, only_unaligned, limit))
            cameras = [dict(row, aligned=bool(row["aligned"])) for row in rows]
            result.update(reported=n, aligned=aligned, unaligned=n-aligned, cameras=cameras,
                          next_offset=cameras[-1]["position"]+1 if cameras else offset)
            return result
        if only_unaligned:
            raise ValueError("Фильтр only_unaligned применим к выравниванию камер.")
        if result["kind"] in ("reconstruction", "texture_export", "camera_optimization",
                              "tie_point_filtering", "sensor_calibration", "camera_reference_exif",
                              "reference_crs_transform",
                              "raster_products", "marker_detection", "marker_refinement",
                              "point_cloud", "model_finishing", "ground_classification",
                              "point_cloud_cleanup",
                              "contours", "autopilot", *INPLACE_KINDS) \
                or result["kind"] in HOST_JOB_KINDS or result["kind"].startswith("export_"):
            return result
        counts = self.db.execute("SELECT count(score), count(error) FROM images WHERE job_id=?",
                                 (job_id,)).fetchone()
        result.update(scored=counts[0], failed_images=counts[1], processed=sum(counts))
        rows = self.db.execute("""
            SELECT position, camera_key, label, score, error FROM images
            WHERE job_id=? AND position>=? AND (score IS NOT NULL OR error IS NOT NULL)
            ORDER BY position LIMIT ?""", (job_id, offset, limit))
        result["images"] = [dict(row) for row in rows]
        result["next_offset"] = result["images"][-1]["position"] + 1 if result["images"] else offset
        return result

    async def wait(self, job_id, timeout_seconds):
        loop = asyncio.get_running_loop()
        started = loop.time()
        deadline = started + timeout_seconds
        while self.info(job_id)["state"] in ("running", "cancelling") and loop.time() < deadline:
            await asyncio.sleep(min(0.5, max(0.0, deadline - loop.time())))
        result = self.status(job_id, 0, 40, False)
        result["still_running"] = result["state"] in ("running", "cancelling")
        result["waited_seconds"] = round(loop.time() - started, 3)
        return result

    async def close(self):
        if self.task is not None and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        self.db.close()
