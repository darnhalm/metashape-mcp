"""Camera alignment inside the Metashape GUI thread."""

import hashlib
import json
import math
import os
import re
import shutil
import zipfile
import tempfile
from pathlib import Path
from uuid import uuid4

import Metashape

from model_finishing import apply_step, model_figures
from autopilot import COPY_ITEMS
from progress_tracker import TRACKER
from tie_point_filtering import CRITERIA
from sensor_calibration import SENSOR_TYPES
from model_roundtrip import (bounds_from_points, compare_bounds, file_fingerprint,
                             matrix_max_delta)


MODEL_IMPORT_FORMATS = {
    "obj": "ModelFormatOBJ",
    "ply": "ModelFormatPLY",
    "stl": "ModelFormatSTL",
    "fbx": "ModelFormatFBX",
    "glb": "ModelFormatGLTF",
}


def model_local_bounds(model):
    transform = model.transform
    return bounds_from_points(
        transform.mulp(vertex.coord) if transform is not None else vertex.coord
        for vertex in model.vertices)


def model_import_preview(document, source, parameters):
    manifest = parameters["manifest"]
    recorded = manifest["source"]
    export = manifest["export"]
    if export.get("coordinate_space") != "chunk_internal":
        raise ValueError("Проверяемый реимпорт поддерживает только локальный экспорт без EPSG. "
                         "Повторите export_model без epsg; CRS и transform останутся в манифесте.")
    current_project = str(Path(document.path).resolve()) if document.path else None
    if recorded.get("project_path") != current_project:
        raise ValueError("Манифест создан для другого файла проекта. Проверяемый реимпорт заблокирован.")
    if recorded.get("chunk_key") != source.key:
        raise ValueError("Манифест создан для другого ключа чанка.")
    if export.get("clip_to_boundary"):
        raise ValueError("Модель экспортирована с clip_to_boundary=true: её границы могли измениться. "
                         "Для проверяемого реимпорта повторите экспорт с clip_to_boundary=false.")
    current_matrix = ([float(source.transform.matrix[row, column])
                       for row in range(4) for column in range(4)]
                      if source.transform.matrix is not None else None)
    transform_delta = matrix_max_delta(recorded.get("chunk_transform"), current_matrix)
    if transform_delta is None or transform_delta > 1e-9:
        raise ValueError("Transform чанка изменился после экспорта; координатный реимпорт заблокирован.")
    current_crs = _crs_summary(source.crs)
    if recorded.get("chunk_coordinate_system") != current_crs:
        raise ValueError("CRS чанка изменилась после экспорта; координатный реимпорт заблокирован.")
    model = next((item for item in source.models if item.key == recorded.get("model_key")), None)
    if model is None:
        raise ValueError("Исходная модель из манифеста больше не найдена в чанке.")
    source_check = compare_bounds(recorded["local_bounds"], model_local_bounds(model), 1e-9)
    if not source_check["coordinate_match"]:
        raise ValueError("Исходная модель или её положение изменились после экспорта.")
    if parameters.get("transfer_texture") and not len(model.textures):
        raise ValueError("Перенос текстуры запрошен, но у исходной модели нет текстуры.")
    return {
        "source_model": {"key": model.key, "label": model.label,
                         "vertices": len(model.vertices), "faces": len(model.faces),
                         "textures": len(model.textures),
                         "local_bounds": model_local_bounds(model)},
        "coordinate_space": export["coordinate_space"],
        "coordinate_system": export.get("coordinate_system"),
        "chunk_transform_max_delta": transform_delta,
        "modified_since_export": bool(parameters.get("modified_since_export")),
        "validation_note": ("Старт повторно проверит файл, CRS и transform; после импорта сравнит "
                            "центр и размер модели до разрешения accept_job_result."),
    }


def camera_inputs(chunk):
    return [(c.key, c.label, c.enabled, c.photo.path, c.photo.layer) for c in chunk.cameras
            if c.type == Metashape.Camera.Type.Regular and c.photo is not None]


def calibration_summary(calibration):
    if calibration is None:
        return None
    return {
        "type": str(calibration.type).split(".")[-1],
        "width": int(calibration.width), "height": int(calibration.height),
        "parameters": {
            name: float(getattr(calibration, name))
            for name in ("f", "cx", "cy", "b1", "b2", "k1", "k2", "k3", "k4",
                         "p1", "p2", "p3", "p4")
        },
    }


def sensor_calibration_summary(sensor, chunk):
    return {
        "key": sensor.key, "label": sensor.label,
        "type": str(sensor.type).split(".")[-1],
        "fixed": bool(sensor.fixed_calibration), "fixed_params": list(sensor.fixed_params),
        "camera_count": sum(camera.sensor == sensor for camera in chunk.cameras),
        "adjusted_calibration": calibration_summary(sensor.calibration),
        "initial_calibration": calibration_summary(sensor.user_calib),
    }


def _photo_metadata(camera):
    return dict(camera.photo.meta) if camera.photo is not None else {}


def _metadata_flags(camera):
    keys = [str(key).lower() for key in _photo_metadata(camera)]
    latitude = any("gpslatitude" in key for key in keys)
    longitude = any("gpslongitude" in key for key in keys)
    accuracy = any("accuracy" in key and ("xmp" in key or "gps" in key) for key in keys)
    rotation_names = ("yaw", "pitch", "roll", "omega", "phi", "kappa", "orientation")
    rotation = any(any(name in key for name in rotation_names)
                   and ("xmp" in key or "gps" in key) for key in keys)
    return {"gps": latitude and longitude, "accuracy": accuracy, "rotation": rotation}


def camera_reference_figures(cameras, sample_limit=10):
    locations = []
    rotations = []
    samples = []
    for camera in cameras:
        reference = camera.reference
        location = ([float(value) for value in reference.location]
                    if reference.location is not None else None)
        accuracy = ([float(value) for value in reference.location_accuracy]
                    if reference.location_accuracy is not None else None)
        rotation = ([float(value) for value in reference.rotation]
                    if reference.rotation is not None else None)
        rotation_accuracy = ([float(value) for value in reference.rotation_accuracy]
                             if reference.rotation_accuracy is not None else None)
        if location is not None:
            locations.append(location)
        if rotation is not None:
            rotations.append(rotation)
        if len(samples) < sample_limit and (location is not None or rotation is not None):
            samples.append({"key": camera.key, "label": camera.label,
                            "location": location, "location_accuracy": accuracy,
                            "location_enabled": bool(reference.location_enabled),
                            "rotation": rotation, "rotation_accuracy": rotation_accuracy,
                            "rotation_enabled": bool(reference.rotation_enabled)})
    return {
        "camera_count": len(cameras),
        "locations": len(locations),
        "locations_with_accuracy": sum(
            camera.reference.location is not None
            and camera.reference.location_accuracy is not None
            and all(float(value) > 0 for value in camera.reference.location_accuracy)
            for camera in cameras),
        "enabled_locations": sum(camera.reference.location is not None
                                 and camera.reference.location_enabled for camera in cameras),
        "rotations": len(rotations),
        "enabled_rotations": sum(camera.reference.rotation is not None
                                 and camera.reference.rotation_enabled for camera in cameras),
        "unique_locations": len({tuple(round(value, 10) for value in item)
                                 for item in locations}),
        "altitude_range": ([min(item[2] for item in locations),
                            max(item[2] for item in locations)] if locations else None),
        "sample": samples,
    }


def _crs_summary(crs):
    if crs is None:
        return None
    return {"name": crs.name or None, "authority": crs.authority or None,
            "proj4": crs.proj4 or None}


def _crs_epsg(crs):
    authority = str(crs.authority or "") if crs is not None else ""
    match = re.fullmatch(r"EPSG(?:::|:)(\d+)", authority, re.IGNORECASE)
    return int(match.group(1)) if match else None


def _reference_crs_preview(source, parameters, limit=10):
    source_crs = Metashape.CoordinateSystem(f"EPSG::{parameters['source_epsg']}")
    target_crs = Metashape.CoordinateSystem(f"EPSG::{parameters['target_epsg']}")
    if not source_crs.name or not target_crs.name:
        raise ValueError("Metashape не распознала исходный или целевой код EPSG.")
    samples = []
    counts = {"cameras": 0, "markers": 0}
    camera_locations = sum(camera.reference.location is not None for camera in source.cameras)
    marker_locations = sum(marker.reference.location is not None for marker in source.markers)
    if parameters["set_chunk_crs"]:
        if camera_locations and not parameters["transform_cameras"]:
            raise ValueError("Нельзя менять CRS чанка, оставляя координаты камер в прежней "
                             "системе: включите transform_cameras.")
        if marker_locations and not parameters["transform_markers"]:
            raise ValueError("Нельзя менять CRS чанка, оставляя координаты маркеров в прежней "
                             "системе: включите transform_markers.")

    def add(kind, item, location):
        counts[kind + "s"] += 1
        if len(samples) < limit:
            transformed = Metashape.CoordinateSystem.transform(
                Metashape.Vector([float(value) for value in location]), source_crs, target_crs)
            if not all(math.isfinite(float(value)) for value in transformed):
                raise ValueError(f"Преобразование {kind} {item.label} дало неконечные координаты.")
            samples.append({"kind": kind, "key": item.key, "label": item.label,
                            "before": [float(value) for value in location],
                            "after": [float(value) for value in transformed]})

    if parameters["transform_cameras"]:
        actual = source.camera_crs or source.crs
        if _crs_epsg(actual) != parameters["source_epsg"]:
            raise ValueError("CRS опорных координат камер не совпадает с source_epsg. "
                             "Сначала проверьте get_reference_summary; не назначайте CRS наугад.")
        for camera in source.cameras:
            if camera.reference.location is not None:
                if camera.reference.rotation is not None:
                    raise ValueError("Преобразование опорных углов камер между CRS пока не "
                                     "поддерживается. Уберите ориентации или используйте другой чанк.")
                add("camera", camera, camera.reference.location)
    if parameters["transform_markers"]:
        actual = source.marker_crs or source.crs
        if _crs_epsg(actual) != parameters["source_epsg"]:
            raise ValueError("CRS опорных координат маркеров не совпадает с source_epsg. "
                             "Сначала проверьте get_reference_summary; не назначайте CRS наугад.")
        for marker in source.markers:
            if marker.reference.location is not None:
                add("marker", marker, marker.reference.location)
    if not sum(counts.values()):
        raise ValueError("В выбранных классах нет опорных координат для преобразования.")
    return source_crs, target_crs, counts, samples


def _point_cloud_figures(cloud):
    extent = cloud.extent()
    return {
        "key": cloud.key, "label": cloud.label,
        "point_count": int(cloud.point_count),
        "class_counts": {int(key): int(value)
                         for key, value in sorted(cloud.point_count_by_class.items())},
        "extent": {"minimum": [float(value) for value in extent.min],
                   "maximum": [float(value) for value in extent.max]},
    }


def _point_cloud_cleanup_preview(source, parameters):
    cloud = next((item for item in source.point_clouds
                  if item.key == parameters["point_cloud_key"]), None)
    if cloud is None or not cloud.point_count:
        raise ValueError("Указанное облако точек отсутствует или пусто.")
    if source.point_cloud is None or cloud.key != source.point_cloud.key:
        raise ValueError("В этой версии очищается только активное облако: сначала сделайте "
                         "нужное облако активным и подготовьте план заново.")
    before = _point_cloud_figures(cloud)
    requested = parameters["point_classes"]
    actual_classes = set(before["class_counts"])
    missing = sorted(set(requested) - actual_classes)
    if missing:
        raise ValueError(f"В облаке отсутствуют запрошенные классы: {missing}.")
    selected_by_class = {}
    if parameters["selector"] == "classes":
        selected_by_class = {key: before["class_counts"][key] for key in requested}
        has_confidence = None
    else:
        reader = Metashape.PointCloud.Reader()
        reader.open(cloud)
        has_confidence = bool(reader.has_point_confidence)
        if not has_confidence:
            raise ValueError("Облако не содержит confidence; выберите классы или постройте "
                             "облако с point_confidence=true.")
        allowed = set(requested) if requested else actual_classes
        # row_count/column_count describe structured scans and can be zero for an ordinary
        # photogrammetric cloud. point_count is the number Reader can actually return.
        remaining = int(cloud.point_count)
        while remaining:
            points = reader.read(min(100000, remaining))
            if not len(points):
                break
            for point in points:
                class_id = int(point.classification)
                if (class_id in allowed
                        and parameters["min_confidence"] <= int(point.confidence)
                        <= parameters["max_confidence"]):
                    selected_by_class[class_id] = selected_by_class.get(class_id, 0) + 1
            remaining -= len(points)
    selected = sum(selected_by_class.values())
    fraction = selected / before["point_count"]
    if not selected:
        raise ValueError("Селектор не выбрал ни одной точки.")
    if selected >= before["point_count"]:
        raise ValueError("Селектор выбрал всё облако; полное удаление точек запрещено.")
    if fraction > parameters["max_selected_fraction"]:
        raise ValueError(
            f"Селектор выбрал {selected} из {before['point_count']} ({fraction:.1%}), "
            f"больше разрешённых {parameters['max_selected_fraction']:.1%}. Уточните "
            "селектор или осознанно увеличьте max_selected_fraction.")
    return cloud, before, {
        "selector": parameters["selector"], "point_classes": requested or sorted(actual_classes),
        "min_confidence": parameters["min_confidence"],
        "max_confidence": parameters["max_confidence"],
        "has_confidence": has_confidence, "selected_points": selected,
        "selected_fraction": fraction, "selected_by_class": selected_by_class,
        "max_selected_fraction": parameters["max_selected_fraction"],
    }


def exif_metadata_preview(cameras):
    flags = [(camera, _metadata_flags(camera)) for camera in cameras]
    key_counts = {}
    for camera, _ in flags:
        for key in _photo_metadata(camera):
            if any(name in str(key).lower() for name in
                   ("gps", "latitude", "longitude", "altitude", "yaw", "pitch", "roll",
                    "accuracy", "orientation")):
                key_counts[str(key)] = key_counts.get(str(key), 0) + 1
    candidates = [camera for camera, value in flags if value["gps"]]
    return {
        "gps_candidates": len(candidates),
        "accuracy_metadata_candidates": sum(value["accuracy"] for _, value in flags),
        "rotation_metadata_candidates": sum(value["rotation"] for _, value in flags),
        "candidate_camera_keys": [camera.key for camera in candidates[:20]],
        "metadata_key_counts": dict(sorted(key_counts.items())),
    }


def _criterion_filter(chunk, criterion, progress=None):
    filter_ = Metashape.TiePoints.Filter()
    options = {"criterion": getattr(Metashape.TiePoints.Filter, CRITERIA[criterion])}
    if progress is not None:
        options["progress"] = progress
    filter_.init(chunk, **options)
    return filter_


def _distribution(values):
    values = sorted(float(value) for value in values if math.isfinite(float(value)))
    if not values:
        return {"minimum": None, "median": None, "p90": None, "maximum": None}

    def at(fraction):
        return values[min(len(values) - 1, int((len(values) - 1) * fraction))]

    return {"minimum": values[0], "median": at(0.5), "p90": at(0.9),
            "maximum": values[-1]}


def tie_point_filter_preview(chunk, step):
    filter_ = _criterion_filter(chunk, step["criterion"])
    points = list(chunk.tie_points.points)
    values = list(filter_.values)
    if len(values) == len(points):
        values = [value for point, value in zip(points, values) if point.valid]
    valid_count = sum(point.valid for point in points)
    selected = sum(float(value) > step["threshold"] for value in values)
    return {"criterion": step["criterion"], "threshold": step["threshold"],
            "valid_tie_points": valid_count, "selected_tie_points": selected,
            "selected_fraction": selected / valid_count if valid_count else 0.0,
            "distribution": _distribution(values), "optimize": step["optimize"]}


def _reference_error_metrics(chunk):
    matrix = chunk.transform.matrix

    def residual(point, reference, crs):
        if matrix is None or point is None or reference.location is None:
            return None
        estimated = matrix.mulp(point)
        target_crs = crs or chunk.crs
        if target_crs is None:
            return float((estimated - reference.location).norm())
        target = target_crs.unproject(reference.location)
        return float(target_crs.localframe(target).mulv(estimated - target).norm())

    def summarize(items, point, crs, enabled):
        values = []
        for item in items:
            reference = item.reference
            if reference.location is None or reference.enabled != enabled:
                continue
            value = residual(point(item), reference, crs)
            if value is not None:
                values.append(value)
        return {"count": len(values),
                "rms_3d": math.sqrt(sum(value * value for value in values) / len(values))
                if values else None,
                "max_3d": max(values) if values else None}

    cameras = [camera for camera in chunk.cameras if camera.type == Metashape.Camera.Type.Regular]
    return {
        "units": "meters" if any(value is not None for value in
                                  (chunk.crs, chunk.camera_crs, chunk.marker_crs))
        else "chunk_coordinate_units",
        "cameras": summarize(cameras, lambda camera: camera.center, chunk.camera_crs, True),
        "control_points": summarize(chunk.markers, lambda marker: marker.position,
                                    chunk.marker_crs, True),
        "check_points": summarize(chunk.markers, lambda marker: marker.position,
                                  chunk.marker_crs, False),
    }


def tie_point_metrics(chunk):
    tie_points = chunk.tie_points
    points = list(tie_points.points)
    point_ids = [-1] * len(tie_points.tracks)
    valid_count = 0
    for point_id, point in enumerate(points):
        if point.valid and 0 <= point.track_id < len(point_ids):
            point_ids[point.track_id] = point_id
            valid_count += 1
    squared_sum = 0.0
    projection_count = 0
    for camera in chunk.cameras:
        if camera.transform is None or camera.type != Metashape.Camera.Type.Regular:
            continue
        try:
            projections = tie_points.projections[camera]
        except KeyError:
            continue
        for projection in projections:
            if not 0 <= projection.track_id < len(point_ids):
                continue
            point_id = point_ids[projection.track_id]
            if point_id < 0:
                continue
            squared_sum += camera.error(points[point_id].coord, projection.coord).norm() ** 2
            projection_count += 1
    return {"tie_point_count": valid_count, "projection_count": projection_count,
            "aligned_cameras": sum(camera.transform is not None for camera in chunk.cameras
                                   if camera.type == Metashape.Camera.Type.Regular),
            "reprojection_error_rms_px": math.sqrt(squared_sum / projection_count)
            if projection_count else None,
            "reference_errors": _reference_error_metrics(chunk)}


# Task parameters given in metres. A probe (0.34) set SmoothPointCloud.smoothing_radius = 0.05 on a
# small object (region ~0.1 m): the step ran for minutes and had to be cancelled by hand.
METRIC_PARAMETERS = {"smoothing_radius", "point_spacing", "pixel_size"}
METRIC_FRACTION = 0.05


def metric_warnings(chunk, steps):
    """Region size in metres and warnings for metric parameters that are large against it."""
    region = chunk.region
    scale = chunk.transform.scale if chunk.transform is not None else None
    if region is None or not scale:
        return None, [f"{step['task']}.{name}: у чанка нет масштаба, метры не к чему привязать — "
                      "значение будет в условных единицах чанка."
                      for step in steps for name in step["parameters"] if name in METRIC_PARAMETERS]
    size = [float(value) * float(scale) for value in region.size]
    smallest = min(size)
    warnings = []
    for step in steps:
        for name, value in step["parameters"].items():
            if (name in METRIC_PARAMETERS and isinstance(value, (int, float))
                    and value > METRIC_FRACTION * smallest):
                warnings.append(f"{step['task']}.{name} = {value} м — это больше "
                                f"{int(METRIC_FRACTION * 100)} % наименьшего размера области "
                                f"({smallest:.3g} м). Проверьте единицы: шаг может идти очень долго "
                                "или испортить данные.")
    return [round(value, 4) for value in size], warnings


def autopilot_check(chunk, task):
    """Verify that a step produced its result; raise to stop the chain on the first failure."""
    if task == "AlignCameras":
        regular = [c for c in chunk.cameras if c.type == Metashape.Camera.Type.Regular and c.enabled]
        aligned = sum(c.transform is not None for c in regular)
        if not aligned:
            raise RuntimeError("AlignCameras: не выровнена ни одна камера.")
        return {"aligned_cameras": aligned, "cameras": len(regular)}
    if task == "BuildDepthMaps":
        if chunk.depth_maps is None:
            raise RuntimeError("BuildDepthMaps: карты глубины не созданы.")
        return {"depth_maps_key": chunk.depth_maps.key}
    if task in ("BuildPointCloud", "ClassifyGroundPoints"):
        if chunk.point_cloud is None or not chunk.point_cloud.point_count:
            raise RuntimeError(f"{task}: облако точек пустое.")
        return {"point_count": int(chunk.point_cloud.point_count),
                "class_counts": {int(k): int(v) for k, v in chunk.point_cloud.point_count_by_class.items()}}
    if task in ("BuildModel", "DecimateModel", "CloseHoles", "BuildUV", "BuildTexture"):
        if chunk.model is None or not len(chunk.model.faces):
            raise RuntimeError(f"{task}: модель пустая.")
        result = {"faces": len(chunk.model.faces), "textures": len(chunk.model.textures)}
        if task == "BuildTexture" and not chunk.model.textures:
            raise RuntimeError("BuildTexture: текстура не создана.")
        return result
    if task in ("SmoothModel", "RefineModel", "ColorizeModel", "RemoveLighting"):
        if chunk.model is None or not len(chunk.model.faces):
            raise RuntimeError(f"{task}: модель пустая.")
        if task == "RemoveLighting" and not chunk.model.textures:
            raise RuntimeError("RemoveLighting: у модели нет текстуры.")
        return {"faces": len(chunk.model.faces), "textures": len(chunk.model.textures)}
    if task in ("CalculatePointNormals", "ColorizePointCloud", "SmoothPointCloud", "FilterPointCloud",
                "ClassifyPoints"):
        if chunk.point_cloud is None or not chunk.point_cloud.point_count:
            raise RuntimeError(f"{task}: облако точек пустое.")
        return {"point_cloud_key": chunk.point_cloud.key,
                "point_count": int(chunk.point_cloud.point_count),
                "class_counts": {int(k): int(v) for k, v in chunk.point_cloud.point_count_by_class.items()}}
    if task == "TriangulateTiePoints":
        if chunk.tie_points is None or not len(chunk.tie_points.points):
            raise RuntimeError("TriangulateTiePoints: связующих точек нет.")
        return {"tie_points": len(chunk.tie_points.points)}
    if task == "BuildTiledModel":
        if chunk.tiled_model is None:
            raise RuntimeError("BuildTiledModel: тайловая модель не создана.")
        return {"tiled_model_key": chunk.tiled_model.key}
    if task == "BuildSeamlines":
        return {"shapes": len(chunk.shapes) if chunk.shapes is not None else 0}
    if task == "BuildDem":
        if chunk.elevation is None:
            raise RuntimeError("BuildDem: ЦМР не создана.")
        return {"elevation_key": chunk.elevation.key}
    if task == "BuildOrthomosaic":
        if chunk.orthomosaic is None:
            raise RuntimeError("BuildOrthomosaic: ортомозаика не создана.")
        return {"orthomosaic_key": chunk.orthomosaic.key}
    if task == "DetectMarkers":
        return {"markers": len(chunk.markers)}
    if task == "OptimizeCameras":
        return {"aligned_cameras": sum(c.transform is not None for c in chunk.cameras)}
    return {}


# Chunk.copy item name -> data folder name inside .files/<chunk>/<frame>/ (folders may carry a
# version suffix such as "model.1"). Tie points live in "point_cloud", dense clouds in "dense_cloud".
COPY_FOLDERS = {"tie_points": "point_cloud", "depth_maps": "depth_maps", "point_cloud": "dense_cloud",
                "model": "model", "elevation": "elevation", "orthomosaic": "orthomosaic",
                "masks": "masks", "tiled_model": "tiled_model"}
FREE_SPACE_RESERVE = 2 * 1024 ** 3
# Kinds that may run in the source chunk (in_place=True). Kept only where probe 0.33 showed that
# the job's restore point brings the chunk back completely after the project was saved:
# - point_cloud: adds a new cloud folder; rollback makes the old cloud active again, nothing lost.
# Refused (copy only): ground_classification (save deleted the previous cloud version's .oc3, the
# rollback then refers to a missing file), model_finishing (save rewrites model/model.zip in place),
# autopilot (steps such as AlignCameras or DecimateModel rewrite existing assets the same way).
INPLACE_NOTES = {
    "point_cloud": ("Облако строится прямо в исходном чанке, без копии. Откат по точке задания "
                    "проверен (с сохранением проекта): прежнее облако снова активно, новое убрано."),
}
FREE_SPACE_FACTOR = 1.5


def folder_bytes(path):
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            if not name.startswith("._"):
                try:
                    total += os.path.getsize(os.path.join(root, name))
                except OSError:
                    pass
    return total


def copy_estimate(document, source, names):
    """Upper estimate of the bytes Chunk.copy writes for these items, and the free space check.

    Counts the data folders the source frame refers to (all versions of an asset type, so it is an
    upper bound) plus thumbnails. Results the job builds later are not included.
    """
    if not document.path or Path(document.path).suffix.lower() != ".psx":
        return None
    files = Path(document.path).with_suffix(".files")
    frame_directory = files / str(source.key) / str(source.frame.key)
    try:
        with zipfile.ZipFile(frame_directory / "frame.zip") as archive:
            text = archive.read("doc.xml").decode("utf-8", "replace")
    except (OSError, KeyError, zipfile.BadZipFile):
        return None
    # probe 0.33: Chunk.copy always copies tie points ("point_cloud") and thumbnails, listed or not.
    wanted = {COPY_FOLDERS[name] for name in names} | {"thumbnails", "point_cloud"}
    folders = sorted({ref.split("/")[0] for ref in re.findall(r'path="([^"/]+/[^"]+)"', text)
                      if not ref.startswith("..") and ref.split("/")[0].split(".")[0] in wanted})
    by_folder = {folder: folder_bytes(frame_directory / folder) for folder in folders}
    copy_bytes = sum(by_folder.values())
    free = shutil.disk_usage(files).free
    required = int(copy_bytes * FREE_SPACE_FACTOR) + FREE_SPACE_RESERVE
    return {"items": sorted(names), "folders": by_folder, "copy_bytes": copy_bytes,
            "free_bytes": free, "required_free_bytes": required, "enough_space": free >= required,
            "note": "оценка сверху; данные, которые задание построит, сюда не входят"}


def require_space(estimate):
    if estimate is not None and not estimate["enough_space"]:
        gib = 1024 ** 3
        raise ValueError(
            f"Мало места для рабочей копии: копия ≈ {estimate['copy_bytes'] / gib:.1f} ГБ, "
            f"нужно свободных ≥ {estimate['required_free_bytes'] / gib:.1f} ГБ "
            f"(1.5× копии + 2 ГБ), свободно {estimate['free_bytes'] / gib:.1f} ГБ. "
            "Освободите место (лишние чанки — remove_chunk, данные уходят в карантин рядом с "
            "проектом и место освобождается, когда карантин удалён).")


class CameraAlignment:
    def __init__(self):
        self.document = None
        self.sources = {}
        self.target = None

    def snapshot(self, chunk_key, kind="alignment", parameters=None):
        doc = Metashape.app.document
        if self.document != doc:
            self.document = doc
            self.sources.clear()
        source = next((c for c in doc.chunks if c.key == chunk_key), None) if doc is not None else None
        if source is None:
            raise ValueError("Исходный чанк отсутствует в открытом проекте.")
        entry = self.sources.get(chunk_key)
        if entry is None or entry[0] != source:
            entry = self.sources[chunk_key] = (source, uuid4().hex)
        if kind in ("sensor_calibration", "camera_reference_exif", "reference_crs_transform",
                    "point_cloud_cleanup"):
            cameras = [c for c in source.cameras
                       if c.type == Metashape.Camera.Type.Regular and c.photo is not None]
        else:
            cameras = [c for c in source.cameras
                       if c.enabled and c.type == Metashape.Camera.Type.Regular]
        if kind not in ("alignment", "marker_detection", "marker_refinement", "autopilot",
                        "sensor_calibration", "camera_reference_exif", "reference_crs_transform",
                        "point_cloud_cleanup"):
            cameras = [camera for camera in cameras if camera.transform is not None]
            if kind in ("reconstruction", "camera_optimization", "tie_point_filtering") and (
                    len(cameras) < 2 or source.tie_points is None):
                raise ValueError("Нужны связующие точки и минимум две выровненные включённые камеры.")
            if kind == "raster_products" and not cameras:
                raise ValueError("Для ортомозаики нужна минимум одна выровненная включённая камера.")
        if kind == "texture_export" and (source.model is None or not len(source.model.faces)):
            raise ValueError("Для текстурирования нужна активная полигональная модель.")
        if kind == "ground_classification" and (source.point_cloud is None or not source.point_cloud.point_count):
            raise ValueError("Для классификации нужно активное непустое плотное облако.")
        if kind == "contours" and source.elevation is None:
            raise ValueError("Для горизонталей нужна активная ЦМР (DEM).")
        if kind == "autopilot":
            available = {"tie_points": source.tie_points is not None, "depth_maps": source.depth_maps is not None,
                         "point_cloud": source.point_cloud is not None, "model": source.model is not None,
                         "elevation": source.elevation is not None, "orthomosaic": source.orthomosaic is not None}
            missing = [name for name in (parameters or {}).get("inputs", []) if not available.get(name)]
            if missing:
                raise ValueError(f"В исходном чанке нет данных, нужных первым шагам: {', '.join(missing)}.")
        if kind == "model_finishing" and (source.model is None or not len(source.model.faces)):
            raise ValueError("Для доработки нужна активная полигональная модель.")
        if kind == "raster_products" and source.point_cloud is None and source.tie_points is None:
            raise ValueError("Для ЦМР нужно плотное облако или связующие точки.")
        if kind == "point_cloud" and source.depth_maps is None:
            raise ValueError("Для построения облака точек нужен активный набор карт глубины.")
        if kind == "point_cloud_cleanup" and not source.point_clouds:
            raise ValueError("Для очистки нужно непустое облако точек.")
        digest = hashlib.sha256()
        if kind == "autopilot":
            digest.update(json.dumps(parameters.get("steps"), sort_keys=True).encode())
        if kind == "marker_refinement":
            marker_keys = parameters.get("marker_keys", []) if parameters else []
            requested = set(marker_keys)
            selected = [marker for marker in source.markers if marker.key in requested]
            missing = sorted(requested - {marker.key for marker in selected})
            if missing:
                raise ValueError(f"Маркеры с ключами не найдены: {missing[:20]}")
            eligible_cameras = set(cameras)
            projections = []
            for marker in selected:
                entries = []
                for camera in marker.projections.keys():
                    projection = marker.projections[camera]
                    if camera in eligible_cameras and projection is not None:
                        entries.append((camera.key, str(projection.coord), projection.pinned))
                projections.append((marker.key, marker.label, entries))
            if not any(entries for _, _, entries in projections):
                raise ValueError("У выбранных маркеров нет проекций на включённых фотокамерах.")
            digest.update(json.dumps(projections).encode())
        minimum = 0 if kind in ("model_finishing", "ground_classification", "contours", "autopilot",
                                "reference_crs_transform", "point_cloud_cleanup") else 1 if kind in (
            "texture_export", "raster_products", "marker_detection", "marker_refinement",
            "sensor_calibration"
        ) else 2
        if len(cameras) < minimum:
            raise ValueError(f"Для операции нужно включённых фотокамер: минимум {minimum}.")
        if kind == "ground_classification":
            digest.update(str((source.point_cloud.key, int(source.point_cloud.point_count),
                               sorted(source.point_cloud.point_count_by_class.items()))).encode())
        if kind == "contours":
            digest.update(str((source.elevation.key, source.elevation.label)).encode())
        if kind in ("texture_export", "model_finishing"):
            digest.update(str((source.model.key, len(source.model.faces), len(source.model.vertices))).encode())
        if kind in ("camera_optimization", "tie_point_filtering"):
            if kind == "tie_point_filtering":
                digest.update(str(len(source.tie_points.points)).encode())
                for point in source.tie_points.points:
                    digest.update(f"{point.track_id}:{int(point.valid)}:{point.coord}".encode())
            for marker in source.markers:
                projections = []
                for camera in marker.projections.keys():
                    projection = marker.projections[camera]
                    projections.append((camera.key, str(projection.coord), projection.pinned))
                digest.update(json.dumps([
                    marker.key, marker.label, str(marker.position), marker.enabled,
                    str(marker.reference.location), str(marker.reference.accuracy),
                    marker.reference.enabled, projections,
                ]).encode())
            for scalebar in source.scalebars:
                digest.update(json.dumps([
                    scalebar.key, scalebar.point0.key, scalebar.point1.key,
                    scalebar.reference.distance, scalebar.reference.accuracy,
                    scalebar.reference.enabled,
                ]).encode())
        if kind in ("sensor_calibration", "camera_reference_exif", "reference_crs_transform") \
                and source.tie_points is not None:
            digest.update(str(len(source.tie_points.points)).encode())
            for point in source.tie_points.points:
                digest.update(f"{point.track_id}:{int(point.valid)}:{point.coord}".encode())
        if kind == "sensor_calibration":
            parameters = parameters or {}
            sensor = next((item for item in source.sensors
                           if item.key == parameters.get("sensor_key")), None)
            if sensor is None:
                raise ValueError(f"Сенсор {parameters.get('sensor_key')} не найден.")
            if not any(camera.sensor == sensor for camera in cameras):
                raise ValueError("У выбранного сенсора нет обычных фотокамер с исходными снимками.")
            digest.update(json.dumps(sensor_calibration_summary(sensor, source),
                                     sort_keys=True).encode())
            calibration_path = parameters.get("calibration_path")
            if calibration_path:
                path = Path(calibration_path)
                if path.suffix.lower() != ".xml":
                    raise ValueError("В этой версии загружается калибровка только в формате XML.")
                stat = path.stat()
                if not path.is_file() or stat.st_size > 16 * 1024 * 1024:
                    raise ValueError("Файл калибровки отсутствует или больше 16 МиБ.")
                digest.update(json.dumps([str(path), stat.st_size, stat.st_mtime_ns]).encode())
                digest.update(hashlib.sha256(path.read_bytes()).digest())
        if kind == "camera_reference_exif":
            for camera in cameras:
                reference = camera.reference
                digest.update(json.dumps([
                    camera.key, str(reference.location), str(reference.location_accuracy),
                    reference.location_enabled, str(reference.rotation),
                    str(reference.rotation_accuracy), reference.rotation_enabled,
                    sorted((str(key), str(value)) for key, value in _photo_metadata(camera).items()),
                ]).encode())
        if kind == "reference_crs_transform":
            digest.update(json.dumps([
                _crs_summary(source.crs), _crs_summary(source.camera_crs),
                _crs_summary(source.marker_crs), parameters,
            ], sort_keys=True).encode())
            for camera in source.cameras:
                reference = camera.reference
                digest.update(json.dumps([camera.key, str(reference.location),
                                          str(reference.location_accuracy),
                                          reference.location_enabled,
                                          str(reference.rotation)]).encode())
            for marker in source.markers:
                reference = marker.reference
                digest.update(json.dumps([marker.key, str(reference.location),
                                          str(reference.accuracy), reference.enabled]).encode())
        if kind == "raster_products":
            asset = source.point_cloud if source.point_cloud is not None else source.tie_points
            digest.update(str(("point_cloud" if source.point_cloud is not None else "tie_points",
                               asset.key if hasattr(asset, "key") else None,
                               source.crs.name if source.crs is not None else None,
                               source.crs.authority if source.crs is not None else None)).encode())
        if kind == "point_cloud":
            digest.update(json.dumps([
                source.depth_maps.key, source.depth_maps.label,
                dict(source.depth_maps.meta),
            ], sort_keys=True).encode())
        if kind == "point_cloud_cleanup":
            _, before, preview = _point_cloud_cleanup_preview(source, parameters)
            digest.update(json.dumps([before, preview], sort_keys=True).encode())
        for camera in cameras:
            if kind != "alignment":
                digest.update(str(camera.transform).encode())
            photo = camera.photo
            if photo is None or not photo.path:
                raise ValueError(f"У камеры {camera.label} отсутствует исходный снимок.")
            path = Path(photo.path)
            if not path.is_absolute():
                path = Path(doc.path).parent / path
            stat = path.stat()
            digest.update(json.dumps([camera.key, camera.label, camera.sensor.key, str(path),
                                      photo.layer, stat.st_size, stat.st_mtime_ns]).encode())
            digest.update(b"mask" if camera.mask is not None else b"no-mask")
            uses_mask_pixels = (
                (kind in ("marker_detection", "alignment") and parameters
                 and (parameters.get("filter_mask") or parameters.get("match", {}).get("filter_mask")))
                or kind == "reconstruction")
            if uses_mask_pixels:
                mask = camera.mask
                if mask is None:
                    digest.update(b"no-mask")
                else:
                    image = mask.image()
                    raw = image.tostring()
                    if isinstance(raw, str):
                        raw = raw.encode("latin-1")
                    digest.update(json.dumps([image.width, image.height]).encode())
                    digest.update(raw)
        fingerprint = [entry[1], doc.path, source.label, source.frames.index(source.frame), digest.hexdigest()]
        return source, cameras, hashlib.sha256(json.dumps(fingerprint).encode()).hexdigest()

    def prepare(self, key, kind="alignment", parameters=None):
        source, cameras, fingerprint = self.snapshot(key, kind, parameters)
        path = self.document.path
        result = {"project": Path(path).name if path else None, "chunk_label": source.label,
                  "frame_index": source.frames.index(source.frame), "total": len(cameras),
                  "fingerprint": fingerprint}
        if kind == "raster_products":
            result["elevation_source"] = "point_cloud" if source.point_cloud is not None else "tie_points"
            result["coordinate_system"] = {
                "name": source.crs.name or None,
                "authority": source.crs.authority or None,
            } if source.crs is not None else None
        result["cameras_with_masks"] = sum(camera.mask is not None for camera in cameras)
        if (parameters or {}).get("in_place"):
            if kind not in INPLACE_NOTES:
                raise ValueError("Для этого вида задания работа на месте не разрешена: откат не "
                                 "возвращает его данные полностью. Используйте копию.")
            result["copy_estimate"] = None
            result["in_place_note"] = INPLACE_NOTES[kind]
        else:
            estimate = copy_estimate(self.document, source,
                                     self.copy_item_names(kind, source, parameters))
            result["copy_estimate"] = estimate
            require_space(estimate)
        if kind == "autopilot":
            result["steps_effective"] = [self.configured_task(step)[1] for step in parameters["steps"]]
            result["region_size_m"], result["metric_warnings"] = metric_warnings(
                source, parameters["steps"])
        if kind == "contours":
            result["coordinate_system"] = {
                "name": source.crs.name or None if source.crs is not None else None,
                "authority": source.crs.authority or None if source.crs is not None else None,
            }
        if kind == "point_cloud":
            result["depth_maps_key"] = source.depth_maps.key
            result["depth_maps_label"] = source.depth_maps.label
        if kind == "tie_point_filtering":
            previews = [
                tie_point_filter_preview(source, step) for step in parameters["passes"]]
            first = previews[0]
            limit = parameters["max_selected_fraction"]
            if not first["selected_tie_points"]:
                raise ValueError("Первый проход с этим порогом не выбирает ни одной связующей "
                                 "точки. Измените порог.")
            if first["selected_fraction"] > limit:
                raise ValueError(
                    f"Первый проход выберет {first['selected_tie_points']} из "
                    f"{first['valid_tie_points']} ({first['selected_fraction']:.1%}), больше "
                    f"разрешённых {limit:.1%}. Увеличьте порог очистки или осознанно увеличьте "
                    "max_selected_fraction.")
            for preview in previews:
                preview["exceeds_limit_on_source"] = preview["selected_fraction"] > limit
            result["filter_previews"] = previews
            result["metrics_before"] = tie_point_metrics(source)
            result["preview_note"] = (
                "Каждый проход оценён на исходном чанке независимо. Во время задания выбор "
                "пересчитывается после предыдущих удалений и оптимизации и снова проверяется "
                "по max_selected_fraction.")
        if kind == "sensor_calibration":
            sensor = next(item for item in source.sensors
                          if item.key == parameters["sensor_key"])
            proposed = {key: parameters.get(key)
                        for key in ("sensor_type", "fixed", "fixed_params")
                        if parameters.get(key) is not None}
            calibration_path = parameters.get("calibration_path")
            if calibration_path:
                calibration = Metashape.Calibration()
                calibration.load(calibration_path)
                proposed["initial_calibration"] = calibration_summary(calibration)
                proposed["calibration_path"] = calibration_path
            result["sensor_before"] = sensor_calibration_summary(sensor, source)
            result["proposed_changes"] = proposed
        if kind == "camera_reference_exif":
            preview = exif_metadata_preview(cameras)
            if not preview["gps_candidates"]:
                raise ValueError("В метаданных фотографий не найдены пары GPS latitude/longitude.")
            result["metadata_preview"] = preview
            result["references_before"] = camera_reference_figures(cameras)
            result["coordinate_system"] = {"authority": "EPSG::4326", "name": "WGS 84"}
        if kind == "reference_crs_transform":
            source_crs, target_crs, counts, samples = _reference_crs_preview(
                source, parameters)
            result["source_coordinate_system"] = _crs_summary(source_crs)
            result["target_coordinate_system"] = _crs_summary(target_crs)
            result["coordinate_preview"] = samples
            result["counts"] = counts
            result["warnings"] = [
                "Точности опорных координат хранятся в метрах и не преобразуются.",
                "Вертикальная система и геоид этим заданием не меняются; Z требует отдельной проверки.",
            ]
        if kind == "point_cloud_cleanup":
            _, before, preview = _point_cloud_cleanup_preview(source, parameters)
            result["point_cloud_before"] = before
            result["selection_preview"] = preview
            result["warning"] = (
                "Удаление выполняется только на копии и без compactPoints; исходное облако "
                "остаётся неизменным до accept_job_result.")
        if kind == "model_import":
            result.update(model_import_preview(self.document, source, parameters))
        return result

    @staticmethod
    def copy_item_names(kind, source, parameters):
        """Data items a copy-based job copies from the source chunk (masks always included)."""
        names = {"alignment": [], "reconstruction": ["tie_points"],
                 "camera_optimization": ["tie_points"], "tie_point_filtering": ["tie_points"],
                 "sensor_calibration": (["tie_points"] if source.tie_points is not None else []),
                 "camera_reference_exif": (["tie_points"] if source.tie_points is not None else []),
                 "reference_crs_transform": (["tie_points"] if source.tie_points is not None else []),
                 "texture_export": ["model"],
                 "marker_detection": [], "marker_refinement": [], "point_cloud": ["depth_maps"],
                 "point_cloud_cleanup": ["point_cloud"],
                 "model_finishing": ["model"], "model_import": ["model"],
                 "ground_classification": ["point_cloud"],
                 "contours": ["elevation"],
                 "autopilot": list((parameters or {}).get("inputs", [])),
                 "raster_products": ["point_cloud"] if source.point_cloud is not None
                 else ["tie_points"]}[kind]
        return names + ["masks"]

    def duplicate(self, request):
        kind = request["method"].removesuffix("_copy")
        source, cameras, fingerprint = self.snapshot(
            request["chunk_key"], kind, request.get("parameters"))
        if fingerprint != request["fingerprint"]:
            raise ValueError("Проект или исходные камеры изменились. Подготовьте новый план.")
        if (request.get("parameters") or {}).get("in_place"):
            if kind not in INPLACE_NOTES:
                raise ValueError("Для этого вида задания работа на месте не разрешена.")
            # no copy: the job works on the source chunk after the restore point of this stage
            self.target = (request["job_id"], self.document, source, camera_inputs(source))
            return {"copy_key": source.key, "in_place": True}
        names = self.copy_item_names(kind, source, request.get("parameters"))
        require_space(copy_estimate(self.document, source, names))
        items = [getattr(Metashape, COPY_ITEMS[name]) for name in names if name != "masks"]
        # Chunk.copy copies only the listed data sources; without MasksData the copy silently
        # loses all masks (verified in 2.2.0), so masks always travel with the working copy.
        items = items + [Metashape.MasksData]
        laser_scans = ([cloud for cloud in source.point_clouds if cloud.is_laser_scan]
                       if kind == "point_cloud_cleanup" else [])
        copy = source.copy(frames=[source], cameras=cameras, items=items, keypoints=False,
                           laser_scans=laser_scans)
        if copy == source or copy.key == source.key:
            raise RuntimeError("Metashape не создала отдельный чанк.")
        copy.label = request["copy_label"]
        self.target = (request["job_id"], self.document, copy, camera_inputs(copy))
        return {"copy_key": copy.key}

    @staticmethod
    def task_parameters(task):
        return sorted(name for name in dir(task) if not name.startswith("_")
                      and not callable(getattr(task, name, None))
                      and name not in ("name", "target", "workitem_count"))

    @staticmethod
    def enum_members(value):
        """Names of a Metashape enum's members, or None for plain values."""
        if isinstance(value, (bool, int, float, str, list, tuple)) or value is None:
            return None
        kind = type(value)
        members = [name for name in dir(kind) if not name.startswith("_")
                   and isinstance(getattr(kind, name, None), kind)]
        return members or None

    @classmethod
    def describe_tasks(cls, tasks, only=None):
        """Parameters of allowed autopilot tasks with types, defaults and enum members."""
        result = []
        for name, (inputs, outputs) in tasks.items():
            if only and name != only:
                continue
            factory = getattr(Metashape.Tasks, name, None)
            if factory is None:
                result.append({"task": name, "available": False})
                continue
            task = factory()
            parameters = {}
            from autopilot import FORBIDDEN_PARAMETERS
            for parameter in cls.task_parameters(task):
                if parameter in FORBIDDEN_PARAMETERS:
                    continue
                value = getattr(task, parameter, None)
                members = cls.enum_members(value)
                entry = {"type": type(value).__name__,
                         "default": value if members is None and (
                             isinstance(value, (bool, int, float, str)) or value is None)
                         else (list(value) if isinstance(value, (list, tuple)) else str(value))}
                if members:
                    entry["values"] = members
                parameters[parameter] = entry
            result.append({"task": name, "available": True, "needs": list(inputs),
                           "produces": list(outputs), "parameters": parameters})
        return result

    @staticmethod
    def configured_task(step):
        """Create a Metashape task, reject unknown parameter names, return (task, effective dict).

        encode()/encodeJSON() of a fresh task return null in 2.2.0: they hold only parameters
        changed from defaults. The parameter list therefore comes from the task's attributes.
        """
        factory = getattr(Metashape.Tasks, step["task"], None)
        if factory is None:
            raise ValueError(f"В этой версии Metashape нет задачи {step['task']}.")
        task = factory()
        names = sorted(name for name in dir(task) if not name.startswith("_")
                       and not callable(getattr(task, name, None))
                       and name not in ("name", "target", "workitem_count"))
        unknown = sorted(set(step["parameters"]) - set(names))
        if unknown:
            raise ValueError(f"{step['task']}: неизвестные параметры {unknown}. Допустимые: {names}")
        for name, value in step["parameters"].items():
            before = getattr(task, name, None)
            members = CameraAlignment.enum_members(before)
            if members and isinstance(value, str):
                # enums come from JSON as member names, e.g. "DepthMapsData"
                if value.split(".")[-1] not in members:
                    raise ValueError(f"{step['task']}.{name}: {value!r} не из {members}.")
                value = getattr(type(before), value.split(".")[-1])
            try:
                setattr(task, name, value)
            except Exception as error:
                raise ValueError(f"{step['task']}.{name} = {value!r}: {error}") from error
            after = getattr(task, name, None)
            # Metashape silently ignores some wrong types (e.g. a string for downscale)
            accepted = (after == value or str(after) == str(value)
                        or (isinstance(value, (int, float)) and not isinstance(value, bool)
                            and isinstance(after, (int, float)) and float(after) == float(value)))
            if not accepted:
                raise ValueError(f"{step['task']}.{name}: Metashape не приняла значение {value!r} "
                                 f"(осталось {after!r}, тип по умолчанию {type(before).__name__}).")

        def plain(value):
            if isinstance(value, (bool, int, float, str)) or value is None:
                return value
            if isinstance(value, (list, tuple)):
                return [plain(item) for item in value]
            return str(value)
        effective = {name: plain(getattr(task, name, None)) for name in names}
        changed = task.encodeJSON()
        return task, {"parameters": effective, "changed": json.loads(changed) if changed else {}}

    def finish_model(self, copy, steps):
        """Apply model clean-up steps in order to the copy's active model; record figures per step."""
        if copy.model is None:
            raise RuntimeError("В рабочей копии нет активной модели.")
        history = [{"step": "source", **model_figures(copy.model)}]
        for index, step in enumerate(steps):
            before = ([vertex.coord for vertex in copy.model.vertices]
                      if step["operation"] == "smooth" else None)
            TRACKER(100.0 * index / len(steps))

            def within(value, index=index):
                TRACKER(100.0 * (index + float(value) / 100.0) / len(steps))
            apply_step(copy, step, within)
            if copy.model is None or not len(copy.model.faces):
                raise RuntimeError(f"После шага {step['operation']} модель пустая.")
            entry = {"step": step["operation"], "parameters": step, **model_figures(copy.model)}
            if before is not None and len(before) == len(copy.model.vertices):
                shifts = [(vertex.coord - old).norm() for vertex, old in zip(copy.model.vertices, before)]
                entry["vertex_shift"] = {"max": max(shifts), "mean": sum(shifts) / len(shifts),
                                         "units": "внутренние координаты чанка"}
            history.append(entry)
        self.target = None
        return {"model_key": copy.model.key, "steps": history,
                "extrapolated_surface": any(step["operation"] == "close_holes" for step in steps),
                "textures_after": len(copy.model.textures)}

    def execute(self, request):
        job_id, doc, copy, inputs = self.target or (None, None, None, None)
        if (job_id != request["job_id"] or doc != Metashape.app.document or copy not in doc.chunks
                or copy.key != request["copy_key"]):
            raise ValueError("Рабочая копия недоступна в текущем проекте.")
        if camera_inputs(copy) != inputs:
            raise ValueError("Список камер рабочей копии изменён. Обработка остановлена.")
        if request["method"] == "model_import_import":
            parameters = request["parameters"]
            if file_fingerprint(parameters["path"]) != parameters["fingerprint"]:
                raise ValueError("Файл модели изменился после preview. Подготовьте реимпорт заново.")
            manifest = parameters["manifest"]
            export = manifest["export"]
            source_model = copy.model
            if source_model is None:
                raise RuntimeError("В рабочей копии не найдена исходная модель из манифеста.")
            copied_source_check = compare_bounds(
                manifest["source"]["local_bounds"], model_local_bounds(source_model), 1e-9)
            if not copied_source_check["coordinate_match"]:
                raise RuntimeError("Исходная модель в рабочей копии не совпала с манифестом.")
            before = {model.key for model in copy.models}
            options = {}
            shift = export.get("shift")
            if shift is not None:
                options["shift"] = Metashape.Vector([float(value) for value in shift])
            if export["coordinate_space"] == "epsg":
                options["crs"] = Metashape.CoordinateSystem(f"EPSG::{export['epsg']}")
            elif export["coordinate_space"] == "chunk_crs":
                if copy.crs is None:
                    raise RuntimeError("В рабочей копии отсутствует CRS из манифеста.")
                options["crs"] = copy.crs
            copy.importModel(
                path=parameters["path"],
                format=getattr(Metashape, MODEL_IMPORT_FORMATS[parameters["format"]]),
                replace_asset=False, decode_udim=True, progress=TRACKER, **options)
            created = [model for model in copy.models if model.key not in before]
            if len(created) != 1:
                raise RuntimeError(f"Ожидалась одна импортированная модель, появилось {len(created)}.")
            imported = created[0]
            if not len(imported.vertices) or not len(imported.faces):
                raise RuntimeError("Импортированная модель пуста.")
            copy.model = imported
            coordinate_check = compare_bounds(
                manifest["source"]["local_bounds"], model_local_bounds(imported),
                parameters["coordinate_tolerance_ratio"])
            if not coordinate_check["coordinate_match"]:
                raise RuntimeError(
                    "Импортированная модель не прошла проверку координат: "
                    f"сдвиг центра {coordinate_check['center_shift_ratio']:.6g}, "
                    f"изменение размера {coordinate_check['size_change_ratio']:.6g}, "
                    f"допуск {parameters['coordinate_tolerance_ratio']:.6g}. "
                    f"Δцентра={coordinate_check['center_delta']}, "
                    f"границы импорта={coordinate_check['imported_bounds']}. "
                    "Рабочую копию не принимайте.")
            texture = {"requested": bool(parameters["transfer_texture"]), "transferred": False}
            if parameters["transfer_texture"]:
                uv_rebuilt = not len(imported.tex_vertices)
                if uv_rebuilt:
                    copy.buildUV(progress=TRACKER, mapping_mode=Metashape.GenericMapping,
                                 page_count=1, texture_size=parameters["texture_size"])
                imported.removeTextures()
                task = Metashape.Tasks.BuildTexture()
                task.source_data = Metashape.ModelData
                task.source_asset = source_model.key
                task.transfer_texture = True
                task.texture_size = parameters["texture_size"]
                task.apply(copy, progress=TRACKER)
                if not imported.textures:
                    raise RuntimeError("Metashape не перенесла текстуру на импортированную модель.")
                texture.update(transferred=True, source_model_key=source_model.key,
                               texture_count=len(imported.textures),
                               texture_size=parameters["texture_size"],
                               imported_uv_preserved=not uv_rebuilt,
                               uv_rebuilt=uv_rebuilt)
            self.target = None
            return {
                "source_model_key": source_model.key,
                "imported_model_key": imported.key,
                "imported_model": {"label": imported.label, "vertices": len(imported.vertices),
                                   "faces": len(imported.faces), "textures": len(imported.textures),
                                   "local_bounds": model_local_bounds(imported)},
                "coordinate_validation": coordinate_check,
                "texture_transfer": texture,
                "source_chunk_unchanged": True,
            }
        if request["method"] == "marker_detection_detect":
            target_types = {
                "circular_12bit": Metashape.CircularTarget12bit,
                "circular_14bit": Metashape.CircularTarget14bit,
                "circular_16bit": Metashape.CircularTarget16bit,
                "circular_20bit": Metashape.CircularTarget20bit,
            }
            before = {marker.key for marker in copy.markers}
            parameters = request["parameters"]
            copy.detectMarkers(progress=TRACKER, 
                target_type=target_types[parameters["target_type"]],
                tolerance=parameters["tolerance"],
                filter_mask=parameters["filter_mask"],
                inverted=False, noparity=False, merge_markers=False,
                cameras=[camera.key for camera in copy.cameras if camera.enabled],
            )
            added = [marker for marker in copy.markers if marker.key not in before]
            self.target = None
            return {
                "marker_count": len(copy.markers), "added_markers": len(added),
                "added_marker_keys": [marker.key for marker in added],
                "projection_count": sum(len(marker.projections.keys()) for marker in added),
            }
        if request["method"] == "marker_refinement_refine":
            requested = set(request["parameters"]["marker_keys"])
            selected = [marker for marker in copy.markers if marker.key in requested]
            missing = sorted(requested - {marker.key for marker in selected})
            if missing:
                raise ValueError(f"Маркеры рабочей копии не найдены: {missing[:20]}")
            before = {}
            for marker in selected:
                for camera in marker.projections.keys():
                    projection = marker.projections[camera]
                    if projection is not None:
                        before[(marker.key, camera.key)] = (
                            float(projection.coord.x), float(projection.coord.y))
            copy.refineMarkers(progress=TRACKER, markers=[marker.key for marker in selected])
            shifts = []
            for marker in selected:
                for camera in marker.projections.keys():
                    projection = marker.projections[camera]
                    previous = before.get((marker.key, camera.key))
                    if projection is not None and previous is not None:
                        shifts.append(math.hypot(
                            float(projection.coord.x) - previous[0],
                            float(projection.coord.y) - previous[1]))
            self.target = None
            return {
                "refined_markers": len(selected), "projection_count": len(shifts),
                "changed_projections": sum(value > 1e-12 for value in shifts),
                "max_shift_px": max(shifts) if shifts else None,
            }
        if request["method"] == "point_cloud_build":
            before = {cloud.key for cloud in copy.point_clouds}
            parameters = request["parameters"]
            copy.buildPointCloud(progress=TRACKER, 
                source_data=Metashape.DepthMapsData,
                point_colors=parameters["point_colors"],
                point_confidence=parameters["point_confidence"],
                keep_depth=parameters["keep_depth"],
                max_neighbors=parameters["max_neighbors"],
                uniform_sampling=parameters["uniform_sampling"],
                points_spacing=parameters["points_spacing"],
                replace_asset=False,
            )
            created = [cloud for cloud in copy.point_clouds if cloud.key not in before]
            if len(created) != 1 or not created[0].point_count:
                raise RuntimeError("Metashape не создала непустое облако точек.")
            cloud = created[0]
            self.target = None
            return {
                "point_cloud_key": cloud.key,
                "point_cloud_label": cloud.label,
                "point_count": int(cloud.point_count),
                "class_counts": [
                    {"class_id": int(class_id), "count": int(count)}
                    for class_id, count in sorted(cloud.point_count_by_class.items())
                ],
                "depth_maps_preserved": copy.depth_maps is not None,
            }
        if request["method"].startswith("autopilot_step"):
            index = int(request["method"].removeprefix("autopilot_step"))
            steps = request["parameters"]["steps"]
            task, effective = self.configured_task(steps[index])
            task.apply(copy, progress=TRACKER)
            check = autopilot_check(copy, steps[index]["task"])
            if index == len(steps) - 1:
                self.target = None
            return {f"step{index}": {"task": steps[index]["task"], "parameters": effective, **check}}
        if request["method"] == "ground_classification_classify":
            cloud = copy.point_cloud
            before = {int(k): int(v) for k, v in cloud.point_count_by_class.items()}
            parameters = {key: value for key, value in request["parameters"].items()
                          if key != "in_place"}
            cloud.classifyGroundPoints(**parameters, progress=TRACKER)
            after = {int(k): int(v) for k, v in cloud.point_count_by_class.items()}
            self.target = None
            ground = int(Metashape.PointClass.Ground)
            return {"point_cloud_key": cloud.key, "point_count": int(cloud.point_count),
                    "class_counts_before": before, "class_counts_after": after,
                    "ground_points": after.get(ground, 0), "ground_class_id": ground}
        if request["method"] == "contours_contours":
            p = request["parameters"]
            before = len(copy.shapes) if copy.shapes is not None else 0
            copy.buildContours(progress=TRACKER, source_data=Metashape.ElevationData, interval=p["interval"],
                               min_value=p["min_value"], max_value=p["max_value"],
                               prevent_intersections=p["prevent_intersections"])
            after = len(copy.shapes) if copy.shapes is not None else 0
            if after <= before:
                raise RuntimeError("Metashape не создала горизонтали (проверьте интервал и диапазон высот).")
            return {"contour_shapes": after - before}
        if request["method"] == "contours_export":
            p = request["parameters"]
            formats = {"shp": ("ShapesFormatSHP", "contours.shp"), "geojson": ("ShapesFormatGeoJSON", "contours.geojson"),
                       "dxf": ("ShapesFormatDXF", "contours.dxf"), "kml": ("ShapesFormatKML", "contours.kml")}
            constant, name = formats[p["format"]]
            # GeoJSON/KML are WGS84 formats; with a local CRS Metashape opens a modal
            # "Unsupported datum transformation / retry?" dialog (seen in 0.28.0), so refuse first.
            if p["format"] in ("geojson", "kml") and (copy.crs is None or not copy.crs.authority):
                raise ValueError("GeoJSON и KML требуют географической системы координат; у чанка "
                                 "локальная. Выберите shp или dxf.")
            folder = Path(tempfile.mkdtemp(prefix="metashape-contours-", dir=p["directory"]))
            try:
                target = folder / name
                if target.exists():
                    raise ValueError(f"Путь вывода уже существует: {target}")
                copy.exportShapes(str(target), save_polylines=True,
                                  format=getattr(Metashape, constant, None) or getattr(Metashape.ShapesFormat, constant))
                files = sorted(f for f in folder.iterdir() if f.is_file() and not f.name.startswith("."))
                if not files or not all(f.stat().st_size for f in files):
                    raise ValueError("Metashape не создала файл горизонталей.")
            except Exception:
                shutil.rmtree(folder, ignore_errors=True)
                raise
            self.target = None
            return {"output_directory": str(folder), "files": [str(f) for f in files]}
        if request["method"] == "model_finishing_finish":
            return self.finish_model(copy, request["parameters"]["steps"])
        if request["method"].startswith("tie_point_filtering_pass"):
            index = int(request["method"].removeprefix("tie_point_filtering_pass"))
            parameters = request["parameters"]
            step = parameters["passes"][index]
            before = tie_point_metrics(copy)

            def initializing(value):
                TRACKER(0.30 * float(value))

            filter_ = _criterion_filter(copy, step["criterion"], initializing)
            filter_.resetSelection()
            filter_.selectPoints(step["threshold"])
            valid_count = sum(point.valid for point in copy.tie_points.points)
            selected_count = sum(point.valid and point.selected for point in copy.tie_points.points)
            selected_fraction = selected_count / valid_count if valid_count else 0.0
            if not selected_count:
                raise ValueError(f"Проход {index + 1}: порог не выбрал ни одной связующей точки.")
            if selected_fraction > parameters["max_selected_fraction"]:
                raise ValueError(
                    f"Проход {index + 1}: выбрано {selected_count} из {valid_count} "
                    f"({selected_fraction:.1%}), больше разрешённых "
                    f"{parameters['max_selected_fraction']:.1%}. Подготовьте новый план с "
                    "осознанно увеличенным max_selected_fraction или измените порог.")
            copy.tie_points.removeSelectedPoints()
            TRACKER(40.0)
            remaining = sum(point.valid for point in copy.tie_points.points)
            if not remaining:
                raise RuntimeError("После фильтрации не осталось связующих точек.")
            if step["optimize"]:

                def optimizing(value):
                    TRACKER(40.0 + 0.60 * float(value))

                copy.optimizeCameras(progress=optimizing, **parameters["optimization"])
            else:
                TRACKER(100.0)
            after = tie_point_metrics(copy)
            if index == len(parameters["passes"]) - 1:
                self.target = None
            return {"pass_index": index, "criterion": step["criterion"],
                    "threshold": step["threshold"], "optimized": step["optimize"],
                    "selected_tie_points": selected_count,
                    "selected_fraction": selected_fraction,
                    "before": before, "after": after}
        if request["method"] == "camera_optimization_optimize":
            copy.optimizeCameras(progress=TRACKER, **request["parameters"])
            self.target = None
            return {
                "aligned_cameras": sum(camera.transform is not None for camera in copy.cameras),
                "tie_point_count": sum(point.valid for point in copy.tie_points.points),
                "marker_count": len(copy.markers), "scalebar_count": len(copy.scalebars),
            }
        if request["method"] == "sensor_calibration_apply":
            parameters = request["parameters"]
            sensor = next((item for item in copy.sensors
                           if item.key == parameters["sensor_key"]), None)
            if sensor is None:
                raise ValueError("Выбранный сенсор отсутствует в рабочей копии.")
            before = sensor_calibration_summary(sensor, copy)
            alignment_before = tie_point_metrics(copy) if copy.tie_points is not None else None
            seeded_from_adjusted = False
            if parameters.get("calibration_path"):
                calibration = Metashape.Calibration()
                calibration.load(parameters["calibration_path"])
                sensor.user_calib = calibration
            elif ((parameters.get("fixed") is not None
                   or parameters.get("fixed_params") is not None)
                  and sensor.user_calib is None and sensor.calibration is not None):
                # Fixing parameters makes Metashape use the initial calibration. If none exists,
                # seed it from the solved calibration so merely fixing F does not reset the lens
                # to EXIF defaults and invalidate an already aligned chunk.
                sensor.user_calib = sensor.calibration.copy()
                seeded_from_adjusted = True
            if parameters.get("sensor_type") is not None:
                sensor.type = getattr(Metashape.Sensor.Type,
                                      SENSOR_TYPES[parameters["sensor_type"]])
            if parameters.get("fixed") is not None:
                sensor.fixed_calibration = parameters["fixed"]
            if parameters.get("fixed_params") is not None:
                if parameters.get("fixed") is None:
                    sensor.fixed_calibration = False
                sensor.fixed_params = parameters["fixed_params"]
            after = sensor_calibration_summary(sensor, copy)
            alignment_after = tie_point_metrics(copy) if copy.tie_points is not None else None
            warnings = []
            if alignment_before and alignment_after:
                old = alignment_before["reprojection_error_rms_px"]
                new = alignment_after["reprojection_error_rms_px"]
                if old and new and new > old * 1.25:
                    warnings.append("Ошибка репроекции выросла больше чем на 25%; не принимайте "
                                    "копию без проверки и повторной оптимизации/выравнивания.")
            TRACKER(100.0)
            self.target = None
            return {"sensor_before": before, "sensor_after": after,
                    "affected_cameras": after["camera_count"],
                    "initial_seeded_from_adjusted": seeded_from_adjusted,
                    "alignment_before": alignment_before, "alignment_after": alignment_after,
                    "warnings": warnings}
        if request["method"] == "camera_reference_exif_apply":
            parameters = request["parameters"]
            cameras = [camera for camera in copy.cameras
                       if camera.type == Metashape.Camera.Type.Regular and camera.photo is not None]
            before = camera_reference_figures(cameras)
            saved = {}
            for camera in cameras:
                reference = camera.reference
                saved[camera.key] = {
                    "location": ([float(value) for value in reference.location]
                                 if reference.location is not None else None),
                    "location_accuracy": ([float(value) for value in reference.location_accuracy]
                                          if reference.location_accuracy is not None else None),
                    "location_enabled": bool(reference.location_enabled),
                    "rotation": ([float(value) for value in reference.rotation]
                                 if reference.rotation is not None else None),
                    "rotation_accuracy": ([float(value) for value in reference.rotation_accuracy]
                                          if reference.rotation_accuracy is not None else None),
                    "rotation_enabled": bool(reference.rotation_enabled),
                }
            crs = Metashape.CoordinateSystem("EPSG::4326")
            if not crs.name:
                raise ValueError("Metashape не распознала EPSG:4326.")
            copy.camera_crs = crs
            copy.loadReferenceExif(load_rotation=parameters["load_rotation"],
                                   load_accuracy=parameters["load_accuracy"])
            loaded_locations = 0
            metadata_accuracies = 0
            fallback_accuracies = 0
            loaded_rotations = 0
            for camera in cameras:
                flags = _metadata_flags(camera)
                reference = camera.reference
                previous = saved[camera.key]
                if previous["location"] is not None and not parameters["replace_existing"]:
                    reference.location = Metashape.Vector(previous["location"])
                    reference.location_accuracy = (Metashape.Vector(previous["location_accuracy"])
                                                   if previous["location_accuracy"] else None)
                    reference.location_enabled = previous["location_enabled"]
                elif flags["gps"] and reference.location is not None:
                    loaded_locations += 1
                    valid_accuracy = (reference.location_accuracy is not None
                                      and all(float(value) > 0
                                              for value in reference.location_accuracy))
                    if parameters["load_accuracy"] and valid_accuracy:
                        metadata_accuracies += 1
                    else:
                        reference.location_accuracy = Metashape.Vector(
                            parameters["fallback_accuracy_m"])
                        fallback_accuracies += 1
                    reference.location_enabled = parameters["enable_locations"]
                if parameters["load_rotation"]:
                    if previous["rotation"] is not None and not parameters["replace_existing"]:
                        reference.rotation = Metashape.Vector(previous["rotation"])
                        reference.rotation_accuracy = (
                            Metashape.Vector(previous["rotation_accuracy"])
                            if previous["rotation_accuracy"] else None)
                        reference.rotation_enabled = previous["rotation_enabled"]
                    elif flags["rotation"] and reference.rotation is not None:
                        loaded_rotations += 1
                        reference.rotation_accuracy = Metashape.Vector(
                            parameters["rotation_accuracy_deg"])
                        reference.rotation_enabled = parameters["enable_rotations"]
            if not loaded_locations:
                raise ValueError("EXIF/XMP не добавил новых координат с выбранной политикой. "
                                 "Для замены существующих координат задайте replace_existing=true.")
            if parameters["update_transform"]:
                copy.updateTransform()
            after = camera_reference_figures(cameras)
            warnings = []
            if after["unique_locations"] <= max(1, after["locations"] // 10):
                warnings.append("У многих камер одинаковые координаты; это не RTK-траектория.")
            if after["altitude_range"] and after["altitude_range"] == [0.0, 0.0]:
                warnings.append("Все EXIF-высоты равны нулю; высотную привязку нельзя считать надёжной.")
            TRACKER(100.0)
            self.target = None
            return {"references_before": before, "references_after": after,
                    "loaded_locations": loaded_locations,
                    "metadata_accuracies": metadata_accuracies,
                    "fallback_accuracies": fallback_accuracies,
                    "loaded_rotations": loaded_rotations,
                    "camera_crs": {"authority": "EPSG::4326", "name": crs.name},
                    "transform_updated": parameters["update_transform"],
                    "warnings": warnings}
        if request["method"] == "reference_crs_transform_apply":
            parameters = request["parameters"]
            source_crs, target_crs, expected, preview = _reference_crs_preview(copy, parameters)
            alignment_before = tie_point_metrics(copy) if copy.tie_points is not None else None
            transformed = {"cameras": 0, "markers": 0}
            if parameters["transform_cameras"]:
                for camera in copy.cameras:
                    reference = camera.reference
                    if reference.location is not None:
                        reference.location = Metashape.CoordinateSystem.transform(
                            reference.location, source_crs, target_crs)
                        if not all(math.isfinite(float(value)) for value in reference.location):
                            raise ValueError(f"Преобразование камеры {camera.label} дало "
                                             "неконечные координаты.")
                        transformed["cameras"] += 1
                copy.camera_crs = target_crs
            if parameters["transform_markers"]:
                for marker in copy.markers:
                    reference = marker.reference
                    if reference.location is not None:
                        reference.location = Metashape.CoordinateSystem.transform(
                            reference.location, source_crs, target_crs)
                        if not all(math.isfinite(float(value)) for value in reference.location):
                            raise ValueError(f"Преобразование маркера {marker.label} дало "
                                             "неконечные координаты.")
                        transformed["markers"] += 1
                copy.marker_crs = target_crs
            if transformed != expected:
                raise RuntimeError("Число преобразованных координат изменилось после подготовки плана.")
            if parameters["set_chunk_crs"]:
                copy.crs = target_crs
            if parameters["update_transform"]:
                copy.updateTransform()
            alignment_after = tie_point_metrics(copy) if copy.tie_points is not None else None
            after_preview = []
            for item in preview:
                after_preview.append({"kind": item["kind"], "key": item["key"],
                                      "label": item["label"], "before": item["before"],
                                      "after": item["after"]})
            TRACKER(100.0)
            self.target = None
            return {
                "transformed": transformed, "coordinate_preview": after_preview,
                "source_coordinate_system": _crs_summary(source_crs),
                "target_coordinate_system": _crs_summary(target_crs),
                "chunk_coordinate_system": _crs_summary(copy.crs),
                "camera_coordinate_system": _crs_summary(copy.camera_crs),
                "marker_coordinate_system": _crs_summary(copy.marker_crs),
                "chunk_crs_changed": parameters["set_chunk_crs"],
                "transform_updated": parameters["update_transform"],
                "alignment_before": alignment_before, "alignment_after": alignment_after,
                "warnings": [
                    "Точности оставлены в метрах без изменения.",
                    "Z не проверен по независимой вертикальной системе/геоиду.",
                ],
            }
        if request["method"] == "point_cloud_cleanup_apply":
            parameters = request["parameters"]
            cloud, before, preview = _point_cloud_cleanup_preview(copy, parameters)
            classes = (parameters["point_classes"]
                       or sorted(int(key) for key in cloud.point_count_by_class))
            try:
                cloud.resetFilters()
                if parameters["selector"] == "confidence":
                    cloud.setConfidenceFilter(parameters["min_confidence"],
                                              parameters["max_confidence"])
                cloud.removePoints(classes, progress=lambda value: TRACKER(float(value) * 0.8))
            finally:
                cloud.resetFilters()
            cloud.updateStatistics(progress=lambda value: TRACKER(80.0 + float(value) * 0.2))
            after = _point_cloud_figures(cloud)
            removed = before["point_count"] - after["point_count"]
            if removed != preview["selected_points"]:
                raise RuntimeError(
                    f"Metashape удалила {removed} точек вместо ожидаемых "
                    f"{preview['selected_points']}; рабочую копию не принимайте.")
            TRACKER(100.0)
            self.target = None
            return {"point_cloud_before": before, "point_cloud_after": after,
                    "selection_preview": preview, "removed_points": removed,
                    "removed_fraction": removed / before["point_count"],
                    "filters_reset": True, "compacted": False}
        if request["method"] == "texture_export_uv":
            copy.buildUV(progress=TRACKER, mapping_mode=Metashape.GenericMapping, page_count=1,
                         texture_size=request["parameters"]["texture_size"])
            return {}
        if request["method"] == "texture_export_texture":
            copy.buildTexture(progress=TRACKER, blending_mode=Metashape.MosaicBlending, texture_size=request["parameters"]["texture_size"])
            if not copy.model.textures:
                raise ValueError("Metashape не создала текстуру.")
            return {"model_key": copy.model.key, "texture_count": len(copy.model.textures)}
        if request["method"] == "texture_export_export":
            folder = Path(tempfile.mkdtemp(prefix="metashape-obj-", dir=request["parameters"]["directory"]))
            try:
                path = folder / "model.obj"
                copy.exportModel(str(path), format=Metashape.ModelFormatOBJ, texture_format=Metashape.ImageFormatJPEG,
                                 save_texture=True, save_uv=True)
                files = sorted(f for f in folder.iterdir() if not f.name.startswith(".") and f.is_file())
                if not all(any(f.suffix.lower() == ext and f.stat().st_size for f in files)
                           for ext in (".obj", ".mtl", ".jpg")):
                    raise ValueError("Metashape не создала полный комплект OBJ.")
            except Exception:
                shutil.rmtree(folder)
                raise
            self.target = None
            return {"model_path": str(path), "output_directory": str(folder),
                    "files": [str(f) for f in files]}
        if request["method"] == "raster_products_dem":
            projection = Metashape.OrthoProjection()
            projection.crs = copy.crs
            source = (Metashape.PointCloudData
                      if request["parameters"]["elevation_source"] == "point_cloud"
                      else Metashape.TiePointsData)
            arguments = {"source_data": source, "interpolation": Metashape.EnabledInterpolation,
                         "projection": projection, "replace_asset": False}
            if request["parameters"].get("classes"):
                arguments["classes"] = request["parameters"]["classes"]
            if request["parameters"]["resolution"]:
                arguments["resolution"] = request["parameters"]["resolution"]
            copy.buildDem(progress=TRACKER, **arguments)
            if copy.elevation is None:
                raise RuntimeError("Metashape не создала ЦМР.")
            return {"elevation_key": copy.elevation.key, "elevation_label": copy.elevation.label}
        if request["method"] == "raster_products_orthomosaic":
            projection = Metashape.OrthoProjection()
            projection.crs = copy.crs
            arguments = {"surface_data": Metashape.ElevationData,
                         "blending_mode": Metashape.MosaicBlending,
                         "fill_holes": True,
                         "refine_seamlines": request["parameters"]["refine_seamlines"],
                         "projection": projection, "replace_asset": False}
            if request["parameters"]["resolution"]:
                arguments["resolution"] = request["parameters"]["resolution"]
            copy.buildOrthomosaic(progress=TRACKER, **arguments)
            if copy.orthomosaic is None:
                raise RuntimeError("Metashape не создала ортомозаику.")
            return {"orthomosaic_key": copy.orthomosaic.key,
                    "orthomosaic_label": copy.orthomosaic.label}
        if request["method"] == "raster_products_export":
            folder = Path(tempfile.mkdtemp(prefix="metashape-raster-",
                                           dir=request["parameters"]["directory"]))
            try:
                projection = Metashape.OrthoProjection()
                projection.crs = copy.crs
                dem = folder / "dem.tif"
                orthomosaic = folder / "orthomosaic.tif"
                copy.exportRaster(str(dem), source_data=Metashape.ElevationData,
                                  projection=projection)
                copy.exportRaster(str(orthomosaic), source_data=Metashape.OrthomosaicData,
                                  projection=projection)
                files = [dem, orthomosaic]
                if not all(path.is_file() and path.stat().st_size for path in files):
                    raise ValueError("Metashape не создала полный комплект GeoTIFF.")
            except Exception:
                shutil.rmtree(folder)
                raise
            self.target = None
            return {"dem_path": str(dem), "orthomosaic_path": str(orthomosaic),
                    "output_directory": str(folder), "files": [str(path) for path in files]}
        if request["method"] == "reconstruction_depth_maps":
            copy.buildDepthMaps(progress=TRACKER, downscale=request["parameters"]["downscale"], filter_mode=Metashape.MildFiltering)
            if copy.depth_maps is None:
                raise RuntimeError("Metashape не создала набор карт глубины.")
            return {"depth_maps_key": copy.depth_maps.key}
        if request["method"] == "reconstruction_model":
            faces = {"low": Metashape.LowFaceCount, "medium": Metashape.MediumFaceCount, "high": Metashape.HighFaceCount}
            copy.buildModel(progress=TRACKER, source_data=Metashape.DepthMapsData, surface_type=Metashape.Arbitrary,
                            face_count=faces[request["parameters"]["faces"]], keep_depth=True, replace_asset=False)
            model = copy.model
            if model is None or not len(model.faces):
                raise RuntimeError("Metashape не создала полигональную модель.")
            self.target = None
            return {"model_key": model.key, "face_count": len(model.faces)}
        if request["method"] == "alignment_match":
            for camera in copy.cameras:
                if camera.transform is not None:
                    camera.transform = None
            copy.matchPhotos(progress=TRACKER, **request["parameters"]["match"])
            return {}
        error = None
        try:
            copy.alignCameras(progress=TRACKER, **request["parameters"]["align"])
        except Exception as exc:
            error = str(exc) or type(exc).__name__
        cameras = [{"key": c.key, "label": c.label, "aligned": c.transform is not None}
                   for c in copy.cameras if c.enabled and c.type == Metashape.Camera.Type.Regular]
        self.target = None
        return {"cameras": cameras, "error": error}
